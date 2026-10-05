-- Несколько дел в одном сообщении (этап 023).
--
-- Схема — techspec/03-schema.md §3.3–3.4; поведение —
-- techspec/23-several-tasks.md §23.6. Применяется `supabase db push` или тем же
-- текстом в SQL-редакторе панели Supabase (supabase/README.md). Применённая
-- миграция не правится: следующая правка — следующий файл.
--
-- Правила доступа не меняются (§4.2): функции записи — только `service_role`.
-- Старые подписи `record_understanding`, `insert_message_task` и
-- `record_separately` удаляются: бот этапа их не зовёт, а прежний бот
-- перезапускается сразу за миграцией (supabase/README.md).

-- Номер дела в сообщении (§23.3): 1 — верхние поля разбора, 2–10 — дела
-- `also` по порядку.
alter table public.tasks add column source_item smallint;

-- До этапа сообщение давало одну задачу — у неё номер 1. Это не правка
-- задачи, поэтому `updated_at` на время переноса не ставится.
alter table public.tasks disable trigger tasks_set_updated_at;
update public.tasks t
   set source_item = 1
 where t.source_message_id is not null;
alter table public.tasks enable trigger tasks_set_updated_at;

-- Номер есть ровно у задач из сообщения, и у одного сообщения номера не
-- повторяются: повтор номера в одном вызове откатывает всё сообщение.
alter table public.tasks
  add constraint tasks_source_item_check check (
    (source_message_id is null) = (source_item is null)
    and (source_item is null or source_item between 1 and 10)
  ),
  add constraint tasks_source_item_key unique (source_message_id, source_item);

-- Две перегрузки PostgREST различать нечем (§3.4): прежние удаляются.
drop function public.record_understanding(
  uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb,
  text, uuid
);
drop function public.record_separately(bigint, uuid, jsonb, jsonb, text);
drop function public.insert_message_task(bigint, uuid, jsonb, jsonb);

-- Задача из сообщения с её напоминаниями (§15.6) — плюс номер дела (§23.6).
-- Остальное тело — как в `20260930200000_duplicates.sql`. Владельца
-- сообщения проверяет вызывающий.
create function public.insert_message_task(
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
    source_message_id, source_item, repeat, occurrence_at
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
    new_occurrence
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

revoke execute on function public.insert_message_task(bigint, uuid, jsonb, jsonb, smallint)
  from public, anon, authenticated;
grant execute on function public.insert_message_task(bigint, uuid, jsonb, jsonb, smallint)
  to service_role;

-- Шаг второй (§3.4, §23.6): всё сообщение — одной транзакцией. Вместо пары
-- `task`, `reminders` — `tasks`, массив новых дел
-- `{"item": N, "task": {…}, "reminders": […]}`. Возвращает задачу ответа,
-- правки или дубля, если она есть, и новые задачи по номерам.
--
-- Сверх `20261004200000_overdue_ask.sql`:
-- - сообщение читается под блокировкой до всякой записи, и повтор
--   узнаётся по нему: у сообщения уже есть ответ, связанная задача или
--   задачи с его `source_message_id`. Тогда функция возвращает задачи
--   сообщения и не пишет ничего — так и у сообщения из одних дублей, и у
--   ждущего выбора кнопкой, у которых задач нет;
-- - открытых вопросов на вызов не больше одного: `amend.question`,
--   `edit.question` и `open_question` дел вместе (§23.3);
-- - ответ на вопрос и правка функцию не кончают: новые дела пишутся следом;
-- - `messages.task_id` — задача ответа, правки или дубля; иначе
--   единственная новая задача, если у неё номер 1; иначе пусто.
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
  same_task uuid default null
) returns setof public.tasks
language plpgsql
as $$
declare
  pressed public.messages;
  saved public.tasks;
  was public.tasks;
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
begin
  if coalesce(jsonb_typeof(record_understanding.tasks), 'null') not in ('null', 'array') then
    raise exception 'record_understanding: tasks must be an array';
  end if;
  if jsonb_typeof(record_understanding.tasks) = 'array' then
    new_count := jsonb_array_length(record_understanding.tasks);
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
  -- ни задачи; возвращаются задачи сообщения, связанная — первой.
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

  -- Память пишется для любого вида сообщения (§8.2, §8.3). Цель конфликта —
  -- имя ограничения, а не список колонок: в plpgsql список совпадает с
  -- именами параметров.
  if jsonb_typeof(record_understanding.facts) = 'array' then
    insert into public.facts (owner_telegram_id, category, text, status, source_message_id)
    select record_understanding.owner_telegram_id,
           fact.value ->> 'category',
           fact.value ->> 'text',
           coalesce(fact.value ->> 'status', 'guess'),
           pressed.id
      from jsonb_array_elements(record_understanding.facts) as fact(value)
     where coalesce(fact.value ->> 'text', '') <> ''
    on conflict on constraint facts_owner_telegram_id_category_text_key do update
       set status = 'fact',
           source_message_id = excluded.source_message_id
     where excluded.status = 'fact'
       and public.facts.status = 'guess';
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
    text, uuid
  )
  from public, anon, authenticated;
