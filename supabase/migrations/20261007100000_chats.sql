-- Личные чаты: договорённости и «кому не ответили» (этап 025).
--
-- Схема — techspec/03-schema.md §3.11–3.15; поведение — techspec/25-chats.md.
-- Применяется `supabase db push` или тем же текстом в SQL-редакторе панели
-- Supabase (supabase/README.md). Применённая миграция не правится: следующая
-- правка — следующий файл.
--
-- Общая часть для всех площадок: Telegram (этап 025), Instagram (026) и MAX
-- (027) пишут сюда же, поэтому площадка — сразу из трёх значений. Приложение
-- эти таблицы не читает; пишет и читает их только бот функциями
-- `service_role` (§4.2).

-- Согласие на площадку (§25.5): подключена ли, id подключения, спросили ли,
-- согласился ли владелец. Строка одна на площадку.
create table public.chat_sources (
  id uuid primary key default gen_random_uuid(),
  owner_telegram_id bigint not null,
  platform text not null,
  -- Telegram — id бизнес-подключения; у площадки без него пусто
  connection_id text,
  -- подключение действует; `false` — владелец отключил бота
  is_enabled boolean not null default true,
  -- вопрос о согласии ушёл владельцу
  asked_at timestamptz,
  -- «Согласен»; до него сообщения площадки не хранятся
  consented_at timestamptz,
  -- «Не надо»
  declined_at timestamptz,
  created_at timestamptz not null default now(),
  constraint chat_sources_platform_check check (platform in ('telegram', 'instagram', 'max')),
  constraint chat_sources_owner_platform_key unique (owner_telegram_id, platform),
  constraint chat_sources_consent_check check (consented_at is null or declined_at is null)
);

-- Чат (§25.1): собеседник или группа на площадке и «ждёт ответа».
create table public.chat_threads (
  id uuid primary key default gen_random_uuid(),
  owner_telegram_id bigint not null,
  platform text not null,
  -- ключ чата на площадке: у Telegram — id чата строкой
  chat_key text not null,
  -- имя собеседника или группы, последнее известное
  name text not null default '',
  -- вести ли «ждёт ответа»: у MAX не ведётся (§27.3)
  tracks_waiting boolean not null default true,
  -- когда пришло последнее сообщение: по нему чат «затих» (§25.3)
  last_message_at timestamptz not null default now(),
  -- последнее сообщение владельца, время площадки: оно снимает «ждёт ответа»
  last_out_at timestamptz,
  -- «ждёт ответа» (§25.4): с какого времени, о чём, кому — и напомнено ли
  waiting_since timestamptz,
  waiting_about text,
  waiting_to text,
  waiting_reminded_at timestamptz,
  -- неудачных разборов подряд: третья — пропуск куска (§25.3)
  failures smallint not null default 0,
  created_at timestamptz not null default now(),
  constraint chat_threads_platform_check check (platform in ('telegram', 'instagram', 'max')),
  constraint chat_threads_key unique (owner_telegram_id, platform, chat_key),
  constraint chat_threads_chat_key_check check (char_length(chat_key) between 1 and 200),
  constraint chat_threads_waiting_check check (
    (waiting_since is null) = (waiting_about is null)
    and (waiting_since is null) = (waiting_to is null)
    and (waiting_reminded_at is null or waiting_since is not null)
  ),
  constraint chat_threads_failures_check check (failures >= 0)
);

-- Разбор куска переписки (§25.3): какой кусок, сколько дел, след и
-- сообщение владельцу.
create table public.chat_analyses (
  id uuid primary key default gen_random_uuid(),
  owner_telegram_id bigint not null,
  thread_id uuid not null references public.chat_threads (id) on delete cascade,
  -- `done` — модель разобрала; `skipped` — три неудачи или читать нечего
  status text not null,
  -- какой кусок разобран: сколько сообщений и от какого до какого времени
  messages_count integer not null,
  first_sent_at timestamptz not null,
  last_sent_at timestamptz not null,
  -- сколько дел записано
  items smallint not null default 0,
  -- с кем переписка — имя в творительном падеже для сообщения владельцу
  chat_with text,
  -- что ждёт ответа, если ждёт (§25.4)
  waiting_about text,
  -- след для разработчика: ответ модели, токены и длительность (§25.3)
  analysis jsonb,
  ai_model text,
  input_tokens integer,
  output_tokens integer,
  duration_ms integer,
  -- сообщение владельцу ушло: «отправить → пометить» (§6.2)
  report_message_id bigint,
  reported_at timestamptz,
  created_at timestamptz not null default now(),
  constraint chat_analyses_status_check check (status in ('done', 'skipped')),
  constraint chat_analyses_items_check check (items between 0 and 5 and (status = 'done' or items = 0)),
  constraint chat_analyses_count_check check (messages_count >= 1),
  constraint chat_analyses_reported_check check (report_message_id is null or reported_at is not null)
);

