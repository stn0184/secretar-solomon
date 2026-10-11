-- Разобрать позже, если модель недоступна (этап 037).
--
-- Схема — techspec/03-schema.md §3.2, §3.12, §3.15; поведение —
-- techspec/33-retry-later.md. Применяется `supabase db push` или тем же текстом
-- в SQL-редакторе панели Supabase (supabase/README.md). Применённая миграция не
-- правится: следующая правка — следующий файл.
--
-- 2026-10-11 посредник Claude пять часов отвечал 503: поручения легли
-- «как есть», куски переписки — без дел. Теперь временный отказ модели не
-- запись «как есть», а ожидание: сообщение владельца помнит, что его надо
-- разобрать позже, сколько было попыток и когда следующая; чат — сколько
-- временных неудач подряд, с какого времени и когда пробовать снова.
--
-- Разбор отложенного сообщения пишется прежним `record_understanding` — его
-- проверка повтора по сообщению (§3.4) не даёт второй задачи и второго ответа.
-- Поэтому «⏳ Принял…» в `reply` не пишется: заполненный `reply` — это повтор.

-- --- Отложенные сообщения владельца ------------------------------------------

-- Сообщение ждёт, пока `retry_at` не пуст: и до разбора, и когда ответ уже
-- записан, но ещё не ушёл владельцу. `retry_attempts` — сколько раз модель
-- звали (первый заход тоже), `retry_started_at` — когда взята текущая
-- попытка, `retry_input` — что нужно разбору сверх строки: свайп, вид снимка,
-- текст переписки (§33.2).
alter table public.messages
  add column retry_at timestamptz,
  add column retry_attempts smallint not null default 0,
  add column retry_started_at timestamptz,
  add column retry_input jsonb,
  add constraint messages_retry_attempts_check check (retry_attempts >= 0),
  add constraint messages_retry_check check (
    retry_at is not null or (retry_started_at is null and retry_input is null)
  );

-- Очередь владельца — по порядку получения.
create index messages_retry_idx
  on public.messages (owner_telegram_id, received_at, id)
  where retry_at is not null;

-- Отложить сообщение (§33.2, шаг 1): модель временно недоступна или очередь
-- не пуста. Ответ у сообщения уже есть — откладывать нечего, `false`. Уже
-- ждёт — повтор того же обновления: ничего не меняется, `true`. Расшифровка
-- голоса ложится текстом сообщения — разбор позже пойдёт по ней (§9.3).
--
-- Новое сообщение — повод попробовать раньше: очередь не ждёт дольше него, и
-- после сбоя первое же сообщение будит её, а не ждёт получасовой паузы.
create function public.defer_message(
  owner_telegram_id bigint,
  message_id uuid,
  retry_at timestamptz,
  attempts smallint,
  retry_input jsonb default null,
  transcript text default null,
  transcript_confidence numeric default null
) returns boolean
language plpgsql
as $$
declare
  pressed public.messages;
begin
  if defer_message.retry_at is null then
    raise exception 'defer_message: retry_at is required';
  end if;

  select m.* into pressed
    from public.messages m
   where m.id = defer_message.message_id
     and m.owner_telegram_id = defer_message.owner_telegram_id
     for update;

  if not found then
    raise exception 'defer_message: message % is not owned by %',
      defer_message.message_id, defer_message.owner_telegram_id;
  end if;

  if pressed.reply is not null then
    return false;
  end if;
  if pressed.retry_at is not null then
    return true;
  end if;

  update public.messages m
     set retry_at = defer_message.retry_at,
         retry_attempts = greatest(coalesce(defer_message.attempts, 0), 0),
         retry_started_at = null,
         retry_input = defer_message.retry_input,
         text = coalesce(defer_message.transcript, m.text),
         transcript_confidence = coalesce(
           defer_message.transcript_confidence, m.transcript_confidence
         )
   where m.id = pressed.id;

  update public.messages m
     set retry_at = defer_message.retry_at
   where m.owner_telegram_id = defer_message.owner_telegram_id
     and m.id <> pressed.id
     and m.reply is null
     and m.retry_at > defer_message.retry_at;

  return true;
