-- Повторяющиеся задачи: правило, «Сделано» и пропуск, пропущенный раз.
--
-- Схема — techspec/03-schema.md §3.3–3.6; поведение — §13. Применяется
-- `supabase db push` или тем же текстом в SQL-редакторе панели Supabase
-- (supabase/README.md). Применённая миграция не правится: следующая правка —
-- следующий файл.

-- Правило повтора в одном месте (§13.2). Форма строгая, лишнее — отказ:
--   every     — 'day' | 'week' | 'month' | 'year';
--   interval  — целое 1–99: каждый N-й день, неделя, месяц, год;
--   weekdays  — только у week: непустой список различных дней 1–7 (пн = 1);
--   month_day — у month: 1–31 или −1 (последний день); у year: 1–31, не
--               больше длины месяца (у февраля 29); у остальных пусто;
--   month     — только у year: 1–12;
--   time      — 'HH:MM' или пусто; ставит база из срока, не модель.
-- Пустое правило (`null`) проходит: у разовой задачи его нет.
--
-- Типы из jsonb читаются только после проверки вида: приведение строки к
-- числу посреди проверки сломало бы запись вместо честного `false`.
create function public.repeat_valid(rule jsonb)
returns boolean
language plpgsql
immutable
as $$
declare
  period text;
  value jsonb;
  amount numeric;
  day_of_month int;
  month_of_year int;
