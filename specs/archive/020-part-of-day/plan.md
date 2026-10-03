# 020. План

Порядок работы и решения по неоднозначностям. Источник правил —
`techspec/21-part-of-day.md`; здесь только то, чего там нет.

## Порядок (коммит на задачу, галочка в нём же)

1. `plan.md`, статус «в работе».
2. База — `supabase/migrations/20261004100000_part_of_day.sql`:
   - `tasks_due_precision_check` заново, пять значений;
   - `reminder_plan` — `create or replace`, у части `before` = null
     (null отсекается прежним `where`);
   - `change_task` — `create or replace` с телом из
     `20260929200000_repeat.sql`, плюс ключ `due_precision`;
   - `revoke`/`grant` обеих функций повторены.

   Тесты: `reminder_plan`, `chat_edit`, `record_understanding`,
   `morning_plan`, `edit_task` и «миграция не трогает старые строки
   `tasks`» (`withDatabaseBefore`, в `reminder_plan.test.ts`).
3. `services/parts.py` + `bot/tests/test_parts.py`:
   - тест первым, показан красным;
   - вызов в `understanding.py` во всех трёх путях;
   - тесты вызова в `test_understanding.py`.
4. Модель:
   - `RULES` по §21.5;
   - пример в `EDIT_RULES`;
   - `Literal` из пяти значений у `Understanding` и `TaskEdit`;
   - отпечатки в `test_understanding.py`, пометка строки 7 фикстуры.
5. `texts.py`:
   - слова частей;
   - `format_due`, `format_short_due`, `format_move_target`;
   - `format_due_moment(due_at, precision, now)`.
6. `services/edits.py::_new_due` — ветка части; вопрос о переносе
   идёт через `texts`.
7. `services/reminders.py::_text_for`:
   - точность в `format_due_moment`;
   - «Срок был» по §21.3;
   - правка ожиданий `test_reminders.py` («Срок: сегодня» без 18:00).
8. `services/morning.py::plan_lines` — строки частей.
9. Приложение:
   - `lib/format.ts` (тип `DuePrecision`, слова частей, `formatDue`);
   - `lib/tasks.ts` (разбор пяти значений, `dueHint(task, draft, now)`);
   - вызов в `TaskEdit.tsx`.
10. Тесты сервиса:
    - `test_tasks_service.py` — запись части;
    - `test_chat_edit_service.py` — перенос на часть;
    - галочка «Тесты без сети» — когда всё из списка есть.
11. Документы.
12. Ворота, закрытие.

## Решения

- **`parts.py` на примитивах.** `settle(due_at, precision, *,
  repeating, timezone) -> (due_at, precision)`. Модуль не импортирует
  `understanding.py`: тот зовёт `parts`.

  Приведение разбора (`model_copy` верхнего уровня и `edit`) — функция
  в `understanding.py`. Её зовут три пути (`analyze`, `analyze_photo`,
  `analyze_conversation`) до `Analysis*`.

  `repeating` считается отдельно:
  - у поручения — `parsed.repeat is not None`;
  - у правки — `edit.repeat is not None`.
- **Выбор «сегодня или завтра»** при неназванном дне делает модель по
  правилам §21.5. Бот ставит только час части на тот день, который
  отдала модель: день берётся из её `due_at`. У момента без пояса —
  его дата, у момента с поясом — дата в поясе владельца.
- **Часть без `due_at`** (`due_precision` = часть, `due_at = null`)
  остаётся как есть: срока нет, ставить нечего. Запись пропустит её
  тем же путём, что `day` без `due_at`.
- **Тип `DuePrecision` в приложении** переезжает в `lib/format.ts`:
  `formatDue` его принимает, а `format.ts` не импортирует
  `tasks.ts`. `tasks.ts` реэкспортирует тип.

  Слова частей («утром», «днём», «вечером») — тоже в `format.ts`.
  Часов частей у приложения нет: час подсказки берётся из `task.dueAt`.
- **`dueHint` получает задачу:** `dueHint(task, draft, now)`. Ветка
  части включается, когда у задачи часть, день в черновике тот же, час
  пуст и повтор не тронут. Иначе — прежние ветки по черновику.
- **«Срок был»** (`_text_for`):
  - у `time` — `due_at` раньше начала текущей минуты (`now` без секунд
    и микросекунд);
  - у остальных, включая `None`, — локальная дата срока раньше
    сегодняшней.
- **Утренний план:** метка строки — `HH:MM` у `time`, «Утром» /
  «Днём» / «Вечером» у части, иначе дело на день. Сортировка по
  `due_at` (стабильная) — у всех с меткой; дела на день — последними,
  в порядке базы.

  Дело со временем и часть в одну минуту (18:00 и «Вечером») идут в
  порядке базы: `day_tasks` сортирует по `due_at`, затем по записи.
- **Ключ `due_precision` у `change_task`** сверяется до всего
  остального.
  - Отказ: значение не строка или не из `time` / `morning` /
    `afternoon` / `evening`; `due_precision` без `due_at`; с
    `due_at = null`; вместе с `due_date`.
  - Точность при `due_at` — `coalesce(changes->>'due_precision', 'time')`.
- **Повтор делу с частью** без нового срока: `repeat_rule` строит
  правило без часа, потому что час ставится только у `time`. Код не
  меняется — закрепляется тестом базы.
- **Тест «миграция не трогает старые строки»** — в
  `reminder_plan.test.ts`: там же проверяется расписание, которое
  миграция тоже не пересчитывает.
