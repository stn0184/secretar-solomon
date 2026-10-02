-- Разговор: от кого переслано сообщение.
--
-- Схема — techspec/03-schema.md §3.2, §3.4; поведение — §17.5. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.
--
-- Новых таблиц и индексов нет, правила доступа не меняются: на колонку
-- действует прежняя политика `messages` (§4.2).

-- Имя того, от кого переслано сообщение, — то же, что в строке «Переслано
-- от» промпта. Нужно недавнему разговору (блок 6, §17.3): без него чужие
-- слова выглядели бы словами владельца. У своего сообщения пусто; у старых
-- строк тоже — были ли они пересланы, уже не восстановить.
alter table public.messages
  add column forwarded_from text;

-- Восьмой аргумент — отправитель. Две перегрузки с умолчаниями PostgREST
-- различать нечем, поэтому семиаргументная уходит, а новая принимает и
-- прежние вызовы.
drop function public.record_message(bigint, bigint, bigint, text, text, text, int);

create function public.record_message(
  owner_telegram_id bigint,
  chat_id bigint,
  telegram_message_id bigint,
  text text,
  kind text default 'text',
  telegram_file_id text default null,
  duration_seconds int default null,
  forwarded_from text default null
) returns public.messages
language sql
as $$
  with new_message as (
    insert into public.messages (
      owner_telegram_id, chat_id, telegram_message_id, kind, text,
      telegram_file_id, duration_seconds, forwarded_from
    )
    values (
      record_message.owner_telegram_id,
      record_message.chat_id,
      record_message.telegram_message_id,
      coalesce(record_message.kind, 'text'),
      record_message.text,
      record_message.telegram_file_id,
      record_message.duration_seconds,
      record_message.forwarded_from
    )
    on conflict (owner_telegram_id, chat_id, telegram_message_id) do nothing
    returning *
  )
  select * from new_message
  union all
  -- Повтор: сообщение уже было, новых строк нет — отдаём прежнее как есть,
  -- с прежним отправителем и ответом, который бот уже давал.
  select m.*
  from public.messages m
  where not exists (select 1 from new_message)
    and m.owner_telegram_id = record_message.owner_telegram_id
    and m.chat_id = record_message.chat_id
    and m.telegram_message_id = record_message.telegram_message_id
  limit 1;
$$;

-- Зовёт только бот ключом service-role. Право execute выдано и роли
-- public, из неё оно наследуется — поэтому забираем и там.
revoke execute on function
  public.record_message(bigint, bigint, bigint, text, text, text, int, text)
  from public, anon, authenticated;
grant execute on function
  public.record_message(bigint, bigint, bigint, text, text, text, int, text)
  to service_role;
