-- Голосовые: речь становится поручением.
--
-- Схема — techspec/03-schema.md §3.2, §3.4; поведение — §9. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.

-- Вид сообщения: к тексту добавляются голосовое и видео-кружок (§9.1).
-- Inline-ограничение из миграции 001 Postgres назвал `messages_kind_check`.
alter table public.messages
  drop constraint messages_kind_check,
  add constraint messages_kind_check
    check (kind in ('text', 'voice', 'video_note')),
  -- у голоса текст пуст, пока его не расслышали (§9.3)
  alter column text set default '',
  -- файл в Telegram: по нему звук можно скачать снова, пока Telegram его хранит
  add column telegram_file_id text,
  -- длительность звука — мера стоимости распознавания
  add column duration_seconds int,
  -- уверенность распознавания 0–1 (§9.4)
  add column transcript_confidence numeric;

-- Шаг первый — с видом сообщения и файлом (§3.4). Поручение лежит в базе
-- до того, как кто-то его расслышал (инвариант 5): у голоса `text` пуст, а
-- `telegram_file_id` и `duration_seconds` заполнены. У четырёхаргументной
-- версии места для файла нет, а две перегрузки PostgREST различать нечем —
-- поэтому старая уходит, новая с необязательными аргументами принимает и
-- текст, как раньше.
drop function public.record_message(bigint, bigint, bigint, text);

create function public.record_message(
  owner_telegram_id bigint,
  chat_id bigint,
  telegram_message_id bigint,
  text text,
  kind text default 'text',
  telegram_file_id text default null,
  duration_seconds int default null
) returns public.messages
language sql
as $$
  with new_message as (
    insert into public.messages (
      owner_telegram_id, chat_id, telegram_message_id, kind, text,
      telegram_file_id, duration_seconds
    )
    values (
      record_message.owner_telegram_id,
      record_message.chat_id,
      record_message.telegram_message_id,
      coalesce(record_message.kind, 'text'),
      record_message.text,
      record_message.telegram_file_id,
      record_message.duration_seconds
    )
    on conflict (owner_telegram_id, chat_id, telegram_message_id) do nothing
    returning *
  )
  select * from new_message
  union all
  -- Повтор: сообщение уже было, новых строк нет — отдаём прежнее вместе
  -- с ответом, который бот уже давал.
  select m.*
  from public.messages m
  where not exists (select 1 from new_message)
    and m.owner_telegram_id = record_message.owner_telegram_id
    and m.chat_id = record_message.chat_id
    and m.telegram_message_id = record_message.telegram_message_id
  limit 1;
$$;

-- Зовёт только бот ключом service-role. Одного `revoke ... from anon,
-- authenticated` мало: право execute выдано роли public, и из неё оно
-- наследуется, поэтому забираем и там.
revoke execute on function
  public.record_message(bigint, bigint, bigint, text, text, text, int)
  from public, anon, authenticated;
grant execute on function
  public.record_message(bigint, bigint, bigint, text, text, text, int)
  to service_role;

-- Шаг второй — с расшифровкой (§9.3): тем же вызовом, что разбор, ответ,
-- задача, напоминания и память, она становится текстом сообщения.
-- Десятиаргументная версия уходит по той же причине, что и у
-- `record_message`.
drop function public.record_understanding(uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb);

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
  transcript_confidence numeric default null
) returns public.tasks
language plpgsql
as $$
declare
  saved public.tasks;
  people_names text[] := '{}';
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
         -- Расшифровка становится текстом сообщения. У текста и у
         -- нерасслышанного голоса она null — тогда текст не трогается:
         -- у первого он уже есть, у второго остаётся пустым, а `reply`
         -- «не расслышал» ложится рядом с `telegram_file_id`.
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
  if jsonb_typeof(record_understanding.facts) = 'array' then
    insert into public.facts (owner_telegram_id, category, text, status, source_message_id)
    select record_understanding.owner_telegram_id,
           item.value ->> 'category',
           item.value ->> 'text',
           coalesce(item.value ->> 'status', 'guess'),
           record_understanding.message_id
      from jsonb_array_elements(record_understanding.facts) as item(value)
     where coalesce(item.value ->> 'text', '') <> ''
    on conflict (owner_telegram_id, category, text) do update
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
    -- Повтор того же сообщения: задача уже есть, и напоминания к ней тоже.
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

  insert into public.tasks (
    owner_telegram_id, title, kind, due_at, due_precision,
    priority, promise, people, needs_review, source_message_id
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
    uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric
  )
  from public, anon, authenticated;
grant execute on function
  public.record_understanding(
    uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric
  )
  to service_role;

-- Mini App читает `kind` и `duration_seconds` из `messages` под политикой
-- «messages: owner only» из миграции 001 — новых правил не нужно.
