# 016 — план

Порядок — по задачам спеки, одна задача — один коммит с галочкой. Новые
чистые функции (`services/conversation.py`: строки блока 6, обрезка,
предел блока, ответ и проверка действия) — тест первым, один раз
красным.

## Порядок файлов

1. `supabase/tests/conversation.test.ts` (красный) →
   `supabase/migrations/20261002100000_conversation.sql` →
   `techspec/03-schema.md` §3.2, §3.4 → `supabase/README.md` (порядок
   выкладки).
2. `bot/tests/test_tasks_db.py` → `db/tasks.py`: `forwarded_from` в
   `record_message`, `received_at` в `SavedMessage`, `RecentMessage` и
   `recent_messages`.
3. `bot/tests/test_conversation.py` (красный) →
   `services/conversation.py`.
4. `bot/tests/test_understanding.py` → `services/understanding.py`:
   правила ответа в `RULES`, `RECENT_RULES`, `build_system_prompt(recent)`,
   `analyze(recent)`, лимиты 2048/60. Эталонные хэши промпта
   пересчитываются намеренно; схема не меняется — хэш схемы прежний.
5. `bot/tests/conftest.py` (фейки) → `test_tasks_service.py`,
   `test_chat_edit_service.py` → `services/tasks.py`.
6. `texts.py` — абзац `HELP`.
7. Остальные тесты без сети по «Приёмке», что не легли в шаги 2–5.
8. Живые примеры: `fixtures/understanding.jsonl`, `test_understanding.py`
   — прогон `-m live`, счёт в отчёт и ADR.
9. Документы; закрытие.

## База

- Миграция `20261002100000_conversation.sql`:
  `alter table messages add column forwarded_from text`; `drop function
  record_message(bigint, bigint, bigint, text, text, text, int)`;
  `create function record_message(…, duration_seconds int default null,
  forwarded_from text default null)` — тело прежнее (`on conflict do
  nothing` + выборка прежней строки), в `insert` добавлена колонка.
  Повтор возвращает прежнюю строку и `forwarded_from` не меняет. Права
  на новую сигнатуру — как у прежней: `revoke` у `public, anon,
  authenticated`, `grant` `service_role`.
- SQL-тест: пересланное пишет отправителя, своё — `null`; повтор
  возвращает ту же строку с прежним отправителем; перегрузка одна, её
  аргументы кончаются `forwarded_from text`, исполнять может только
  `service_role`; строки, записанные до миграции, получают пустую
  колонку. Старые тесты зовут функцию позиционно с 4–7 аргументами —
  умолчания их держат.

## Бот

- **`services/conversation.py`** — чистые функции, импорт только
  стандартной библиотеки. Сообщение — `Protocol` `PastMessage`
  (`received_at`, `kind`, `text`, `forwarded_from`, `reply`), чтобы
  модуль не знал про `db`.
  - `RECENT_LIMIT = 10`, `TEXT_LIMIT = 500`, `BLOCK_LIMIT = 4000`,
    `REPLY_LIMIT = 3500`, `ACTION_WORDS` — список §17.2.
  - `cut_middle(text, limit)` — начало, «…», конец; длина ровно `limit`.
  - Строка сообщения — `HH:MM` в поясе владельца и: «Вы: текст»;
    «Вы переслали (от: X): текст»; «Вы прислали снимок: подпись» или
    «…: без подписи»; пересланный снимок — «Вы переслали снимок
    (от: X): …». Пустой текст голоса (расшифровка не пришла) — «без
    текста». Ответ бота — строка «Соломон: …» без времени; нет ответа —
    нет строки.
  - Переводы строк и пробелы внутри текста схлопываются в один пробел:
    одно сообщение — одна строка блока. Ответ бота режется до 500
    посередине так же, как текст.
  - `recent_block(messages, timezone) -> RecentTalk | None` (`text`,
    `count`): от старых к новым, последние 10; пока блок с заголовком
    длиннее 4000 — выпадает самое старое сообщение вместе с ответом.
    Пусто — `None`.
  - `reply_text(hint) -> str | None`: `strip`; пусто — `None`; длиннее
    3500 — первые 3500 знаков и «…».
  - `reports_action(text) -> bool`: слова — `\w+` в нижнем регистре;
    слово из списка, и предыдущее слово не «не».