-- Тик (§25.4) ищет разборы с делами, о которых владелец ещё не узнал.
create index chat_analyses_unreported_idx
  on public.chat_analyses (owner_telegram_id, created_at)
  where reported_at is null and items > 0;

-- Сообщение чата (§25.1). Текст живёт семь дней, потом стирается.
create table public.chat_messages (
  id uuid primary key default gen_random_uuid(),
  owner_telegram_id bigint not null,
  thread_id uuid not null references public.chat_threads (id) on delete cascade,
  -- id сообщения на площадке
  external_id text not null,
  -- `in` — собеседник, `out` — владелец
  direction text not null,
  sender text not null default '',
  -- время на площадке
  sent_at timestamptz not null,
  kind text not null default 'text',
  -- текст, подпись или расшифровка; стёртый — пустой
  text text not null default '',
  -- разбор, которым сообщение разобрано; пусто — ещё нет
  analysis_id uuid references public.chat_analyses (id),
  -- текст стёрт удалением или сроком
  erased_at timestamptz,
  -- когда пришло: по нему считаются тишина, два часа и семь дней
  created_at timestamptz not null default now(),
  constraint chat_messages_external_key unique (thread_id, external_id),
  constraint chat_messages_external_check check (char_length(external_id) between 1 and 200),
  constraint chat_messages_direction_check check (direction in ('in', 'out')),
  constraint chat_messages_kind_check check (kind in ('text', 'voice', 'video_note', 'photo', 'other')),
  constraint chat_messages_erased_check check (erased_at is null or text = '')
);

-- Разбор берёт неразобранные сообщения чата и до 20 прежних.
create index chat_messages_thread_idx on public.chat_messages (thread_id, sent_at);
-- Срок хранения: тик стирает текст, который ещё не стёрт.
create index chat_messages_kept_idx
  on public.chat_messages (owner_telegram_id, created_at)
  where erased_at is null;

-- Задача знает свой разбор (§25.1) — по образцу `source_message_id` и
-- `source_item`: номер дела в разборе 1–5, оба вместе.
alter table public.tasks
  add column chat_analysis_id uuid references public.chat_analyses (id),
  add column chat_item smallint,
  add constraint tasks_chat_item_check check (
    (chat_analysis_id is null) = (chat_item is null)
    and (chat_item is null or chat_item between 1 and 5)
  ),
  add constraint tasks_chat_item_key unique (chat_analysis_id, chat_item),
  add constraint tasks_chat_source_check check (source_message_id is null or chat_analysis_id is null);

-- Правила доступа (§4.2): владелец видит только свои строки. Для anon
-- политик нет намеренно. Приложение эти таблицы не читает.
alter table public.chat_sources enable row level security;
alter table public.chat_threads enable row level security;
alter table public.chat_messages enable row level security;
alter table public.chat_analyses enable row level security;

create policy "chat_sources: owner only"
  on public.chat_sources
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);

create policy "chat_threads: owner only"
  on public.chat_threads
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);

create policy "chat_messages: owner only"
  on public.chat_messages
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);

create policy "chat_analyses: owner only"
  on public.chat_analyses
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);

-- --- Согласие ---------------------------------------------------------------

-- Площадка подключена или отключена (§25.2, §25.5). Повтор обновляет id и
-- включённость. Владелец отказался, потом отключил бота и подключил снова —
-- это новое включение: отказ снимается, и вопрос уйдёт ещё раз. Согласие не
-- снимается ничем, кроме «Не надо».
create function public.connect_chat_source(
  owner_telegram_id bigint,
  platform text,
  connection_id text,
  is_enabled boolean
) returns public.chat_sources
language sql
as $$
  insert into public.chat_sources as s (owner_telegram_id, platform, connection_id, is_enabled)
  values (
    connect_chat_source.owner_telegram_id,
    connect_chat_source.platform,
    connect_chat_source.connection_id,
    connect_chat_source.is_enabled
  )
  on conflict on constraint chat_sources_owner_platform_key do update
     set connection_id = excluded.connection_id,
         is_enabled = excluded.is_enabled,
         asked_at = case
           when excluded.is_enabled and not s.is_enabled and s.declined_at is not null then null
           else s.asked_at
         end,
         declined_at = case
           when excluded.is_enabled and not s.is_enabled then null
           else s.declined_at
         end
  returning s.*;
