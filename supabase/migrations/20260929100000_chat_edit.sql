-- Правка задачи словом в чате: перенести, поправить, закрыть, убрать.
--
-- Схема — techspec/03-schema.md §3.2–3.6; поведение — §12. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.

-- Третий статус — «убрана» (§12.3): задачу больше не надо делать, но она не
-- выполнена и не удалена. Приложение показывает только активные, поэтому
-- убранная пропадает из списка, как закрытая. Имя ограничения — то, что
-- Postgres дал безымянному `check` миграции 001.
alter table public.tasks
  drop constraint tasks_status_check,
  add constraint tasks_status_check check (status in ('active', 'done', 'cancelled'));

-- Задача, о которой сообщение (§12.4): заведённая из него, дополненная
-- ответом на вопрос, поправленная, закрытая, убранная, с вопросом по правке
-- или выбранная кнопкой. По ней бот находит последнюю задачу в разговоре и
-- свайп на своё сообщение (§12.2). Удаление задачи ссылку обнуляет, а не
-- запрещено: сообщение — след, а не часть задачи. Ссылочные действия
-- правилам доступа не подчиняются, поэтому удаление из приложения под RLS
-- проходит, как и раньше.
alter table public.messages
  add column task_id uuid references public.tasks (id) on delete set null;

-- У сообщений до миграции задача — та, что заведена из них.
update public.messages m
   set task_id = t.id
  from public.tasks t
 where t.source_message_id = m.id
   and t.owner_telegram_id = m.owner_telegram_id;

-- Под запрос «последняя задача в разговоре»: свежие сообщения владельца с
-- задачей. Сообщения без задачи в индекс не попадают.
create index messages_owner_task_idx
  on public.messages (owner_telegram_id, received_at desc)
  where task_id is not null;

-- Под `on delete set null`: удаление задачи ищет сообщения по ссылке.
create index messages_task_id_idx on public.messages (task_id);

-- Ядро правки полей задачи (§12.4) — бывшее тело `edit_task`, теперь одно на
-- приложение и чат («бот и Mini App зовут одно и то же», CLAUDE.md §«Слои»).
--
-- changes — только изменённые поля: title, kind, priority, promise, people
-- и срок одним из ключей: due_at — момент со смещением (точность time),
-- due_date — день YYYY-MM-DD (точность day, 18:00 этого дня в поясе
-- владельца), due_at = null — срока нет. Незнакомый ключ, оба ключа срока
-- сразу, пустая суть — отказ целиком.
--
-- schedule — чем заменить неотправленные напоминания, если сменились срок,
-- точность или вид:
--   null — путь приложения: план считает база (`reminder_plan` по поясу из
--          `owner_settings`), перенос ставит отметку для строки в чат (§11.4);
--   [{stage, fire_at}] — путь чата: план посчитал бот у той же
--          `reminder_plan` и назвал его в ответе (инвариант 4); отметка не
--          ставится — ответ на сообщение уже сказал «Перенёс».
--
-- Чужая, несуществующая, не активная задача — null, ничего не записано.
--
-- Под токеном владельца (роль `authenticated`) ядро не даёт больше, чем
-- `edit_task` и правила доступа (§4.2): владелец обязан совпасть с клеймом,
-- а готовый план отбрасывается — его считает база, как у `edit_task`.
-- Функция `security invoker`, поэтому сверх этого каждую строку режет RLS.
create function public.change_task(
  owner_telegram_id bigint,
  task_id uuid,
  changes jsonb,
  schedule jsonb default null
) returns public.tasks
language plpgsql
security invoker
as $$
declare
  owner_id bigint := change_task.owner_telegram_id;
  planned jsonb := change_task.schedule;
  fields jsonb := coalesce(change_task.changes, '{}'::jsonb);
  was public.tasks;
  saved public.tasks;
  owner_zone text;
  stray text;
  new_title text;
  new_due timestamptz;
  new_precision text;
  new_kind text;
  new_priority text;
  new_promise text;
  new_people text[];
  moved boolean;
  replan boolean;