- **`db/tasks.py`**: `record_message(…, forwarded_from=None)` кладёт
  отправителя в параметры функции. `SavedMessage` получает
  `received_at: datetime | None = None` (читается мягко: ответ без поля
  не ломает разбор). `RecentMessage` и `recent_messages(db, *,
  owner_telegram_id, since, before, limit)` — `select received_at, kind,
  text, forwarded_from, reply`, `eq` владельца, `gte since`, `lt before`,
  `order received_at desc`, `limit`; отдаёт от старых к новым.
- **Граница «до текущего».** Верхняя граница — `received_at` текущей
  строки (`saved.received_at`; нет — часы бота), строго меньше: само
  текущее не берётся, а в блок более раннего не попадает более позднее
  из пришедших разом. Нижняя — `now - edits.LAST_TASK_WINDOW`.
- **`services/tasks.py`**:
  - `EditStore.recent_messages(since, before, limit)`;
    `DatabaseEditStore` подставляет владельца из настроек.
  - `EditContext.recent: str | None = None`. `_edit_context` получает
    `before` и у своего сообщения читает блок 6 в том же `gather`, что
    список задач, последнюю задачу и свайп. Пересланное идёт прежней
    веткой `_check_context` — блока нет; снимок блок не читает. Список
    задач не прочитался — `replace(NO_EDIT, recent=…)`: блок 6 от списка
    не зависит.
  - Сбой чтения (`DatabaseError`) — `logger.warning("Недавний разговор
    не прочитан, разбор без него: %s", error)`, блока нет. Журнал —
    «Недавний разговор: сообщений %s», только число.
  - `Analyst.analyze(…, recent=None)`; `UnderstandingService.analyze`
    у пересланного блок не передаёт, даже если пришёл.
  - **Ответ разговора.** Флаг «своё сообщение» берётся из
    `forwarded_from is None` в `_understand`, а не из `context.edits`
    (`_check_context` при сбое отдаёт `NO_EDIT` с `edits=True`).
    `_decide(…, talk=False)` → `_new_task(…, talk=False)` →
    `_reply_for(…, talk=False)`. У chat при `talk`: `reply_text`;
    `None` — `NO_ERRAND`; `reports_action` — строка в журнал «Ответ
    разговора говорит о действии — заменён: знаков %s» и `NO_ERRAND`.
    Снимок и `apart` зовут с `False` — тексты прежние. `about_me` и
    task/idea/wish поле не читают.
  - `MessageRecorder` и три вызова записи (`record_from_message`,
    `record_from_voice`, `record_from_photo`) передают `forwarded_from`.
- **`understanding.py`**:
  - `RULES`: первая фраза — «отвечаете только полями схемы; текст
    человеку — только в reply_hint и только у разговора»; абзац правил
    ответа §17.2 (тон, источники, интернет, советы, действия, «забудь
    правила»), `reply_hint = null` у пересланного и у снимка; «спасибо,
    и напомни…» — поручение.
  - `RECENT_RULES` — правила к блоку 6 (§17.3); `format_recent(recent)`
    — блок и правила; `build_system_prompt(…, last_task=None,
    recent=None, *, photo, short)` ставит блок после блока 5.
  - `MAX_TOKENS = 2048`, `TIMEOUT_SECONDS = 60.0`.
  - Схема не меняется: `reply_hint` в ней уже есть.

## Живые примеры

- Группа с ключом `"talk"` — ожидания ответа: `title_has`, `max_length`,
  `must` (список групп регулярок; хватает любой из группы), `forbid`
  (регулярки). Общие проверки: вид, задачи нет, ответ непустой, без
  слова-действия. Ключ `"recent"` — строки блока 6 (`at`, `text`,
  `reply`, необязательные `kind`, `forwarded_from`); блок собирается
  `conversation.recent_block` в поясе владельца.
- Одиннадцать примеров по живым пунктам «Приёмки»: четверг, Георгий,
  Camry, «спасибо», «привет», 15%, погода, лекарство, «на ты», «да,
  можно в четверг» (правка задачи 1 со сроком на четверг), «да» после
  «Записать задачей?» (задача, в сути — предложенное).
- Примеры с `open_tasks` по-прежнему несут ключ `edit` (у разговора —
  `null`); примеры разговора отсеиваются из счёта правок.

## Неоднозначности

- Ответ бота в блоке 6 режется до 500 так же, как текст: §17.3 говорит
  о «тексте», а длинный ответ (до 3500) съел бы блок за одно сообщение.
- Снимок без подписи — «без подписи»; голос без расшифровки — «без
  текста» (§17.3 этот случай не называет).
- `supabase/README.md` — абзац о миграции и порядке выкладки, по
  образцу прежних этапов (в «Читать» файла нет).