$$;

-- Вопрос о согласии ушёл (§25.5): «отправить → пометить». Только у площадки,
-- о которой ещё не спрашивали и решения нет.
create function public.mark_consent_asked(
  owner_telegram_id bigint,
  platform text
) returns boolean
language sql
as $$
  with asked as (
    update public.chat_sources s
       set asked_at = now()
     where s.owner_telegram_id = mark_consent_asked.owner_telegram_id
       and s.platform = mark_consent_asked.platform
       and s.asked_at is null
       and s.consented_at is null
       and s.declined_at is null
    returning 1
  )
  select exists (select 1 from asked);
$$;

-- «Согласен» или «Не надо» (§25.5). Решение можно поменять кнопкой ещё раз.
-- Площадки нет — пустая строка.
create function public.answer_consent(
  owner_telegram_id bigint,
  platform text,
  agreed boolean
) returns public.chat_sources
language sql
as $$
  update public.chat_sources s
     set consented_at = case when answer_consent.agreed then coalesce(s.consented_at, now()) end,
         declined_at = case when answer_consent.agreed then null else coalesce(s.declined_at, now()) end,
         asked_at = coalesce(s.asked_at, now())
   where s.owner_telegram_id = answer_consent.owner_telegram_id
     and s.platform = answer_consent.platform
  returning s.*;
$$;

-- --- Приём ------------------------------------------------------------------

-- Сообщение чата (§25.1, §25.2). Хранится, только если площадка подключена
-- этим подключением, включена и владелец согласился, — проверяет сама база:
-- до «Согласен» ничего не пишется (§25.5). Ответ — что вышло:
-- `stored`, `repeat` (уже есть — id прежнего), `no_source`,
-- `unknown_connection` (чужое или новое подключение — бот спросит Telegram),
-- `disabled`, `no_consent`.
--
-- Чат заводится по ключу; имя — последнее известное. `tracks_waiting`
-- ставится при заведении чата. Сообщение владельца двигает `last_out_at` и
-- снимает «ждёт ответа», заданное не позже него (§25.4).
create function public.store_chat_message(
  owner_telegram_id bigint,
  platform text,
  connection_id text,
  chat_key text,
  chat_name text,
  external_id text,
  direction text,
  sender text,
  sent_at timestamptz,
  kind text,
  message_text text,
  tracks_waiting boolean default true
) returns table (outcome text, message_id uuid)
language plpgsql
as $$
declare
  src public.chat_sources;
  chat_id uuid;
  saved uuid;
begin
  select s.* into src
    from public.chat_sources s
   where s.owner_telegram_id = store_chat_message.owner_telegram_id
     and s.platform = store_chat_message.platform;

  if not found then
    return query select 'no_source'::text, null::uuid;
    return;
  end if;
  if src.connection_id is distinct from store_chat_message.connection_id then
    return query select 'unknown_connection'::text, null::uuid;
    return;
  end if;
  if not src.is_enabled then
    return query select 'disabled'::text, null::uuid;
    return;
  end if;
  if src.consented_at is null then
    return query select 'no_consent'::text, null::uuid;
    return;
  end if;

  insert into public.chat_threads as t (owner_telegram_id, platform, chat_key, name, tracks_waiting)
  values (
    store_chat_message.owner_telegram_id,
    store_chat_message.platform,
    store_chat_message.chat_key,
    coalesce(btrim(store_chat_message.chat_name), ''),
    coalesce(store_chat_message.tracks_waiting, true)
  )
  on conflict on constraint chat_threads_key do update
     set name = coalesce(nullif(btrim(excluded.name), ''), t.name)
  returning t.id into chat_id;

  insert into public.chat_messages as m (
    owner_telegram_id, thread_id, external_id, direction, sender, sent_at, kind, text
  )
  values (
    store_chat_message.owner_telegram_id,
    chat_id,
    store_chat_message.external_id,
    store_chat_message.direction,
    coalesce(btrim(store_chat_message.sender), ''),
    store_chat_message.sent_at,
    store_chat_message.kind,
    coalesce(store_chat_message.message_text, '')
  )
  on conflict on constraint chat_messages_external_key do nothing
  returning m.id into saved;

  if saved is null then
    select m.id into saved
      from public.chat_messages m
     where m.thread_id = chat_id
       and m.external_id = store_chat_message.external_id;
    return query select 'repeat'::text, saved;
    return;
  end if;

  update public.chat_threads t
     set last_message_at = now(),
         last_out_at = case
           when store_chat_message.direction = 'out'
             then greatest(t.last_out_at, store_chat_message.sent_at)
           else t.last_out_at
         end,
         waiting_since = case
           when store_chat_message.direction = 'out' and t.waiting_since <= store_chat_message.sent_at
             then null
           else t.waiting_since
         end,
         waiting_about = case
           when store_chat_message.direction = 'out' and t.waiting_since <= store_chat_message.sent_at
             then null
           else t.waiting_about
         end,
         waiting_to = case
           when store_chat_message.direction = 'out' and t.waiting_since <= store_chat_message.sent_at
             then null
           else t.waiting_to
         end,
         waiting_reminded_at = case
           when store_chat_message.direction = 'out' and t.waiting_since <= store_chat_message.sent_at
             then null
           else t.waiting_reminded_at
         end
   where t.id = chat_id;

  return query select 'stored'::text, saved;
