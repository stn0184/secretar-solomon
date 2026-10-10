-- Часы по сферам в базе (этап 035): одна функция счёта на бота и приложение.
--
-- Схема — techspec/03-schema.md §3.18; поведение — techspec/31-hours.md и
-- techspec/32-redesign.md §32.3; доступ — techspec/04-access.md §4.2.
-- Применяется `supabase db push` или тем же текстом в SQL-редакторе панели
-- Supabase (supabase/README.md). Применённая миграция не правится: следующая
-- правка — следующий файл.
--
-- До этапа часы считал бот (`services/hours.py`), и приложению они были не
-- видны. Теперь счёт живёт здесь, правила прежние (§31.1): `sphere_hours` —
-- минуты сфер за любое окно с разбивкой «встречи · переписка», `day_spans` —
-- отрезки для нити дня и недели. Окна — сегодня, вчера, неделя, день недели —
-- собирает тот, кто зовёт. Обе функции — определители: таблицы переписки
-- приложению закрыты, и текст сообщений до него не доходит — только время и
-- сфера отрезков.

-- --- Переписка закрыта приложению ----------------------------------------------

-- Приложение переписку не читало и раньше (§4.2), но правило доступа пустило бы
-- его к своим строкам — с текстом сообщений, пересказом «ждёт ответа» и ответом
-- модели. Решение владельца (2026-10-11): текст переписки приложению недоступен
-- совсем. Бот пишет и читает эти таблицы ключом service-role, функции часов —
-- от своего владельца; правило доступа на таблицах остаётся.
revoke all on table public.chat_messages, public.chat_threads, public.chat_analyses
  from anon, authenticated;

-- --- Владелец вызова -------------------------------------------------------------

-- Чьи часы считать. Приложение владельца не передаёт: он — клейм `telegram_id`
-- токена, а переданный и другой — отказ. Без клейма владелец — параметр, но
-- только у бота (service-role) и в SQL-редакторе: роль приложения или `anon`
-- без клейма — отказ, как и пустой параметр. Внутри определителя
-- `current_setting('role')` — роль сессии, которую поставил PostgREST, а не
-- владелец функции. Отказ — 42501, PostgREST отдаёт 403.
create function public.hours_owner(owner_telegram_id bigint)
returns bigint
language plpgsql
stable
set search_path = ''
as $$
declare
  claimed bigint := (auth.jwt() ->> 'telegram_id')::bigint;
begin
  if claimed is not null then
    if hours_owner.owner_telegram_id is not null
       and hours_owner.owner_telegram_id <> claimed then
      raise exception 'hours: owner refused' using errcode = '42501';
    end if;
    return claimed;
  end if;

  if coalesce(current_setting('role', true), 'none') in ('authenticated', 'anon')
     or hours_owner.owner_telegram_id is null then
    raise exception 'hours: owner refused' using errcode = '42501';
  end if;
  return hours_owner.owner_telegram_id;
end;
$$;

-- --- Сессии переписки ----------------------------------------------------------------

-- Сессии личных чатов владельца (§31.1) по сообщениям не раньше `since`: сообщения
-- чата по времени площадки, следующее не дальше 10 минут продолжает сессию;
-- длина — от первого до последнего и ещё 2 минуты; без сообщения владельца
-- (`out`) сессии нет. Сфера — нынешняя сфера чата: смена сферы чата переносит и
-- его прошлые часы; убранная сфера — без сферы. Стёртые сроком сообщения
-- считаются: время и направление у них остаются (§3.13).
create function public.chat_sessions(owner_telegram_id bigint, since timestamptz)
returns table (sphere_id uuid, starts_at timestamptz, ends_at timestamptz)
language sql
stable
set search_path = ''
as $$
  with stamps as (
    select m.id, m.thread_id, m.sent_at, m.direction,
           case
             when lag(m.sent_at) over w is null
               or m.sent_at - lag(m.sent_at) over w > interval '10 minutes' then 1
             else 0
           end as fresh
      from public.chat_messages m
     where m.owner_telegram_id = chat_sessions.owner_telegram_id
       and m.sent_at >= chat_sessions.since
    window w as (partition by m.thread_id order by m.sent_at, m.id)
  ),
  numbered as (
    select s.thread_id, s.sent_at, s.direction,
           sum(s.fresh) over (
             partition by s.thread_id order by s.sent_at, s.id rows unbounded preceding
           ) as session
      from stamps s
  )
  select live.id, min(n.sent_at), max(n.sent_at) + interval '2 minutes'
    from numbered n
    join public.chat_threads th
      on th.id = n.thread_id
     and th.owner_telegram_id = chat_sessions.owner_telegram_id
    left join public.spheres live
      on live.id = th.sphere_id
     and live.owner_telegram_id = chat_sessions.owner_telegram_id
     and live.removed_at is null
   group by n.thread_id, n.session, live.id
  having bool_or(n.direction = 'out')
