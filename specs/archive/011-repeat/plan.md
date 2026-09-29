# 011 — план

Порядок — по задачам спеки, одна задача — один коммит с галочкой. Новые
чистые функции (`repeat_next`/`repeat_valid`, `services/repeat.py`,
`lib/repeat.ts`) — тест первым, один раз красным.

## Порядок файлов

1. `supabase/tests/repeat.test.ts` (красный) →
   `supabase/migrations/20260929200000_repeat.sql` →
   `techspec/03-schema.md` §3.3–3.6. Задачи 1 и 2 — вместе: миграция без
   её тестов не сдаётся, прежние тесты PGlite не правятся.
2. `bot/tests/test_repeat.py` (красный) → `services/repeat.py` →
   `services/understanding.py` (+ `test_understanding.py`, `conftest.py`).
3. `db/tasks.py`, `db/reminders.py` (+ `test_tasks_db.py`).
4. `texts.py`, `services/edits.py`, `services/tasks.py`,
   `services/reminders.py`, `handlers.py`, `runner.py` — задачи 5–7
   одним заходом, тесты бота рядом.
5. `miniapp/src/lib/repeat.test.ts` (красный) → `lib/repeat.ts` →
   `lib/tasks.ts`, `lib/telegram.ts` (+ `tasks.test.ts`).
6. `styles.css`, `components/TaskRow.tsx`, `TaskCard.tsx`, `TaskEdit.tsx`,
   новый `components/RepeatRow.tsx`, `App.tsx`.
7. `bot/tests/fixtures/understanding.jsonl`, живой прогон.
8. Документы; закрытие: архивы, витрина `miniapp/src/prototype/011/` и
   ветка в `main.tsx` удаляются.

## База

- `repeat_valid(jsonb)` — `immutable`, `null` проходит. Форма строгая:
  ключи только `every, interval, weekdays, month_day, month, time`;
  `interval` целое 1–99; `weekdays` — непустой список различных 1–7 у
  `week`, у остальных пусто (`null`/нет ключа); `month_day` 1–31 или −1
  у `month`, 1–31 у `year` и не больше длины месяца (29 для февраля);
  `month` 1–12 только у `year`; `time` — `HH:MM` или `null`.
- `repeat_rule(rule, due_at, due_precision, timezone)` — служебная
  чистая: правило в каноническом виде (дни отсортированы, лишние поля
  `null`) и `time` из срока (`null` при точности `day`). Ею ставят
  правило `change_task` и `record_understanding` (задача и `amend`) —
  `time` ни модель, ни приложение не шлют. Право — как у `repeat_next`.
- `repeat_next(repeat, occurrence_at, after, timezone)` — `stable`,
  перебор ряда от раза в поясе владельца: дни от дня раза, недели от
  понедельника недели раза, месяцы от месяца раза, годы от года раза с
  шагом `interval`; первый кандидат строго позже `after`. Число больше
  длины месяца — последний день. Цикл ограничен (защита от бесконечности
  при кривом правиле — отказ).
- Ядро перехода — `advance_task(owner, task_id, occurrence bigint,
  next_at, schedule)`, `security invoker`, право `authenticated` и
  `service_role`, как у `change_task`: под токеном владелец обязан
  совпасть с клеймом, а `next_at` и `schedule` отбрасываются — их
  считает база (`repeat_next` от `max(occurrence_at, now())`,
  `reminder_plan` по поясу `owner_settings`). Строка держится `for
  update`. Не активная — как есть. Повторяющаяся: `occurrence` задан и
  не равен разу (секунды Unix) — как есть; иначе переход. Разовая —
  `done`, как раньше.
- **Решение:** кнопка с разом у задачи, которая успела стать разовой
  («больше не повторяй» после напоминания), закрывает её, как кнопка
  без раза. Проверка раза защищает серию от двойного перехода, а
  закрытие разовой и так идемпотентно; «как есть» оставило бы человека
  с нажатой кнопкой без действия.
- `mark_task_done(bigint, uuid, bigint default null)` и
  `complete_task(uuid, bigint default null)` — обёртки над ядром,
  прежние перегрузки удаляются, права — как были (`service_role` /
  `authenticated`).
- `edit_from_chat`: `done`/`skip`/`cancel`. Разовая: `done` — закрыть,
  `skip` и `cancel` — убрать. Повторяющаяся: `cancel` — убрать серию
  (правило остаётся); `done`/`skip` — нужен `next_at` (нет — отказ
  `raise`), `occurrence` обязан совпасть с разом (не совпал или не
  передан — `null`, и `record_understanding` откатывает всё), дальше
  ядро с готовыми `next_at` и `schedule`. Поля `edit`: `occurrence`
  (секунды Unix), `next_at` (ISO), `schedule`.
