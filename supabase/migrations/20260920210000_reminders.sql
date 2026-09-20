-- Напоминания: когда и о чём стучаться.
--
-- Схема — techspec/03-schema.md §3.5, поведение — §6. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.

-- Напоминания живут в базе, а не в памяти бота: перезапуск и выключенный
-- компьютер их не теряют (spec.md §3.4, инвариант 5). Рождаются в той же
-- транзакции, что задача, — их считает бот (§6.1), база только хранит.
create table public.reminders (
  id uuid primary key default gen_random_uuid(),
  owner_telegram_id bigint not null,
  task_id uuid not null references public.tasks (id) on delete cascade,
  -- заранее или к сроку (§6.1)
  stage text not null check (stage in ('before', 'due')),
  fire_at timestamptz not null,
  -- пусто — ещё ждёт своего часа
  sent_at timestamptz,
  -- сообщение в Telegram, под которым кнопка «Сделано»; пусто, пока не ушло
  telegram_message_id bigint,
  created_at timestamptz not null default now(),
  -- одна ступень на задачу: повторная запись второй строки не заводит
  unique (task_id, stage)
);

-- Под запрос тика: что у владельца созрело и ещё не ушло. Частичный индекс,
-- потому что отправленные напоминания в этом запросе не участвуют никогда.
create index reminders_owner_pending_idx
  on public.reminders (owner_telegram_id, fire_at)
  where sent_at is null;

-- Правила доступа (§4.2). Таблица с данными человека без RLS в public —
-- баг уровня безопасности (инвариант 2), а не недоделка.
alter table public.reminders enable row level security;

-- Для anon политик нет намеренно: включённый RLS без политики — отказ.
create policy "reminders: owner only"
  on public.reminders
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);

-- Напоминания заводятся вместе с задачей, одной транзакцией (§3.5): иначе
-- возможен отказ между вставками — задача есть, а стучаться о ней нечем.
-- Поэтому у `record_understanding` появляется девятый аргумент, а прежняя
-- восьмиаргументная версия уходит: две перегрузки PostgREST различать нечем.
drop function public.record_understanding(uuid, bigint, jsonb, text, int, int, text, jsonb);

create function public.record_understanding(
  message_id uuid,
  owner_telegram_id bigint,
  analysis jsonb,
  ai_model text,
  ai_input_tokens int,
  ai_output_tokens int,
  reply text,
  task jsonb,
  reminders jsonb
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

-- Запрос тика (§6.2): что созрело у владельца. Только активные задачи —
-- о закрытой стучаться незачем, даже если строка осталась.
create function public.due_reminders(
  owner_telegram_id bigint,
  now timestamptz
) returns table (
  id uuid,
  task_id uuid,
  stage text,
  fire_at timestamptz,
  title text,
  due_at timestamptz,
  due_precision text
)
language sql
as $$
  select r.id, r.task_id, r.stage, r.fire_at, t.title, t.due_at, t.due_precision
    from public.reminders r
    join public.tasks t on t.id = r.task_id
   where r.owner_telegram_id = due_reminders.owner_telegram_id
     and r.sent_at is null
     and r.fire_at <= due_reminders.now
     and t.status = 'active'
   order by r.fire_at;
$$;

-- Помечается после отправки (§6.2): порядок «отправить → пометить», поэтому
-- строка гасится только когда сообщение уже в Telegram. Уже помеченные не
-- трогаются — их `sent_at` остаётся временем первой отправки.
create function public.mark_reminders_sent(
  owner_telegram_id bigint,
  ids uuid[],
  telegram_message_id bigint
) returns void
language sql
as $$
  update public.reminders r
     set sent_at = now(),
         telegram_message_id = mark_reminders_sent.telegram_message_id
   where r.owner_telegram_id = mark_reminders_sent.owner_telegram_id
     and r.id = any(mark_reminders_sent.ids)
     and r.sent_at is null;
$$;

-- Кнопка «Сделано» (§6.3): закрыть задачу и снять её неотправленные
-- напоминания — одной транзакцией, иначе закрытая задача может ещё раз
-- постучаться. Повторное нажатие безвредно: задача возвращается как есть.
create function public.mark_task_done(
  owner_telegram_id bigint,
  task_id uuid
) returns public.tasks
language plpgsql
as $$
declare
  saved public.tasks;
begin
  select t.* into saved
    from public.tasks t
   where t.id = mark_task_done.task_id
     and t.owner_telegram_id = mark_task_done.owner_telegram_id;

  -- Чужая или несуществующая задача: ничего не меняем и ничего не отдаём.
  if not found then
    return null;
  end if;

  delete from public.reminders r
   where r.task_id = mark_task_done.task_id
     and r.owner_telegram_id = mark_task_done.owner_telegram_id
     and r.sent_at is null;

  if saved.status = 'done' then
    return saved;
  end if;

  update public.tasks t
     set status = 'done'
   where t.id = mark_task_done.task_id
     and t.owner_telegram_id = mark_task_done.owner_telegram_id
   returning t.* into saved;

  return saved;
end;
$$;

-- Все четыре зовёт только бот ключом service-role. Одного `revoke ... from
-- anon, authenticated` мало: право execute выдано роли public, и из неё оно
-- наследуется, поэтому забираем и там.
revoke execute on function
  public.record_understanding(uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function
  public.record_understanding(uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb)
  to service_role;

revoke execute on function public.due_reminders(bigint, timestamptz)
  from public, anon, authenticated;
grant execute on function public.due_reminders(bigint, timestamptz)
  to service_role;

revoke execute on function public.mark_reminders_sent(bigint, uuid[], bigint)
  from public, anon, authenticated;
grant execute on function public.mark_reminders_sent(bigint, uuid[], bigint)
  to service_role;

revoke execute on function public.mark_task_done(bigint, uuid)
  from public, anon, authenticated;
grant execute on function public.mark_task_done(bigint, uuid)
  to service_role;