end;
$$;

-- Ждёт ли другое сообщение владельца (§33.2, шаг 5): пока очередь не пуста,
-- новое встаёт в неё, а не зовёт модель мимо очереди.
create function public.deferred_waiting(
  owner_telegram_id bigint,
  message_id uuid
) returns boolean
language sql
stable
as $$
  select exists (
    select 1
      from public.messages m
     where m.owner_telegram_id = deferred_waiting.owner_telegram_id
       and m.retry_at is not null
       and m.id <> deferred_waiting.message_id
  );
$$;

-- Взять голову очереди (§33.2): самое раннее ждущее сообщение владельца —
-- атомарно, одной правкой строки. Голову держит другой заход, начатый не
-- раньше `stale_before`, — пусто: очередь ждёт её, порядок не нарушается.
-- Начатую раньше считают брошенной перезапуском и берут снова. Записанный и не
-- ушедший ответ берётся сразу, разбор — когда `retry_at` не позже
-- `due_before`; попытка разбора прибавляет счёт.
create function public.take_deferred(
  owner_telegram_id bigint,
  due_before timestamptz,
  stale_before timestamptz
) returns setof public.messages
language plpgsql
as $$
declare
  head public.messages;
begin
  select m.* into head
    from public.messages m
   where m.owner_telegram_id = take_deferred.owner_telegram_id
     and m.retry_at is not null
   order by m.received_at, m.id
   limit 1
     for update;

  if not found then
    return;
  end if;
  if head.retry_started_at is not null
     and head.retry_started_at >= take_deferred.stale_before then
    return;
  end if;
  if head.reply is null and head.retry_at > take_deferred.due_before then
    return;
  end if;

  return query
    update public.messages m
       set retry_started_at = now(),
           retry_attempts = m.retry_attempts + case when m.reply is null then 1 else 0 end
     where m.id = head.id
    returning m.*;
end;
$$;

-- Попытка не удалась или ответ не ушёл — вернуть сообщение в очередь со
-- следующим временем. Не ждёт или чужое — `false`.
create function public.release_deferred(
  owner_telegram_id bigint,
  message_id uuid,
  retry_at timestamptz
) returns boolean
language sql
as $$
  with released as (
    update public.messages m
       set retry_started_at = null,
           retry_at = release_deferred.retry_at
     where m.id = release_deferred.message_id
       and m.owner_telegram_id = release_deferred.owner_telegram_id
       and m.retry_at is not null
       and release_deferred.retry_at is not null
    returning 1
  )
  select exists (select 1 from released);
$$;

-- Ответ ушёл владельцу — сообщение больше не ждёт (порядок «записать →
-- отправить → пометить», как у поисков §24.3). Счёт попыток остаётся следом.
-- Не ждёт или чужое — `false`.
create function public.finish_deferred(
  owner_telegram_id bigint,
  message_id uuid
) returns boolean
language sql
as $$
  with finished as (
    update public.messages m
       set retry_at = null,
           retry_started_at = null,
           retry_input = null
     where m.id = finish_deferred.message_id
       and m.owner_telegram_id = finish_deferred.owner_telegram_id
       and m.retry_at is not null
    returning 1
  )
  select exists (select 1 from finished);
$$;

-- --- Переписка из чатов (§33.3) ------------------------------------------------

-- Временные неудачи разбора куска считаются отдельно от `failures`: три
-- неудачи подряд — только постоянные. `retry_since` — первая временная неудача
-- текущего куска, от неё считаются сутки; `retry_at` — раньше него чат к
-- разбору не идёт.
alter table public.chat_threads
  add column retry_failures smallint not null default 0,
  add column retry_since timestamptz,
  add column retry_at timestamptz,
  add constraint chat_threads_retry_check check (
    retry_failures >= 0
    and (retry_failures = 0) = (retry_since is null)
    and (retry_at is null or retry_since is not null)
  );

