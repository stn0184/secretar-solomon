# 013 — план

Порядок — по задачам спеки, одна задача — один коммит с галочкой; задачи
бота 3–6 могут лечь вместе, если без соседней не собираются тесты.
Новые чистые функции (данные кнопки `apart:`, абзац накладки, ответ
«Это уже записано», короткий блок 5) — тест первым, один раз красным.

## Порядок файлов

1. `supabase/tests/duplicates.test.ts` (красный) →
   `supabase/migrations/20260930200000_duplicates.sql` →
   `supabase/tests/photo.test.ts` (хвост сигнатуры) → `database.ts`
   (помощник, если нужен) → `techspec/03-schema.md` §3.4.
2. `bot/tests/test_tasks_db.py` → `db/tasks.py`: `same_task`,
   `record_separately`, `same_minute_tasks`.
3. `bot/tests/test_understanding.py` → `services/understanding.py`:
   `same_as`, короткий блок 5, правила дубля; эталонные хэши промпта и
   схемы пересчитываются намеренно.
4. `test_edits.py` (красный) → `services/edits.py` (`apart_data`,
   `parse_apart`); `test_tasks_service.py`, `test_chat_edit_service.py`
   → `texts.py`, `services/tasks.py`, `conftest.py` — дубль.
5. `test_handlers.py` / `test_chat_edit_handlers.py` → `handlers.py`,
   `services/tasks.py::apart` — кнопка.
6. Накладка на путях §15.5 — `services/tasks.py`, тесты сервиса.
7. Живые случаи в `understanding.jsonl`, дубль снимка в
   `test_understanding.py`, прогон `-m live`.
8. Документы; закрытие.

## База

- Миграция `20260930200000_duplicates.sql` (после `photo.sql` того же
  дня).
- Внутренняя `public.insert_message_task(owner bigint, message_id uuid,
  task jsonb, reminders jsonb) returns tasks` — вставка задачи из тела
  `record_understanding` (люди, `open_question` с `question_asked_at`,
  `repeat_rule` через зону владельца, `occurrence_at`, напоминания с
  `on conflict do nothing`). Права: `revoke all` у `public, anon,
  authenticated, service_role` — её зовут только функции базы
  (`security definer` не нужен: владелец функций один).
- `record_understanding`: `drop` 15-аргументной, `create` с 16-м
  `same_task uuid default null`. `same_task` вместе с `task`, `amend`
  или `edit` — исключение до любой записи. Ветка `same_task` идёт там
  же, где ветка задачи: после повтора (задача по `source_message_id`
  уже есть — вернуть её) и снятия открытого вопроса; задача чужая или
  не `active` — исключение, транзакция откатывается; иначе
  `messages.task_id = same_task`, вернуть задачу. Права — как у
  прежней.
- `record_separately(owner_telegram_id, message_id, task, reminders,
  reply) returns messages` — по образцу `pick_task`: сообщение владельца
  `for update`, нет — исключение; задача с `source_message_id` уже есть
  — вернуть сообщение как есть; у новой задачи `open_question` —
  снять прежние открытые вопросы владельца; вставка внутренней
  функцией; `update messages set task_id, reply returning *`. Права
  только `service_role`.

## Бот

- **Разрешение правки.** `EditContext` получает флаг `edits`
  (правка разрешена). Пересланное: список задач читается, `last_task`
  и свайп — нет, `edits=False`. Снимок: список читается тем же
  `_open_tasks`, `edits=False`. Сбой чтения — `tasks=None`: разбор без
  блока 5, без правки и без дубля. `edit` у пересланного и снимка
  отбрасывается, как раньше.
- **Короткий блок.** `format_open_tasks(..., short=False)`: при `short`
  строки задач, пометка «Это сообщение задач не меняет: edit = null» и
  правила дубля; пустой список при `short` — `""` (блока нет). Короткий
  выбирается в `analyze` по `forwarded_from`, у `analyze_photo` — всегда.
- **`RULES`**: `edit` и `same_as` — только при блоке 5; ответ на вопрос
  — `answers_question`, `edit = null`, `same_as = null`.
- **Порядок в `_decide`**: ответ на вопрос → правка (только при
  `context.edits` и прочитанном списке) → дубль → запись с вопросом →
  обычная запись. Дубль: `same_as` не `null`, список прочитан, номер в
  нём, вид в `TASK_KINDS`, `edit is None`. Иначе при заданном `same_as`
  — строка в журнал и обычный путь.
- **Ответ дубля**: «Это уже записано: <суть>. [Повтор: ….] Срок: ….»
  по найденной задаче — `texts.duplicate_reply(title, due, repeat)`;
  срок — `_due_words` найденной задачи (`occurrence_at`/`due_at`).
  `Decision(same_task=id, buttons=(Записать отдельно,))`. Вопрос не
  ставится; память пишется; вопрос снимается базой.
- **Накладка**: метод сервиса `_same_time(due_at, precision, exclude)`
  → абзац или `None`; без хранилища — `None`; сбой — журнал и `None`.
  Абзац добавляется через `\n\n` после основной строки, до «На снимке
  ещё». Пути: обычная запись, запись с вопросом, ответ на вопрос
  (`amend`), `_edit_known` при `due_changed` и точности `time` (в том
  числе `pick`), `_unfound_move`, `apart`.
- **Кнопка `apart:<telegram_message_id>`**: `store.message` → нет —
  «Не нашёл это сообщение.»; разбор не читается или вид не задача —
  то же. Задача строится общей с `_decide` функцией обычной записи
  (вопрос модели — `asked_reply`); план на момент нажатия; накладка;
  у снимка «На снимке ещё». `store.record_separately` → `PickedMessage`;
  ответ заменяет сообщение без кнопок; легла чужая запись (второе
  нажатие) — сохранённый `reply`. `DatabaseError` или сбой плана —
  `NOT_SAVED` подсказкой, кнопка остаётся.
- **Старые разборы**: `analysis` без `same_as` читается с подстановкой
  `None`; разбор снимка узнаётся по ключу `more_tasks` и читается
  `PhotoUnderstanding`.
- **Повтор обновления** — уже есть: сохранённый ответ из `messages`
  без кнопок; дубль этого не меняет.