$$;

-- --- Часы за окно -------------------------------------------------------------------

-- Минуты сфер владельца за окно `[window_start, window_end)` (§31.1–31.2). Встреча —
-- дело с длительностью, `active` или `done`, отрезок от срока на длительность;
-- переписка — сессии. Отрезки сферы объединяются: пересечение внутри сферы
-- считается один раз и встречей (переписка во время встречи — её часть), разные
-- сферы не вычитаются. Правый край — не позже «сейчас»: не прошедшее не
-- считается, какое бы окно ни передали. Запас чтения — сутки до окна: встреча
-- длится до суток, сессия задевает окно концом. Сообщения читаются до «сейчас»,
-- а не до края окна: сессия через край режется, а не обрывается.
--
-- Минуты округляются у итога сферы и у встреч, переписка — их разность: части в
-- сумме дают итог. Строки — только сферы с минутами: живые по порядку заведения,
-- «без сферы» (`sphere_id` пусто) — последней. Как прежний счёт бота.
create function public.sphere_hours(
  window_start timestamptz,
  window_end timestamptz,
  owner_telegram_id bigint default null
) returns table (sphere_id uuid, name text, meetings integer, chat integer)
language plpgsql
stable
security definer
set search_path = ''
as $$
#variable_conflict use_column
declare
  owner_id bigint := public.hours_owner(sphere_hours.owner_telegram_id);
  head timestamptz := sphere_hours.window_start;
  tail timestamptz := least(sphere_hours.window_end, now());
begin
  if head is null or sphere_hours.window_end is null then
    raise exception 'sphere_hours: window needs a start and an end' using errcode = '22023';
  end if;

  return query
  with live as (
    select s.id, s.name, s.created_at
      from public.spheres s
     where s.owner_telegram_id = owner_id
       and s.removed_at is null
  ),
  spans as (
    select live.id as key, true as met,
           greatest(t.due_at, head) as s,
           least(t.due_at + make_interval(mins => t.duration::integer), tail) as e
      from public.tasks t
      left join live on live.id = t.sphere_id
     where t.owner_telegram_id = owner_id
       and t.status in ('active', 'done')
       and t.duration is not null
       and t.due_at >= head - interval '1 day'
       and t.due_at < tail
    union all
    select c.sphere_id, false, greatest(c.starts_at, head), least(c.ends_at, tail)
      from public.chat_sessions(owner_id, head - interval '1 day') c
  ),
  -- «all» — встречи и переписка вместе (итог сферы), «met» — только встречи.
  parts as (
    select x.key, 'all'::text as part, x.s, x.e from spans x where x.s < x.e
    union all
    select x.key, 'met'::text, x.s, x.e from spans x where x.s < x.e and x.met
  ),
  -- Объединение отрезков: новый кусок — там, где отрезок начинается позже конца
  -- всех предыдущих.
  flagged as (
    select p.key, p.part, p.s, p.e,
           case
             when p.s <= max(p.e) over (
               partition by p.key, p.part order by p.s, p.e
               rows between unbounded preceding and 1 preceding
             ) then 0
             else 1
           end as fresh
      from parts p
  ),
  islands as (
    select f.key, f.part, f.s, f.e,
           sum(f.fresh) over (
             partition by f.key, f.part order by f.s, f.e rows unbounded preceding
           ) as island
      from flagged f
  ),
  pieces as (
    select i.key, i.part, max(i.e) - min(i.s) as length
      from islands i
     group by i.key, i.part, i.island
  ),
  -- Целые минуты, половина — вверх.
  lengths as (
    select pc.key, pc.part,
           floor((extract(epoch from sum(pc.length)) + 30) / 60)::integer as minutes
      from pieces pc
     group by pc.key, pc.part
  ),
  totals as (
    select l.key,
           max(l.minutes) filter (where l.part = 'all') as total,
           coalesce(max(l.minutes) filter (where l.part = 'met'), 0) as met
      from lengths l
     group by l.key
  )
  select t.key, live.name, t.met, t.total - t.met
    from totals t
    left join live on live.id = t.key
   where t.total > 0
   order by t.key is null, live.created_at, live.id;