end;
$$;

-- Расшифровка голосового (§25.2): пишется после записи сообщения, только
-- пока оно не разобрано и не стёрто.
create function public.set_chat_transcript(
  owner_telegram_id bigint,
  message_id uuid,
  transcript text
) returns boolean
language sql
as $$
  with saved as (
    update public.chat_messages m
       set text = set_chat_transcript.transcript
     where m.id = set_chat_transcript.message_id
       and m.owner_telegram_id = set_chat_transcript.owner_telegram_id
       and m.analysis_id is null
       and m.erased_at is null
    returning 1
  )
  select exists (select 1 from saved);
$$;

-- Правка (§25.1): до разбора меняет текст, после — ничего. Подключение
-- сверяется: чужое правке не подлежит, даже если ключи чата и сообщения
-- совпали.
create function public.edit_chat_message(
  owner_telegram_id bigint,
  platform text,
  connection_id text,
  chat_key text,
  external_id text,
  message_text text
) returns boolean
language sql
as $$
  with saved as (
    update public.chat_messages m
       set text = coalesce(edit_chat_message.message_text, '')
      from public.chat_threads t, public.chat_sources s
     where t.id = m.thread_id
       and t.owner_telegram_id = edit_chat_message.owner_telegram_id
       and t.platform = edit_chat_message.platform
       and t.chat_key = edit_chat_message.chat_key
       and s.owner_telegram_id = edit_chat_message.owner_telegram_id
       and s.platform = edit_chat_message.platform
       and s.connection_id is not distinct from edit_chat_message.connection_id
       and m.owner_telegram_id = edit_chat_message.owner_telegram_id
       and m.external_id = edit_chat_message.external_id
       and m.analysis_id is null
       and m.erased_at is null
    returning 1
  )
  select exists (select 1 from saved);
$$;

-- Удаление (§25.1) стирает текст — и до разбора, и после. Возвращает,
-- сколько стёрто.
create function public.erase_chat_messages(
  owner_telegram_id bigint,
  platform text,
  connection_id text,
  chat_key text,
  external_ids text[]
) returns integer
language sql
as $$
  with erased as (
    update public.chat_messages m
       set text = '',
           erased_at = now()
      from public.chat_threads t, public.chat_sources s
     where t.id = m.thread_id
       and t.owner_telegram_id = erase_chat_messages.owner_telegram_id
       and t.platform = erase_chat_messages.platform
       and t.chat_key = erase_chat_messages.chat_key
       and s.owner_telegram_id = erase_chat_messages.owner_telegram_id
       and s.platform = erase_chat_messages.platform
       and s.connection_id is not distinct from erase_chat_messages.connection_id
       and m.owner_telegram_id = erase_chat_messages.owner_telegram_id
       and m.external_id = any(erase_chat_messages.external_ids)
       and m.erased_at is null
    returning 1
  )
  select count(*)::integer from erased;
$$;

-- --- Разбор -----------------------------------------------------------------

-- Чаты к разбору (§25.3): есть неразобранные сообщения, и разговор затих
-- (последнее пришло раньше `quiet_before`) или первое неразобранное пришло
-- раньше `stale_before`. Только площадки с согласием: после «Не надо»
-- хранимое больше не уходит модели. Старшие — первыми.
create function public.chats_to_analyze(
  owner_telegram_id bigint,
  quiet_before timestamptz,
  stale_before timestamptz
) returns table (
  thread_id uuid,
  platform text,
  chat_key text,
  name text
)
language sql
stable
as $$
  select t.id, t.platform, t.chat_key, t.name
    from public.chat_threads t
    join public.chat_sources s
      on s.owner_telegram_id = t.owner_telegram_id
     and s.platform = t.platform
     and s.consented_at is not null
    join lateral (
      select min(m.created_at) as first_at
        from public.chat_messages m
       where m.thread_id = t.id
         and m.owner_telegram_id = t.owner_telegram_id
         and m.analysis_id is null
    ) pending on pending.first_at is not null
   where t.owner_telegram_id = chats_to_analyze.owner_telegram_id
     and (t.last_message_at < chats_to_analyze.quiet_before
          or pending.first_at < chats_to_analyze.stale_before)
   order by pending.first_at, t.id;
