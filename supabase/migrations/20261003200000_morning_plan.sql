-- Утренний план: таблица `morning_plans`, три функции бота и ещё одна
-- причина ждать у вопроса о деле без срока.
--
-- Схема — techspec/03-schema.md §3.9, §3.5; поведение — §20. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.

-- План за день (§20.4): строка на день владельца, в который план ушёл.
-- Пишется сразу после отправки, поэтому `created_at` — время плана.
create table public.morning_plans (
  id uuid primary key default gen_random_uuid(),
  owner_telegram_id bigint not null,
  -- день владельца по его поясу, за который ушёл план
  day date not null,
  -- сообщение плана в чате; строка пишется только после отправки
  telegram_message_id bigint not null,
  created_at timestamptz not null default now(),
  -- план за день один
  constraint morning_plans_owner_day_key unique (owner_telegram_id, day)
);

-- Правила доступа (§4.2): владелец видит только свою строку. Для anon
-- политик нет намеренно. Приложение таблицу не читает.
alter table public.morning_plans enable row level security;

create policy "morning_plans: owner only"
  on public.morning_plans
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);

-- Был ли сегодня план (§20.4): есть ли строка владельца за этот день.
create function public.morning_plan_sent(
  owner_telegram_id bigint,
  day date
) returns boolean
language sql
stable
as $$
  select exists (
    select 1 from public.morning_plans p
     where p.owner_telegram_id = morning_plan_sent.owner_telegram_id
       and p.day = morning_plan_sent.day);
$$;

-- Дела дня (§20.1): активные задачи владельца со сроком от `day_start` до
-- `day_end`. Границы считает бот (`services/morning.py`): сегодняшняя и
-- завтрашняя полночь по поясу владельца. Идеи, желания, закрытые, убранные,
-- просроченные, завтрашние и без срока сюда не попадают. Повторяющаяся
-- задача стоит на своём разе: перекатывание идёт раньше в том же тике.
--
-- Порядок — `due_at`, `created_at`, `id`: строки со временем по времени,
-- дела на день (у них у всех 18:00) — в порядке записи. Бот ставит дела со
-- временем перед делами на день сам.
create function public.day_tasks(
  owner_telegram_id bigint,
  day_start timestamptz,
  day_end timestamptz
) returns table (
  task_id uuid,
  title text,
  due_at timestamptz,
  due_precision text
)
language sql
stable
as $$
  select t.id, t.title, t.due_at, t.due_precision
    from public.tasks t
   where t.owner_telegram_id = day_tasks.owner_telegram_id
     and t.status = 'active'
     and t.kind = 'task'
     and t.due_at >= day_tasks.day_start
     and t.due_at < day_tasks.day_end
   order by t.due_at, t.created_at, t.id;
$$;

-- План ушёл — записать его (§20.4). Строка за этот день уже есть — ничего
-- не меняется, ответ `false`: второй план за день не пишется.
--
-- Цель конфликта названа ограничением, а не колонками: в plpgsql
-- `on conflict (owner_telegram_id, day)` совпадает с именами параметров.
create function public.record_morning_plan(
  owner_telegram_id bigint,
  day date,
  telegram_message_id bigint
) returns boolean
language plpgsql
as $$
begin
  insert into public.morning_plans as p (owner_telegram_id, day, telegram_message_id)
  values (
    record_morning_plan.owner_telegram_id,
    record_morning_plan.day,
    record_morning_plan.telegram_message_id
  )
  on conflict on constraint morning_plans_owner_day_key do nothing;
  return found;
end;
$$;

-- Вопрос о деле без срока (§19.4) — заново, с той же подписью: ещё одна
-- причина пустоты — утренний план, ушедший меньше 15 минут назад (§20.2).
-- Остальное тело — как в миграции 018 (`20261003100000_undated_ask.sql`).
create or replace function public.undated_to_ask(
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
     -- и утреннего плана (§20.2)
     and not exists (
       select 1 from public.morning_plans p
        where p.owner_telegram_id = undated_to_ask.owner_telegram_id
          and p.created_at >= undated_to_ask.quiet_since)
   order by a.sent_at is not null,
            case when a.sent_at is null then t.created_at end desc,
            a.sent_at,
            t.id
   limit 1;
$$;

-- Все три новые зовёт только бот ключом service-role, владелец — явным
-- аргументом. Право execute выдано и роли public, из неё оно наследуется —
-- поэтому забираем и там. `create or replace` права `undated_to_ask` не
-- трогает; они повторены, чтобы файл читался сам по себе.
revoke execute on function public.morning_plan_sent(bigint, date)
  from public, anon, authenticated;
grant execute on function public.morning_plan_sent(bigint, date)
  to service_role;

revoke execute on function public.day_tasks(bigint, timestamptz, timestamptz)
  from public, anon, authenticated;
grant execute on function public.day_tasks(bigint, timestamptz, timestamptz)
  to service_role;

revoke execute on function public.record_morning_plan(bigint, date, bigint)
  from public, anon, authenticated;
grant execute on function public.record_morning_plan(bigint, date, bigint)
  to service_role;

revoke execute on function
  public.undated_to_ask(bigint, timestamptz, timestamptz, timestamptz, timestamptz)
  from public, anon, authenticated;
grant execute on function
  public.undated_to_ask(bigint, timestamptz, timestamptz, timestamptz, timestamptz)
  to service_role;