end;
$$;

-- --- Отрезки дня -----------------------------------------------------------------------

-- Отрезки, задевающие `[span_start, span_end)`, — для нити «Сегодня» и строк
-- «Недели» (§32.3): `meeting` — встречи, и будущие тоже (нить и неделя рисуют их
-- «впереди»), `task_id` — дело встречи; `chat` — сессии переписки. Отрезки не
-- режутся окном: день режет тот, кто рисует. Колонок с текстом нет — ни сути
-- дела, ни слов переписки: приложение берёт суть из своих дел.
create function public.day_spans(
  span_start timestamptz,
  span_end timestamptz,
  owner_telegram_id bigint default null
) returns table (kind text, starts_at timestamptz, ends_at timestamptz, sphere_id uuid, task_id uuid)
language plpgsql
stable
security definer
set search_path = ''
as $$
#variable_conflict use_column
declare
  owner_id bigint := public.hours_owner(day_spans.owner_telegram_id);
begin
  if day_spans.span_start is null or day_spans.span_end is null then
    raise exception 'day_spans: window needs a start and an end' using errcode = '22023';
  end if;

  return query
  select x.kind, x.starts_at, x.ends_at, x.sphere_id, x.task_id
    from (
      select 'meeting'::text as kind,
             t.due_at as starts_at,
             t.due_at + make_interval(mins => t.duration::integer) as ends_at,
             live.id as sphere_id,
             t.id as task_id
        from public.tasks t
        left join public.spheres live
          on live.id = t.sphere_id
         and live.owner_telegram_id = owner_id
         and live.removed_at is null
       where t.owner_telegram_id = owner_id
         and t.status in ('active', 'done')
         and t.duration is not null
         and t.due_at >= day_spans.span_start - interval '1 day'
         and t.due_at < day_spans.span_end
         and t.due_at + make_interval(mins => t.duration::integer) > day_spans.span_start
      union all
      select 'chat'::text, c.starts_at, c.ends_at, c.sphere_id, null::uuid
        from public.chat_sessions(owner_id, day_spans.span_start - interval '1 day') c
       where c.starts_at < day_spans.span_end
         and c.ends_at > day_spans.span_start
    ) x
   order by x.starts_at, x.kind desc;
end;
$$;

-- --- Права -----------------------------------------------------------------------------

-- Часы и отрезки зовут приложение (владелец — клейм) и бот (владелец —
-- параметр). Владельца вызова и сессии зовут только сами определители: снаружи
-- `chat_sessions` отдала бы переписку любого владельца по номеру.
revoke execute on function public.hours_owner(bigint)
  from public, anon, authenticated, service_role;
revoke execute on function public.chat_sessions(bigint, timestamptz)
  from public, anon, authenticated, service_role;

revoke execute on function public.sphere_hours(timestamptz, timestamptz, bigint)
  from public, anon;
grant execute on function public.sphere_hours(timestamptz, timestamptz, bigint)
  to authenticated, service_role;

revoke execute on function public.day_spans(timestamptz, timestamptz, bigint)
  from public, anon;
grant execute on function public.day_spans(timestamptz, timestamptz, bigint)
  to authenticated, service_role;