$$;

-- Разбор одной транзакцией (§25.3): строка разбора, пометка сообщений,
-- задачи с напоминаниями и номерами, «ждёт ответа», счёт неудач — в ноль.
--
-- `tasks` — массив `{"item": N, "task": {title, due_at, due_precision,
-- promise, people}, "reminders": [{stage, fire_at}]}`, не больше пяти;
-- `waiting` — `{"about", "to", "since"}` или `null`. Сообщения — те из
-- `message_ids`, что ещё не разобраны; таких нет — повтор: ничего не
-- пишется, ответ — `null`.
--
-- «Ждёт ответа» не пишется у чата без признака и если владелец писал в чат
-- после вопроса. Чат уже ждёт и не напомнено — время прежнее, фраза новая.
-- Разбор без «ждёт ответа» прежнее не снимает (§25.4).
create function public.record_chat_analysis(
  owner_telegram_id bigint,
  thread_id uuid,
  message_ids uuid[],
  analysis jsonb,
  ai_model text,
  input_tokens integer,
  output_tokens integer,
  duration_ms integer,
  chat_with text,
  waiting jsonb,
  tasks jsonb
) returns uuid
language plpgsql
as $$
declare
  chat public.chat_threads;
  covered integer;
  first_at timestamptz;
  last_at timestamptz;
  new_count integer := 0;
  saved_id uuid;
  entry jsonb;
  people_names text[];
  new_task_id uuid;
  since timestamptz;
  about text;
  whom text;
