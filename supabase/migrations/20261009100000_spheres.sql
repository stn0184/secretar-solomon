-- Сферы жизни владельца (этап 032).
--
-- Схема — techspec/03-schema.md §3.3, §3.7, §3.12, §3.17; поведение —
-- techspec/30-spheres.md. Применяется `supabase db push` или тем же текстом в
-- SQL-редакторе панели Supabase (supabase/README.md). Применённая миграция не
-- правится: следующая правка — следующий файл.
--
-- Сфера — название, которое дал владелец; у дела, записи памяти и личного
-- чата — не больше одной. Здесь: таблица `spheres`, колонка `sphere_id` у
-- `tasks`, `facts` и `chat_threads`, поиск и заведение сферы по названию,
-- сфера в записи разбора, в правке словом, в разборе чатов и в отчёте о
-- переписке. Правила доступа — §4.2: таблица с `owner_telegram_id` под той же
-- политикой; функции — только `service_role`.

-- --- Таблица -------------------------------------------------------------------

-- «Убери сферу» строку не удаляет, а ставит `removed_at` (§30.4): повторы
-- названий запрещены только среди живых, поэтому та же сфера заводится заново
-- новой строкой, а убранная остаётся следом.
create table public.spheres (
  id uuid primary key default gen_random_uuid(),
  owner_telegram_id bigint not null,
  name text not null,
  created_at timestamptz not null default now(),
  removed_at timestamptz,
  constraint spheres_name_check check (char_length(name) between 1 and 40 and name = btrim(name)),
  -- Под составной внешний ключ: ссылка на сферу — только своего владельца.
  constraint spheres_owner_id_key unique (owner_telegram_id, id)
);

create unique index spheres_name_key
  on public.spheres (owner_telegram_id, lower(name))
  where removed_at is null;

alter table public.spheres enable row level security;

create policy "spheres: owner only"
  on public.spheres
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);

-- Сфера дела, записи памяти и чата. Ключ составной: даже прямая правка под RLS
-- не сошлётся на сферу другого владельца (инвариант 2). Строку сферы бот не
-- удаляет; удалённая всё же — ссылки обнуляются, сами строки остаются.
alter table public.tasks
  add column sphere_id uuid,
  add constraint tasks_sphere_fkey foreign key (owner_telegram_id, sphere_id)
    references public.spheres (owner_telegram_id, id) on delete set null (sphere_id);

alter table public.facts
  add column sphere_id uuid,
  add constraint facts_sphere_fkey foreign key (owner_telegram_id, sphere_id)
    references public.spheres (owner_telegram_id, id) on delete set null (sphere_id);

alter table public.chat_threads
  add column sphere_id uuid,
  add constraint chat_threads_sphere_fkey foreign key (owner_telegram_id, sphere_id)
    references public.spheres (owner_telegram_id, id) on delete set null (sphere_id);

-- Ответ свайпом на отчёт о переписке (§30.2) ищет разбор по сообщению бота.
create index chat_analyses_report_idx
  on public.chat_analyses (owner_telegram_id, report_message_id)
  where report_message_id is not null;

-- --- Сфера по названию -----------------------------------------------------------

-- Живая сфера владельца по названию без учёта регистра. Нет — `null`, а с
-- `create_missing` — заводится (владелец назвал её сам, §30.2). Живых сфер не
-- больше 12 (§30.1): тринадцатая — отказ, бот проверяет предел раньше и
-- отвечает словами. Пустое название — `null`.
create function public.sphere_id_of(
  owner_telegram_id bigint,
  sphere_name text,
  create_missing boolean default false
) returns uuid
language plpgsql
as $$
declare
  wanted text := btrim(left(btrim(coalesce(sphere_id_of.sphere_name, '')), 40));
  found_id uuid;
  alive integer;
begin
  if wanted = '' then
    return null;
  end if;

  select s.id into found_id
    from public.spheres s
   where s.owner_telegram_id = sphere_id_of.owner_telegram_id
     and s.removed_at is null
     and lower(s.name) = lower(wanted)
   limit 1;

  if found_id is not null or not sphere_id_of.create_missing then
    return found_id;
  end if;

  select count(*)::integer into alive
    from public.spheres s
   where s.owner_telegram_id = sphere_id_of.owner_telegram_id
     and s.removed_at is null;
  if alive >= 12 then
    raise exception 'sphere_id_of: owner % already has 12 spheres', sphere_id_of.owner_telegram_id;
  end if;

  insert into public.spheres as s (owner_telegram_id, name)
  values (sphere_id_of.owner_telegram_id, wanted)
  returning s.id into found_id;

  return found_id;