begin
  if current_user in ('authenticated', 'anon') then
    if owner_id is distinct from (auth.jwt() ->> 'telegram_id')::bigint then
      return null;
    end if;
    planned := null;
  end if;

  if owner_id is null then
    return null;
  end if;

  if jsonb_typeof(fields) <> 'object' then
    raise exception 'change_task: changes must be an object';
  end if;

  if planned is not null and jsonb_typeof(planned) <> 'array' then
    raise exception 'change_task: schedule must be an array';
  end if;

  select k.name into stray
    from jsonb_object_keys(fields) as k(name)
   where k.name not in ('title', 'due_at', 'due_date', 'kind', 'priority', 'promise', 'people')
   limit 1;
  if stray is not null then
    raise exception 'change_task: unknown field %', stray;
  end if;

  if fields ? 'due_at' and fields ? 'due_date' then
    raise exception 'change_task: due_at and due_date are exclusive';
  end if;

  -- Только активная задача, и строка держится до конца транзакции: правка
  -- и «Сделано» не перекрещиваются.
  select t.* into was
    from public.tasks t
   where t.id = change_task.task_id
     and t.owner_telegram_id = owner_id
     and t.status = 'active'
     for update;

  if not found then
    return null;
  end if;

  select s.timezone into owner_zone
    from public.owner_settings s
   where s.owner_telegram_id = owner_id;

  new_title := was.title;
  if fields ? 'title' then
    new_title := btrim(fields ->> 'title');
    if coalesce(new_title, '') = '' then
      raise exception 'change_task: title is empty';
    end if;
  end if;

  new_due := was.due_at;
  new_precision := was.due_precision;
  if fields ? 'due_date' then
    -- День без часа: 18:00 этого дня в поясе владельца (§3.3); календарный
    -- день — как выбран, без пересчёта через пояс (§11.3).
    if owner_zone is null then
      raise exception 'change_task: owner has no timezone';
    end if;
    new_due := ((fields ->> 'due_date')::date + time '18:00') at time zone owner_zone;
    new_precision := 'day';
  elsif fields ? 'due_at' then
    if jsonb_typeof(fields -> 'due_at') = 'null' then
      new_due := null;
      new_precision := null;
    else
      -- Момент без смещения база прочла бы в своём поясе, а час — по часам
      -- телефона или словам человека: такой срок молча съехал бы.
      if (fields ->> 'due_at') !~ '([Zz]|[+-][0-9]{2}(:?[0-9]{2})?)$' then
        raise exception 'change_task: due_at needs an offset';
      end if;
      new_due := (fields ->> 'due_at')::timestamptz;
      new_precision := 'time';
    end if;
  end if;

  new_kind := case when fields ? 'kind' then fields ->> 'kind' else was.kind end;
  new_priority := case when fields ? 'priority' then fields ->> 'priority' else was.priority end;
  new_promise := case when fields ? 'promise' then fields ->> 'promise' else was.promise end;

  new_people := was.people;
  if fields ? 'people' then
    if jsonb_typeof(fields -> 'people') <> 'array' then
      raise exception 'change_task: people must be an array';
    end if;
    select coalesce(array_agg(btrim(person.name) order by person.n), '{}')
      into new_people
      from jsonb_array_elements_text(fields -> 'people') with ordinality as person(name, n)
     where btrim(person.name) <> '';
  end if;

  -- Изменение считается по значениям, а не по ключам: тот же день ещё раз —
  -- не перенос. Пустая точность у срока читается как день (§3.3).
  moved := new_due is distinct from was.due_at
        or (new_due is not null
            and coalesce(new_precision, 'day') is distinct from coalesce(was.due_precision, 'day'));
  replan := moved or new_kind is distinct from was.kind;

  -- Без пояса расписание дня не посчитать: задача без напоминаний,
  -- записанная молча, хуже отказа (§11.3). Готовый план пояса не требует.
  if replan and planned is null and owner_zone is null then
    raise exception 'change_task: owner has no timezone';
  end if;

  -- Правка — это «я проверил» (§11.2): пометка и вопрос снимаются у этой
  -- задачи. Вопросы других задач не трогаются.
  update public.tasks t
     set title = new_title,
         due_at = new_due,
         due_precision = new_precision,
         kind = new_kind,
         priority = new_priority,
         promise = new_promise,
         people = new_people,
         needs_review = false,
         open_question = null,
         question_asked_at = null,
         due_moved_at = case
           when moved and planned is null then clock_timestamp()
           else t.due_moved_at end
   where t.id = was.id
     and t.owner_telegram_id = owner_id
  returning t.* into saved;

  -- Срок, точность или вид изменились — расписание заново по §6.1:
  -- неотправленные уходят, ушедшая ступень, которая по новому сроку снова в
  -- будущем, взводится заново (как ответ на вопрос, §3.4). Правка сути,
  -- срочности, обещания и людей расписание не трогает.
  if replan then
    delete from public.reminders r
     where r.task_id = saved.id
       and r.owner_telegram_id = owner_id
       and r.sent_at is null;

    if planned is null then
      insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
      select owner_id, saved.id, p.stage, p.fire_at
        from public.reminder_plan(saved.due_at, saved.due_precision, saved.kind, owner_zone, now())
             as p
      on conflict on constraint reminders_task_id_stage_key do update
         set fire_at = excluded.fire_at,
             sent_at = null,
             telegram_message_id = null;
    else
      insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
      select owner_id, saved.id, p.stage, p.fire_at
        from jsonb_to_recordset(planned) as p(stage text, fire_at timestamptz)
      on conflict on constraint reminders_task_id_stage_key do update
         set fire_at = excluded.fire_at,
             sent_at = null,
             telegram_message_id = null;
    end if;
  end if;

  return saved;