begin
  if coalesce(jsonb_typeof(record_chat_analysis.tasks), 'null') not in ('null', 'array') then
    raise exception 'record_chat_analysis: tasks must be an array';
  end if;
  if jsonb_typeof(record_chat_analysis.tasks) = 'array' then
    new_count := jsonb_array_length(record_chat_analysis.tasks);
  end if;
  if new_count > 5 then
    raise exception 'record_chat_analysis: at most five tasks';
  end if;

  -- Владелец сверяется с владельцем чата (§4.3); блокировка — чтобы два
  -- прохода одного куска не записали его дважды.
  select t.* into chat
    from public.chat_threads t
   where t.id = record_chat_analysis.thread_id
     and t.owner_telegram_id = record_chat_analysis.owner_telegram_id
     for update;
  if not found then
    raise exception 'record_chat_analysis: thread % is not owned by %',
      record_chat_analysis.thread_id, record_chat_analysis.owner_telegram_id;
  end if;

  select count(*)::integer, min(m.sent_at), max(m.sent_at)
    into covered, first_at, last_at
    from public.chat_messages m
   where m.thread_id = chat.id
     and m.owner_telegram_id = record_chat_analysis.owner_telegram_id
     and m.analysis_id is null
     and m.id = any(record_chat_analysis.message_ids);
  if covered = 0 then
    return null;
  end if;

  about := nullif(btrim(record_chat_analysis.waiting ->> 'about'), '');
  whom := nullif(btrim(record_chat_analysis.waiting ->> 'to'), '');
  since := (record_chat_analysis.waiting ->> 'since')::timestamptz;

  insert into public.chat_analyses as a (
    owner_telegram_id, thread_id, status, messages_count, first_sent_at, last_sent_at,
    items, chat_with, waiting_about, analysis, ai_model, input_tokens, output_tokens, duration_ms
  )
  values (
    record_chat_analysis.owner_telegram_id,
    chat.id,
    'done',
    covered,
    first_at,
    last_at,
    new_count,
    nullif(btrim(record_chat_analysis.chat_with), ''),
    about,
    record_chat_analysis.analysis,
    record_chat_analysis.ai_model,
    record_chat_analysis.input_tokens,
    record_chat_analysis.output_tokens,
    record_chat_analysis.duration_ms
  )
  returning a.id into saved_id;

  update public.chat_messages m
     set analysis_id = saved_id
   where m.thread_id = chat.id
     and m.owner_telegram_id = record_chat_analysis.owner_telegram_id
     and m.analysis_id is null
     and m.id = any(record_chat_analysis.message_ids);

  -- Дела — новые задачи (§25.3): из чата задачи только добавляются.
  for entry in
    select listed.value
      from jsonb_array_elements(
        case when new_count > 0 then record_chat_analysis.tasks else '[]'::jsonb end
      ) as listed(value)
  loop
    if jsonb_typeof(entry -> 'task') is distinct from 'object' then
      raise exception 'record_chat_analysis: task must be an object';
    end if;

    people_names := '{}';
    if jsonb_typeof(entry -> 'task' -> 'people') = 'array' then
      select coalesce(array_agg(person.name), '{}') into people_names
        from jsonb_array_elements_text(entry -> 'task' -> 'people') as person(name);
    end if;

    insert into public.tasks as k (
      owner_telegram_id, title, kind, due_at, due_precision, priority, promise, people,
      needs_review, chat_analysis_id, chat_item
    )
    values (
      record_chat_analysis.owner_telegram_id,
      entry -> 'task' ->> 'title',
      'task',
      (entry -> 'task' ->> 'due_at')::timestamptz,
      entry -> 'task' ->> 'due_precision',
      'normal',
      entry -> 'task' ->> 'promise',
      people_names,
      false,
      saved_id,
      (entry ->> 'item')::smallint
    )
    returning k.id into new_task_id;

    -- Список `{stage, fire_at}` считает бот по общему правилу (§6.1).
    if jsonb_typeof(entry -> 'reminders') = 'array' then
      insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
      select record_chat_analysis.owner_telegram_id,
             new_task_id,
             planned.reminder ->> 'stage',
             (planned.reminder ->> 'fire_at')::timestamptz
        from jsonb_array_elements(entry -> 'reminders') as planned(reminder)
      on conflict on constraint reminders_task_id_stage_key do nothing;
    end if;
  end loop;

  if about is not null and whom is not null and since is not null
     and chat.tracks_waiting
     and (chat.last_out_at is null or chat.last_out_at < since) then
    if chat.waiting_since is not null and chat.waiting_reminded_at is null then
      update public.chat_threads t
         set waiting_about = about,
             waiting_to = whom,
             failures = 0
       where t.id = chat.id;
    else
      update public.chat_threads t
         set waiting_since = since,
             waiting_about = about,
             waiting_to = whom,
             waiting_reminded_at = null,
             failures = 0
       where t.id = chat.id;
    end if;
  else
    update public.chat_threads t
       set failures = 0
     where t.id = chat.id;
  end if;

  return saved_id;
end;
$$;

-- Неудачный разбор (§25.3): счёт подряд плюс один. Чужой чат — `null`.
create function public.chat_failed(
  owner_telegram_id bigint,
  thread_id uuid
) returns integer
language sql
as $$
  update public.chat_threads t
     set failures = t.failures + 1
   where t.id = chat_failed.thread_id
     and t.owner_telegram_id = chat_failed.owner_telegram_id
  returning t.failures::integer;
$$;

-- Пропуск куска (§25.3): три неудачи подряд или читать нечего. Сообщения
-- помечаются разобранными разбором `skipped` без дел, счёт — в ноль. Нечего
-- пометить — `null`.
create function public.skip_chat_messages(
  owner_telegram_id bigint,
  thread_id uuid,
  message_ids uuid[]
) returns uuid
language plpgsql
as $$
declare
  chat public.chat_threads;
  covered integer;
  first_at timestamptz;
  last_at timestamptz;
  saved_id uuid;
begin
  select t.* into chat
    from public.chat_threads t
   where t.id = skip_chat_messages.thread_id
     and t.owner_telegram_id = skip_chat_messages.owner_telegram_id
     for update;
  if not found then
    raise exception 'skip_chat_messages: thread % is not owned by %',
      skip_chat_messages.thread_id, skip_chat_messages.owner_telegram_id;
  end if;

  select count(*)::integer, min(m.sent_at), max(m.sent_at)
    into covered, first_at, last_at
    from public.chat_messages m
   where m.thread_id = chat.id
     and m.owner_telegram_id = skip_chat_messages.owner_telegram_id
     and m.analysis_id is null
     and m.id = any(skip_chat_messages.message_ids);
  if covered = 0 then
    return null;
  end if;

  insert into public.chat_analyses as a (
    owner_telegram_id, thread_id, status, messages_count, first_sent_at, last_sent_at
  )
  values (skip_chat_messages.owner_telegram_id, chat.id, 'skipped', covered, first_at, last_at)
  returning a.id into saved_id;

  update public.chat_messages m
     set analysis_id = saved_id
   where m.thread_id = chat.id
     and m.owner_telegram_id = skip_chat_messages.owner_telegram_id
     and m.analysis_id is null
     and m.id = any(skip_chat_messages.message_ids);

  update public.chat_threads t
     set failures = 0
   where t.id = chat.id;

  return saved_id;