- `change_task`: ключ `repeat` — объект ставит правило через
  `repeat_rule` по сроку после правки и `occurrence_at` = этот срок;
  `null` снимает. Правило без срока или у вида не `task` — отказ целиком.
  Без ключа: срок снят или вид не `task` — правило снимается; иначе
  правило и раз остаются (перенос одного раза).
- Функция «Вернуть» — `return_occurrence(owner, task_id, back_to bigint,
  moved_from bigint, schedule)`, только `service_role`: задача активна,
  повторяется и стоит на `moved_from` → `due_at = occurrence_at =
  back_to`, точность серии, план бота; иначе как есть; нет — `null`.
  Бот понимает исход по разу в ответе: `back_to` — вернул, другой — ушла
  дальше.
- `roll_repeats(owner, now) returns setof tasks`, только `service_role`:
  пока срок в прошлом и начало следующего раза наступило — шаг; начало
  = `min(полночь дня раза, ступень «заранее»)`; план нового раза
  считается на момент чуть раньше его начала — обе ступени, созревшие
  уйдут тем же тиком одним сообщением. Пояса нет — ничего.
- `due_reminders`, `moved_tasks` — `drop` + `create` с `repeat` и
  `occurrence_at`, права заново только `service_role`.
- **Решение (на реализации):** следующий раз всегда на дне позже дня
  раза. Раз, перенесённый на 10:00 при правиле «в 18:00», иначе дал бы
  «следующий» тем же днём в 18:00 — в один день у ряда не больше одного
  раза.
- **Решение (на реализации):** `complete_task` — право только
  `authenticated`, у `service_role` забрано: §3.6 выдаёт действия
  приложения только под токеном, бот зовёт `mark_task_done`.
- **Решение (на реализации):** `repeat_rule` отбрасывает присланный
  `time` и ставит его из срока — правило и срок не расходятся.

## Бот

- `services/repeat.py`: `clean_rule(raw) -> dict | None` (форма §13.2 без
  `time`, дни отсортированы), `RuleOutcome` для записи: отброшено молча
  (идея, желание, без срока), не по форме (пометка и причина «Не разобрал
  повтор — записал разовой»; своя причина модели сохраняется первой),
  принято; `occurrence_seconds(moment)`.
- Слова правила — `texts.repeat_words(rule)`; тот же список примеров —
  в тестах бота и приложения.
- «Сделано»/«пропуск» словом и кнопкой кандидата: бот берёт следующий раз
  у `repeat_next` (новая функция в `db/reminders.py`) и план у
  `reminder_plan`, кнопка «Вернуть» — `back:<id>:<X>:<Y>`.
- Правило у задачи без срока, срок не назван — вопрос §12.3; своего
  вопроса модель не дала — «С какого дня начать повтор?».
- Правило не по форме в правке словом — отбрасывается (строка в лог);
  больше менять нечего — «ничего не менял».
- Кнопка напоминания: `done:<id>:<раз>` у повторяющейся, `done:<id>` у
  разовой; `Notifier` получает раз, `runner.py` строит кнопку с ним
  (файла нет в «Коде» — нужен ради кнопки). Нажатие отдаёт задачу целиком
  (`TaskDetails`): повторяющаяся активная — «✓ Сделано. Следующий раз:
  <срок>», иначе — как раньше.
- Перекатывание — отдельный протокол `Roller` в `ReminderService`; тик:
  перекатить (сбой — лог) → созревшее → «Перенёс».

## Mini App

- `Task.repeat` (`Repeat | null`, в camelCase: `monthDay`) и
  `Task.occurrenceAt`. `completeTask(id, occurrenceAt)` отдаёт задачу из
  базы; активна — остаётся в списке с заметкой «✓ Сделано. Следующий раз:
  …» (заметка живёт в состоянии `App` до следующего чтения списка).
- Форма: `RepeatRow` — выбор, шаг, дни, число; правило из даты
  (`lib/repeat.ts`), подгонка даты (`fitDate`) — ближайший подходящий
  день не раньше неё. `taskChanges` шлёт `repeat` только при изменённом
  выборе — вместе со сроком формы (`due_date`/`due_at`). «Нет» при
  правиле у задачи — `repeat: null`.
- Неделя без дня — кнопка «Сохранить» недоступна и строка ошибки
  «Отметьте хотя бы один день недели.» (текст решается на реализации —
  README прототипа).
