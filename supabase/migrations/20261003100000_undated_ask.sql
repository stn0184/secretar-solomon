-- Вопрос о деле без срока: ступень `ask` и две функции бота.
--
-- Схема — techspec/03-schema.md §3.5; поведение — §19. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.
--
-- Новых таблиц и колонок нет, правила доступа не меняются: строка `ask` —
-- обычная строка `reminders` под прежней политикой (§4.2).

-- Ступень `ask` (§19.4): когда бот в последний раз спрашивал о деле без
-- срока. Строка всегда ушедшая (`sent_at` стоит с рождения), поэтому всё,
-- что работает с неотправленными напоминаниями, её не видит. Inline-
-- ограничение из `20260920210000_reminders.sql` Postgres назвал
-- `reminders_stage_check`.
alter table public.reminders
  drop constraint reminders_stage_check,
  add constraint reminders_stage_check
    check (stage in ('before', 'due', 'ask'));

-- О каком деле спросить сейчас (§19.1, §19.2): ноль строк или одна. Границы
-- считает бот (`services/asks.py`): `day_start` — сегодняшняя полночь по
-- поясу владельца, `asked_before` — полночь шесть дней назад, `question_since`
-- — сутки назад, `quiet_since` — 15 минут назад.
--
-- Пусто, если сегодня уже спрашивал, у владельца живой открытый вопрос или
-- нет 15 минут тишины: владелец писал боту либо бот присылал напоминание.
-- Иначе — первое по порядку §19.1 дело: сначала не спрошенные, от новых к
-- старым, потом то, о котором спрашивал давнее всех.
create function public.undated_to_ask(
  owner_telegram_id bigint,
  day_start timestamptz,
  asked_before timestamptz,
  question_since timestamptz,
  quiet_since timestamptz
) returns table (
  task_id uuid,
  title text,
  created_at timestamptz,
  asked_at timestamptz
)
language sql
stable
as $$
  select t.id, t.title, t.created_at, a.sent_at
    from public.tasks t
    left join public.reminders a
      on a.task_id = t.id
     and a.owner_telegram_id = undated_to_ask.owner_telegram_id
     and a.stage = 'ask'
   where t.owner_telegram_id = undated_to_ask.owner_telegram_id
     and t.status = 'active'
     and t.kind = 'task'
     and t.due_at is null
     and t.updated_at < undated_to_ask.day_start
     and (a.sent_at is null or a.sent_at < undated_to_ask.asked_before)
     -- сегодня уже спрашивал
     and not exists (
       select 1 from public.reminders r
        where r.owner_telegram_id = undated_to_ask.owner_telegram_id
          and r.stage = 'ask'
          and r.sent_at >= undated_to_ask.day_start)
     -- свой же живой вопрос бот не перебивает (§10.3)
     and not exists (
       select 1 from public.tasks q
        where q.owner_telegram_id = undated_to_ask.owner_telegram_id
          and q.status = 'active'
          and q.question_asked_at >= undated_to_ask.question_since)
     -- 15 минут тишины: владелец не писал
     and not exists (
       select 1 from public.messages m
        where m.owner_telegram_id = undated_to_ask.owner_telegram_id
          and m.received_at >= undated_to_ask.quiet_since)
     -- и бот не присылал напоминаний
     and not exists (
       select 1 from public.reminders s
        where s.owner_telegram_id = undated_to_ask.owner_telegram_id
          and s.sent_at >= undated_to_ask.quiet_since)
   order by a.sent_at is not null,
            case when a.sent_at is null then t.created_at end desc,
            a.sent_at,
            t.id
   limit 1;
$$;

-- Вопрос ушёл — записать его одной транзакцией (§19.4). Задача владельца
-- активна, это задача и срока у неё нет — иначе `null` и ничего не записано:
-- дело за этот миг получило срок или закрыто.
--
-- Открытый вопрос у владельца один (§10.1): у остальных задач он снят, у
-- этой — `question` и время. `needs_review` не трогается: дело записано
-- верно, ему не хватает только срока (§19.3). Строка `ask` — одна на
-- задачу: повторный вопрос обновляет её время и сообщение.
--
-- Триггер `tasks_set_updated_at` сдвигает `updated_at` и спрошенной задачи,
-- и тех, с чьих вопросов снят: сегодня о них не спросят, и это верно —
-- вопрос сегодня уже был. Снятие трогает только задачи с вопросом.
--
-- Цель конфликта названа ограничением, а не колонками: в plpgsql
-- `on conflict (task_id, stage)` совпадает с именем параметра.
create function public.record_ask(
  owner_telegram_id bigint,
  task_id uuid,
  question text,
  telegram_message_id bigint
) returns public.tasks
language plpgsql
as $$
declare
  saved public.tasks;
begin
  if nullif(btrim(record_ask.question), '') is null then
    raise exception 'record_ask: question must not be empty';
  end if;

  perform 1
     from public.tasks t
    where t.id = record_ask.task_id
      and t.owner_telegram_id = record_ask.owner_telegram_id
      and t.status = 'active'
      and t.kind = 'task'
      and t.due_at is null
      for update;
  if not found then
    return null;
  end if;

  update public.tasks t
     set open_question = null,
         question_asked_at = null
   where t.owner_telegram_id = record_ask.owner_telegram_id
     and t.id <> record_ask.task_id
     and (t.open_question is not null or t.question_asked_at is not null);

  update public.tasks t
     set open_question = record_ask.question,
         question_asked_at = now()
   where t.id = record_ask.task_id
     and t.owner_telegram_id = record_ask.owner_telegram_id
  returning t.* into saved;

  insert into public.reminders as r (
    owner_telegram_id, task_id, stage, fire_at, sent_at, telegram_message_id
  )
  values (
    record_ask.owner_telegram_id,
    record_ask.task_id,
    'ask',
    now(),
    now(),
    record_ask.telegram_message_id
  )
  on conflict on constraint reminders_task_id_stage_key do update
     set fire_at = excluded.fire_at,
         sent_at = excluded.sent_at,
         telegram_message_id = excluded.telegram_message_id;

  return saved;
end;
$$;

-- Обе зовёт только бот ключом service-role, владелец — явным аргументом.
-- Право execute выдано и роли public, из неё оно наследуется — поэтому
-- забираем и там.
revoke execute on function
  public.undated_to_ask(bigint, timestamptz, timestamptz, timestamptz, timestamptz)
  from public, anon, authenticated;
grant execute on function
  public.undated_to_ask(bigint, timestamptz, timestamptz, timestamptz, timestamptz)
  to service_role;

revoke execute on function public.record_ask(bigint, uuid, text, bigint)
  from public, anon, authenticated;
grant execute on function public.record_ask(bigint, uuid, text, bigint)
  to service_role;
