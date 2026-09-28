-- Правка задачи из приложения: расписание в базе, пояс владельца, отметка
-- «срок перенесён».
--
-- Схема — techspec/03-schema.md §3.3, §3.5, §3.6, §3.8; поведение — §11.
-- Применяется `supabase db push` или тем же текстом в SQL-редакторе панели
-- Supabase (supabase/README.md). Применённая миграция не правится: следующая
-- правка — следующий файл.

-- Пояс владельца (§11.3). Источник правды — OWNER_TIMEZONE в .env бота;
-- база держит его зеркало, потому что edit_task исполняется под ролью
-- владельца и окружения бота не видит. Бот пишет пояс при каждом запуске.
create table public.owner_settings (
  owner_telegram_id bigint primary key,
  -- имя пояса IANA: «Asia/Yekaterinburg»
  timezone text not null,
  updated_at timestamptz not null default now()
);

-- Правила доступа (§4.2): владелец видит только свою строку. Для anon
-- политик нет намеренно.
alter table public.owner_settings enable row level security;

create policy "owner_settings: owner only"
  on public.owner_settings
  for all
  to authenticated
  using      (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint)
  with check (owner_telegram_id = (auth.jwt() ->> 'telegram_id')::bigint);

-- Запись пояса ботом. Имя проверяет сама база: незнакомый пояс — отказ
-- здесь, при запуске, а не на первой правке срока.
--
-- Цель конфликта названа ограничением, а не колонкой: в plpgsql
-- `on conflict (owner_telegram_id)` совпадает с именем параметра.
create function public.save_owner_timezone(
  owner_telegram_id bigint,
  timezone text
) returns public.owner_settings
language plpgsql
as $$
declare
  saved public.owner_settings;
begin
  perform now() at time zone save_owner_timezone.timezone;

  insert into public.owner_settings as s (owner_telegram_id, timezone)
  values (save_owner_timezone.owner_telegram_id, save_owner_timezone.timezone)
  on conflict on constraint owner_settings_pkey do update
     set timezone = excluded.timezone,
         updated_at = now()
  returning s.* into saved;

  return saved;
end;
$$;

-- Расписание напоминаний (§6.1) — одно место на бота и приложение (§11.3).
-- Чистая функция: таблиц не читает, владельца не знает, «сейчас» и пояс —
-- аргументами, поэтому все ветки проверяются без часов.
--
-- Пусто — стучаться не о чем или некогда: не задача, нет срока, срок уже
-- прошёл. Точность time — за час и в срок; день (и пустая точность) —
-- в 09:00 дня срока по поясу владельца и в срок. Момент, который уже
-- прошёл, не заводится; утро не раньше срока — тоже.
create function public.reminder_plan(
  due_at timestamptz,
  due_precision text,
  kind text,
  timezone text,
  now timestamptz
) returns table (stage text, fire_at timestamptz)
language sql
stable
as $$
  with planned as (
    select reminder_plan.due_at as due,
           case
             when reminder_plan.due_precision = 'time'
               then reminder_plan.due_at - interval '1 hour'
             -- календарный день срока — в поясе владельца, а не в UTC
             else ((reminder_plan.due_at at time zone reminder_plan.timezone)::date
                   + time '09:00') at time zone reminder_plan.timezone
           end as before
     where reminder_plan.kind = 'task'
       and reminder_plan.due_at is not null
       and reminder_plan.due_at > reminder_plan.now
  )
  select 'before'::text, p.before
    from planned p
   where reminder_plan.now < p.before
     and p.before < p.due
  union all
  select 'due'::text, p.due
    from planned p
  order by 2
$$;

-- Зовут бот (запись поручения и ответ на вопрос) и edit_task под ролью
-- владельца. Право execute выдано роли public и наследуется из неё —
-- забирается и там.
revoke execute on function public.reminder_plan(timestamptz, text, text, text, timestamptz)
  from public, anon;
grant execute on function public.reminder_plan(timestamptz, text, text, text, timestamptz)
  to authenticated, service_role;

-- Отметка «срок перенесён» (§11.4): момент правки, которая сдвинула срок.
-- Бот пишет строку в чат и снимает отметку, только если она всё ещё та,
-- что он прочитал, — правка между чтением и снятием не теряется.
alter table public.tasks
  add column due_moved_at timestamptz;

-- Под запрос минутного цикла: задачи владельца с отметкой. Строки без
-- отметки в индекс не попадают — их подавляющее большинство.
create index tasks_owner_due_moved_idx
  on public.tasks (owner_telegram_id)
  where due_moved_at is not null;

