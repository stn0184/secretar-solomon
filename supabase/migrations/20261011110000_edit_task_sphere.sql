-- Сфера дела из карточки приложения (этап 034).
--
-- Схема — techspec/03-schema.md §3.6; поведение — techspec/32-redesign.md §32.2,
-- techspec/11-edit.md §11.2. Применяется `supabase db push` или тем же текстом в
-- SQL-редакторе панели Supabase (supabase/README.md). Применённая миграция не
-- правится: следующая правка — следующий файл.
--
-- «Изменить» в карточке правит и сферу дела: выбор из живых сфер владельца или
-- «без сферы». Правка идёт тем же ядром `change_task`, что и остальные поля, —
-- через `edit_task` из приложения; новых функций и таблиц нет, права прежние.

-- Ядро правки (§12.4, §13.5) — заново, с той же подписью. Сверх
-- `20261010100000_hours.sql` — ключ `sphere_id`: id живой сферы этого
-- владельца строкой или `null` — снять. Не строка и не uuid — отказ
-- `change_task: invalid sphere_id`; чужая, убранная или несуществующая сфера —
-- `change_task: unknown sphere`; правка не проходит целиком. Сфера расписание
-- не трогает и переносом не считается. Остальное тело — как было.
create or replace function public.change_task(
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
  new_repeat jsonb;
  new_occurrence timestamptz;
  new_duration smallint;
  new_sphere uuid;
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
   where k.name not in (
     'title', 'due_at', 'due_date', 'due_precision', 'kind', 'priority', 'promise',
     'people', 'repeat', 'duration', 'sphere_id'
   )
   limit 1;
  if stray is not null then
    raise exception 'change_task: unknown field %', stray;
  end if;

  if fields ? 'due_at' and fields ? 'due_date' then
    raise exception 'change_task: due_at and due_date are exclusive';
  end if;

  -- Точность срока (§21.2) — только вместе с непустым моментом: `time` (то
  -- же, что без ключа) или часть дня. День ставится ключом `due_date`, а
  -- снятый срок точности не имеет: такая правка не проходит целиком.
  if fields ? 'due_precision' then
    if fields ? 'due_date' then
      raise exception 'change_task: due_precision goes with due_at, not due_date';
    end if;
    if coalesce(jsonb_typeof(fields -> 'due_at'), 'null') = 'null' then
      raise exception 'change_task: due_precision needs a due_at';
    end if;
    if coalesce(jsonb_typeof(fields -> 'due_precision'), 'null') <> 'string'
       or fields ->> 'due_precision' not in ('time', 'morning', 'afternoon', 'evening') then
      raise exception 'change_task: invalid due_precision %', fields -> 'due_precision';
    end if;
  end if;

  -- Длительность встречи (§31.1) — целые минуты от 1 до 1440 или `null`:
  -- снять. У срока без часа её снимет триггер, ключ не нужен.
  if fields ? 'duration'
     and coalesce(jsonb_typeof(fields -> 'duration'), 'null') <> 'null'
     and (jsonb_typeof(fields -> 'duration') <> 'number'
          or (fields ->> 'duration') !~ '^[0-9]+$'
          or (fields ->> 'duration')::integer not between 1 and 1440) then
    raise exception 'change_task: invalid duration %', fields -> 'duration';
  end if;

  -- Сфера дела (§32.2) — id сферы строкой или `null`: снять. Не строка и не
  -- uuid — отказ здесь, до чтения задачи; есть ли такая живая сфера у владельца
  -- — ниже.
  if fields ? 'sphere_id'
     and coalesce(jsonb_typeof(fields -> 'sphere_id'), 'null') <> 'null'
     and (jsonb_typeof(fields -> 'sphere_id') <> 'string'
          or (fields ->> 'sphere_id')
             !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') then
    raise exception 'change_task: invalid sphere_id %', fields -> 'sphere_id';
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
      -- Часть дня приходит с началом части в `due_at` (§21.2): его ставит
      -- бот, база кладёт как есть.
      new_precision := coalesce(fields ->> 'due_precision', 'time');
    end if;
  end if;

  new_kind := case when fields ? 'kind' then fields ->> 'kind' else was.kind end;
  new_duration := case
    when fields ? 'duration' then (fields ->> 'duration')::smallint
    else was.duration end;
  new_priority := case when fields ? 'priority' then fields ->> 'priority' else was.priority end;

  -- Сфера — только живая и только этого владельца: чужую не пустит и
  -- составной ключ, но убранную он пропустил бы, а отказ должен быть словами.
  -- Под ролью приложения чужие сферы ещё и скрыты правилом доступа.
  new_sphere := was.sphere_id;
  if fields ? 'sphere_id' then
    new_sphere := (fields ->> 'sphere_id')::uuid;
    if new_sphere is not null and not exists (
      select 1
        from public.spheres s
       where s.id = new_sphere
         and s.owner_telegram_id = owner_id
         and s.removed_at is null
    ) then
      raise exception 'change_task: unknown sphere %', new_sphere;
    end if;
  end if;
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

  if coalesce(jsonb_typeof(fields -> 'repeat'), 'null') <> 'null' then
    if new_due is null or new_kind is distinct from 'task' then
      raise exception 'change_task: repeat needs a due date of a task';
    end if;
    if owner_zone is null then
      raise exception 'change_task: owner has no timezone';
    end if;
    new_repeat := public.repeat_rule(fields -> 'repeat', new_due, new_precision, owner_zone);
    new_occurrence := new_due;
  elsif fields ? 'repeat' or new_due is null or new_kind is distinct from 'task' then
    new_repeat := null;
    new_occurrence := null;
  else
    new_repeat := was.repeat;
    new_occurrence := was.occurrence_at;
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
         repeat = new_repeat,
         occurrence_at = new_occurrence,
         duration = new_duration,
         sphere_id = new_sphere,
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
  -- срочности, обещания, людей и правила расписание не трогает.
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

-- `create or replace` права не трогает; они повторены, чтобы файл читался
-- сам по себе.
revoke execute on function public.change_task(bigint, uuid, jsonb, jsonb) from public, anon;
grant execute on function public.change_task(bigint, uuid, jsonb, jsonb)
  to authenticated, service_role;
