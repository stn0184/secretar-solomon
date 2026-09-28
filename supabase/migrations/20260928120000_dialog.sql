-- Уточняющий вопрос: один вопрос у задачи и ответ в ту же задачу.
--
-- Схема — techspec/03-schema.md §3.3, §3.4; поведение — §10. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.

-- Вопрос живёт у задачи, а не в истории разговора с моделью (инвариант 5):
-- текст вопроса и момент, когда он задан. Оба пусты — вопроса нет; старше
-- суток — считается снятым (§10.3), даже если ещё не очищен.
alter table public.tasks
  add column open_question text,
  add column question_asked_at timestamptz;

-- Шаг второй — с поправкой к уже заведённой задаче (§10.2). Двенадцати-
-- аргументная версия уходит: две перегрузки PostgREST различать нечем, а
-- новый аргумент необязательный, и прежний вызов бота на новой версии
-- работает как раньше.
drop function public.record_understanding(
  uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric
);

create function public.record_understanding(
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
  amend jsonb default null
) returns public.tasks
language plpgsql
as $$
declare
  saved public.tasks;
  people_names text[] := '{}';
  changes jsonb;
  asked text;
begin
  -- Владелец передаётся явно и сверяется с владельцем сообщения (§4.3):
  -- ключ service-role правила доступа обходит, поэтому разделение держит
  -- здесь сама функция, а не RLS.
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
         )
   where m.id = record_understanding.message_id
     and m.owner_telegram_id = record_understanding.owner_telegram_id;

  if not found then
    raise exception 'record_understanding: message % is not owned by %',
      record_understanding.message_id, record_understanding.owner_telegram_id;
  end if;

  -- Память пишется для любого вида сообщения — и для тех, где задачи нет
  -- (`about_me`, `chat`), поэтому раньше ранних выходов (§8.2, §8.3).
  --
  -- Цель конфликта названа ограничением, а не списком колонок: в plpgsql
  -- `on conflict (owner_telegram_id, …)` совпадает с именем параметра, и
  -- Postgres отказывает «column reference is ambiguous» на каждом вызове —
  -- так было в версиях 006 и 007. Имя — то, что Postgres дал безымянному
  -- `unique (owner_telegram_id, category, text)` миграции 006.
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

  select t.* into saved
    from public.tasks t
   where t.source_message_id = record_understanding.message_id
   limit 1;
  if found then
    -- Повтор того же сообщения: задача уже есть, напоминания к ней тоже, а
    -- вопрос, который поставил первый проход, остаётся на месте.
    return saved;
  end if;

  -- Любой записанный разбор снимает открытые вопросы владельца (§10.3):
  -- ответ, новое поручение, разговор, сведение о себе и вопрос старше суток
  -- закрываются одним правилом. Новый вопрос ставится ниже, уже после.
  update public.tasks t
     set open_question = null,
         question_asked_at = null
   where t.owner_telegram_id = record_understanding.owner_telegram_id
     and (t.open_question is not null or t.question_asked_at is not null);

  -- Ответ на вопрос (§10.2): вместо новой задачи — поправка к названной.
  -- Меняются только ключи из `fields`; чужая или несуществующая задача —
  -- отказ, и вся транзакция, включая разбор и память, откатывается.
  if record_understanding.amend is not null
     and jsonb_typeof(record_understanding.amend) = 'object' then
    changes := coalesce(record_understanding.amend -> 'fields', '{}'::jsonb);

    if jsonb_typeof(changes -> 'people') = 'array' then
      select coalesce(array_agg(person.name), '{}') into people_names
        from jsonb_array_elements_text(changes -> 'people') as person(name);
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
             else t.needs_review end
     where t.id = (record_understanding.amend ->> 'task_id')::uuid
       and t.owner_telegram_id = record_understanding.owner_telegram_id
    returning t.* into saved;

    if not found then
      raise exception 'record_understanding: task % is not owned by %',
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
      on conflict (task_id, stage) do update
         set fire_at = excluded.fire_at,
             sent_at = null,
             telegram_message_id = null;
    end if;

    return saved;
  end if;

  if record_understanding.task is null
     or jsonb_typeof(record_understanding.task) = 'null' then
    return null;
  end if;

  if jsonb_typeof(record_understanding.task -> 'people') = 'array' then
    select array_agg(person.name) into people_names
      from jsonb_array_elements_text(record_understanding.task -> 'people') as person(name);
  end if;

  -- Уточняющий вопрос (§10.1): задача записывается сразу, вопрос — у неё.
  asked := nullif(btrim(record_understanding.task ->> 'open_question'), '');

  insert into public.tasks (
    owner_telegram_id, title, kind, due_at, due_precision,
    priority, promise, people, needs_review, open_question, question_asked_at,
    source_message_id
  )
  values (
    record_understanding.owner_telegram_id,
    record_understanding.task ->> 'title',
    coalesce(record_understanding.task ->> 'kind', 'task'),
    (record_understanding.task ->> 'due_at')::timestamptz,
    record_understanding.task ->> 'due_precision',
    coalesce(record_understanding.task ->> 'priority', 'normal'),
    record_understanding.task ->> 'promise',
    coalesce(people_names, '{}'),
    coalesce((record_understanding.task ->> 'needs_review')::boolean, false),
    asked,
    case when asked is null then null else now() end,
    record_understanding.message_id
  )
  returning * into saved;

  -- Список `{stage, fire_at}` считает бот (§6.1); пустой — напоминаний нет
  -- (срока нет, срок прошёл, это идея или желание).
  if jsonb_typeof(record_understanding.reminders) = 'array' then
    insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
    select record_understanding.owner_telegram_id,
           saved.id,
           planned.item ->> 'stage',
           (planned.item ->> 'fire_at')::timestamptz
      from jsonb_array_elements(record_understanding.reminders) as planned(item)
    on conflict (task_id, stage) do nothing;
  end if;

  return saved;
end;
$$;

revoke execute on function
  public.record_understanding(
    uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric, jsonb
  )
  from public, anon, authenticated;
grant execute on function
  public.record_understanding(
    uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric, jsonb
  )
  to service_role;

-- Mini App читает задачи явным списком колонок под политикой «tasks: owner
-- only» — новые колонки ей не видны и не нужны (§10.4), новых правил нет.