end;
$$;

revoke execute on function public.sphere_id_of(bigint, text, boolean)
  from public, anon, authenticated;
grant execute on function public.sphere_id_of(bigint, text, boolean)
  to service_role;

-- --- Запись разбора ----------------------------------------------------------------

-- Задача из сообщения (§23.6) — заново, с той же подписью. Сверх
-- `20261006100000_several_tasks.sql` — ключ `sphere` в `task`: название сферы
-- из списка (§30.2). Сфера по смыслу не заводится: нет такой живой — задача
-- без сферы.
create or replace function public.insert_message_task(
  owner_telegram_id bigint,
  message_id uuid,
  task jsonb,
  reminders jsonb,
  item smallint
) returns public.tasks
language plpgsql
as $$
declare
  saved public.tasks;
  people_names text[] := '{}';
  asked text;
  owner_zone text;
  new_due timestamptz;
  new_repeat jsonb;
  new_occurrence timestamptz;
begin
  if jsonb_typeof(insert_message_task.task) is distinct from 'object' then
    raise exception 'insert_message_task: task must be an object';
  end if;

  if insert_message_task.item is null or insert_message_task.item not between 1 and 10 then
    raise exception 'insert_message_task: item must be between 1 and 10';
  end if;

  if jsonb_typeof(insert_message_task.task -> 'people') = 'array' then
    select coalesce(array_agg(person.name), '{}') into people_names
      from jsonb_array_elements_text(insert_message_task.task -> 'people') as person(name);
  end if;

  -- Уточняющий вопрос (§10.1): задача записывается сразу, вопрос — у неё.
  asked := nullif(btrim(insert_message_task.task ->> 'open_question'), '');

  -- Правило повтора (§13.5): первый раз — срок, час серии — его час.
  if coalesce(jsonb_typeof(insert_message_task.task -> 'repeat'), 'null') <> 'null' then
    new_due := (insert_message_task.task ->> 'due_at')::timestamptz;
    if new_due is null
       or coalesce(insert_message_task.task ->> 'kind', 'task') <> 'task' then
      raise exception 'insert_message_task: repeat needs a due date of a task';
    end if;

    select s.timezone into owner_zone
      from public.owner_settings s
     where s.owner_telegram_id = insert_message_task.owner_telegram_id;
    if owner_zone is null then
      raise exception 'insert_message_task: owner has no timezone';
    end if;

    new_repeat := public.repeat_rule(
      insert_message_task.task -> 'repeat',
      new_due,
      insert_message_task.task ->> 'due_precision',
      owner_zone
    );
    new_occurrence := new_due;
  end if;

  if asked is not null then
    update public.tasks t
       set open_question = null,
           question_asked_at = null
     where t.owner_telegram_id = insert_message_task.owner_telegram_id
       and (t.open_question is not null or t.question_asked_at is not null);
  end if;

  insert into public.tasks (
    owner_telegram_id, title, kind, due_at, due_precision,
    priority, promise, people, needs_review, open_question, question_asked_at,
    source_message_id, source_item, repeat, occurrence_at, sphere_id
  )
  values (
    insert_message_task.owner_telegram_id,
    insert_message_task.task ->> 'title',
    coalesce(insert_message_task.task ->> 'kind', 'task'),
    (insert_message_task.task ->> 'due_at')::timestamptz,
    insert_message_task.task ->> 'due_precision',
    coalesce(insert_message_task.task ->> 'priority', 'normal'),
    insert_message_task.task ->> 'promise',
    people_names,
    coalesce((insert_message_task.task ->> 'needs_review')::boolean, false),
    asked,
    case when asked is null then null else now() end,
    insert_message_task.message_id,
    insert_message_task.item,
    new_repeat,
    new_occurrence,
    public.sphere_id_of(
      insert_message_task.owner_telegram_id, insert_message_task.task ->> 'sphere', false
    )
  )
  returning * into saved;

  -- Список `{stage, fire_at}` считает бот (§6.1); пустой — напоминаний нет.
  -- У каждого дела свои напоминания: разные дела не сливаются (§23.3).
  if jsonb_typeof(insert_message_task.reminders) = 'array' then
    insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
    select insert_message_task.owner_telegram_id,
           saved.id,
           planned.reminder ->> 'stage',
           (planned.reminder ->> 'fire_at')::timestamptz
      from jsonb_array_elements(insert_message_task.reminders) as planned(reminder)
    on conflict on constraint reminders_task_id_stage_key do nothing;
  end if;

  return saved;