end;
$$;

-- --- Что видит владелец -------------------------------------------------------

-- Дела разбора для сообщения владельцу (§25.4): площадка, имя чата, «с кем»
-- и задачи по номерам — какими они стали сейчас. Им и шлётся отчёт, и
-- правится после «Убрать».
create function public.chat_report(
  owner_telegram_id bigint,
  analysis_id uuid
) returns table (
  platform text,
  chat_name text,
  chat_with text,
  item smallint,
  task_id uuid,
  title text,
  due_at timestamptz,
  due_precision text,
  promise text,
  status text
)
language sql
stable
as $$
  select t.platform, t.name, a.chat_with, k.chat_item, k.id, k.title, k.due_at,
         k.due_precision, k.promise, k.status
    from public.chat_analyses a
    join public.chat_threads t on t.id = a.thread_id
    join public.tasks k
      on k.chat_analysis_id = a.id
     and k.owner_telegram_id = a.owner_telegram_id
   where a.id = chat_report.analysis_id
     and a.owner_telegram_id = chat_report.owner_telegram_id
   order by k.chat_item;
$$;

-- Сообщение о разборе ушло (§25.4) — «отправить → пометить». Id сообщения
-- пуст, если слать было нечего (задачи разбора удалили раньше).
create function public.mark_chat_report_sent(
  owner_telegram_id bigint,
  analysis_id uuid,
  telegram_message_id bigint
) returns boolean
language sql
as $$
  with sent as (
    update public.chat_analyses a
       set reported_at = now(),
           report_message_id = mark_chat_report_sent.telegram_message_id
     where a.id = mark_chat_report_sent.analysis_id
       and a.owner_telegram_id = mark_chat_report_sent.owner_telegram_id
       and a.reported_at is null
    returning 1
  )
  select exists (select 1 from sent);
$$;

-- «Убрать» (§25.4): активная задача разбора уходит в `cancelled`,
-- неотправленные напоминания стираются. Задача уже не активна — как есть,
-- без правки. Нет такой — пустая строка.
create function public.drop_chat_task(
  owner_telegram_id bigint,
  analysis_id uuid,
  item smallint
) returns public.tasks
language plpgsql
as $$
declare
  saved public.tasks;
begin
  select k.* into saved
    from public.tasks k
   where k.chat_analysis_id = drop_chat_task.analysis_id
     and k.chat_item = drop_chat_task.item
     and k.owner_telegram_id = drop_chat_task.owner_telegram_id
     for update;
  if not found then
    return null;
  end if;
  if saved.status <> 'active' then
    return saved;
  end if;

  update public.tasks k
     set status = 'cancelled'
   where k.id = saved.id
  returning k.* into saved;

  delete from public.reminders r
   where r.task_id = saved.id
     and r.owner_telegram_id = drop_chat_task.owner_telegram_id
     and r.sent_at is null;

  return saved;
end;
$$;

-- Кому не ответили (§25.4): чат ведёт «ждёт ответа», вопрос задан не позже
-- `asked_before`, напоминания не было, владелец в чат после вопроса не писал.
create function public.chats_waiting(
  owner_telegram_id bigint,
  asked_before timestamptz
) returns table (
  thread_id uuid,
  platform text,
  name text,
  waiting_since timestamptz,
  waiting_about text,
  waiting_to text
)
language sql
stable
as $$
  select t.id, t.platform, t.name, t.waiting_since, t.waiting_about, t.waiting_to
    from public.chat_threads t
   where t.owner_telegram_id = chats_waiting.owner_telegram_id
     and t.tracks_waiting
     and t.waiting_since is not null
     and t.waiting_since <= chats_waiting.asked_before
     and t.waiting_reminded_at is null
     and (t.last_out_at is null or t.last_out_at < t.waiting_since)
   order by t.waiting_since, t.id;
$$;

-- Напоминание о неотвеченном ушло: второго не будет (§25.4). Только если
-- вопрос тот же — с тем же временем.
create function public.mark_waiting_reminded(
  owner_telegram_id bigint,
  thread_id uuid,
  waiting_since timestamptz
) returns boolean
language sql
as $$
  with reminded as (
    update public.chat_threads t
       set waiting_reminded_at = now()
     where t.id = mark_waiting_reminded.thread_id
       and t.owner_telegram_id = mark_waiting_reminded.owner_telegram_id
       and t.waiting_since = mark_waiting_reminded.waiting_since
       and t.waiting_reminded_at is null
    returning 1
  )
  select exists (select 1 from reminded);
