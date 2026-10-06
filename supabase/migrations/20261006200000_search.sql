-- Поиск по поручению: таблица `searches` и восемь функций бота.
--
-- Схема — techspec/03-schema.md §3.10; поведение — §24. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.

-- Строка на поиск (§24.5). Ответ поиска живёт здесь, а не в
-- `messages.reply`: там остаётся «Ищу: …», и текст чужих сайтов в промпт
-- разбора не попадает (инвариант 3).
create table public.searches (
  id uuid primary key default gen_random_uuid(),
  owner_telegram_id bigint not null,
  -- сообщение с просьбой; поиск из сообщения один
  message_id uuid not null references public.messages(id) on delete cascade,
  -- запрос целиком, как его понял разбор (`title`)
  query text not null,
  status text not null default 'pending',
  -- сколько раз поиск начинали
  attempts smallint not null default 0,
  -- когда начата текущая попытка; пусто — не начат или попытку вернули
  started_at timestamptz,
  -- ответ поиска: записывается до отправки
  answer text,
  -- сообщение с ответом в чате
  telegram_message_id bigint,
  -- след для разработчика (§24.2)
  input_tokens integer,
  output_tokens integer,
  web_searches integer,
  web_fetches integer,
  duration_ms integer,
  created_at timestamptz not null default now(),
  finished_at timestamptz,
  constraint searches_message_id_key unique (message_id),
  constraint searches_query_check check (char_length(query) between 1 and 500),
  constraint searches_status_check check (status in ('pending', 'done', 'failed')),
  constraint searches_attempts_check check (attempts >= 0),
  constraint searches_answer_check check (answer is null or char_length(answer) between 1 and 4096),
  -- конец есть у завершённого и не удавшегося, у ждущего — нет
  constraint searches_finished_check check ((status = 'pending') = (finished_at is null)),
  -- `done` — ответ ушёл: он записан, и сообщение с ним известно
  constraint searches_done_check check (
    (status = 'done') = (telegram_message_id is not null)
    and (status <> 'done' or answer is not null)
  )
);

-- Тик (§24.3) ищет поиски владельца, которые ещё ждут.
create index searches_pending_idx
  on public.searches (owner_telegram_id, created_at)
  where status = 'pending';

-- Правила доступа (§4.2): владелец видит только свои строки. Для anon
-- политик нет намеренно. Приложение таблицу не читает.
alter table public.searches enable row level security;

create policy "searches: owner only"
  on public.searches
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);

-- Завести поиск (§24.3, шаг 1) — раньше, чем бот скажет «Ищу». Сообщение
-- чужое или его нет — отказ. Строка по этому сообщению уже есть — её id, а
-- запрос прежний: повтор того же обновления второго поиска не заводит.
--
-- Цель конфликта названа ограничением: в plpgsql `on conflict (message_id)`
-- совпадает с именем параметра.
create function public.start_search(
  owner_telegram_id bigint,
  message_id uuid,
  query text
) returns uuid
language plpgsql
as $$
declare
  search_id uuid;
begin
  perform 1
     from public.messages m
    where m.id = start_search.message_id
      and m.owner_telegram_id = start_search.owner_telegram_id;
  if not found then
    raise exception 'start_search: message % is not owned by %',
      start_search.message_id, start_search.owner_telegram_id;
  end if;

  insert into public.searches as s (owner_telegram_id, message_id, query)
  values (start_search.owner_telegram_id, start_search.message_id, start_search.query)
  on conflict on constraint searches_message_id_key do nothing
  returning s.id into search_id;

  if search_id is null then
    select s.id into search_id
      from public.searches s
     where s.message_id = start_search.message_id
       and s.owner_telegram_id = start_search.owner_telegram_id;
  end if;
  return search_id;
end;
$$;

-- Взять поиск в работу (§24.3): атомарно, одной правкой строки — второй
-- заход по той же строке её уже не возьмёт. Берётся ждущий без ответа, не
-- начатый или начатый раньше `stale_before` (брошенный перезапуском).
-- Возвращает строку с чатом и сообщением просьбы — им бот ответит.
create function public.take_search(
  owner_telegram_id bigint,
  search_id uuid,
  stale_before timestamptz
) returns table (
  id uuid,
  query text,
  attempts smallint,
  answer text,
  created_at timestamptz,
  request_chat_id bigint,
  request_message_id bigint
)
language sql
as $$
  with taken as (
    update public.searches s
       set started_at = now(),
           attempts = s.attempts + 1
     where s.id = take_search.search_id
       and s.owner_telegram_id = take_search.owner_telegram_id
       and s.status = 'pending'
       and s.answer is null
       and (s.started_at is null or s.started_at < take_search.stale_before)
    returning s.id, s.query, s.attempts, s.answer, s.created_at, s.message_id
  )
  select t.id, t.query, t.attempts, t.answer, t.created_at, m.chat_id, m.telegram_message_id
    from taken t
    join public.messages m on m.id = t.message_id;
$$;

-- Записать ответ и след (§24.2, §24.3, шаг 4) — до отправки. След ложится
-- с ответом, а не с `done`: записанный и не ушедший ответ следующий тик шлёт
-- без нового вызова, и след уже должен быть в строке.
create function public.record_search_answer(
  owner_telegram_id bigint,
  search_id uuid,
  answer text,
  input_tokens integer,
  output_tokens integer,
  web_searches integer,
  web_fetches integer,
  duration_ms integer
) returns boolean
language sql
as $$
  with saved as (
    update public.searches s
       set answer = record_search_answer.answer,
           input_tokens = record_search_answer.input_tokens,
           output_tokens = record_search_answer.output_tokens,
           web_searches = record_search_answer.web_searches,
           web_fetches = record_search_answer.web_fetches,
           duration_ms = record_search_answer.duration_ms
     where s.id = record_search_answer.search_id
       and s.owner_telegram_id = record_search_answer.owner_telegram_id
       and s.status = 'pending'
       and s.answer is null
    returning 1
  )
  select exists (select 1 from saved);