end;
$$;

-- Действие над задачей из чата (§12.3) — заново, с той же подписью. Сверх
-- `20260929200000_repeat.sql` — действие `sphere` (§30.2): сфера задачи —
-- `edit.sphere`, название; пусто — снять. Сферы с таким названием нет —
-- заводится: «это по X» владелец говорит сам. Остальное — как было.
create or replace function public.edit_from_chat(owner_telegram_id bigint, edit jsonb)
returns public.tasks
language plpgsql
as $$
declare
  target_id uuid;
  verb text;
  asked text;
  fields jsonb;
  planned jsonb;
  saved public.tasks;
begin
  if jsonb_typeof(edit_from_chat.edit) is distinct from 'object' then
    raise exception 'edit_from_chat: edit must be an object';
  end if;

  target_id := (edit_from_chat.edit ->> 'task_id')::uuid;
  verb := edit_from_chat.edit ->> 'action';

  if verb = 'sphere' then
    select t.* into saved
      from public.tasks t
     where t.id = target_id
       and t.owner_telegram_id = edit_from_chat.owner_telegram_id
       and t.status = 'active'
       for update;

    if not found then
      return null;
    end if;

    update public.tasks t
       set sphere_id = public.sphere_id_of(
             edit_from_chat.owner_telegram_id, edit_from_chat.edit ->> 'sphere', true
           )
     where t.id = saved.id
       and t.owner_telegram_id = edit_from_chat.owner_telegram_id
    returning t.* into saved;

    return saved;
  end if;

  if verb in ('done', 'cancel', 'skip') then
    select t.* into saved
      from public.tasks t
     where t.id = target_id
       and t.owner_telegram_id = edit_from_chat.owner_telegram_id
       and t.status = 'active'
       for update;

    if not found then
      return null;
    end if;

    if saved.repeat is null or verb = 'cancel' then
      update public.tasks t
         set status = case verb when 'done' then 'done' else 'cancelled' end
       where t.id = saved.id
         and t.owner_telegram_id = edit_from_chat.owner_telegram_id
      returning t.* into saved;

      delete from public.reminders r
       where r.task_id = saved.id
         and r.owner_telegram_id = edit_from_chat.owner_telegram_id
         and r.sent_at is null;

      return saved;
    end if;

    if coalesce(jsonb_typeof(edit_from_chat.edit -> 'next_at'), 'null') = 'null' then
      raise exception 'edit_from_chat: next_at is required for a repeating task';
    end if;

    if coalesce(jsonb_typeof(edit_from_chat.edit -> 'occurrence'), 'null') = 'null' then
      return null;
    end if;
    if (edit_from_chat.edit ->> 'occurrence')::bigint
       <> floor(extract(epoch from saved.occurrence_at))::bigint then
      return null;
    end if;

    planned := edit_from_chat.edit -> 'schedule';
    if planned is null or jsonb_typeof(planned) = 'null' then
      planned := '[]'::jsonb;
    end if;

    return public.advance_task(
      edit_from_chat.owner_telegram_id,
      saved.id,
      (edit_from_chat.edit ->> 'occurrence')::bigint,
      (edit_from_chat.edit ->> 'next_at')::timestamptz,
      planned
    );
  end if;

  if verb is distinct from 'change' then
    raise exception 'edit_from_chat: unknown action %', verb;
  end if;

  asked := nullif(btrim(edit_from_chat.edit ->> 'question'), '');
  if asked is not null then
    update public.tasks t
       set needs_review = true,
           open_question = asked,
           question_asked_at = now()
     where t.id = target_id
       and t.owner_telegram_id = edit_from_chat.owner_telegram_id
       and t.status = 'active'
    returning t.* into saved;

    if not found then
      return null;
    end if;

    -- У владельца открыт не больше одного вопроса (§10.1): новый снимает
    -- прежние, даже если его ставит нажатие кнопки, а не запись сообщения.
    -- Только после того, как вопрос встал: задачи нет — не тронуто ничего.
    update public.tasks t
       set open_question = null,
           question_asked_at = null
     where t.owner_telegram_id = edit_from_chat.owner_telegram_id
       and t.id <> target_id
       and (t.open_question is not null or t.question_asked_at is not null);

    return saved;
  end if;

  fields := edit_from_chat.edit -> 'changes';
  if fields is null or jsonb_typeof(fields) = 'null' then
    fields := '{}'::jsonb;
  end if;

  if fields = '{}'::jsonb then
    select t.* into saved
      from public.tasks t
     where t.id = target_id
       and t.owner_telegram_id = edit_from_chat.owner_telegram_id
       and t.status = 'active'
       for update;

    if not found then
      return null;
    end if;
    return saved;
  end if;

  planned := edit_from_chat.edit -> 'schedule';
  if planned is null or jsonb_typeof(planned) = 'null' then
    planned := '[]'::jsonb;
  end if;

  return public.change_task(edit_from_chat.owner_telegram_id, target_id, fields, planned);
