-- Первые таблицы с данными человека: входящие сообщения и задачи.
--
-- Схема — techspec/03-schema.md §3.2–3.4, правила доступа — §4.2.
-- Применяется `supabase db push` или тем же текстом в SQL-редакторе панели
-- Supabase (supabase/README.md). Применённая миграция не правится: следующая
-- правка — следующий файл.

-- Входящее сообщение хранится целиком: из него сделан разбор, и к нему
-- возвращаются, когда разбор оказался неверным (spec.md §3.1).
create table public.messages (
  id uuid primary key default gen_random_uuid(),
  owner_telegram_id bigint not null,
  chat_id bigint not null,
  telegram_message_id bigint not null,
  kind text not null default 'text' check (kind in ('text')),
  text text not null,
  received_at timestamptz not null default now(),
  -- long polling может отдать обновление повторно: второй раз сообщение
  -- не заводится, а `record_task` возвращает уже заведённую задачу.
  unique (owner_telegram_id, chat_id, telegram_message_id)
);

create table public.tasks (
  id uuid primary key default gen_random_uuid(),
  owner_telegram_id bigint not null,
  title text not null,
  status text not null default 'active' check (status in ('active', 'done')),
  -- пусто у задач, заведённых не из сообщения
  source_message_id uuid references public.messages (id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Под главный запрос Mini App: активные задачи владельца.
create index tasks_owner_status_idx on public.tasks (owner_telegram_id, status);

-- `updated_at` не доверяется вызывающему: его ставит база при любой правке.
create function public.set_updated_at() returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger tasks_set_updated_at
  before update on public.tasks
  for each row execute function public.set_updated_at();

-- Приём сообщения одной транзакцией (§3.4): либо есть обе строки, либо ни
-- одной — между двумя отдельными вставками возможен отказ, и тогда сообщение
-- есть, а задачи нет, то есть поручение потеряно (инвариант 5).
--
-- Сообщение уже заведено — новых строк не пишется, возвращается задача,
-- привязанная к нему.
create function public.record_task(
  owner_telegram_id bigint,
  chat_id bigint,
  telegram_message_id bigint,
  text text
) returns public.tasks
language sql
as $$
  with new_message as (
    insert into public.messages (owner_telegram_id, chat_id, telegram_message_id, kind, text)
    values (
      record_task.owner_telegram_id,
      record_task.chat_id,
      record_task.telegram_message_id,
      'text',
      record_task.text
    )
    on conflict (owner_telegram_id, chat_id, telegram_message_id) do nothing
    returning id
  ),
  new_task as (
    insert into public.tasks (owner_telegram_id, title, source_message_id)
    select record_task.owner_telegram_id, record_task.text, new_message.id
    from new_message
    returning *
  )
  select * from new_task
  union all
  -- Повтор: сообщение уже было, новых строк нет — отдаём прежнюю задачу.
  select t.*
  from public.tasks t
  join public.messages m on m.id = t.source_message_id
  where not exists (select 1 from new_message)
    and m.owner_telegram_id = record_task.owner_telegram_id
    and m.chat_id = record_task.chat_id
    and m.telegram_message_id = record_task.telegram_message_id
  order by created_at
  limit 1;
$$;

-- Функцию зовёт только бот ключом service-role. Одного `revoke ... from anon,
-- authenticated` мало: право execute выдано роли public, а из неё оно
-- и наследуется, поэтому забираем и там.
revoke execute on function public.record_task(bigint, bigint, bigint, text)
  from public, anon, authenticated;
grant execute on function public.record_task(bigint, bigint, bigint, text)
  to service_role;

-- Правила доступа (§4.2). Таблица с данными человека без RLS в public —
-- баг уровня безопасности (инвариант 2), а не недоделка.
alter table public.messages enable row level security;
alter table public.tasks enable row level security;

-- Для anon политик нет намеренно: включённый RLS без политики — отказ.
create policy "messages: owner only"
  on public.messages
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);

create policy "tasks: owner only"
  on public.tasks
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);