end;
$$;

-- Зовут `edit_task` под токеном (она invoker, право нужно самой роли) и
-- правка из чата ключом service-role. Право execute выдано роли public и
-- наследуется из неё — забирается и там.
revoke execute on function public.change_task(bigint, uuid, jsonb, jsonb) from public, anon;
grant execute on function public.change_task(bigint, uuid, jsonb, jsonb)
  to authenticated, service_role;

-- Правка из приложения (§11.2) — обёртка над ядром: владелец из клейма,
-- план считает база, перенос ставит отметку. Сигнатура, права и поведение —
-- как в миграции 009; `create or replace` права сохраняет.
create or replace function public.edit_task(task_id uuid, changes jsonb)
returns public.tasks
language plpgsql
security invoker
as $$
begin
  return public.change_task(
    (auth.jwt() ->> 'telegram_id')::bigint,
    edit_task.task_id,
    edit_task.changes,
    null
  );
end;
$$;

-- Действие над задачей из чата (§12.3) — одно на `record_understanding` и
-- `pick_task`. edit — `{task_id, action, changes, schedule, question}`:
--   done, cancel — закрыть или убрать: статус и снятие неотправленных
--                  напоминаний, как `mark_task_done` (§3.5);
--   change + question — непонятно новое значение: ничего не меняется,
--                  задача получает пометку и открытый вопрос (§10.1);
--   change без changes — менять нечего: задача только проверяется;
--   change + changes — ядро `change_task` с готовым планом; нет плана —
--                  пустой: путь чата отметку «перенёс» не ставит никогда.
--
-- Задача не активна, чужая или её нет — null, ничего не записано: что с
-- этим делать, решает вызывающий.
create function public.edit_from_chat(owner_telegram_id bigint, edit jsonb)
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

  if verb in ('done', 'cancel') then
    update public.tasks t
       set status = case verb when 'done' then 'done' else 'cancelled' end
     where t.id = target_id
       and t.owner_telegram_id = edit_from_chat.owner_telegram_id
       and t.status = 'active'
    returning t.* into saved;

    if not found then
      return null;
    end if;

    delete from public.reminders r
     where r.task_id = saved.id
       and r.owner_telegram_id = edit_from_chat.owner_telegram_id
       and r.sent_at is null;

    return saved;
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