$$;

-- Ответ ушёл — `done` с id сообщения (§24.3, шаг 4).
create function public.finish_search(
  owner_telegram_id bigint,
  search_id uuid,
  telegram_message_id bigint
) returns boolean
language sql
as $$
  with finished as (
    update public.searches s
       set status = 'done',
           telegram_message_id = finish_search.telegram_message_id,
           finished_at = now()
     where s.id = finish_search.search_id
       and s.owner_telegram_id = finish_search.owner_telegram_id
       and s.status = 'pending'
       and s.answer is not null
    returning 1
  )
  select exists (select 1 from finished);
$$;

-- Попытка не удалась (§24.6) — вернуть её: поиск снова не начат, следующий
-- тик возьмёт его сразу. Возвращает, сколько раз поиск уже начинали; по
-- этому числу бот решает, не пора ли сказать «Не получилось». Нет такого
-- ждущего поиска без ответа — `null`.
create function public.release_search(
  owner_telegram_id bigint,
  search_id uuid
) returns integer
language sql
as $$
  update public.searches s
     set started_at = null
   where s.id = release_search.search_id
     and s.owner_telegram_id = release_search.owner_telegram_id
     and s.status = 'pending'
     and s.answer is null
  returning s.attempts::integer;
$$;

-- Поиск не удался или не успел (§24.6) — `failed`. Пишется после того, как
-- владелец узнал: порядок «отправить → пометить». У поиска с записанным
-- ответом — `false`: ответ ещё уйдёт.
create function public.fail_search(
  owner_telegram_id bigint,
  search_id uuid
) returns boolean
language sql
as $$
  with failed as (
    update public.searches s
       set status = 'failed',
           finished_at = now()
     where s.id = fail_search.search_id
       and s.owner_telegram_id = fail_search.owner_telegram_id
       and s.status = 'pending'
       and s.answer is null
    returning 1
  )
  select exists (select 1 from failed);
$$;

-- Поиски для тика (§24.3, шаг 3): ждущие, о которых бот уже сказал «Ищу»
-- (у сообщения с просьбой есть ответ), — с записанным ответом, не начатые
-- или начатые раньше `stale_before`. Сколько им лет и сколько было попыток,
-- решает бот. Поиск, о котором «Ищу» не прозвучало (запись разбора упала),
-- не берётся: повтор того же обновления запустит его сам.
create function public.searches_to_resume(
  owner_telegram_id bigint,
  stale_before timestamptz
) returns table (
  id uuid,
  query text,
  attempts smallint,
  answer text,
  created_at timestamptz,
  request_chat_id bigint,
  request_message_id bigint
)
language sql
stable
as $$
  select s.id, s.query, s.attempts, s.answer, s.created_at, m.chat_id, m.telegram_message_id
    from public.searches s
    join public.messages m on m.id = s.message_id
   where s.owner_telegram_id = searches_to_resume.owner_telegram_id
     and m.owner_telegram_id = searches_to_resume.owner_telegram_id
     and s.status = 'pending'
     and m.reply is not null
     and (s.answer is not null
          or s.started_at is null
          or s.started_at < searches_to_resume.stale_before)
   order by s.created_at, s.id;
$$;

-- Прошлый поиск для уточнения вдогонку (§24.2): последний завершённый
-- владельца, заведённый раньше `before` и завершённый не раньше `since`.
create function public.previous_search(
  owner_telegram_id bigint,
  before timestamptz,
  since timestamptz
) returns table (
  query text,
  answer text
)
language sql
stable
as $$
  select s.query, s.answer
    from public.searches s
   where s.owner_telegram_id = previous_search.owner_telegram_id
     and s.status = 'done'
     and s.created_at < previous_search.before
     and s.finished_at >= previous_search.since
   order by s.created_at desc, s.id desc
   limit 1;
$$;

-- Все восемь зовёт только бот ключом service-role, владелец — явным
-- аргументом. Право execute выдано и роли public, из неё оно наследуется —
-- поэтому забираем и там.
revoke execute on function public.start_search(bigint, uuid, text)
  from public, anon, authenticated;
grant execute on function public.start_search(bigint, uuid, text)
  to service_role;

revoke execute on function public.take_search(bigint, uuid, timestamptz)
  from public, anon, authenticated;
grant execute on function public.take_search(bigint, uuid, timestamptz)
  to service_role;

revoke execute on function
  public.record_search_answer(bigint, uuid, text, integer, integer, integer, integer, integer)
  from public, anon, authenticated;
grant execute on function
  public.record_search_answer(bigint, uuid, text, integer, integer, integer, integer, integer)
  to service_role;

revoke execute on function public.finish_search(bigint, uuid, bigint)
  from public, anon, authenticated;
grant execute on function public.finish_search(bigint, uuid, bigint)
  to service_role;

revoke execute on function public.release_search(bigint, uuid)
  from public, anon, authenticated;
grant execute on function public.release_search(bigint, uuid)
  to service_role;

revoke execute on function public.fail_search(bigint, uuid)
  from public, anon, authenticated;
grant execute on function public.fail_search(bigint, uuid)
  to service_role;

revoke execute on function public.searches_to_resume(bigint, timestamptz)
  from public, anon, authenticated;
grant execute on function public.searches_to_resume(bigint, timestamptz)
  to service_role;

revoke execute on function public.previous_search(bigint, timestamptz, timestamptz)
  from public, anon, authenticated;
grant execute on function public.previous_search(bigint, timestamptz, timestamptz)
  to service_role;
