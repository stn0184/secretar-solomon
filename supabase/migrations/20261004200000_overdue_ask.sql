-- Вопрос о прошедшем деле: ступень `overdue`, две функции бота и новый
-- вопрос задачи в ответе на свой вопрос.
--
-- Схема — techspec/03-schema.md §3.4, §3.5; поведение — §22. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.
--
-- Новых таблиц и колонок нет, правила доступа не меняются: строка `overdue` —
-- обычная строка `reminders` под прежней политикой (§4.2).

-- Ступень `overdue` (§22.4): когда бот в последний раз спрашивал о деле, срок
-- которого прошёл. Строка всегда ушедшая (`sent_at` стоит с рождения), как
-- `ask`: тик, «Напомню» строки «Перенёс», перепланирование и приложение её не
-- видят, перенос задачи её не стирает — все они работают с неотправленными
-- или только с `before` и `due`.
alter table public.reminders
  drop constraint reminders_stage_check,
  add constraint reminders_stage_check
    check (stage in ('before', 'due', 'ask', 'overdue'));

-- О каком прошедшем деле спросить сейчас (§22.1, §22.2): ноль строк или одна.
-- Границы считает бот (`services/overdue.py`): `day_start` — сегодняшняя
-- полночь по поясу владельца, `asked_before` — полночь шесть дней назад;
-- `question_since` и `quiet_since` — по месту вызова: у плана сегодняшняя
-- полночь и `null`, у отдельного вопроса сутки и 15 минут назад.
--
-- Пусто, если у владельца живой открытый вопрос; при `quiet_since` не `null`
-- — ещё и без 15 минут тишины: владелец писал боту, бот присылал напоминание,
-- вопрос или план. `quiet_since = null` — тишина не проверяется: план её не
-- ждёт.
--
-- Иначе — первое по порядку §22.1 дело: активная разовая задача со сроком
-- раньше `day_start`, о которой не спрашивал, спрашивал о прежнем сроке
-- (строка `overdue` раньше нынешнего `due_at`) или неделю назад. Сначала не
-- спрошенные — от позднего срока к раннему, потом то, о котором спрашивал
-- давнее всех. `asked_at` — время вопроса о нынешнем сроке, иначе `null`.
create function public.overdue_to_ask(
  owner_telegram_id bigint,
  day_start timestamptz,
  asked_before timestamptz,
  question_since timestamptz,
  quiet_since timestamptz
) returns table (
  task_id uuid,
  title text,
  due_at timestamptz,
  due_precision text,
  asked_at timestamptz
)
language sql
stable
as $$
  select c.id, c.title, c.due_at, c.due_precision, c.asked_at
    from (
      select t.id, t.title, t.due_at, t.due_precision, t.created_at,
             case when a.sent_at >= t.due_at then a.sent_at end as asked_at
        from public.tasks t
        left join public.reminders a
          on a.task_id = t.id
         and a.owner_telegram_id = overdue_to_ask.owner_telegram_id
         and a.stage = 'overdue'
       where t.owner_telegram_id = overdue_to_ask.owner_telegram_id
         and t.status = 'active'
         and t.kind = 'task'
         and t.repeat is null
         and t.due_at < overdue_to_ask.day_start
         and (a.sent_at is null
              or a.sent_at < t.due_at
              or a.sent_at < overdue_to_ask.asked_before)
         -- свой же живой вопрос бот не перебивает (§10.3)
         and not exists (
           select 1 from public.tasks q
            where q.owner_telegram_id = overdue_to_ask.owner_telegram_id
              and q.status = 'active'
              and q.question_asked_at >= overdue_to_ask.question_since)
         -- 15 минут тишины, если их ждут: владелец не писал
         and (overdue_to_ask.quiet_since is null or not exists (
           select 1 from public.messages m
            where m.owner_telegram_id = overdue_to_ask.owner_telegram_id
              and m.received_at >= overdue_to_ask.quiet_since))
         -- бот не присылал напоминаний и вопросов
         and (overdue_to_ask.quiet_since is null or not exists (
           select 1 from public.reminders s
            where s.owner_telegram_id = overdue_to_ask.owner_telegram_id
              and s.sent_at >= overdue_to_ask.quiet_since))
         -- и плана
         and (overdue_to_ask.quiet_since is null or not exists (
           select 1 from public.morning_plans p
            where p.owner_telegram_id = overdue_to_ask.owner_telegram_id
              and p.created_at >= overdue_to_ask.quiet_since))
    ) c
   order by c.asked_at is not null,
            case when c.asked_at is null then c.due_at end desc,
            case when c.asked_at is null then c.created_at end desc,
            c.asked_at,
            c.id
   limit 1;
