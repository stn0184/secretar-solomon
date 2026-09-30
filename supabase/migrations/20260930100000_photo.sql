-- Снимки: фото и картинка файлом становятся поручением.
--
-- Схема — techspec/03-schema.md §3.2, §3.4; поведение — §14. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.

-- Вид сообщения: к тексту и голосу добавляется снимок (§14.1). Сам снимок
-- не хранится (решение владельца): след — `telegram_file_id`, подпись в
-- `text` и то, что модель прочитала, в `photo_text`.
alter table public.messages
  drop constraint messages_kind_check,
  add constraint messages_kind_check
    check (kind in ('text', 'voice', 'video_note', 'photo')),
  -- что модель прочитала со снимка, до 500 знаков (обрезает бот, §14.3)
  add column photo_text text;

-- Шаг второй (§14.2, §14.5). Сверх миграции 011 — прочитанное со снимка
-- ложится в строку сообщения той же транзакцией, что разбор и задача.
-- Четырнадцатиаргументная версия уходит: две перегрузки PostgREST различать
-- нечем, а новый аргумент необязательный, и прежний вызов бота по именам
-- аргументов на новой версии работает как раньше. Тело — как в миграции 011.
drop function public.record_understanding(
  uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb
);

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
  facts jsonb,
  transcript text default null,
  transcript_confidence numeric default null,
  amend jsonb default null,
  edit jsonb default null,
  photo_text text default null
) returns public.tasks
language plpgsql
as $$
declare
  saved public.tasks;
  was public.tasks;
  linked uuid;
  people_names text[] := '{}';
  changes jsonb;
  asked text;
  owner_zone text;
  new_due timestamptz;
  new_precision text;
  new_repeat jsonb;
  new_occurrence timestamptz;
