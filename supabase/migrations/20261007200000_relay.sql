-- Переписка от Partner Assistant (этап 028).
--
-- Устройство — techspec/28-relay.md; схема — techspec/03-schema.md §3.16;
-- доступ — techspec/04-access.md §4.1–4.2. Применяется `supabase db push` или
-- тем же текстом в SQL-редакторе панели Supabase (supabase/README.md).
-- Применённая миграция не правится: следующая правка — следующий файл.
--
-- Partner Assistant подключён к аккаунту человека в «Автоматизации чатов» и с
-- его разрешения передаёт Соломону переписку: зовёт функцию
-- `relay_chat_events` по HTTPS с публичным anon-ключом и ключом передачи в
-- теле. Ни `service_role`, ни доступа к таблицам у него нет: функция —
-- `security definer`, и `anon` может только её вызвать. Принятое ложится общим
-- путём §25 — площадка `telegram`, подключение `relay:<источник>`.

-- Ключи передачи (§28.2): источник, SHA-256 ключа и отозван ли. Сам ключ в
-- базе не хранится. Данных человека в строке нет — поэтому и владельца нет:
-- чей аккаунт, называет каждое событие (`account_telegram_id`).
create table public.chat_relays (
  id uuid primary key default gen_random_uuid(),
  -- источник: `partner` — Partner Assistant; подключение площадки —
  -- `relay:<источник>`
  name text not null,
  -- SHA-256 ключа передачи, hex
  key_hash text not null,
  -- ключ отозван: больше не принимается
  revoked_at timestamptz,
  created_at timestamptz not null default now(),
  constraint chat_relays_name_check check (name ~ '^[a-z][a-z0-9_]{0,31}$'),
  constraint chat_relays_hash_check check (key_hash ~ '^[0-9a-f]{64}$'),
  constraint chat_relays_hash_key unique (key_hash)
);

-- Действующий ключ у источника один.
create unique index chat_relays_active_key on public.chat_relays (name) where revoked_at is null;

-- RLS без политик и без прав: таблицу читает только функция приёма, пишет —
-- бот функцией `register_chat_relay` (§4.2). Ни приложение, ни anon её не
-- видят даже пустой.
alter table public.chat_relays enable row level security;
revoke all on table public.chat_relays from public, anon, authenticated;

-- Ключ из окружения бота (§28.2): бот зовёт при каждом запуске. Хэш становится
-- единственным действующим ключом источника — заводится или возвращается из
-- отозванных, — прочие ключи источника отзываются. Хэша нет (`null` — в
-- окружении бота нет ключа) — отзываются все: приём выключен. Ответ — сколько
-- ключей отозвано сейчас.
create function public.register_chat_relay(
  name text,
  key_hash text
) returns integer
language plpgsql
as $$
declare
  revoked integer;
begin
  update public.chat_relays r
     set revoked_at = now()
   where r.name = register_chat_relay.name
     and r.revoked_at is null
     and r.key_hash is distinct from register_chat_relay.key_hash;
  get diagnostics revoked = row_count;

  if register_chat_relay.key_hash is not null then
    insert into public.chat_relays as r (name, key_hash)
    values (register_chat_relay.name, register_chat_relay.key_hash)
    on conflict on constraint chat_relays_hash_key do update
       set name = excluded.name,
           revoked_at = null;
  end if;

  return revoked;
end;
$$;

-- Одно событие от источника (§28.3) — итог: `accepted`, `duplicate`,
-- `not_consented`, `unknown_account`, `invalid`. Зовёт только
-- `relay_chat_events`: ключ к этому моменту проверен, пределы вызова — тоже.
--
-- Чей аккаунт — `account_telegram_id`: принимается, только если человек
-- пользуется Соломоном (есть строка `owner_settings`, §3.8). Площадка —
-- `telegram`, подключение — `relay:<источник>`; сообщения, правки и удаления
-- принимаются, только пока площадка включена этим подключением и есть
-- согласие (§25.5) — `store_chat_message` сверяет это и сама.
--
-- `linked` включает площадку. Было другое подключение Telegram (прямое,
-- §25.2) — его место занимает источник: у аккаунта в «Автоматизации чатов»
-- один бот. Согласие тогда спрашивается заново — вопрос с припиской об
-- источнике задаст тик бота (`asked_at` пуст). Тот же источник подключён
-- снова — как новое подключение (`connect_chat_source`): согласие остаётся,
-- отказ снимается. `unlinked` выключает площадку, только если она включена
-- этим источником.
create function public.relay_chat_event(
  relay_name text,
  event jsonb
) returns text
language plpgsql
as $$
declare
  connection text := 'relay:' || relay_chat_event.relay_name;
  kind_of text;
  account bigint;
  src public.chat_sources;
  chat_key text;
  direction text;
  by_assistant boolean;
  kind text;
  message_text text;
  chat_name text;
  ids text[];
  stored text;