$$;

-- Вопрос ушёл — записать его одной транзакцией (§22.4). Задача владельца
-- активна, это задача, она разовая и срок её уже прошёл — иначе `null` и
-- ничего не записано: дело за этот миг закрыли или перенесли.
--
-- Открытый вопрос у владельца один (§10.1): у остальных задач он снят, у
-- этой — `question` и время. `needs_review` не трогается: дело записано
-- верно. Строка `overdue` — одна на задачу: повторный вопрос обновляет её
-- время и сообщение. У вопроса в плане сообщения нет — `null`: свайп на план
-- остаётся обычным сообщением (§20.3).
--
-- Цель конфликта названа ограничением, а не колонками: в plpgsql
-- `on conflict (task_id, stage)` совпадает с именем параметра.
create function public.record_overdue_ask(
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
  if nullif(btrim(record_overdue_ask.question), '') is null then
    raise exception 'record_overdue_ask: question must not be empty';
  end if;

  perform 1
     from public.tasks t
    where t.id = record_overdue_ask.task_id
      and t.owner_telegram_id = record_overdue_ask.owner_telegram_id
      and t.status = 'active'
      and t.kind = 'task'
      and t.repeat is null
      and t.due_at < now()
      for update;
  if not found then
    return null;
  end if;

  update public.tasks t
     set open_question = null,
         question_asked_at = null
   where t.owner_telegram_id = record_overdue_ask.owner_telegram_id
     and t.id <> record_overdue_ask.task_id
     and (t.open_question is not null or t.question_asked_at is not null);

  update public.tasks t
     set open_question = record_overdue_ask.question,
         question_asked_at = now()
   where t.id = record_overdue_ask.task_id
     and t.owner_telegram_id = record_overdue_ask.owner_telegram_id
  returning t.* into saved;

  insert into public.reminders as r (
    owner_telegram_id, task_id, stage, fire_at, sent_at, telegram_message_id
  )
  values (
    record_overdue_ask.owner_telegram_id,
    record_overdue_ask.task_id,
    'overdue',
    now(),
    now(),
    record_overdue_ask.telegram_message_id
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
  public.overdue_to_ask(bigint, timestamptz, timestamptz, timestamptz, timestamptz)
  from public, anon, authenticated;
grant execute on function
  public.overdue_to_ask(bigint, timestamptz, timestamptz, timestamptz, timestamptz)
  to service_role;

revoke execute on function public.record_overdue_ask(bigint, uuid, text, bigint)
  from public, anon, authenticated;
grant execute on function public.record_overdue_ask(bigint, uuid, text, bigint)
  to service_role;

-- Шаг второй (§3.4) — заново, с той же подписью: у `amend` новый
-- необязательный ключ `question` (§22.5). Ответ «не успел» на «Получилось?»
-- открывает новый вопрос той же задачи — «На когда перенести?»: после общего
-- снятия вопросов функция пишет его в `open_question` дополняемой задачи со
-- временем. Текст без пробелов по краям; пустая строка или нет ключа —
-- вопроса нет, как раньше. `needs_review` — по-прежнему из `fields`.
-- Остальное тело — как в `20260930200000_duplicates.sql`; права у замены
-- прежние: `create or replace` их не трогает.
create or replace function public.record_understanding(
  message_id uuid,
  owner_telegram_id bigint,
  analysis jsonb,
  ai_model text,
  ai_input_tokens int,
  ai_output_tokens int,
  reply text,
  task jsonb,
  reminders jsonb,
  facts jsonb,
  transcript text default null,
  transcript_confidence numeric default null,
  amend jsonb default null,
  edit jsonb default null,
  photo_text text default null,
  same_task uuid default null
) returns public.tasks
language plpgsql
as $$
declare
  saved public.tasks;
  was public.tasks;
  linked uuid;
  people_names text[] := '{}';
  changes jsonb;
  owner_zone text;
  new_due timestamptz;
  new_precision text;
  new_repeat jsonb;
  new_occurrence timestamptz;
  new_question text;
begin
  -- Дубль — это «задачи из сообщения нет, оно о найденной» (§15.3): вместе с
  -- новой задачей, поправкой или правкой он бессмыслен. Отказ до всякой
  -- записи — ошибка бота, а не гонка.
  if record_understanding.same_task is not null
     and (coalesce(jsonb_typeof(record_understanding.task), 'null') <> 'null'
          or coalesce(jsonb_typeof(record_understanding.amend), 'null') <> 'null'
          or coalesce(jsonb_typeof(record_understanding.edit), 'null') <> 'null') then
    raise exception 'record_understanding: same_task goes without task, amend and edit';
  end if;

  -- Владелец передаётся явно и сверяется с владельцем сообщения (§4.3):
  -- ключ service-role правила доступа обходит, поэтому разделение держит
  -- здесь сама функция, а не RLS. Задача, о которой сообщение уже знает,
  -- читается тем же запросом — по ней узнаётся повтор.
  update public.messages m
     set analysis = record_understanding.analysis,
         ai_model = record_understanding.ai_model,
         ai_input_tokens = record_understanding.ai_input_tokens,
         ai_output_tokens = record_understanding.ai_output_tokens,
         reply = record_understanding.reply,
         -- Расшифровка становится текстом сообщения (§9.3); у текста и у
         -- нерасслышанного голоса она null — тогда текст не трогается.
         text = coalesce(record_understanding.transcript, m.text),
         transcript_confidence = coalesce(
           record_understanding.transcript_confidence, m.transcript_confidence
         ),
         -- Прочитанное со снимка (§14.3); у текста и голоса аргумента нет —
         -- null, и колонка не трогается: повтор без него прочитанного не стирает.
         photo_text = coalesce(record_understanding.photo_text, m.photo_text)
   where m.id = record_understanding.message_id
     and m.owner_telegram_id = record_understanding.owner_telegram_id
  returning m.task_id into linked;

  if not found then
    raise exception 'record_understanding: message % is not owned by %',
      record_understanding.message_id, record_understanding.owner_telegram_id;
  end if;

  -- Память пишется для любого вида сообщения — и для тех, где задачи нет
  -- (`about_me`, `chat`), поэтому раньше ранних выходов (§8.2, §8.3).
  -- Цель конфликта — имя ограничения, а не список колонок: в plpgsql список
  -- совпадает с именами параметров.
  if jsonb_typeof(record_understanding.facts) = 'array' then
    insert into public.facts (owner_telegram_id, category, text, status, source_message_id)
    select record_understanding.owner_telegram_id,
           item.value ->> 'category',
           item.value ->> 'text',
           coalesce(item.value ->> 'status', 'guess'),
           record_understanding.message_id
      from jsonb_array_elements(record_understanding.facts) as item(value)
     where coalesce(item.value ->> 'text', '') <> ''
    on conflict on constraint facts_owner_telegram_id_category_text_key do update
       set status = 'fact',
           source_message_id = excluded.source_message_id
     where excluded.status = 'fact'
       and public.facts.status = 'guess';
  end if;

  -- Повтор того же сообщения: задача уже заведена, дополнена, поправлена
  -- или найдена дублем — второй раз её не правим, напоминания и вопрос
  -- первого прохода остаются на месте.
  if linked is not null then
    select t.* into saved
      from public.tasks t
     where t.id = linked
       and t.owner_telegram_id = record_understanding.owner_telegram_id;
    return saved;
  end if;

  select t.* into saved
    from public.tasks t
   where t.source_message_id = record_understanding.message_id
   limit 1;
  if found then
    return saved;
  end if;

  -- Любая запись снимает открытые вопросы владельца (§10.3): ответ, новое
  -- поручение, дубль, правка, запись «как есть» при отказе модели, разговор,
  -- сведение о себе и вопрос старше суток закрываются одним правилом. Новый
  -- вопрос ставится ниже, уже после.
  --
  -- Кроме «не расслышал» (§9.3): ни разбора, ни задачи, ни поправки — понять
  -- ещё ничего не удалось, а бот сам просит повторить, и повтор должен застать
  -- вопрос открытым, иначе ответ «в пятницу» станет второй задачей.
  if coalesce(jsonb_typeof(record_understanding.analysis), 'null') <> 'null'
     or coalesce(jsonb_typeof(record_understanding.task), 'null') <> 'null'
     or coalesce(jsonb_typeof(record_understanding.amend), 'null') <> 'null'
     or coalesce(jsonb_typeof(record_understanding.edit), 'null') <> 'null' then
    update public.tasks t
       set open_question = null,
           question_asked_at = null
     where t.owner_telegram_id = record_understanding.owner_telegram_id
       and (t.open_question is not null or t.question_asked_at is not null);
  end if;

  -- Дубль (§15.3): задачу не заводим, сообщение ведёт на найденную — по нему
  -- работают «последняя задача в разговоре» и свайп. Найденную закрыли,
  -- убрали или она чужая — отказ, и вся транзакция, включая разбор и
  -- память, откатывается, как у правки.
  if record_understanding.same_task is not null then
    select t.* into saved
      from public.tasks t
     where t.id = record_understanding.same_task
       and t.owner_telegram_id = record_understanding.owner_telegram_id
       and t.status = 'active'
       for update;

    if not found then
      raise exception 'record_understanding: task % is not an active task of %',
        record_understanding.same_task, record_understanding.owner_telegram_id;
    end if;

    update public.messages m
       set task_id = saved.id
     where m.id = record_understanding.message_id
       and m.owner_telegram_id = record_understanding.owner_telegram_id;

    return saved;
  end if;

  select s.timezone into owner_zone
    from public.owner_settings s
   where s.owner_telegram_id = record_understanding.owner_telegram_id;

  -- Ответ на вопрос (§10.2): вместо новой задачи — поправка к названной.
  -- Меняются только ключи из `fields`; чужая, несуществующая или не активная
  -- (закрытая, убранная) задача — отказ, и вся транзакция, включая разбор и
  -- память, откатывается. Её закрыли или убрали, пока модель разбирала
  -- ответ, и напоминания по ней не уйдут (§6.2 берёт только активные) —
  -- «Напомню» в ответе было бы неправдой (инвариант 4).
  if record_understanding.amend is not null
     and jsonb_typeof(record_understanding.amend) = 'object' then
    changes := coalesce(record_understanding.amend -> 'fields', '{}'::jsonb);
    -- Новый вопрос той же задачи (§22.5); пустой — вопроса нет.
    new_question := nullif(btrim(record_understanding.amend ->> 'question'), '');

    select t.* into was
      from public.tasks t
     where t.id = (record_understanding.amend ->> 'task_id')::uuid
       and t.owner_telegram_id = record_understanding.owner_telegram_id
       and t.status = 'active'
       for update;

    if not found then
      raise exception 'record_understanding: task % is not an active task of %',
        record_understanding.amend ->> 'task_id', record_understanding.owner_telegram_id;
    end if;

    if jsonb_typeof(changes -> 'people') = 'array' then
      select coalesce(array_agg(person.name), '{}') into people_names
        from jsonb_array_elements_text(changes -> 'people') as person(name);
    end if;

    -- Правило — по сроку после поправки (§13.5): срок снят — снято и оно,
    -- срок перенесён без правила — перенесён только этот раз.
    new_due := case when changes ? 'due_at' then (changes ->> 'due_at')::timestamptz
                    else was.due_at end;
    new_precision := case when changes ? 'due_precision' then changes ->> 'due_precision'
                          else was.due_precision end;
    if coalesce(jsonb_typeof(changes -> 'repeat'), 'null') <> 'null' then
      if new_due is null or was.kind <> 'task' then
        raise exception 'record_understanding: repeat needs a due date of a task';
      end if;
      if owner_zone is null then
        raise exception 'record_understanding: owner has no timezone';
      end if;
      new_repeat := public.repeat_rule(changes -> 'repeat', new_due, new_precision, owner_zone);
      new_occurrence := new_due;
    elsif changes ? 'repeat' or new_due is null then
      new_repeat := null;
      new_occurrence := null;
    else
      new_repeat := was.repeat;
      new_occurrence := was.occurrence_at;
    end if;

    update public.tasks t
       set title = case
             when changes ? 'title' then coalesce(nullif(changes ->> 'title', ''), t.title)
             else t.title end,
           due_at = case
             when changes ? 'due_at' then (changes ->> 'due_at')::timestamptz
             else t.due_at end,
           due_precision = case
             when changes ? 'due_precision' then changes ->> 'due_precision'
             else t.due_precision end,
           priority = case
             when changes ? 'priority' then coalesce(changes ->> 'priority', t.priority)
             else t.priority end,
           promise = case
             when changes ? 'promise' then changes ->> 'promise'
             else t.promise end,
           people = case
             when changes ? 'people' then people_names
             else t.people end,
           needs_review = case
             when changes ? 'needs_review'
               then coalesce((changes ->> 'needs_review')::boolean, t.needs_review)
             else t.needs_review end,
           repeat = new_repeat,
           occurrence_at = new_occurrence,
           -- Вопросы владельца сняты выше; новый ставится только по ключу.
           open_question = coalesce(new_question, t.open_question),
           question_asked_at = case
             when new_question is not null then now()
             else t.question_asked_at end
     where t.id = was.id
       and t.owner_telegram_id = record_understanding.owner_telegram_id
       and t.status = 'active'
    returning t.* into saved;

    if not found then
      raise exception 'record_understanding: task % is not an active task of %',
        record_understanding.amend ->> 'task_id', record_understanding.owner_telegram_id;
    end if;

    -- Срок мог измениться: неотправленные напоминания уходят, новые
    -- планирует бот по §6.1. Ушедшая ступень, которая по новому сроку снова
    -- в будущем, взводится заново — иначе строка «Напомню» в ответе
    -- обещала бы то, чего не будет (инвариант 4).
    delete from public.reminders r
     where r.task_id = saved.id
       and r.owner_telegram_id = record_understanding.owner_telegram_id
       and r.sent_at is null;

    if jsonb_typeof(record_understanding.amend -> 'reminders') = 'array' then
      insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
      select record_understanding.owner_telegram_id,
             saved.id,
             planned.item ->> 'stage',
             (planned.item ->> 'fire_at')::timestamptz
        from jsonb_array_elements(record_understanding.amend -> 'reminders') as planned(item)
      on conflict on constraint reminders_task_id_stage_key do update
         set fire_at = excluded.fire_at,
             sent_at = null,
             telegram_message_id = null;
    end if;

    update public.messages m
       set task_id = saved.id
     where m.id = record_understanding.message_id
       and m.owner_telegram_id = record_understanding.owner_telegram_id;

    return saved;
  end if;

  -- Правка задачи словом (§12.3): закрыть, убрать, пропустить раз,
  -- поправить, спросить по правке или «менять нечего». Задачу закрыли,
  -- убрали или перевели на другой раз, пока модель разбирала сообщение, —
  -- отказ и откат целиком, как у ответа на вопрос.
  if coalesce(jsonb_typeof(record_understanding.edit), 'null') <> 'null' then
    saved := public.edit_from_chat(
      record_understanding.owner_telegram_id, record_understanding.edit
    );

    if saved.id is null then
      raise exception 'record_understanding: task % is not an active task of %',
        record_understanding.edit ->> 'task_id', record_understanding.owner_telegram_id;
    end if;

    update public.messages m
       set task_id = saved.id
     where m.id = record_understanding.message_id
       and m.owner_telegram_id = record_understanding.owner_telegram_id;

    return saved;
  end if;

  if record_understanding.task is null
     or jsonb_typeof(record_understanding.task) = 'null' then
    return null;
  end if;

  saved := public.insert_message_task(
    record_understanding.owner_telegram_id,
    record_understanding.message_id,
    record_understanding.task,
    record_understanding.reminders
  );

  update public.messages m
     set task_id = saved.id
   where m.id = record_understanding.message_id
     and m.owner_telegram_id = record_understanding.owner_telegram_id;

  return saved;
end;
$$;