end;
$$;

-- Шаг второй (§3.4) — заново, с новым необязательным `spheres` в конце: вызов
-- без него (бот до выкладки) идёт прежним путём. Подпись меняется, поэтому
-- прежняя снимается: две перегрузки PostgREST различать нечем.
--
-- Сверх `20261006100000_several_tasks.sql`:
-- - `spheres` — `{"drop": [названия], "add": [названия], "chat": {"thread_id",
--   "sphere"}}`, всё необязательно (§30.2). Сначала убираются сферы из
--   `drop` — у дел, записей и чатов они снимаются, — потом заводятся `add`
--   (уже есть — не дублируются), потом сфера чата: у чата и у всех задач его
--   разборов. Всё — до памяти и дел, чтобы они нашли новые сферы;
-- - у записи памяти — ключ `sphere`: знание о сфере, сферы нет — заводится.
--   Повтор той же записи сферу ей ставит, если её не было.
drop function public.record_understanding(
  uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, text, numeric, jsonb, jsonb,
  text, uuid
);

create function public.record_understanding(
  message_id uuid,
  owner_telegram_id bigint,
  analysis jsonb,
  ai_model text,
  ai_input_tokens int,
  ai_output_tokens int,
  reply text,
  tasks jsonb,
  facts jsonb,
  transcript text default null,
  transcript_confidence numeric default null,
  amend jsonb default null,
  edit jsonb default null,
  photo_text text default null,
  same_task uuid default null,
  spheres jsonb default null
) returns setof public.tasks
language plpgsql
as $$
declare
  pressed public.messages;
  saved public.tasks;
  was public.tasks;
  chat public.chat_threads;
  linked uuid;
  new_count int := 0;
  questions int;
  entry jsonb;
  people_names text[] := '{}';
  changes jsonb;
  owner_zone text;
  new_due timestamptz;
  new_precision text;
  new_repeat jsonb;
  new_occurrence timestamptz;
  new_question text;
  named text;
  dropped uuid;
  chat_sphere uuid;