begin
  if jsonb_typeof(relay_chat_event.event) is distinct from 'object' then
    return 'invalid';
  end if;
  kind_of := relay_chat_event.event ->> 'type';
  if kind_of is null or kind_of not in ('linked', 'unlinked', 'message', 'edited', 'deleted') then
    return 'invalid';
  end if;
  -- Telegram-id — целое больше нуля, числом или строкой.
  if coalesce(relay_chat_event.event ->> 'account_telegram_id', '') !~ '^[1-9][0-9]{0,18}$' then
    return 'invalid';
  end if;
  account := (relay_chat_event.event ->> 'account_telegram_id')::bigint;
  if not exists (select 1 from public.owner_settings o where o.owner_telegram_id = account) then
    return 'unknown_account';
  end if;

  if kind_of = 'linked' then
    select s.* into src
      from public.chat_sources s
     where s.owner_telegram_id = account
       and s.platform = 'telegram'
       for update;
    if found and src.connection_id is distinct from connection then
      update public.chat_sources s
         set connection_id = connection,
             is_enabled = true,
             asked_at = null,
             consented_at = null,
             declined_at = null
       where s.id = src.id;
    else
      perform public.connect_chat_source(account, 'telegram', connection, true);
    end if;
    return 'accepted';
  end if;

  if kind_of = 'unlinked' then
    update public.chat_sources s
       set is_enabled = false
     where s.owner_telegram_id = account
       and s.platform = 'telegram'
       and s.connection_id = connection;
    return 'accepted';
  end if;

  -- Чат — личный чат Telegram: id больше нуля. У групп и каналов он
  -- отрицательный — их Соломон от источника не принимает (§28.4).
  if coalesce(relay_chat_event.event ->> 'chat_id', '') !~ '^[1-9][0-9]{0,18}$' then
    return 'invalid';
  end if;
  chat_key := relay_chat_event.event ->> 'chat_id';

  if kind_of = 'deleted' then
    if jsonb_typeof(relay_chat_event.event -> 'message_ids') is distinct from 'array'
       or jsonb_array_length(relay_chat_event.event -> 'message_ids') = 0 then
      return 'invalid';
    end if;
    if exists (
      select 1
        from jsonb_array_elements(relay_chat_event.event -> 'message_ids') as listed(value)
       where jsonb_typeof(listed.value) not in ('number', 'string')
          or (listed.value #>> '{}') !~ '^[1-9][0-9]{0,18}$'
    ) then
      return 'invalid';
    end if;
    select array_agg(listed.value #>> '{}') into ids
      from jsonb_array_elements(relay_chat_event.event -> 'message_ids') as listed(value);
  else
    if coalesce(relay_chat_event.event ->> 'message_id', '') !~ '^[1-9][0-9]{0,18}$' then
      return 'invalid';
    end if;
  end if;

  if kind_of = 'edited' then
    if jsonb_typeof(relay_chat_event.event -> 'text') is distinct from 'string' then
      return 'invalid';
    end if;
  end if;

  if kind_of = 'message' then
    direction := relay_chat_event.event ->> 'direction';
    if direction is null or direction not in ('in', 'out') then
      return 'invalid';
    end if;
    -- Ответ источника от имени человека — его исходящее (§28.3).
    if coalesce(jsonb_typeof(relay_chat_event.event -> 'by_assistant'), 'null') not in ('boolean', 'null') then
      return 'invalid';
    end if;
    by_assistant := coalesce((relay_chat_event.event ->> 'by_assistant')::boolean, false);
    if by_assistant and direction <> 'out' then
      return 'invalid';
    end if;
    kind := coalesce(relay_chat_event.event ->> 'kind', 'text');
    if kind not in ('text', 'voice', 'video_note', 'photo', 'other') then
      return 'invalid';
    end if;
    if coalesce(jsonb_typeof(relay_chat_event.event -> 'text'), 'null') not in ('string', 'null')
       or coalesce(jsonb_typeof(relay_chat_event.event -> 'chat_name'), 'null') not in ('string', 'null') then
      return 'invalid';
    end if;
    -- Время — по ISO с датой и часом: слова вроде `now` и `infinity`, которые
    -- Postgres тоже понимает, не годятся. Кривую дату ловит блок ниже.
    if jsonb_typeof(relay_chat_event.event -> 'sent_at') is distinct from 'string'
       or (relay_chat_event.event ->> 'sent_at') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}' then
      return 'invalid';
    end if;
    message_text := coalesce(relay_chat_event.event ->> 'text', '');
    chat_name := btrim(coalesce(relay_chat_event.event ->> 'chat_name', ''));

    select s.outcome into stored
      from public.store_chat_message(
        account,
        'telegram',
        connection,
        chat_key,
        chat_name,
        relay_chat_event.event ->> 'message_id',
        direction,
        case when direction = 'in' then chat_name else '' end,
        (relay_chat_event.event ->> 'sent_at')::timestamptz,
        kind,
        message_text,
        true
      ) as s;
    return case stored
      when 'stored' then 'accepted'
      when 'repeat' then 'duplicate'
      else 'not_consented'
    end;
  end if;

  -- Правка и удаление — только пока площадка включена источником и есть
  -- согласие; сверку подключения делают и сами функции §3.15.
  if not exists (
    select 1
      from public.chat_sources s
     where s.owner_telegram_id = account
       and s.platform = 'telegram'
       and s.connection_id = connection
       and s.is_enabled
       and s.consented_at is not null
  ) then
    return 'not_consented';
  end if;

  if kind_of = 'edited' then
    perform public.edit_chat_message(
      account, 'telegram', connection, chat_key, relay_chat_event.event ->> 'message_id',
      relay_chat_event.event ->> 'text'
    );
  else
    perform public.erase_chat_messages(account, 'telegram', connection, chat_key, ids);
  end if;
  return 'accepted';
exception
  -- Кривое значение (время, число) или запрет таблицы — событие не принято,
  -- остальные события вызова идут дальше. Всё, что событие успело записать,
  -- откатывается вместе с ним.
  when data_exception or integrity_constraint_violation then
    return 'invalid';
end;
$$;

-- Приём событий от источника (§28.2): ключ передачи, пределы вызова, итог по
-- каждому событию в их порядке. Строк не возвращает — только итоги.
--
-- Неверный или отозванный ключ — отказ без подробностей (SQLSTATE 28000,
-- PostgREST отдаёт 403). Больше 50 событий, текст длиннее 4000 знаков или имя
-- чата длиннее 200 — отказ вызова целиком (22023 → 400), ничего не пишется.
-- Источник повторяет вызов только при сбое сети и 5xx.
create function public.relay_chat_events(
  relay_key text,
  events jsonb
) returns text[]
language plpgsql
security definer
set search_path = ''
as $$
declare
  relay_name text;
  event jsonb;
  outcomes text[] := '{}';
begin
  select r.name into relay_name
    from public.chat_relays r
   where r.key_hash = encode(sha256(convert_to(coalesce(relay_chat_events.relay_key, ''), 'UTF8')), 'hex')
     and r.revoked_at is null;
  if relay_name is null then
    raise exception 'relay_chat_events: refused' using errcode = '28000';
  end if;

  if jsonb_typeof(relay_chat_events.events) is distinct from 'array' then
    raise exception 'relay_chat_events: events must be an array' using errcode = '22023';
  end if;
  if jsonb_array_length(relay_chat_events.events) > 50 then
    raise exception 'relay_chat_events: at most 50 events' using errcode = '22023';
  end if;
  if exists (
    select 1
      from jsonb_array_elements(relay_chat_events.events) as listed(value)
     where jsonb_typeof(listed.value) = 'object'
       and (char_length(listed.value ->> 'text') > 4000
            or char_length(listed.value ->> 'chat_name') > 200)
  ) then
    raise exception 'relay_chat_events: text over 4000 or chat name over 200' using errcode = '22023';
  end if;

  for event in
    select listed.value from jsonb_array_elements(relay_chat_events.events) as listed(value)
  loop
    outcomes := outcomes || public.relay_chat_event(relay_name, event);
  end loop;
  return outcomes;
end;
$$;

-- Ключ записывает только бот ключом service-role; одно событие разбирает только
-- `relay_chat_events` (её владелец зовёт его в обход прав). Приём — единственное,
-- что может вызвать `anon`: ни приложение, ни бот его не зовут. Право execute
-- выдано и роли public, из неё оно наследуется — поэтому забираем и там.
revoke execute on function public.register_chat_relay(text, text)
  from public, anon, authenticated;
grant execute on function public.register_chat_relay(text, text)
  to service_role;

revoke execute on function public.relay_chat_event(text, jsonb)
  from public, anon, authenticated, service_role;

revoke execute on function public.relay_chat_events(text, jsonb)
  from public, authenticated, service_role;
grant execute on function public.relay_chat_events(text, jsonb)
  to anon;

-- `anon` не зовёт ничего, кроме приёма (§4.1): триггерная функция миграции 001
-- осталась с правом по умолчанию. Триггер право исполнителя не проверяет —
-- оно нужно только при `create trigger`.
revoke execute on function public.set_updated_at()
  from public, anon, authenticated;
