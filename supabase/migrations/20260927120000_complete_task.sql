-- Действия из Mini App: «Сделано» под правилами доступа.
--
-- Схема — techspec/03-schema.md §3.6. Применяется `supabase db push` или
-- тем же текстом в SQL-редакторе панели Supabase (supabase/README.md).
-- Применённая миграция не правится: следующая правка — следующий файл.

-- Mini App ходит в базу под ролью `authenticated` (§4.1) и владельца из
-- аргумента не получает: он читается из клейма `telegram_id` токена, того
-- же, по которому режут правила доступа. Функция — `security invoker`,
-- поэтому каждая её строка идёт через RLS; явный фильтр по владельцу
-- сверх того — инвариант 2: ни одного запроса к данным без него.
--
-- То же, что `mark_task_done` для бота (§3.5): `status = done` и снятие
-- неотправленных напоминаний одной транзакцией, иначе закрытая задача может
-- ещё раз постучаться. Чужая или несуществующая задача — `null`, не ошибка:
-- для владельца её просто нет.
create function public.complete_task(task_id uuid)
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

  delete from public.reminders r
   where r.task_id = complete_task.task_id
     and r.owner_telegram_id = owner_id
     and r.sent_at is null;

  if saved.status = 'done' then
    return saved;
  end if;

  update public.tasks t
     set status = 'done'
   where t.id = complete_task.task_id
     and t.owner_telegram_id = owner_id
   returning t.* into saved;

  return saved;
end;
$$;

-- Зовёт только Mini App с токеном. Право execute выдано роли public и из
-- неё наследуется, поэтому забирается и там; `service_role` у бота своя
-- функция — `mark_task_done` — с явным владельцем.
revoke execute on function public.complete_task(uuid) from public, anon;
grant execute on function public.complete_task(uuid) to authenticated;

-- Удаление задачи из Mini App — обычный `delete from tasks` под политикой
-- «tasks: owner only» (миграция 001): она объявлена `for all`, удаление
-- в неё входит. Напоминания уходят каскадом (`on delete cascade`, миграция
-- 004) — проверки ссылочной целостности RLS не подчиняются, поэтому каскад
-- срабатывает и под `authenticated`. Сообщение-источник остаётся: это след,
-- а не часть задачи.
--
-- Чтение карточки — `messages` и `reminders` владельца: обе политики
-- («messages: owner only» из 001, «reminders: owner only» из 004) тоже
-- `for all to authenticated`, чтение в них входит. Новых политик не нужно.