begin
  if coalesce(jsonb_typeof(record_understanding.tasks), 'null') not in ('null', 'array') then
    raise exception 'record_understanding: tasks must be an array';
  end if;
  if jsonb_typeof(record_understanding.tasks) = 'array' then
    new_count := jsonb_array_length(record_understanding.tasks);
  end if;

  if coalesce(jsonb_typeof(record_understanding.spheres), 'null') not in ('null', 'object') then
    raise exception 'record_understanding: spheres must be an object';
  end if;

  -- Дубль — «задачи из сообщения нет, оно о найденной» (§15.3): бот
  -- передаёт его только у сообщения об одном деле (§23.6). Отказ до всякой
  -- записи — ошибка бота, а не гонка.
  if record_understanding.same_task is not null
     and (new_count > 0
          or coalesce(jsonb_typeof(record_understanding.amend), 'null') <> 'null'
          or coalesce(jsonb_typeof(record_understanding.edit), 'null') <> 'null') then
    raise exception 'record_understanding: same_task goes without tasks, amend and edit';
  end if;

  -- Вопрос один на ответ (§23.3): два из одного сообщения — ошибка бота.
  select count(*) into questions
    from (
      select record_understanding.amend ->> 'question' as question
      union all
      select record_understanding.edit ->> 'question'
      union all
      select listed.value -> 'task' ->> 'open_question'
        from jsonb_array_elements(
          case when new_count > 0 then record_understanding.tasks else '[]'::jsonb end
        ) as listed(value)
    ) asked
   where nullif(btrim(asked.question), '') is not null;
  if questions > 1 then
    raise exception 'record_understanding: more than one open question';
  end if;

  -- Владелец передаётся явно и сверяется с владельцем сообщения (§4.3):
  -- ключ service-role правила доступа обходит, разделение держит сама
  -- функция. Блокировка — чтобы два прохода одного сообщения не записали
  -- его дважды.
  select m.* into pressed
    from public.messages m
   where m.id = record_understanding.message_id
     and m.owner_telegram_id = record_understanding.owner_telegram_id
     for update;

  if not found then
    raise exception 'record_understanding: message % is not owned by %',
      record_understanding.message_id, record_understanding.owner_telegram_id;
  end if;

  -- Повтор: ответ уже дан, задача заведена, дополнена, поправлена или
  -- найдена дублем. Второй раз не пишется ничего — ни разбор, ни память,
  -- ни сферы, ни задачи; возвращаются задачи сообщения, связанная — первой.
  if pressed.reply is not null
     or pressed.task_id is not null
     or exists (
       select 1
         from public.tasks t
        where t.source_message_id = pressed.id
          and t.owner_telegram_id = record_understanding.owner_telegram_id
     ) then
    return query
      select t.*
        from public.tasks t
       where t.owner_telegram_id = record_understanding.owner_telegram_id
         and (t.id = pressed.task_id or t.source_message_id = pressed.id)
       order by case when t.id = pressed.task_id then 0 else 1 end, t.source_item;
    return;
  end if;

  update public.messages m
     set analysis = record_understanding.analysis,
         ai_model = record_understanding.ai_model,
         ai_input_tokens = record_understanding.ai_input_tokens,
         ai_output_tokens = record_understanding.ai_output_tokens,
         reply = record_understanding.reply,
         -- Расшифровка становится текстом сообщения (§9.3); у текста и у
         -- нерасслышанного голоса она null — тогда текст не трогается.
         text = coalesce(record_understanding.transcript, m.text),
         transcript_confidence = coalesce(
           record_understanding.transcript_confidence, m.transcript_confidence
         ),
         -- Прочитанное со снимка (§14.3); у текста и голоса аргумента нет.
         photo_text = coalesce(record_understanding.photo_text, m.photo_text)
   where m.id = pressed.id;

  -- Сферы (§30.2): убрать, завести, сфера чата — до памяти и дел.
  if jsonb_typeof(record_understanding.spheres) = 'object' then
    for named in
      select listed.value
        from jsonb_array_elements_text(
          coalesce(record_understanding.spheres -> 'drop', '[]'::jsonb)
        ) as listed(value)
    loop
      dropped := null;
      update public.spheres s
         set removed_at = now()
       where s.owner_telegram_id = record_understanding.owner_telegram_id
         and s.removed_at is null
         and lower(s.name) = lower(btrim(named))
      returning s.id into dropped;

      if dropped is not null then
        update public.tasks t
           set sphere_id = null
         where t.owner_telegram_id = record_understanding.owner_telegram_id
           and t.sphere_id = dropped;
        update public.facts f
           set sphere_id = null
         where f.owner_telegram_id = record_understanding.owner_telegram_id
           and f.sphere_id = dropped;
        update public.chat_threads c
           set sphere_id = null
         where c.owner_telegram_id = record_understanding.owner_telegram_id
           and c.sphere_id = dropped;
      end if;
    end loop;

    for named in
      select listed.value
        from jsonb_array_elements_text(
          coalesce(record_understanding.spheres -> 'add', '[]'::jsonb)
        ) as listed(value)
    loop
      perform public.sphere_id_of(record_understanding.owner_telegram_id, named, true);
    end loop;

    -- Сфера чата и его дел (§30.2): ответ на отчёт о переписке.
    if jsonb_typeof(record_understanding.spheres -> 'chat') = 'object' then
      select c.* into chat
        from public.chat_threads c
       where c.id = (record_understanding.spheres -> 'chat' ->> 'thread_id')::uuid
         and c.owner_telegram_id = record_understanding.owner_telegram_id
         for update;

      if not found then
        raise exception 'record_understanding: thread % is not owned by %',
          record_understanding.spheres -> 'chat' ->> 'thread_id',
          record_understanding.owner_telegram_id;
      end if;

      chat_sphere := public.sphere_id_of(
        record_understanding.owner_telegram_id,
        record_understanding.spheres -> 'chat' ->> 'sphere',
        true
      );

      update public.chat_threads c
         set sphere_id = chat_sphere
       where c.id = chat.id;

      update public.tasks t
         set sphere_id = chat_sphere
       where t.owner_telegram_id = record_understanding.owner_telegram_id
         and t.chat_analysis_id in (
           select a.id
             from public.chat_analyses a
            where a.thread_id = chat.id
              and a.owner_telegram_id = record_understanding.owner_telegram_id
         );
    end if;
  end if;

  -- Память пишется для любого вида сообщения (§8.2, §8.3). Цель конфликта —
  -- имя ограничения, а не список колонок: в plpgsql список совпадает с
  -- именами параметров. Статус только растёт; знание о сфере (§30.1) ставит
  -- записи сферу, если её не было.
  if jsonb_typeof(record_understanding.facts) = 'array' then
    insert into public.facts (
      owner_telegram_id, category, text, status, source_message_id, sphere_id
    )
    select record_understanding.owner_telegram_id,
           fact.value ->> 'category',
           fact.value ->> 'text',
           coalesce(fact.value ->> 'status', 'guess'),
           pressed.id,
           public.sphere_id_of(
             record_understanding.owner_telegram_id, fact.value ->> 'sphere', true
           )
      from jsonb_array_elements(record_understanding.facts) as fact(value)
     where coalesce(fact.value ->> 'text', '') <> ''
    on conflict on constraint facts_owner_telegram_id_category_text_key do update
       set status = case
             when excluded.status = 'fact' then 'fact'
             else public.facts.status end,
           source_message_id = case
             when excluded.status = 'fact' and public.facts.status = 'guess'
               then excluded.source_message_id
             else public.facts.source_message_id end,
           sphere_id = coalesce(excluded.sphere_id, public.facts.sphere_id)
     where (excluded.status = 'fact' and public.facts.status = 'guess')
        or (excluded.sphere_id is not null
            and excluded.sphere_id is distinct from public.facts.sphere_id);
  end if;

  -- Любая запись снимает открытые вопросы владельца (§10.3); новый вопрос
  -- ставится ниже. Кроме «не расслышал» (§9.3): ни разбора, ни дел, ни
  -- поправки — повтор должен застать вопрос открытым.
  if coalesce(jsonb_typeof(record_understanding.analysis), 'null') <> 'null'
     or new_count > 0
     or coalesce(jsonb_typeof(record_understanding.amend), 'null') <> 'null'
     or coalesce(jsonb_typeof(record_understanding.edit), 'null') <> 'null' then
    update public.tasks t
       set open_question = null,
           question_asked_at = null
     where t.owner_telegram_id = record_understanding.owner_telegram_id
       and (t.open_question is not null or t.question_asked_at is not null);
  end if;

  -- Дубль (§15.3): задачу не заводим, сообщение ведёт на найденную.
  if record_understanding.same_task is not null then
    select t.* into saved
      from public.tasks t
     where t.id = record_understanding.same_task
       and t.owner_telegram_id = record_understanding.owner_telegram_id
       and t.status = 'active'
       for update;

    if not found then
      raise exception 'record_understanding: task % is not an active task of %',
        record_understanding.same_task, record_understanding.owner_telegram_id;
    end if;

    update public.messages m
       set task_id = saved.id
     where m.id = pressed.id;

    return next saved;
    return;
  end if;

  select s.timezone into owner_zone
    from public.owner_settings s
   where s.owner_telegram_id = record_understanding.owner_telegram_id;

  -- Ответ на вопрос (§10.2, §22.5) — тело прежнее, но функция за ним не
  -- кончается: новые дела сообщения пишутся следом.
  if record_understanding.amend is not null
     and jsonb_typeof(record_understanding.amend) = 'object' then
    changes := coalesce(record_understanding.amend -> 'fields', '{}'::jsonb);
    new_question := nullif(btrim(record_understanding.amend ->> 'question'), '');

    select t.* into was
      from public.tasks t
     where t.id = (record_understanding.amend ->> 'task_id')::uuid
       and t.owner_telegram_id = record_understanding.owner_telegram_id
       and t.status = 'active'
       for update;

    if not found then
      raise exception 'record_understanding: task % is not an active task of %',
        record_understanding.amend ->> 'task_id', record_understanding.owner_telegram_id;
    end if;

    if jsonb_typeof(changes -> 'people') = 'array' then
      select coalesce(array_agg(person.name), '{}') into people_names
        from jsonb_array_elements_text(changes -> 'people') as person(name);
    end if;

    new_due := case when changes ? 'due_at' then (changes ->> 'due_at')::timestamptz
                    else was.due_at end;
    new_precision := case when changes ? 'due_precision' then changes ->> 'due_precision'
                          else was.due_precision end;
    if coalesce(jsonb_typeof(changes -> 'repeat'), 'null') <> 'null' then
      if new_due is null or was.kind <> 'task' then
        raise exception 'record_understanding: repeat needs a due date of a task';
      end if;
      if owner_zone is null then
        raise exception 'record_understanding: owner has no timezone';
      end if;
      new_repeat := public.repeat_rule(changes -> 'repeat', new_due, new_precision, owner_zone);
      new_occurrence := new_due;
    elsif changes ? 'repeat' or new_due is null then
      new_repeat := null;
      new_occurrence := null;
    else
      new_repeat := was.repeat;
      new_occurrence := was.occurrence_at;
    end if;

    update public.tasks t
       set title = case
             when changes ? 'title' then coalesce(nullif(changes ->> 'title', ''), t.title)
             else t.title end,
           due_at = case
             when changes ? 'due_at' then (changes ->> 'due_at')::timestamptz
             else t.due_at end,
           due_precision = case
             when changes ? 'due_precision' then changes ->> 'due_precision'
             else t.due_precision end,
           priority = case
             when changes ? 'priority' then coalesce(changes ->> 'priority', t.priority)
             else t.priority end,
           promise = case
             when changes ? 'promise' then changes ->> 'promise'
             else t.promise end,
           people = case
             when changes ? 'people' then people_names
             else t.people end,
           needs_review = case
             when changes ? 'needs_review'
               then coalesce((changes ->> 'needs_review')::boolean, t.needs_review)
             else t.needs_review end,
           repeat = new_repeat,
           occurrence_at = new_occurrence,
           open_question = coalesce(new_question, t.open_question),
           question_asked_at = case
             when new_question is not null then now()
             else t.question_asked_at end
     where t.id = was.id
       and t.owner_telegram_id = record_understanding.owner_telegram_id
       and t.status = 'active'
    returning t.* into saved;

    if not found then
      raise exception 'record_understanding: task % is not an active task of %',
        record_understanding.amend ->> 'task_id', record_understanding.owner_telegram_id;
    end if;

    delete from public.reminders r
     where r.task_id = saved.id
       and r.owner_telegram_id = record_understanding.owner_telegram_id
       and r.sent_at is null;

    if jsonb_typeof(record_understanding.amend -> 'reminders') = 'array' then
      insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
      select record_understanding.owner_telegram_id,
             saved.id,
             planned.reminder ->> 'stage',
             (planned.reminder ->> 'fire_at')::timestamptz
        from jsonb_array_elements(record_understanding.amend -> 'reminders') as planned(reminder)
      on conflict on constraint reminders_task_id_stage_key do update
         set fire_at = excluded.fire_at,
             sent_at = null,
             telegram_message_id = null;
    end if;

    linked := saved.id;
    return next saved;

  -- Правка задачи словом (§12.3) — одна (§12.7); новые дела — следом.
  elsif coalesce(jsonb_typeof(record_understanding.edit), 'null') <> 'null' then
    saved := public.edit_from_chat(
      record_understanding.owner_telegram_id, record_understanding.edit
    );

    if saved.id is null then
      raise exception 'record_understanding: task % is not an active task of %',
        record_understanding.edit ->> 'task_id', record_understanding.owner_telegram_id;
    end if;

    linked := saved.id;
    return next saved;
  end if;

  -- Новые дела — по номерам (§23.3). Повтор номера ломает уникальность,
  -- номер вне 1–10 — проверку вставки: откатывается всё сообщение.
  for entry in
    select listed.value
      from jsonb_array_elements(
        case when new_count > 0 then record_understanding.tasks else '[]'::jsonb end
      ) as listed(value)
     order by (listed.value ->> 'item')::smallint
  loop
    saved := public.insert_message_task(
      record_understanding.owner_telegram_id,
      pressed.id,
      entry -> 'task',
      entry -> 'reminders',
      (entry ->> 'item')::smallint
    );
    return next saved;
  end loop;

  if linked is null and new_count = 1 and saved.source_item = 1 then
    linked := saved.id;
  end if;

  if linked is not null then
    update public.messages m
       set task_id = linked
     where m.id = pressed.id;
  end if;

  return;