$$;

-- Срок хранения (§25.1): текст сообщений, пришедших раньше `before`,
-- стирается. Возвращает, сколько стёрто.
create function public.erase_old_chat_messages(
  owner_telegram_id bigint,
  before timestamptz
) returns integer
language sql
as $$
  with erased as (
    update public.chat_messages m
       set text = '',
           erased_at = now()
     where m.owner_telegram_id = erase_old_chat_messages.owner_telegram_id
       and m.created_at < erase_old_chat_messages.before
       and m.erased_at is null
    returning 1
  )
  select count(*)::integer from erased;
$$;

-- Все функции зовёт только бот ключом service-role, владелец — явным
-- аргументом. Право execute выдано и роли public, из неё оно наследуется —
-- поэтому забираем и там.
revoke execute on function public.connect_chat_source(bigint, text, text, boolean)
  from public, anon, authenticated;
grant execute on function public.connect_chat_source(bigint, text, text, boolean)
  to service_role;

revoke execute on function public.mark_consent_asked(bigint, text)
  from public, anon, authenticated;
grant execute on function public.mark_consent_asked(bigint, text)
  to service_role;

revoke execute on function public.answer_consent(bigint, text, boolean)
  from public, anon, authenticated;
grant execute on function public.answer_consent(bigint, text, boolean)
  to service_role;

revoke execute on function
  public.store_chat_message(bigint, text, text, text, text, text, text, text, timestamptz, text, text, boolean)
  from public, anon, authenticated;
grant execute on function
  public.store_chat_message(bigint, text, text, text, text, text, text, text, timestamptz, text, text, boolean)
  to service_role;

revoke execute on function public.set_chat_transcript(bigint, uuid, text)
  from public, anon, authenticated;
grant execute on function public.set_chat_transcript(bigint, uuid, text)
  to service_role;

revoke execute on function public.edit_chat_message(bigint, text, text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.edit_chat_message(bigint, text, text, text, text, text)
  to service_role;

revoke execute on function public.erase_chat_messages(bigint, text, text, text, text[])
  from public, anon, authenticated;
grant execute on function public.erase_chat_messages(bigint, text, text, text, text[])
  to service_role;

revoke execute on function public.chats_to_analyze(bigint, timestamptz, timestamptz)
  from public, anon, authenticated;
grant execute on function public.chats_to_analyze(bigint, timestamptz, timestamptz)
  to service_role;

revoke execute on function
  public.record_chat_analysis(bigint, uuid, uuid[], jsonb, text, integer, integer, integer, text, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function
  public.record_chat_analysis(bigint, uuid, uuid[], jsonb, text, integer, integer, integer, text, jsonb, jsonb)
  to service_role;

revoke execute on function public.chat_failed(bigint, uuid)
  from public, anon, authenticated;
grant execute on function public.chat_failed(bigint, uuid)
  to service_role;

revoke execute on function public.skip_chat_messages(bigint, uuid, uuid[])
  from public, anon, authenticated;
grant execute on function public.skip_chat_messages(bigint, uuid, uuid[])
  to service_role;

revoke execute on function public.chat_report(bigint, uuid)
  from public, anon, authenticated;
grant execute on function public.chat_report(bigint, uuid)
  to service_role;

revoke execute on function public.mark_chat_report_sent(bigint, uuid, bigint)
  from public, anon, authenticated;
grant execute on function public.mark_chat_report_sent(bigint, uuid, bigint)
  to service_role;

revoke execute on function public.drop_chat_task(bigint, uuid, smallint)
  from public, anon, authenticated;
grant execute on function public.drop_chat_task(bigint, uuid, smallint)
  to service_role;

revoke execute on function public.chats_waiting(bigint, timestamptz)
  from public, anon, authenticated;
grant execute on function public.chats_waiting(bigint, timestamptz)
  to service_role;

revoke execute on function public.mark_waiting_reminded(bigint, uuid, timestamptz)
  from public, anon, authenticated;
grant execute on function public.mark_waiting_reminded(bigint, uuid, timestamptz)
  to service_role;

revoke execute on function public.erase_old_chat_messages(bigint, timestamptz)
  from public, anon, authenticated;
grant execute on function public.erase_old_chat_messages(bigint, timestamptz)
  to service_role;