begin
  if rule is null or jsonb_typeof(rule) = 'null' then
    return true;
  end if;

  if jsonb_typeof(rule) <> 'object' then
    return false;
  end if;

  if exists (
    select 1
      from jsonb_object_keys(rule) as k(name)
     where k.name not in ('every', 'interval', 'weekdays', 'month_day', 'month', 'time')
  ) then
    return false;
  end if;

  if jsonb_typeof(rule -> 'every') is distinct from 'string' then
    return false;
  end if;
  period := rule ->> 'every';
  if period not in ('day', 'week', 'month', 'year') then
    return false;
  end if;

  value := rule -> 'interval';
  if jsonb_typeof(value) is distinct from 'number' then
    return false;
  end if;
  amount := (value #>> '{}')::numeric;
  if amount <> trunc(amount) or amount < 1 or amount > 99 then
    return false;
  end if;

  value := rule -> 'weekdays';
  if period = 'week' then
    if jsonb_typeof(value) is distinct from 'array' or jsonb_array_length(value) = 0 then
      return false;
    end if;
    if exists (
      select 1
        from jsonb_array_elements(value) as d(item)
       where not coalesce(
               case when jsonb_typeof(d.item) = 'number'
                 then (d.item #>> '{}')::numeric in (1, 2, 3, 4, 5, 6, 7)
                 else false end,
               false)
    ) then
      return false;
    end if;
    if (select count(distinct (d.item #>> '{}')::numeric)
          from jsonb_array_elements(value) as d(item)) <> jsonb_array_length(value) then
      return false;
    end if;
  elsif coalesce(jsonb_typeof(value), 'null') <> 'null' then
    return false;
  end if;

  value := rule -> 'month_day';
  if period in ('month', 'year') then
    if jsonb_typeof(value) is distinct from 'number' then
      return false;
    end if;
    amount := (value #>> '{}')::numeric;
    if amount <> trunc(amount) then
      return false;
    end if;
    if amount = -1 and period = 'month' then
      day_of_month := -1;
    elsif amount between 1 and 31 then
      day_of_month := amount::int;
    else
      return false;
    end if;
  elsif coalesce(jsonb_typeof(value), 'null') <> 'null' then
    return false;
  end if;

  value := rule -> 'month';
  if period = 'year' then
    if jsonb_typeof(value) is distinct from 'number' then
      return false;
    end if;
    amount := (value #>> '{}')::numeric;
    if amount <> trunc(amount) or amount < 1 or amount > 12 then
      return false;
    end if;
    month_of_year := amount::int;
    -- Високосный 2000 год даёт февралю 29 дней: «29 февраля» — законное
    -- правило, в обычный год его раз — 28-е (§13.2).
    if day_of_month > extract(day from make_date(2000, month_of_year, 1)
                                        + interval '1 month' - interval '1 day') then
      return false;
    end if;
  elsif coalesce(jsonb_typeof(value), 'null') <> 'null' then
    return false;
  end if;

  value := rule -> 'time';
  if coalesce(jsonb_typeof(value), 'null') <> 'null' then
    if jsonb_typeof(value) <> 'string'
       or (value #>> '{}') !~ '^([01][0-9]|2[0-3]):[0-5][0-9]$' then
      return false;
    end if;
  end if;

  return true;
end;
$$;

-- Правило в каноническом виде (§13.2): все шесть ключей, дни недели по
-- порядку, неположенные поля пусты, `time` — час срока в поясе владельца
-- (при точности `day` — пусто). Ни модель, ни приложение `time` не шлют:
-- присланный отбрасывается, час серии всегда берётся из срока.
create function public.repeat_rule(
  rule jsonb,
  due_at timestamptz,
  due_precision text,
  timezone text
) returns jsonb
language plpgsql
stable
as $$
declare
  period text;
  shape jsonb;
begin
  if rule is null or jsonb_typeof(rule) <> 'object' then
    raise exception 'repeat_rule: invalid repeat %', rule;
  end if;

  shape := rule - 'time';
  if not public.repeat_valid(shape) then
    raise exception 'repeat_rule: invalid repeat %', rule;
  end if;

  if repeat_rule.due_precision = 'time' and repeat_rule.timezone is null then
    raise exception 'repeat_rule: timezone is required';
  end if;

  period := shape ->> 'every';

  return jsonb_build_object(
    'every', period,
    'interval', (shape ->> 'interval')::numeric::int,
    'weekdays', case when period = 'week' then (
      select jsonb_agg(d.day order by d.day)
        from (select distinct (item #>> '{}')::numeric::int as day
                from jsonb_array_elements(shape -> 'weekdays') as w(item)) as d
    ) end,
    'month_day', case when period in ('month', 'year')
      then (shape ->> 'month_day')::numeric::int end,
    'month', case when period = 'year' then (shape ->> 'month')::numeric::int end,
    'time', case
      when repeat_rule.due_precision = 'time' and repeat_rule.due_at is not null
        then to_char(repeat_rule.due_at at time zone repeat_rule.timezone, 'HH24:MI')
    end
  );
end;
$$;

-- Следующий раз серии (§13.2): первый раз ряда строго позже `after`.
-- Ряд строится от раза `occurrence_at` в поясе владельца: дни — от дня
-- раза, недели — от понедельника недели раза, месяцы — от месяца раза,
-- годы — от года раза, с шагом `interval`. Час — `time` правила, без него
-- 18:00 (§3.3). Число больше длины месяца — последний день месяца.
--
-- Считается от раза, а не от срока: перенос одного раза серию не сдвигает.
-- В один день у ряда не больше одного раза, поэтому следующий — всегда на
-- дне позже дня раза, даже если час раза разошёлся с часом правила.
-- Далёкий `after` не перебирается по шагу: поиск начинается с периода чуть
-- раньше него. Не нашлось за десяток периодов — правило кривое, отказ.
create function public.repeat_next(
  repeat jsonb,
  occurrence_at timestamptz,
  after timestamptz,
  timezone text
) returns timestamptz
language plpgsql
stable
as $$
declare
  period text;
  step int;
  days int[];
  day_of_month int;
  month_of_year int;
  at_time time;
  base date;
  after_day date;
  week_start date;
  first_period bigint;
  n bigint;
  months bigint;
  year_of int;
  month_of int;
  last_day int;
  on_day date;
  weekday int;
  candidate timestamptz;
begin
  if repeat_next.repeat is null
     or jsonb_typeof(repeat_next.repeat) = 'null'
     or repeat_next.occurrence_at is null
     or repeat_next.after is null
     or repeat_next.timezone is null then
    return null;
  end if;

  if not public.repeat_valid(repeat_next.repeat) then
    raise exception 'repeat_next: invalid repeat %', repeat_next.repeat;
  end if;

  period := repeat_next.repeat ->> 'every';
  step := (repeat_next.repeat ->> 'interval')::numeric::int;
  day_of_month := (repeat_next.repeat ->> 'month_day')::numeric::int;
  month_of_year := (repeat_next.repeat ->> 'month')::numeric::int;
  at_time := coalesce((repeat_next.repeat ->> 'time')::time, time '18:00');

  if period = 'week' then
    select array_agg(d.day order by d.day) into days
      from (select distinct (item #>> '{}')::numeric::int as day
              from jsonb_array_elements(repeat_next.repeat -> 'weekdays') as w(item)) as d;
  end if;

  base := (repeat_next.occurrence_at at time zone repeat_next.timezone)::date;
  after_day := (repeat_next.after at time zone repeat_next.timezone)::date;
  week_start := base - (extract(isodow from base)::int - 1);

  first_period := greatest(0, floor(
    case period
      when 'day' then (after_day - base)::numeric
      when 'week' then (after_day - week_start)::numeric / 7
      when 'month' then ((extract(year from after_day) * 12 + extract(month from after_day))
                         - (extract(year from base) * 12 + extract(month from base)))::numeric
      else (extract(year from after_day) - extract(year from base))::numeric
    end / step
  )::bigint - 1);

  for n in first_period .. first_period + 10 loop
    if period = 'day' then
      on_day := base + (n * step)::int;
      candidate := (on_day + at_time) at time zone repeat_next.timezone;
      if on_day > base and candidate > repeat_next.after then
        return candidate;
      end if;
    elsif period = 'week' then
      foreach weekday in array days loop
        on_day := week_start + (n * step * 7)::int + (weekday - 1);
        candidate := (on_day + at_time) at time zone repeat_next.timezone;
        if on_day > base and candidate > repeat_next.after then
          return candidate;
        end if;
      end loop;
    else
      if period = 'month' then
        months := extract(year from base)::bigint * 12 + extract(month from base)::bigint - 1
                  + n * step;
        year_of := (months / 12)::int;
        month_of := (months % 12)::int + 1;
      else
        year_of := extract(year from base)::int + (n * step)::int;
        month_of := month_of_year;
      end if;
      last_day := extract(day from make_date(year_of, month_of, 1)
                                   + interval '1 month' - interval '1 day')::int;
      on_day := make_date(
        year_of,
        month_of,
        case when day_of_month = -1 then last_day else least(day_of_month, last_day) end
      );
      candidate := (on_day + at_time) at time zone repeat_next.timezone;
      if on_day > base and candidate > repeat_next.after then
        return candidate;
      end if;
    end if;
  end loop;

  raise exception 'repeat_next: no occurrence after % for %', repeat_next.after, repeat_next.repeat;
end;
$$;

-- Правило и раз (§13.2). Раз — момент ряда, на котором стоит задача; срок
-- (`due_at`) — момент этого раза, если его не переносили. Следующий раз
-- считается от раза, поэтому разовый перенос серию не сдвигает.
alter table public.tasks
  add column repeat jsonb,
  add column occurrence_at timestamptz,
  add constraint tasks_repeat_valid check (public.repeat_valid(repeat)),
  add constraint tasks_repeat_occurrence check ((repeat is null) = (occurrence_at is null)),
  add constraint tasks_repeat_needs_due
    check (repeat is null or (due_at is not null and kind = 'task'));

-- Проверку таблицы при правке под токеном выполняет сама роль — ей нужно
-- право на `repeat_valid`. Право execute выдано роли public и наследуется
-- из неё — забирается и там.
revoke execute on function public.repeat_valid(jsonb) from public, anon;
grant execute on function public.repeat_valid(jsonb) to authenticated, service_role;

revoke execute on function public.repeat_rule(jsonb, timestamptz, text, text) from public, anon;
grant execute on function public.repeat_rule(jsonb, timestamptz, text, text)
  to authenticated, service_role;

revoke execute on function public.repeat_next(jsonb, timestamptz, timestamptz, text)
  from public, anon;
grant execute on function public.repeat_next(jsonb, timestamptz, timestamptz, text)
  to authenticated, service_role;

-- Ядро «Сделано» и пропуска (§13.3) — одно на кнопку, приложение и слово.
--
-- Разовая задача закрывается, как раньше. Повторяющаяся переходит на
-- следующий раз: срок и раз — `next_at`, точность — по `time` правила,
-- неотправленные напоминания заменяются планом нового раза, ушедшие
-- ступени взводятся заново. Статус, пометка и вопрос не трогаются, отметка
-- «перенёс» не ставится.
--
-- occurrence — раз, от которого нажато (секунды Unix): задача стоит на
--              другом — возвращается как есть, второе нажатие и кнопка
--              прошлого раза через раз не перескакивают; null — текущий раз;
-- next_at, schedule — путь чата: следующий раз и план посчитал бот и назвал
--              их в ответе (инвариант 4); null — считает база по поясу из
--              `owner_settings`.
--
-- Не активная — как есть; чужая, несуществующая — null.
--
-- Под токеном владельца ядро не даёт больше, чем `edit_task` (§4.2):
-- владелец обязан совпасть с клеймом, готовые раз и план отбрасываются.
create function public.advance_task(
  owner_telegram_id bigint,
  task_id uuid,
  occurrence bigint default null,
  next_at timestamptz default null,
  schedule jsonb default null
) returns public.tasks
language plpgsql
security invoker
as $$
declare
  owner_id bigint := advance_task.owner_telegram_id;
  upcoming timestamptz := advance_task.next_at;
  planned jsonb := advance_task.schedule;
  was public.tasks;
  saved public.tasks;
  owner_zone text;
begin
  if current_user in ('authenticated', 'anon') then
    if owner_id is distinct from (auth.jwt() ->> 'telegram_id')::bigint then
      return null;
    end if;
    upcoming := null;
    planned := null;
  end if;

  if owner_id is null then
    return null;
  end if;

  if planned is not null and jsonb_typeof(planned) <> 'array' then
    raise exception 'advance_task: schedule must be an array';
  end if;

  -- Строка держится до конца транзакции: два нажатия подряд не переводят
  -- задачу дважды.
  select t.* into was
    from public.tasks t
   where t.id = advance_task.task_id
     and t.owner_telegram_id = owner_id
     for update;

  if not found then
    return null;
  end if;

  if was.status <> 'active' then
    return was;
  end if;

  if was.repeat is null then
    delete from public.reminders r
     where r.task_id = was.id
       and r.owner_telegram_id = owner_id
       and r.sent_at is null;

    update public.tasks t
       set status = 'done'
     where t.id = was.id
       and t.owner_telegram_id = owner_id
    returning t.* into saved;

    return saved;
  end if;

  if advance_task.occurrence is not null
     and advance_task.occurrence <> floor(extract(epoch from was.occurrence_at))::bigint then
    return was;
  end if;

  if upcoming is null or planned is null then
    select s.timezone into owner_zone
      from public.owner_settings s
     where s.owner_telegram_id = owner_id;

    if owner_zone is null then
      raise exception 'advance_task: owner has no timezone';
    end if;
  end if;

  if upcoming is null then
    upcoming := public.repeat_next(
      was.repeat, was.occurrence_at, greatest(was.occurrence_at, now()), owner_zone
    );
  end if;

  update public.tasks t
     set due_at = upcoming,
         occurrence_at = upcoming,
         due_precision = case when t.repeat ->> 'time' is null then 'day' else 'time' end
   where t.id = was.id
     and t.owner_telegram_id = owner_id
  returning t.* into saved;

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

  return saved;
end;
$$;

-- Зовут `complete_task` под токеном (она invoker, право нужно самой роли) и
-- бот ключом service-role.
revoke execute on function public.advance_task(bigint, uuid, bigint, timestamptz, jsonb)
  from public, anon;
grant execute on function public.advance_task(bigint, uuid, bigint, timestamptz, jsonb)
  to authenticated, service_role;

-- «Сделано» под напоминанием (§6.3, §13.3): callback несёт раз, с которым
-- ушло напоминание, у старых напоминаний раза нет. Прежняя перегрузка
-- уходит: двух PostgREST различать нечем, а вызов без раза работает и так.
drop function public.mark_task_done(bigint, uuid);

create function public.mark_task_done(
  owner_telegram_id bigint,
  task_id uuid,
  occurrence bigint default null
) returns public.tasks
language plpgsql
as $$
begin
  return public.advance_task(
    mark_task_done.owner_telegram_id, mark_task_done.task_id, mark_task_done.occurrence, null, null
  );
end;
$$;

revoke execute on function public.mark_task_done(bigint, uuid, bigint)
  from public, anon, authenticated;
grant execute on function public.mark_task_done(bigint, uuid, bigint) to service_role;

-- «Сделано» в приложении (§3.6, §13.3) — то же ядро под токеном владельца;
-- occurrence — раз, который видит приложение.
drop function public.complete_task(uuid);

create function public.complete_task(task_id uuid, occurrence bigint default null)
returns public.tasks
language plpgsql
security invoker
as $$
declare
  owner_id bigint := (auth.jwt() ->> 'telegram_id')::bigint;
begin
  if owner_id is null then
    return null;
  end if;

  return public.advance_task(owner_id, complete_task.task_id, complete_task.occurrence, null, null);
end;
$$;

-- Только Mini App с токеном (§3.6): у бота владелец явный — своя функция.
revoke execute on function public.complete_task(uuid, bigint) from public, anon, service_role;
grant execute on function public.complete_task(uuid, bigint) to authenticated;

-- Действие над задачей из чата (§12.3, §13.3). Сверх миграции 010 —
-- действие `skip` и повторяющиеся задачи:
--   разовая: done — закрыть; skip, cancel — убрать;
--   повторяющаяся: cancel — убрать всю серию, правило остаётся и «Вернуть»
--        её возвращает; done, skip — переход на следующий раз через ядро
--        `advance_task` с готовыми `next_at` и `schedule` бота. Без
--        `next_at` — отказ: сказать «Следующий раз» было бы нечего.
--        `occurrence` (секунды Unix) — раз, от которого считал бот; задача
--        стоит на другом или раз не назван — null, как у закрытой задачи.
--
-- Остальное — как в миграции 010.
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

-- Ядро правки (§12.4, §13.5). Сверх миграции 010 — ключ `repeat`:
--   объект — поставить или сменить правило: канон `repeat_rule` по сроку
--            после правки, раз — этот срок, час серии — его час. Названный
--            срок выигрывает; не назван — первым разом становится текущий.
--            Без срока после правки или у идеи и желания — отказ целиком;
--   null   — снять: задача становится разовой с текущим сроком.
-- Без ключа перенос срока меняет только этот раз: правило и раз остаются.
-- Срок снят или вид сменился на идею, желание — правило снимается тоже:
-- повторять нечего.
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
     'title', 'due_at', 'due_date', 'kind', 'priority', 'promise', 'people', 'repeat'
   )
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

-- Шаг второй (§5.3, §13.5). Сверх миграции 010 — правило:
--   task.repeat   — правило новой задачи вместе со сроком первого раза;
--   amend.fields.repeat — ответ на вопрос даёт правило вместе со сроком
--                   (объект — поставить, null — снять).
-- Правило канонизирует `repeat_rule` по сроку и поясу владельца; первый раз —
-- срок. Правило без срока или у идеи и желания бот отбрасывает сам, база
-- на такое отвечает отказом. Сигнатура и остальное — как в миграции 010.
create or replace function public.record_understanding(
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

-- «Вернуть» под «Отметил» и «Пропускаю» (§13.3): задача назад на раз
-- `back_to` в час и точность серии (разовый перенос не восстанавливается),
-- только если она активна, повторяется и стоит на разе `moved_from`; оба —
-- секунды Unix из callback. План считает бот у `reminder_plan` на момент
-- нажатия, как у `reopen_task`.
--
-- Иначе — как есть: задача ушла дальше, её убрали или сняли правило; бот
-- узнаёт исход по разу в ответе. Нет задачи — null.
create function public.return_occurrence(
  owner_telegram_id bigint,
  task_id uuid,
  back_to bigint,
  moved_from bigint,
  schedule jsonb
) returns public.tasks
language plpgsql
as $$
declare
  saved public.tasks;
begin
  if coalesce(jsonb_typeof(return_occurrence.schedule), 'array') <> 'array' then
    raise exception 'return_occurrence: schedule must be an array';
  end if;

  select t.* into saved
    from public.tasks t
   where t.id = return_occurrence.task_id
     and t.owner_telegram_id = return_occurrence.owner_telegram_id
     for update;

  if not found then
    return null;
  end if;

  if saved.status <> 'active'
     or saved.repeat is null
     or floor(extract(epoch from saved.occurrence_at))::bigint
        is distinct from return_occurrence.moved_from then
    return saved;
  end if;

  update public.tasks t
     set due_at = to_timestamp(return_occurrence.back_to),
         occurrence_at = to_timestamp(return_occurrence.back_to),
         due_precision = case when t.repeat ->> 'time' is null then 'day' else 'time' end
   where t.id = saved.id
     and t.owner_telegram_id = return_occurrence.owner_telegram_id
  returning t.* into saved;

  delete from public.reminders r
   where r.task_id = saved.id
     and r.owner_telegram_id = return_occurrence.owner_telegram_id
     and r.sent_at is null;

  insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
  select return_occurrence.owner_telegram_id, saved.id, p.stage, p.fire_at
    from jsonb_to_recordset(coalesce(return_occurrence.schedule, '[]'::jsonb))
         as p(stage text, fire_at timestamptz)
  on conflict on constraint reminders_task_id_stage_key do update
     set fire_at = excluded.fire_at,
         sent_at = null,
         telegram_message_id = null;

  return saved;
end;
$$;

-- Пропущенный раз (§13.4): просроченная повторяющаяся задача стоит на своём
-- разе до начала следующего и тогда молча переходит на него. Начало раза —
-- min(полночь его дня, ступень «заранее») в поясе владельца: у повтора днём
-- это полночь, у «каждый день в 00:30» — 23:30 накануне.
--
-- После простоя — сразу на последний наступивший раз. Следующий считается
-- от max(раз, срок): раз, перенесённый позже следующего, этот следующий
-- поглощает. У нового раза планируются обе ступени — план берётся на миг
-- раньше его начала, созревшие уйдут тем же тиком (§6.2). Отметка «перенёс»
-- не ставится. Двигаются только активные задачи; нет пояса — ничего.
--
-- Отдаёт перекатанные задачи.
create function public.roll_repeats(owner_telegram_id bigint, now timestamptz)
returns setof public.tasks
language plpgsql
as $$
declare
  owner_zone text;
  item public.tasks;
  saved public.tasks;
  current_at timestamptz;
  upcoming timestamptz;
  starts timestamptz;
  steps int;
begin
  select s.timezone into owner_zone
    from public.owner_settings s
   where s.owner_telegram_id = roll_repeats.owner_telegram_id;

  if owner_zone is null then
    return;
  end if;

  for item in
    select t.*
      from public.tasks t
     where t.owner_telegram_id = roll_repeats.owner_telegram_id
       and t.status = 'active'
       and t.repeat is not null
       and t.due_at < roll_repeats.now
     order by t.due_at
       for update
  loop
    current_at := null;
    steps := 0;
    upcoming := public.repeat_next(
      item.repeat, item.occurrence_at, greatest(item.occurrence_at, item.due_at), owner_zone
    );

    loop
      starts := least(
        ((upcoming at time zone owner_zone)::date)::timestamp at time zone owner_zone,
        case when item.repeat ->> 'time' is null then upcoming
             else upcoming - interval '1 hour' end
      );
      exit when starts > roll_repeats.now;

      current_at := upcoming;
      upcoming := public.repeat_next(item.repeat, current_at, current_at, owner_zone);
      steps := steps + 1;
      if steps > 100000 then
        raise exception 'roll_repeats: task % does not settle', item.id;
      end if;
    end loop;

    continue when current_at is null;

    update public.tasks t
       set due_at = current_at,
           occurrence_at = current_at,
           due_precision = case when t.repeat ->> 'time' is null then 'day' else 'time' end
     where t.id = item.id
       and t.owner_telegram_id = roll_repeats.owner_telegram_id
    returning t.* into saved;

    delete from public.reminders r
     where r.task_id = saved.id
       and r.owner_telegram_id = roll_repeats.owner_telegram_id
       and r.sent_at is null;

    starts := least(
      ((current_at at time zone owner_zone)::date)::timestamp at time zone owner_zone,
      case when saved.due_precision = 'time' then current_at - interval '1 hour'
           else current_at end
    );

    insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
    select roll_repeats.owner_telegram_id, saved.id, p.stage, p.fire_at
      from public.reminder_plan(
             saved.due_at, saved.due_precision, saved.kind, owner_zone,
             starts - interval '1 second'
           ) as p
    on conflict on constraint reminders_task_id_stage_key do update
       set fire_at = excluded.fire_at,
           sent_at = null,
           telegram_message_id = null;

    return next saved;
  end loop;

  return;
end;
$$;

-- Запрос тика (§6.2) — с правилом и разом: кнопка «Сделано» несёт раз
-- (§13.3). Колонки добавлены в конец; `returns table` не меняется на месте.
drop function public.due_reminders(bigint, timestamptz);

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
  due_precision text,
  repeat jsonb,
  occurrence_at timestamptz
)
language sql
as $$
  select r.id, r.task_id, r.stage, r.fire_at, t.title, t.due_at, t.due_precision,
         t.repeat, t.occurrence_at
    from public.reminders r
    join public.tasks t on t.id = r.task_id
   where r.owner_telegram_id = due_reminders.owner_telegram_id
     and r.sent_at is null
     and r.fire_at <= due_reminders.now
     and t.status = 'active'
   order by r.fire_at;
$$;

-- Строка «Перенёс» (§11.4) — с правилом и разом: у повторяющейся задачи
-- перенесён только этот раз.
drop function public.moved_tasks(bigint);

create function public.moved_tasks(owner_telegram_id bigint)
returns table (
  id uuid,
  title text,
  due_at timestamptz,
  due_precision text,
  due_moved_at timestamptz,
  next_fire_at timestamptz,
  repeat jsonb,
  occurrence_at timestamptz
)
language sql
stable
as $$
  select t.id, t.title, t.due_at, t.due_precision, t.due_moved_at,
         (select min(r.fire_at)
            from public.reminders r
           where r.task_id = t.id
             and r.owner_telegram_id = moved_tasks.owner_telegram_id
             and r.sent_at is null),
         t.repeat, t.occurrence_at
    from public.tasks t
   where t.owner_telegram_id = moved_tasks.owner_telegram_id
     and t.status = 'active'
     and t.due_moved_at is not null
   order by t.due_moved_at;
$$;

-- Эти зовёт только бот ключом service-role, владелец — явным аргументом.
-- Право execute выдано роли public и наследуется из неё — забирается и там.
revoke execute on function public.return_occurrence(bigint, uuid, bigint, bigint, jsonb)
  from public, anon, authenticated;
grant execute on function public.return_occurrence(bigint, uuid, bigint, bigint, jsonb)
  to service_role;

revoke execute on function public.roll_repeats(bigint, timestamptz)
  from public, anon, authenticated;
grant execute on function public.roll_repeats(bigint, timestamptz) to service_role;

revoke execute on function public.due_reminders(bigint, timestamptz)
  from public, anon, authenticated;
grant execute on function public.due_reminders(bigint, timestamptz) to service_role;

revoke execute on function public.moved_tasks(bigint)
  from public, anon, authenticated;
grant execute on function public.moved_tasks(bigint) to service_role;