-- Временная неудача разбора (§33.3): счёт плюс один, начало — при первой,
-- следующая попытка — не раньше `retry_at`. Возвращает счёт; чужой чат — `null`.
create function public.chat_unavailable(
  owner_telegram_id bigint,
  thread_id uuid,
  retry_at timestamptz
) returns integer
language sql
as $$
  update public.chat_threads t
     set retry_failures = t.retry_failures + 1,
         retry_since = coalesce(t.retry_since, now()),
         retry_at = chat_unavailable.retry_at
   where t.id = chat_unavailable.thread_id
     and t.owner_telegram_id = chat_unavailable.owner_telegram_id
  returning t.retry_failures::integer;
$$;

-- Любой разбор куска — с делами или пропуск — закрывает его ожидание: счёт
-- временных неудач обнуляется, и следующий кусок считает сутки заново.
-- Триггер, а не правка `record_chat_analysis` и `skip_chat_messages`: обе
-- заводят строку разбора, и правило одно на обе.
create function public.chat_analysis_clears_retry() returns trigger
language plpgsql
as $$
begin
  update public.chat_threads t
     set retry_failures = 0,
         retry_since = null,
         retry_at = null
   where t.id = new.thread_id
     and (t.retry_failures > 0 or t.retry_at is not null);
  return new;
end;
$$;

create trigger chat_analyses_clear_retry
  after insert on public.chat_analyses
  for each row execute function public.chat_analysis_clears_retry();

-- Триггер право исполнителя не проверяет — оно нужно только при `create
-- trigger`: функцию никто не зовёт напрямую.
revoke execute on function public.chat_analysis_clears_retry()
  from public, anon, authenticated;

-- Чаты к разбору (§25.3) — прежнее правило, кроме тех, чья следующая попытка
-- после временной неудачи ещё не пришла. Отдаёт и счёт временных неудач с их
-- началом: по ним бот считает паузу и сутки. Тип ответа другой — функция
-- пересоздаётся.
drop function public.chats_to_analyze(bigint, timestamptz, timestamptz);

create function public.chats_to_analyze(
  owner_telegram_id bigint,
  quiet_before timestamptz,
  stale_before timestamptz
) returns table (
  thread_id uuid,
  platform text,
  chat_key text,
  name text,
  retry_failures integer,
  retry_since timestamptz
)
language sql
stable
as $$
  select t.id, t.platform, t.chat_key, t.name, t.retry_failures::integer, t.retry_since
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
     and (t.retry_at is null or t.retry_at <= now())
   order by pending.first_at, t.id;
$$;

-- --- Права ---------------------------------------------------------------------

-- Функции зовёт только бот ключом service-role (§4.3).
revoke execute on function
  public.defer_message(bigint, uuid, timestamptz, smallint, jsonb, text, numeric)
  from public, anon, authenticated;
grant execute on function
  public.defer_message(bigint, uuid, timestamptz, smallint, jsonb, text, numeric)
  to service_role;

revoke execute on function public.deferred_waiting(bigint, uuid)
  from public, anon, authenticated;
grant execute on function public.deferred_waiting(bigint, uuid)
  to service_role;

revoke execute on function public.take_deferred(bigint, timestamptz, timestamptz)
  from public, anon, authenticated;
grant execute on function public.take_deferred(bigint, timestamptz, timestamptz)
  to service_role;

revoke execute on function public.release_deferred(bigint, uuid, timestamptz)
  from public, anon, authenticated;
grant execute on function public.release_deferred(bigint, uuid, timestamptz)
  to service_role;

revoke execute on function public.finish_deferred(bigint, uuid)
  from public, anon, authenticated;
grant execute on function public.finish_deferred(bigint, uuid)
  to service_role;

revoke execute on function public.chat_unavailable(bigint, uuid, timestamptz)
  from public, anon, authenticated;
grant execute on function public.chat_unavailable(bigint, uuid, timestamptz)
  to service_role;

revoke execute on function public.chats_to_analyze(bigint, timestamptz, timestamptz)
  from public, anon, authenticated;
grant execute on function public.chats_to_analyze(bigint, timestamptz, timestamptz)
  to service_role;
