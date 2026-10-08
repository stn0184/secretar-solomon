-- Кнопка «Открыть чат» под сообщениями о переписке (этап 030).
--
-- Схема — techspec/03-schema.md §3.12, §3.15; поведение — techspec/25-chats.md
-- §25.4. Применяется `supabase db push` или тем же текстом в SQL-редакторе
-- панели Supabase (supabase/README.md). Применённая миграция не правится:
-- следующая правка — следующий файл.
--
-- Под «Из переписки с … записал» и «Вы не ответили …» бот ставит ссылку на
-- чат с собеседником: в Telegram — `t.me/<имя>` или профиль по id, в
-- Instagram — Direct по имени. Имя пользователя можно сменить, поэтому оно
-- живёт в состоянии чата и обновляется с каждым сообщением. Здесь — колонка,
-- её приём в `store_chat_message` и ключ чата с именем в ответах
-- `chat_report` и `chats_waiting`. Правила доступа те же; функции — по-прежнему
-- только `service_role`.

-- Имя пользователя собеседника без «@» (§3.12): у Telegram и Instagram;
-- `null` — имени нет или площадка его не знает (MAX, Partner Assistant).
-- Годится только то, что безопасно вставить в ссылку.
alter table public.chat_threads
  add column username text,
  add constraint chat_threads_username_check
    check (username is null or username ~ '^[A-Za-z0-9_.]{1,64}$');

-- Приём сообщения (§3.15) — заново, с необязательным `username` в конце:
-- прежние вызовы без него (`relay_chat_event`, бот до выкладки) идут тем же
-- путём. Подпись меняется, поэтому старая перегрузка снимается.
--
-- `username`: `null` — площадка имени не знает, в чате остаётся прежнее;
-- пустое — имени у собеседника больше нет; иначе — новое, без «@». Негодное
-- для ссылки (не латиница, цифры, `_` и `.`) — как пустое: сообщение
-- сохраняется, ссылки по имени нет.
drop function public.store_chat_message(
  bigint, text, text, text, text, text, text, text, timestamptz, text, text, boolean
);

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
  tracks_waiting boolean default true,
  username text default null
) returns table (outcome text, message_id uuid)
language plpgsql
as $$
declare
  src public.chat_sources;
  chat_id uuid;
  saved uuid;
  handle text;
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

  handle := ltrim(btrim(coalesce(store_chat_message.username, '')), '@');
  if handle !~ '^[A-Za-z0-9_.]{1,64}$' then
    handle := null;
  end if;

  insert into public.chat_threads as t (
    owner_telegram_id, platform, chat_key, name, tracks_waiting, username
  )
  values (
    store_chat_message.owner_telegram_id,
    store_chat_message.platform,
    store_chat_message.chat_key,
    coalesce(btrim(store_chat_message.chat_name), ''),
    coalesce(store_chat_message.tracks_waiting, true),
    handle
  )
  on conflict on constraint chat_threads_key do update
     set name = coalesce(nullif(btrim(excluded.name), ''), t.name),
         username = case
           when store_chat_message.username is null then t.username
           else excluded.username
         end
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

-- Дела разбора для сообщения владельцу (§25.4) — заново: сверх прежнего ключ
-- чата и имя пользователя, из них бот строит «Открыть чат». Имя — нынешнее:
-- отчёт после «Убрать» ведёт туда же, куда ведёт чат сейчас.
drop function public.chat_report(bigint, uuid);

create function public.chat_report(
  owner_telegram_id bigint,
  analysis_id uuid
) returns table (
  platform text,
  chat_key text,
  chat_name text,
  username text,
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
  select t.platform, t.chat_key, t.name, t.username, a.chat_with, k.chat_item, k.id,
         k.title, k.due_at, k.due_precision, k.promise, k.status
    from public.chat_analyses a
    join public.chat_threads t on t.id = a.thread_id
    join public.tasks k
      on k.chat_analysis_id = a.id
     and k.owner_telegram_id = a.owner_telegram_id
   where a.id = chat_report.analysis_id
     and a.owner_telegram_id = chat_report.owner_telegram_id
   order by k.chat_item;
$$;

-- Кому не ответили (§25.4) — заново: сверх прежнего ключ чата и имя
-- пользователя для «Открыть чат». Условия те же.
drop function public.chats_waiting(bigint, timestamptz);

create function public.chats_waiting(
  owner_telegram_id bigint,
  asked_before timestamptz
) returns table (
  thread_id uuid,
  platform text,
  chat_key text,
  name text,
  username text,
  waiting_since timestamptz,
  waiting_about text,
  waiting_to text
)
language sql
stable
as $$
  select t.id, t.platform, t.chat_key, t.name, t.username, t.waiting_since,
         t.waiting_about, t.waiting_to
    from public.chat_threads t
   where t.owner_telegram_id = chats_waiting.owner_telegram_id
     and t.tracks_waiting
     and t.waiting_since is not null
     and t.waiting_since <= chats_waiting.asked_before
     and t.waiting_reminded_at is null
     and (t.last_out_at is null or t.last_out_at < t.waiting_since)
   order by t.waiting_since, t.id;
$$;

-- Пересозданные функции — снова только боту: новое право execute выдано и
-- роли public, из неё оно наследуется.
revoke execute on function
  public.store_chat_message(bigint, text, text, text, text, text, text, text, timestamptz, text, text, boolean, text)
  from public, anon, authenticated;
grant execute on function
  public.store_chat_message(bigint, text, text, text, text, text, text, text, timestamptz, text, text, boolean, text)
  to service_role;

revoke execute on function public.chat_report(bigint, uuid)
  from public, anon, authenticated;
grant execute on function public.chat_report(bigint, uuid)
  to service_role;

revoke execute on function public.chats_waiting(bigint, timestamptz)
  from public, anon, authenticated;
grant execute on function public.chats_waiting(bigint, timestamptz)
  to service_role;