grant execute on function
  public.record_understanding(
    uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, text, numeric, jsonb, jsonb,
    text, uuid
  )
  to service_role;

-- «Записать отдельно» (§15.4, §23.5): дело номер `item` из сохранённого
-- разбора. «Уже записано» — задача с этим сообщением и этим номером, а не
-- любая задача сообщения. Номер 1 — сообщение ведёт на новую задачу, как
-- было; другой номер — `task_id` не трогается. `reply` пишется как передан:
-- под ответом о нескольких делах бот передаёт прежний ответ с итогом
-- нажатия последним абзацем.
create function public.record_separately(
  owner_telegram_id bigint,
  message_id uuid,
  task jsonb,
  reminders jsonb,
  reply text,
  item smallint
) returns public.messages
language plpgsql
as $$
declare
  pressed public.messages;
  saved public.tasks;
begin
  select m.* into pressed
    from public.messages m
   where m.id = record_separately.message_id
     and m.owner_telegram_id = record_separately.owner_telegram_id
     for update;

  if not found then
    raise exception 'record_separately: message % is not owned by %',
      record_separately.message_id, record_separately.owner_telegram_id;
  end if;

  perform 1
     from public.tasks t
    where t.source_message_id = pressed.id
      and t.source_item = record_separately.item
      and t.owner_telegram_id = record_separately.owner_telegram_id;
  if found then
    return pressed;
  end if;

  saved := public.insert_message_task(
    record_separately.owner_telegram_id,
    pressed.id,
    record_separately.task,
    record_separately.reminders,
    record_separately.item
  );

  update public.messages m
     set task_id = case when record_separately.item = 1 then saved.id else m.task_id end,
         reply = record_separately.reply
   where m.id = pressed.id
     and m.owner_telegram_id = record_separately.owner_telegram_id
  returning m.* into pressed;

  return pressed;
end;
$$;

revoke execute on function public.record_separately(bigint, uuid, jsonb, jsonb, text, smallint)
  from public, anon, authenticated;
grant execute on function public.record_separately(bigint, uuid, jsonb, jsonb, text, smallint)
  to service_role;

-- Итог нажатия под ответом о нескольких делах (§23.5) — абзацем к прежнему
-- ответу, чтобы недавний разговор (§17.3) видел и его. Нужна «Вернуть» и
-- откату переноса: выбор задачи и «Записать отдельно» пишут ответ своими
-- функциями. Ответ уже кончается этим абзацем — не пишется ничего: второе
-- нажатие след не удваивает. Сообщения нет или оно чужое — отказ.
create function public.append_reply(
  owner_telegram_id bigint,
  message_id uuid,
  paragraph text
) returns public.messages
language plpgsql
as $$
declare
  pressed public.messages;
begin
  select m.* into pressed
    from public.messages m
   where m.id = append_reply.message_id
     and m.owner_telegram_id = append_reply.owner_telegram_id
     for update;

  if not found then
    raise exception 'append_reply: message % is not owned by %',
      append_reply.message_id, append_reply.owner_telegram_id;
  end if;

  if nullif(btrim(append_reply.paragraph), '') is null
     or coalesce(right(pressed.reply, length(append_reply.paragraph)), '')
        = append_reply.paragraph then
    return pressed;
  end if;

  update public.messages m
     set reply = case
           when coalesce(m.reply, '') = '' then append_reply.paragraph
           else m.reply || E'\n\n' || append_reply.paragraph end
   where m.id = pressed.id
  returning m.* into pressed;

  return pressed;
end;
$$;

revoke execute on function public.append_reply(bigint, uuid, text)
  from public, anon, authenticated;
grant execute on function public.append_reply(bigint, uuid, text)
  to service_role;