end;
$$;

revoke execute on function
  public.record_understanding(
    uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, text, numeric, jsonb, jsonb,
    text, uuid, jsonb
  )
  from public, anon, authenticated;
grant execute on function
  public.record_understanding(
    uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, text, numeric, jsonb, jsonb,
    text, uuid, jsonb
  )
  to service_role;

-- --- Разбор чатов ------------------------------------------------------------------

-- Разбор одной транзакцией (§25.3) — заново, с необязательным `sphere` в конце:
-- название сферы чата из списка (§30.2). Сфера чата липкая: ставится, только
-- пока у чата её нет, — поправленную владельцем следующий разбор не
-- откатывает. Дела разбора получают сферу чата. Сферы с таким названием нет —
-- без сферы: по смыслу сферы не заводятся. Остальное — как в
-- `20261007100000_chats.sql`.
drop function public.record_chat_analysis(
  bigint, uuid, uuid[], jsonb, text, integer, integer, integer, text, jsonb, jsonb
);

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
  tasks jsonb,
  sphere text default null
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
  chat_sphere uuid;
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

  -- Сфера чата (§30.2): есть — остаётся, нет — из разбора, если такая живая.
  chat_sphere := chat.sphere_id;
  if chat_sphere is null then
    chat_sphere := public.sphere_id_of(
      record_chat_analysis.owner_telegram_id, record_chat_analysis.sphere, false
    );
    if chat_sphere is not null then
      update public.chat_threads t
         set sphere_id = chat_sphere
       where t.id = chat.id;
    end if;
  end if;

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
      needs_review, chat_analysis_id, chat_item, sphere_id
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
      (entry ->> 'item')::smallint,
      chat_sphere
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

