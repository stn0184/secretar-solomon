-- Разбор поручения: поля задачи и след разбора у сообщения.
--
-- Схема — techspec/03-schema.md §3.2–3.4, контракт с моделью — §5.
-- Применяется `supabase db push` или тем же текстом в SQL-редакторе панели
-- Supabase (supabase/README.md). Применённая миграция не правится: следующая
-- правка — следующий файл.

-- Что модель поняла о задаче (techspec/05-ai.md §5.3). Значения по умолчанию
-- стоят там, где пустое поле означало бы не «неизвестно», а недописанный код:
-- вид, срочность и признак «перепроверить» есть у каждой задачи.
alter table public.tasks
  add column kind text not null default 'task' check (kind in ('task', 'idea', 'wish')),
  -- при due_precision = 'day' здесь 18:00 того дня в поясе владельца
  add column due_at timestamptz,
  add column due_precision text check (due_precision in ('day', 'time')),
  add column priority text not null default 'normal' check (priority in ('low', 'normal', 'high')),
  add column promise text check (promise in ('mine', 'to_me')),
  add column people text[] not null default '{}',
  add column needs_review boolean not null default false;

-- След разбора у сообщения: и журнал (spec.md §3.6), и мера стоимости одного
-- пользователя (spec.md §5). Всё nullable: заполняется вторым шагом, а у
-- неразобранного сообщения этих значений просто нет.
alter table public.messages
  add column analysis jsonb,
  add column ai_model text,
  add column ai_input_tokens int,
  add column ai_output_tokens int,
  add column reply text;

-- Приём сообщения разделился на два шага (§3.4), поэтому прежняя функция
-- этапа 002 уходит: сообщение теперь сохраняется до модели, а не вместе с
-- задачей.
drop function public.record_task(bigint, bigint, bigint, text);

-- Шаг первый: сообщение в базе с первой секунды, до всякого разбора
-- (инвариант 5). Повтор обновления новых строк не пишет и возвращает
-- прежнюю — по её `reply` бот видит, что ответ уже давался.
create function public.record_message(
  owner_telegram_id bigint,
  chat_id bigint,
  telegram_message_id bigint,
  text text
) returns public.messages
language sql
as $$
  with new_message as (
    insert into public.messages (owner_telegram_id, chat_id, telegram_message_id, kind, text)
    values (
      record_message.owner_telegram_id,
      record_message.chat_id,
      record_message.telegram_message_id,
      'text',
      record_message.text
    )
    on conflict (owner_telegram_id, chat_id, telegram_message_id) do nothing
    returning *
  )
  select * from new_message
  union all
  select m.*
  from public.messages m
  where not exists (select 1 from new_message)
    and m.owner_telegram_id = record_message.owner_telegram_id
    and m.chat_id = record_message.chat_id
    and m.telegram_message_id = record_message.telegram_message_id
  limit 1;
$$;

-- Шаг второй: разбор, ответ бота и задача — одной транзакцией. Задача для
-- этого сообщения уже есть — возвращается она, второй не заводится. `task`
-- пуст для разговора и сведения о себе; тогда функция возвращает null.
create function public.record_understanding(
  message_id uuid,
  owner_telegram_id bigint,
  analysis jsonb,
  ai_model text,
  ai_input_tokens int,
  ai_output_tokens int,
  reply text,
  task jsonb
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
         reply = record_understanding.reply
   where m.id = record_understanding.message_id
     and m.owner_telegram_id = record_understanding.owner_telegram_id;

  if not found then
    raise exception 'record_understanding: message % is not owned by %',
      record_understanding.message_id, record_understanding.owner_telegram_id;
  end if;

  select t.* into saved
    from public.tasks t
   where t.source_message_id = record_understanding.message_id
   limit 1;
  if found then
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

  return saved;
end;
$$;

-- Обе функции зовёт только бот ключом service-role. Одного `revoke ... from
-- anon, authenticated` мало: право execute выдано роли public, и из неё оно
-- наследуется, поэтому забираем и там.
revoke execute on function public.record_message(bigint, bigint, bigint, text)
  from public, anon, authenticated;
grant execute on function public.record_message(bigint, bigint, bigint, text)
  to service_role;

revoke execute on function public.record_understanding(uuid, bigint, jsonb, text, int, int, text, jsonb)
  from public, anon, authenticated;
grant execute on function public.record_understanding(uuid, bigint, jsonb, text, int, int, text, jsonb)
  to service_role;