-- Шаг второй — с правкой задачи словом (§12.4). Тринадцатиаргументная
-- версия уходит: две перегрузки PostgREST различать нечем, а новый аргумент
-- необязательный, и прежний вызов бота по именам аргументов на новой версии
-- работает как раньше.
drop function public.record_understanding(
  uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric, jsonb
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
  edit jsonb default null
) returns public.tasks
language plpgsql
as $$
declare
  saved public.tasks;
  linked uuid;
  people_names text[] := '{}';
  changes jsonb;
  asked text;
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
         )
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

  -- Ответ на вопрос (§10.2): вместо новой задачи — поправка к названной.
  -- Меняются только ключи из `fields`; чужая, несуществующая или не активная
  -- (закрытая, убранная) задача — отказ, и вся транзакция, включая разбор и
  -- память, откатывается. Её закрыли или убрали, пока модель разбирала
  -- ответ, и напоминания по ней не уйдут (§6.2 берёт только активные) —
  -- «Напомню» в ответе было бы неправдой (инвариант 4).
  if record_understanding.amend is not null
     and jsonb_typeof(record_understanding.amend) = 'object' then
    changes := coalesce(record_understanding.amend -> 'fields', '{}'::jsonb);

    if jsonb_typeof(changes -> 'people') = 'array' then
      select coalesce(array_agg(person.name), '{}') into people_names
        from jsonb_array_elements_text(changes -> 'people') as person(name);
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
             else t.needs_review end
     where t.id = (record_understanding.amend ->> 'task_id')::uuid
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

  -- Правка задачи словом (§12.3): закрыть, убрать, поправить, спросить по
  -- правке или «менять нечего». Задачу закрыли или убрали, пока модель
  -- разбирала сообщение, — отказ и откат целиком, как у ответа на вопрос.
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

  insert into public.tasks (
    owner_telegram_id, title, kind, due_at, due_precision,
    priority, promise, people, needs_review, open_question, question_asked_at,
    source_message_id
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
    on conflict on constraint reminders_task_id_stage_key do nothing;
  end if;

  update public.messages m
     set task_id = saved.id
   where m.id = record_understanding.message_id
     and m.owner_telegram_id = record_understanding.owner_telegram_id;

  return saved;
end;
$$;

-- Выбор задачи кнопкой (§12.6). Сообщение владельца уже записано с разбором
-- и без задачи; нажатие пишет правку выбранной задачи, `task_id` и ответ
-- одной транзакцией. Строка сообщения держится до конца: два нажатия подряд
-- не пишут двух правок.
--
-- Отдаёт строку сообщения — по ней бот понимает, что вышло:
--   task_id и reply — его: правка записана этим вызовом;
--   task_id другой — правка по сообщению уже сделана раньше (второе
--          нажатие, другая кнопка): ничего не записано;
--   task_id пуст — выбранную задачу закрыли, убрали или удалили:
--          ничего не записано.
create function public.pick_task(
  owner_telegram_id bigint,
  message_id uuid,
  edit jsonb,
  reply text
) returns public.messages
language plpgsql
as $$
declare
  picked public.messages;
  saved public.tasks;
begin
  select m.* into picked
    from public.messages m
   where m.id = pick_task.message_id
     and m.owner_telegram_id = pick_task.owner_telegram_id
     for update;

  if not found then
    raise exception 'pick_task: message % is not owned by %',
      pick_task.message_id, pick_task.owner_telegram_id;
  end if;

  if picked.task_id is not null then
    return picked;
  end if;

  saved := public.edit_from_chat(pick_task.owner_telegram_id, pick_task.edit);
  if saved.id is null then
    return picked;
  end if;

  update public.messages m
     set task_id = saved.id,
         reply = pick_task.reply
   where m.id = picked.id
     and m.owner_telegram_id = pick_task.owner_telegram_id
  returning m.* into picked;

  return picked;
end;
$$;

-- «Вернуть» под «Закрыл» и «Убрал из списка» (§12.6): задача снова активна,
-- неотправленные напоминания заменены планом, ушедшая ступень, снова
-- оказавшаяся в будущем, взводится заново — как у `edit_task` при смене
-- срока. План считает бот у `reminder_plan` на момент нажатия и называет
-- его в ответе (инвариант 4).
--
-- Нет задачи (удалили) — null. Уже активная — как есть, без записи.
create function public.reopen_task(
  owner_telegram_id bigint,
  task_id uuid,
  schedule jsonb
) returns public.tasks
language plpgsql
as $$
declare
  saved public.tasks;
begin
  if coalesce(jsonb_typeof(reopen_task.schedule), 'array') <> 'array' then
    raise exception 'reopen_task: schedule must be an array';
  end if;

  select t.* into saved
    from public.tasks t
   where t.id = reopen_task.task_id
     and t.owner_telegram_id = reopen_task.owner_telegram_id
     for update;

  if not found then
    return null;
  end if;

  if saved.status = 'active' then
    return saved;
  end if;

  update public.tasks t
     set status = 'active'
   where t.id = saved.id
     and t.owner_telegram_id = reopen_task.owner_telegram_id
  returning t.* into saved;

  delete from public.reminders r
   where r.task_id = saved.id
     and r.owner_telegram_id = reopen_task.owner_telegram_id
     and r.sent_at is null;

  insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
  select reopen_task.owner_telegram_id, saved.id, p.stage, p.fire_at
    from jsonb_to_recordset(coalesce(reopen_task.schedule, '[]'::jsonb))
         as p(stage text, fire_at timestamptz)
  on conflict on constraint reminders_task_id_stage_key do update
     set fire_at = excluded.fire_at,
         sent_at = null,
         telegram_message_id = null;

  return saved;
end;
$$;

-- «Сделано» под напоминанием (§6.3): закрытая или убранная задача
-- возвращается как есть — статус не меняется, напоминания не трогаются
-- (§12.4). Прежде закрытая задача сначала теряла неотправленные
-- напоминания; у закрытой их и так нет.
create or replace function public.mark_task_done(
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

  if saved.status <> 'active' then
    return saved;
  end if;

  delete from public.reminders r
   where r.task_id = mark_task_done.task_id
     and r.owner_telegram_id = mark_task_done.owner_telegram_id
     and r.sent_at is null;

  update public.tasks t
     set status = 'done'
   where t.id = mark_task_done.task_id
     and t.owner_telegram_id = mark_task_done.owner_telegram_id
   returning t.* into saved;

  return saved;
end;
$$;

-- «Сделано» в приложении (§3.6) — то же правило под токеном владельца.
create or replace function public.complete_task(task_id uuid)
returns public.tasks
language plpgsql
security invoker
as $$
declare
  owner_id bigint := (auth.jwt() ->> 'telegram_id')::bigint;
  saved public.tasks;
begin
  if owner_id is null then
    return null;
  end if;

  select t.* into saved
    from public.tasks t
   where t.id = complete_task.task_id
     and t.owner_telegram_id = owner_id;

  if not found then
    return null;
  end if;

  if saved.status <> 'active' then
    return saved;
  end if;

  delete from public.reminders r
   where r.task_id = complete_task.task_id
     and r.owner_telegram_id = owner_id
     and r.sent_at is null;

  update public.tasks t
     set status = 'done'
   where t.id = complete_task.task_id
     and t.owner_telegram_id = owner_id
   returning t.* into saved;

  return saved;
end;
$$;

-- Эти четыре зовёт только бот ключом service-role, владелец — явным
-- аргументом. Право execute выдано роли public и из неё наследуется —
-- забирается и там.
revoke execute on function public.edit_from_chat(bigint, jsonb)
  from public, anon, authenticated;
grant execute on function public.edit_from_chat(bigint, jsonb)
  to service_role;

revoke execute on function
  public.record_understanding(
    uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb
  )
  from public, anon, authenticated;
grant execute on function
  public.record_understanding(
    uuid, bigint, jsonb, text, int, int, text, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb
  )
  to service_role;

revoke execute on function public.pick_task(bigint, uuid, jsonb, text)
  from public, anon, authenticated;
grant execute on function public.pick_task(bigint, uuid, jsonb, text)
  to service_role;

revoke execute on function public.reopen_task(bigint, uuid, jsonb)
  from public, anon, authenticated;
grant execute on function public.reopen_task(bigint, uuid, jsonb)
  to service_role;
