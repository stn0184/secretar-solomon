-- Память о пользователе: что помощник знает о человеке помимо задач.
--
-- Схема — techspec/03-schema.md §3.7, поведение — §8. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.

-- Одна строка — одно обстоятельство одной фразой («Машина — Toyota Camry»).
-- Статус ставит бот по виду сообщения (§8.2): сказано прямо — `fact`,
-- выведено из поручения — `guess`; человек подтверждает или удаляет в
-- приложении. Удалённая запись удаляется, а не прячется (§8.1).
create table public.facts (
  id uuid primary key default gen_random_uuid(),
  owner_telegram_id bigint not null,
  -- семь категорий для группировки на экране; модель своих не выдумывает
  category text not null check (
    category in ('family', 'home', 'car', 'work', 'habit', 'preference', 'other')
  ),
  text text not null,
  status text not null check (status in ('fact', 'guess')),
  -- откуда взялось; сообщение — след, а не часть записи: его удаление
  -- память не стирает
  source_message_id uuid references public.messages (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  -- модель, переспросившая то же самое, второй строки не заводит (§8.3)
  unique (owner_telegram_id, category, text)
);

-- Под запрос промпта: известные факты владельца (§8.2, до 50 строк).
create index facts_owner_status_idx
  on public.facts (owner_telegram_id, status);

-- `updated_at` ставит база, как у `tasks` (миграция 001).
create trigger facts_set_updated_at
  before update on public.facts
  for each row execute function public.set_updated_at();

-- Правила доступа (§4.2). Таблица с данными человека без RLS в public —
-- баг уровня безопасности (инвариант 2), а не недоделка. Одна политика на
-- все операции: приложение читает, подтверждает (`update status`) и
-- удаляет под ней, функций для него не нужно (§8.4).
alter table public.facts enable row level security;

-- Для anon политик нет намеренно: включённый RLS без политики — отказ.
create policy "facts: owner only"
  on public.facts
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);

-- Записи памяти пишутся в той же транзакции, что разбор, задача и
-- напоминания (§3.4): иначе отказ между вставками оставил бы разбор без
-- памяти или память без разбора. Поэтому у `record_understanding` десятый
-- аргумент, а девятиаргументная версия уходит: две перегрузки PostgREST
-- различать нечем.
drop function public.record_understanding(uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb);

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
  facts jsonb
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

  -- Память пишется для любого вида сообщения — и для тех, где задачи нет
  -- (`about_me`, `chat`), поэтому раньше ранних выходов. Список
  -- `{category, text, status}` собирает бот, статус уже проставлен (§8.2).
  -- На совпадении по (владелец, категория, текст) статус только растёт
  -- (§8.3): лежит `guess`, пришёл `fact` — становится `fact`, и источником
  -- становится сообщение, где человек сказал это прямо; `fact` в `guess`
  -- не опускается никогда. Повтор того же сообщения по `unique` безвреден.
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

-- Зовёт только бот ключом service-role. Одного `revoke ... from anon,
-- authenticated` мало: право execute выдано роли public, и из неё оно
-- наследуется, поэтому забираем и там.
revoke execute on function
  public.record_understanding(uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb)
  from public, anon, authenticated;
grant execute on function
  public.record_understanding(uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb)
  to service_role;

-- Подтверждение и удаление записи из Mini App — обычные `update facts set
-- status = 'fact'` и `delete from facts` под политикой «facts: owner only»:
-- она объявлена `for all`, обе операции в неё входят. Исходное сообщение
-- читается из `messages` под её политикой из миграции 001 — новых не нужно.