begin
  -- Владелец передаётся явно и сверяется с владельцем сообщения (§4.3):
  -- ключ service-role правила доступа обходит, поэтому разделение держит
  -- здесь сама функция, а не RLS. Задача, о которой сообщение уже знает,
  -- читается тем же запросом — по ней узнаётся повтор.
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
         -- Прочитанное со снимка (§14.3); у текста и голоса аргумента нет —
         -- null, и колонка не трогается: повтор без него прочитанного не стирает.
         photo_text = coalesce(record_understanding.photo_text, m.photo_text)
   where m.id = record_understanding.message_id
     and m.owner_telegram_id = record_understanding.owner_telegram_id
  returning m.task_id into linked;

  if not found then
    raise exception 'record_understanding: message % is not owned by %',
      record_understanding.message_id, record_understanding.owner_telegram_id;
  end if;

  -- Память пишется для любого вида сообщения — и для тех, где задачи нет
  -- (`about_me`, `chat`), поэтому раньше ранних выходов (§8.2, §8.3).
  -- Цель конфликта — имя ограничения, а не список колонок: в plpgsql список
  -- совпадает с именами параметров.
  if jsonb_typeof(record_understanding.facts) = 'array' then
    insert into public.facts (owner_telegram_id, category, text, status, source_message_id)
    select record_understanding.owner_telegram_id,
           item.value ->> 'category',
           item.value ->> 'text',
           coalesce(item.value ->> 'status', 'guess'),
           record_understanding.message_id
      from jsonb_array_elements(record_understanding.facts) as item(value)
     where coalesce(item.value ->> 'text', '') <> ''
    on conflict on constraint facts_owner_telegram_id_category_text_key do update
       set status = 'fact',
           source_message_id = excluded.source_message_id
     where excluded.status = 'fact'
       and public.facts.status = 'guess';
  end if;

  -- Повтор того же сообщения: задача уже заведена, дополнена или
  -- поправлена им — второй раз её не правим, напоминания и вопрос первого
  -- прохода остаются на месте.
  if linked is not null then
    select t.* into saved
      from public.tasks t
     where t.id = linked
       and t.owner_telegram_id = record_understanding.owner_telegram_id;
    return saved;
  end if;

  select t.* into saved
    from public.tasks t
   where t.source_message_id = record_understanding.message_id
   limit 1;
  if found then
    return saved;
  end if;

  -- Любая запись снимает открытые вопросы владельца (§10.3): ответ, новое
  -- поручение, правка, запись «как есть» при отказе модели, разговор,
  -- сведение о себе и вопрос старше суток закрываются одним правилом. Новый
  -- вопрос ставится ниже, уже после.
  --
  -- Кроме «не расслышал» (§9.3): ни разбора, ни задачи, ни поправки — понять
  -- ещё ничего не удалось, а бот сам просит повторить, и повтор должен застать
  -- вопрос открытым, иначе ответ «в пятницу» станет второй задачей.
  if coalesce(jsonb_typeof(record_understanding.analysis), 'null') <> 'null'
     or coalesce(jsonb_typeof(record_understanding.task), 'null') <> 'null'
     or coalesce(jsonb_typeof(record_understanding.amend), 'null') <> 'null'
     or coalesce(jsonb_typeof(record_understanding.edit), 'null') <> 'null' then
    update public.tasks t
       set open_question = null,
           question_asked_at = null
     where t.owner_telegram_id = record_understanding.owner_telegram_id
       and (t.open_question is not null or t.question_asked_at is not null);
  end if;

  select s.timezone into owner_zone
    from public.owner_settings s
   where s.owner_telegram_id = record_understanding.owner_telegram_id;

  -- Ответ на вопрос (§10.2): вместо новой задачи — поправка к названной.
  -- Меняются только ключи из `fields`; чужая, несуществующая или не активная
  -- (закрытая, убранная) задача — отказ, и вся транзакция, включая разбор и
  -- память, откатывается. Её закрыли или убрали, пока модель разбирала
  -- ответ, и напоминания по ней не уйдут (§6.2 берёт только активные) —
  -- «Напомню» в ответе было бы неправдой (инвариант 4).
  if record_understanding.amend is not null
     and jsonb_typeof(record_understanding.amend) = 'object' then
    changes := coalesce(record_understanding.amend -> 'fields', '{}'::jsonb);

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

    -- Правило — по сроку после поправки (§13.5): срок снят — снято и оно,
    -- срок перенесён без правила — перенесён только этот раз.
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
           occurrence_at = new_occurrence
     where t.id = was.id
       and t.owner_telegram_id = record_understanding.owner_telegram_id
       and t.status = 'active'
    returning t.* into saved;

    if not found then
      raise exception 'record_understanding: task % is not an active task of %',
        record_understanding.amend ->> 'task_id', record_understanding.owner_telegram_id;
    end if;

    -- Срок мог измениться: неотправленные напоминания уходят, новые
    -- планирует бот по §6.1. Ушедшая ступень, которая по новому сроку снова
    -- в будущем, взводится заново — иначе строка «Напомню» в ответе
    -- обещала бы то, чего не будет (инвариант 4).
    delete from public.reminders r
     where r.task_id = saved.id
       and r.owner_telegram_id = record_understanding.owner_telegram_id
       and r.sent_at is null;

    if jsonb_typeof(record_understanding.amend -> 'reminders') = 'array' then
      insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
      select record_understanding.owner_telegram_id,
             saved.id,
             planned.item ->> 'stage',
             (planned.item ->> 'fire_at')::timestamptz
        from jsonb_array_elements(record_understanding.amend -> 'reminders') as planned(item)
      on conflict on constraint reminders_task_id_stage_key do update
         set fire_at = excluded.fire_at,
             sent_at = null,
             telegram_message_id = null;
    end if;

    update public.messages m
       set task_id = saved.id
     where m.id = record_understanding.message_id
       and m.owner_telegram_id = record_understanding.owner_telegram_id;

    return saved;
  end if;

  -- Правка задачи словом (§12.3): закрыть, убрать, пропустить раз,
  -- поправить, спросить по правке или «менять нечего». Задачу закрыли,
  -- убрали или перевели на другой раз, пока модель разбирала сообщение, —
  -- отказ и откат целиком, как у ответа на вопрос.
  if coalesce(jsonb_typeof(record_understanding.edit), 'null') <> 'null' then
    saved := public.edit_from_chat(
      record_understanding.owner_telegram_id, record_understanding.edit
    );

    if saved.id is null then
      raise exception 'record_understanding: task % is not an active task of %',
        record_understanding.edit ->> 'task_id', record_understanding.owner_telegram_id;
    end if;

    update public.messages m
       set task_id = saved.id
     where m.id = record_understanding.message_id
       and m.owner_telegram_id = record_understanding.owner_telegram_id;

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

  -- Уточняющий вопрос (§10.1): задача записывается сразу, вопрос — у неё.
  asked := nullif(btrim(record_understanding.task ->> 'open_question'), '');

  -- Правило повтора (§13.5): первый раз — срок, час серии — его час.
  if coalesce(jsonb_typeof(record_understanding.task -> 'repeat'), 'null') <> 'null' then
    new_due := (record_understanding.task ->> 'due_at')::timestamptz;
    if new_due is null
       or coalesce(record_understanding.task ->> 'kind', 'task') <> 'task' then
      raise exception 'record_understanding: repeat needs a due date of a task';
    end if;
    if owner_zone is null then
      raise exception 'record_understanding: owner has no timezone';
    end if;
    new_repeat := public.repeat_rule(
      record_understanding.task -> 'repeat',
      new_due,
      record_understanding.task ->> 'due_precision',
      owner_zone
    );
    new_occurrence := new_due;
  end if;

  insert into public.tasks (
    owner_telegram_id, title, kind, due_at, due_precision,
    priority, promise, people, needs_review, open_question, question_asked_at,
    source_message_id, repeat, occurrence_at
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
    asked,
    case when asked is null then null else now() end,
    record_understanding.message_id,
    new_repeat,
    new_occurrence
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
    on conflict on constraint reminders_task_id_stage_key do nothing;
  end if;

  update public.messages m
     set task_id = saved.id
   where m.id = record_understanding.message_id
     and m.owner_telegram_id = record_understanding.owner_telegram_id;

  return saved;
end;
$$;

-- Права — как у прежней версии: только ключ бота.
revoke execute on function
  public.record_understanding(
    uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb,
    text
  )
  from public, anon, authenticated;
grant execute on function
  public.record_understanding(
    uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb,
    text
  )
  to service_role;
