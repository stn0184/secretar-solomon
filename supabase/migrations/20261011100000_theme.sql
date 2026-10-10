-- Оформление приложения (этап 034): тема в настройках владельца.
--
-- Схема — techspec/03-schema.md §3.8; поведение — techspec/32-redesign.md
-- §32.1. Применяется `supabase db push` или тем же текстом в SQL-редакторе
-- панели Supabase (supabase/README.md). Применённая миграция не правится:
-- следующая правка — следующий файл.
--
-- Выбор темы — настройка, а не поручение: его пишет приложение, а не бот, и
-- хранится он в базе, чтобы открываться так же на любом устройстве. Новых
-- таблиц нет; правило доступа `owner_settings` прежнее, права приложения на
-- таблицу сужаются до одной колонки.

-- --- Колонка -------------------------------------------------------------------

-- `auto` — как в Telegram: тёмная тема Telegram — «Банк-люкс», светлая —
-- «Тихая роскошь»; `bank` и `quiet` — закреплённая вручную. Строки, записанные
-- до миграции, получают `auto` — так приложение и выглядело бы без выбора.
alter table public.owner_settings
  add column theme text not null default 'auto',
  add constraint owner_settings_theme_check check (theme in ('auto', 'bank', 'quiet'));

-- --- Права приложения ------------------------------------------------------------

-- Строку пишет бот (`save_owner_timezone`, service-role); приложение до этого
-- этапа таблицу только читало изнутри `edit_task`. Теперь оно правит тему — и
-- ничего больше: пояс — зеркало `.env` бота, и правка из приложения разошлась
-- бы с ним. Чужую строку закрывает правило доступа (§4.2), колонку — эти права.
revoke insert, update, delete on public.owner_settings from anon, authenticated;
grant update (theme) on public.owner_settings to authenticated;

-- --- Запись выбора -----------------------------------------------------------------

-- Выбор темы из приложения. Владелец — из клейма токена, фильтр явный сверх
-- правила доступа: прямой `update` без условия Supabase не пропускает, а id
-- владельца у приложения нет. Строки владельца нет (бот ещё не запускался) или
-- токен без клейма — `null`, ничего не записано. Незнакомое значение — отказ
-- ограничения `owner_settings_theme_check`. `updated_at` — отметка бота о
-- поясе, тема её не двигает.
create function public.save_theme(theme text)
returns public.owner_settings
language plpgsql
security invoker
as $$
declare
  owner_id bigint := (auth.jwt() ->> 'telegram_id')::bigint;
  saved public.owner_settings;
begin
  if owner_id is null then
    return null;
  end if;

  update public.owner_settings s
     set theme = save_theme.theme
   where s.owner_telegram_id = owner_id
  returning s.* into saved;

  return saved;
end;
$$;

revoke execute on function public.save_theme(text) from public, anon, service_role;
grant execute on function public.save_theme(text) to authenticated;