-- Правка задачи из Mini App (§11.2). Как complete_task (§3.6): владелец
-- из клейма токена, security invoker — каждая строка идёт через RLS, и
-- явный фильтр по владельцу сверх того (инвариант 2).
--
-- changes — только изменённые поля: title, kind, priority, promise,
-- people и срок одним из ключей: due_at — момент со смещением (точность
-- time), due_date — день YYYY-MM-DD (точность day, 18:00 этого дня в
-- поясе владельца), due_at = null — срока нет. Незнакомый ключ, оба
-- ключа срока сразу, пустая суть — отказ целиком.
--
-- Чужая, несуществующая, закрытая задача и токен без клейма — null,
-- ничего не записано.
create function public.edit_task(task_id uuid, changes jsonb)
returns public.tasks
language plpgsql
security invoker
as $$
declare
  owner_id bigint := (auth.jwt() ->> 'telegram_id')::bigint;
  fields jsonb := coalesce(edit_task.changes, '{}'::jsonb);
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
  if owner_id is null then
    return null;
  end if;

  if jsonb_typeof(fields) <> 'object' then
    raise exception 'edit_task: changes must be an object';
  end if;

  select k.name into stray
    from jsonb_object_keys(fields) as k(name)
   where k.name not in ('title', 'due_at', 'due_date', 'kind', 'priority', 'promise', 'people')
   limit 1;
  if stray is not null then
    raise exception 'edit_task: unknown field %', stray;
  end if;

  if fields ? 'due_at' and fields ? 'due_date' then
    raise exception 'edit_task: due_at and due_date are exclusive';
  end if;

  -- Только активная задача, и строка держится до конца транзакции: правка
  -- и «Сделано» из чата не перекрещиваются.
  select t.* into was
    from public.tasks t
   where t.id = edit_task.task_id
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
      raise exception 'edit_task: title is empty';
    end if;
  end if;

  new_due := was.due_at;
  new_precision := was.due_precision;
  if fields ? 'due_date' then
    -- День без часа: 18:00 этого дня в поясе владельца (§3.3); календарный
    -- день — как выбран, без пересчёта через пояс (§11.3).
    if owner_zone is null then
      raise exception 'edit_task: owner has no timezone';
    end if;
    new_due := ((fields ->> 'due_date')::date + time '18:00') at time zone owner_zone;
    new_precision := 'day';
  elsif fields ? 'due_at' then
    if jsonb_typeof(fields -> 'due_at') = 'null' then
      new_due := null;
      new_precision := null;
    else
      -- Момент без смещения база прочла бы в своём поясе, а час в форме —
      -- по часам телефона: такой срок молча съехал бы.
      if (fields ->> 'due_at') !~ '([Zz]|[+-][0-9]{2}(:?[0-9]{2})?)$' then
        raise exception 'edit_task: due_at needs an offset';
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
      raise exception 'edit_task: people must be an array';
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
  -- записанная молча, хуже отказа (§11.3).
  if replan and owner_zone is null then
    raise exception 'edit_task: owner has no timezone';
  end if;

  -- Сохранение — это «я проверил» (§11.2): пометка и вопрос снимаются у
  -- этой задачи, даже без изменений. Вопросы других задач не трогаются.
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
         due_moved_at = case when moved then clock_timestamp() else t.due_moved_at end
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

    insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
    select owner_id, saved.id, planned.stage, planned.fire_at
      from public.reminder_plan(saved.due_at, saved.due_precision, saved.kind, owner_zone, now())
           as planned
    on conflict on constraint reminders_task_id_stage_key do update
       set fire_at = excluded.fire_at,
           sent_at = null,
           telegram_message_id = null;
  end if;

  return saved;
end;
$$;

-- Зовёт только Mini App с токеном: у бота владелец явный, а service_role
-- клейма не несёт — ему эта функция ни к чему.
revoke execute on function public.edit_task(uuid, jsonb) from public, anon, service_role;
grant execute on function public.edit_task(uuid, jsonb) to authenticated;

-- Строка «Перенёс» (§11.4): активные задачи владельца с отметкой — что
-- лежит в базе в момент чтения, и ближайшее неотправленное напоминание.
-- Закрытая или удалённая задача сюда не попадает.
create function public.moved_tasks(owner_telegram_id bigint)
returns table (
  id uuid,
  title text,
  due_at timestamptz,
  due_precision text,
  due_moved_at timestamptz,
  next_fire_at timestamptz
)
language sql
stable
as $$
  select t.id, t.title, t.due_at, t.due_precision, t.due_moved_at,
         (select min(r.fire_at)
            from public.reminders r
           where r.task_id = t.id
             and r.owner_telegram_id = moved_tasks.owner_telegram_id
             and r.sent_at is null)
    from public.tasks t
   where t.owner_telegram_id = moved_tasks.owner_telegram_id
     and t.status = 'active'
     and t.due_moved_at is not null
   order by t.due_moved_at;
$$;

-- Снять отметку после отправки строки — только ту, что бот прочитал.
-- Правка, пришедшая между чтением и снятием, отметку сменила: она остаётся,
-- и следующий цикл напишет о последнем сроке. true — снята.
create function public.clear_due_moved(
  owner_telegram_id bigint,
  task_id uuid,
  seen timestamptz
) returns boolean
language sql
as $$
  with cleared as (
    update public.tasks t
       set due_moved_at = null
     where t.id = clear_due_moved.task_id
       and t.owner_telegram_id = clear_due_moved.owner_telegram_id
       and t.due_moved_at = clear_due_moved.seen
    returning t.id
  )
  select exists (select 1 from cleared);
$$;

-- Эти три зовёт только бот ключом service-role, владелец — явным аргументом.
revoke execute on function public.save_owner_timezone(bigint, text)
  from public, anon, authenticated;
grant execute on function public.save_owner_timezone(bigint, text)
  to service_role;

revoke execute on function public.moved_tasks(bigint)
  from public, anon, authenticated;
grant execute on function public.moved_tasks(bigint)
  to service_role;

revoke execute on function public.clear_due_moved(bigint, uuid, timestamptz)
  from public, anon, authenticated;
grant execute on function public.clear_due_moved(bigint, uuid, timestamptz)
  to service_role;