revoke execute on function public.record_chat_analysis(
  bigint, uuid, uuid[], jsonb, text, integer, integer, integer, text, jsonb, jsonb, text
) from public, anon, authenticated;
grant execute on function public.record_chat_analysis(
  bigint, uuid, uuid[], jsonb, text, integer, integer, integer, text, jsonb, jsonb, text
) to service_role;

-- Дела разбора для сообщения владельцу (§25.4) — заново: сверх
-- `20261008200000_chat_link.sql` — сфера чата, нынешняя (§30.3).
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
  status text,
  sphere text
)
language sql
stable
as $$
  select t.platform, t.chat_key, t.name, t.username, a.chat_with, k.chat_item, k.id,
         k.title, k.due_at, k.due_precision, k.promise, k.status, s.name
    from public.chat_analyses a
    join public.chat_threads t on t.id = a.thread_id
    join public.tasks k
      on k.chat_analysis_id = a.id
     and k.owner_telegram_id = a.owner_telegram_id
    left join public.spheres s
      on s.id = t.sphere_id
     and s.owner_telegram_id = t.owner_telegram_id
   where a.id = chat_report.analysis_id
     and a.owner_telegram_id = chat_report.owner_telegram_id
   order by k.chat_item;
$$;

revoke execute on function public.chat_report(bigint, uuid)
  from public, anon, authenticated;
grant execute on function public.chat_report(bigint, uuid)
  to service_role;

-- Чат, о разборе которого бот написал этим сообщением (§30.2): ответ свайпом
-- на отчёт о переписке меняет сферу чата. Нет такого отчёта — пусто.
create function public.reported_chat(
  owner_telegram_id bigint,
  telegram_message_id bigint
) returns table (
  thread_id uuid,
  platform text,
  chat_key text,
  chat_name text,
  chat_with text,
  sphere text
)
language sql
stable
as $$
  select t.id, t.platform, t.chat_key, t.name, a.chat_with, s.name
    from public.chat_analyses a
    join public.chat_threads t
      on t.id = a.thread_id
     and t.owner_telegram_id = a.owner_telegram_id
    left join public.spheres s
      on s.id = t.sphere_id
     and s.owner_telegram_id = t.owner_telegram_id
   where a.owner_telegram_id = reported_chat.owner_telegram_id
     and a.report_message_id = reported_chat.telegram_message_id
   order by a.created_at desc
   limit 1;
$$;

revoke execute on function public.reported_chat(bigint, bigint)
  from public, anon, authenticated;
grant execute on function public.reported_chat(bigint, bigint)
  to service_role;
