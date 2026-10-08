-- Напоминания о встречах: за час и за 5 минут до срока (этап 029).
--
-- Правило — techspec/06-reminders.md §6.1; функция — techspec/03-schema.md
-- §3.5, §11.3. Применяется `supabase db push` или тем же текстом в
-- SQL-редакторе панели Supabase (supabase/README.md). Применённая миграция не
-- правится: следующая правка — следующий файл.
--
-- Решение владельца (2026-10-08): у дела с часом (`due_precision = time`)
-- бот напоминает за час и за 5 минут, в сам срок больше не пишет. Ступень
-- `due` остаётся той же строкой — меняется только её момент. Дела на день и
-- части дня не меняются. Новых таблиц и колонок нет, правила доступа те же.

-- Расписание напоминаний (§6.1) — заново, с той же подписью. Сверх миграции
-- 020 — `due` у срока с часом за 5 минут до него. Момент «к сроку» уже прошёл
-- (до встречи 5 минут и меньше) — ступень не заводится, как `before`: не
-- срабатывает сразу. Бот, правка из приложения, «Сделано» у повтора и
-- перекатывание зовут эту же функцию — все получают новое правило.
create or replace function public.reminder_plan(
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
    select case
             -- у встречи — за 5 минут до неё, а не в сам срок
             when reminder_plan.due_precision = 'time'
               then reminder_plan.due_at - interval '5 minutes'
             else reminder_plan.due_at
           end as due,
           case
             when reminder_plan.due_precision = 'time'
               then reminder_plan.due_at - interval '1 hour'
             -- у части заранее не напоминаем: пусто отсекает условие ниже
             when reminder_plan.due_precision in ('morning', 'afternoon', 'evening')
               then null
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
   where reminder_plan.now < p.due
  order by 2
$$;

-- Уже запланированные напоминания живых встреч: неотправленное «в срок»
-- срабатывает за 5 минут до него. Берётся только строка, стоящая ровно на
-- сроке, — повторный прогон того же текста её второй раз не сдвинет.
-- Ушедшие строки, закрытые и убранные дела, дела на день и части дня — как
-- были. Момент мог уже пройти (встреча через 3 минуты) — тогда ступень
-- уйдёт первым же тиком (§6.2): лучше сразу, чем никогда.
update public.reminders r
   set fire_at = t.due_at - interval '5 minutes'
  from public.tasks t
 where t.id = r.task_id
   and t.owner_telegram_id = r.owner_telegram_id
   and t.status = 'active'
   and t.due_precision = 'time'
   and r.stage = 'due'
   and r.sent_at is null
   and r.fire_at = t.due_at;

-- `create or replace` права не трогает; они повторены, чтобы файл читался
-- сам по себе. Право execute выдано и роли public, из неё оно наследуется —
-- поэтому забираем и там.
revoke execute on function public.reminder_plan(timestamptz, text, text, text, timestamptz)
  from public, anon;
grant execute on function public.reminder_plan(timestamptz, text, text, text, timestamptz)
  to authenticated, service_role;
