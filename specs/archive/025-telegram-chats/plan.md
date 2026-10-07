# 025 — план реализации

Для ремонтного захода: порядок файлов и решения по неоднозначностям.
Источник поведения — спека этапа и `techspec/25-chats.md`; здесь — как это
лежит в коде и базе.

## Порядок коммитов

1. `plan.md`, статус «в работе».
2. Миграция `supabase/migrations/20261007100000_chats.sql` +
   `supabase/tests/chats.test.ts` + §3 и §4.2 техспека.
3. `bot/src/solomon/db/chats.py` + тесты разбора ответов базы.
4. Роутер бизнес-обновлений (`handlers.py`, `middlewares.py`, `runner.py`) и
   приём в `services/chats.py` (`ChatService.receive/edited/deleted`).
5. Согласие: `ChatService.connected/ask_consents/answer_consent`, кнопки.
6. Разбор: схема, промпт, вызов, запись (`ChatService.analyze_due`).
7. Сообщение владельцу, «Убрать», «ждёт ответа».
8. Шаг тика в `ReminderService` (`chats=`).
9. Тексты (`texts.py`), `README.md` «Чтение чатов».
10. Живой прогон (`test_chats.py`, маркер `live`), замер в §25.
11. Документы и закрытие.

## База (миграция 025)

Площадка везде — `text check in ('telegram', 'instagram', 'max')`: 026 и 027
миграций для общей части не ждут.

**`chat_sources`** — согласие на площадку: `id`, `owner_telegram_id`,
`platform`, `connection_id text` (Telegram — id бизнес-подключения; у других
площадок может быть пусто), `is_enabled boolean default true`, `asked_at`
(вопрос о согласии ушёл), `consented_at` («Согласен»), `declined_at` («Не
надо»), `created_at`. `unique (owner_telegram_id, platform)`; согласие и
отказ вместе не бывают.

**`chat_threads`** — чат: `id`, `owner_telegram_id`, `platform`, `chat_key
text` (ключ чата на площадке; у Telegram — id чата строкой), `name` (имя
собеседника или группы, последнее известное), `tracks_waiting boolean default
true` (признак «вести „ждёт ответа“»; у MAX — `false`, 027),
`last_message_at` (приход последнего сообщения), `last_out_at` (время
последнего сообщения владельца на площадке), `waiting_since`,
`waiting_about`, `waiting_to`, `waiting_reminded_at`, `failures smallint`,
`created_at`. `unique (owner_telegram_id, platform, chat_key)`.

**`chat_messages`** — сообщение: `id`, `owner_telegram_id`, `thread_id → chat_threads on delete cascade`,
`external_id text` (id сообщения площадки), `direction in ('in','out')`,
`sender`, `sent_at` (время на площадке), `kind in ('text','voice',
'video_note','photo','other')`, `text default ''`, `analysis_id →
chat_analyses` (отметка «разобрано»), `erased_at` (текст стёрт: удалением
или сроком), `created_at` (приход). `unique (thread_id, external_id)`.

**`chat_analyses`** — разбор: `id`, `owner_telegram_id`, `thread_id`,
`status in ('done','skipped')`, `messages_count`, `first_sent_at`,
`last_sent_at` (какой кусок), `items` (сколько дел), `chat_with` (с кем —
творительный падеж для сообщения), `waiting_about`, `analysis jsonb`,
`ai_model`, `input_tokens`, `output_tokens`, `duration_ms`,
`report_message_id`, `reported_at`, `created_at`.

**`tasks`**: `chat_analysis_id uuid → chat_analyses` и `chat_item smallint`
(1–5) — по образцу `source_message_id` + `source_item`: есть ровно вместе,
`unique (chat_analysis_id, chat_item)`, у задачи не бывает и сообщения, и
разбора чата.

RLS — политика «owner only» на всех четырёх таблицах (§4.2), приложение их не
читает. Функции — только `service_role`:

| Функция | Что делает |
| --- | --- |
| `connect_chat_source(owner, platform, connection_id, is_enabled)` → `chat_sources` | заводит или обновляет площадку; включение после «Не надо» снова спрашивает |
| `mark_consent_asked(owner, platform)` → bool | вопрос ушёл |
| `answer_consent(owner, platform, agreed)` → `chat_sources` | «Согласен» / «Не надо» |
| `store_chat_message(owner, platform, connection_id, chat_key, chat_name, external_id, direction, sender, sent_at, kind, text, tracks_waiting)` → `(status, message_id)` | **сама проверяет подключение и согласие**: `stored`, `repeat`, `no_source`, `unknown_connection`, `disabled`, `no_consent`; заводит чат; сообщение владельца снимает «ждёт ответа» |
| `set_chat_transcript(owner, message_id, text)` → bool | расшифровка голосового, только до разбора |
| `edit_chat_message(owner, platform, connection_id, chat_key, external_id, text)` → bool | правка до разбора |
| `erase_chat_messages(owner, platform, connection_id, chat_key, external_ids)` → int | удаление стирает текст |
| `chats_to_analyze(owner, quiet_before, stale_before)` → чаты | есть неразобранные, затих или первое старше 2 ч; по возрасту |
| `record_chat_analysis(owner, thread_id, message_ids, analysis, ai_model, input_tokens, output_tokens, duration_ms, chat_with, waiting, tasks)` → uuid | одна транзакция: разбор, пометка, задачи с напоминаниями, «ждёт ответа», `failures = 0`; повтор — `null` |
| `chat_failed(owner, thread_id)` → int | неудача подряд +1 |
| `skip_chat_messages(owner, thread_id, message_ids)` → uuid | разбор `skipped` без дел, пометка, `failures = 0` |
| `chat_report(owner, analysis_id)` → строки дел | площадка, имя, `chat_with`, номер, задача, суть, срок, обещание, статус |
| `mark_chat_report_sent(owner, analysis_id, telegram_message_id)` → bool | «отправить → пометить» |
| `drop_chat_task(owner, analysis_id, item)` → `tasks` | «Убрать»: `cancelled` и неотправленные напоминания стёрты |
| `chats_waiting(owner, asked_before)` → чаты | ждут ответа дольше 3 ч, не напомнено, владелец не писал после вопроса |
| `mark_waiting_reminded(owner, thread_id, waiting_since)` → bool | напоминание ушло |
| `erase_old_chat_messages(owner, before)` → int | срок 7 дней |

Чтение кусков (новые и «раньше»), отчётов к отправке и площадок к вопросу —
обычные `select` в `db/chats.py` с фильтром владельца.

## Бот

- `db/chats.py` — вызовы и разбор ответов, владелец всегда явным аргументом.
- `services/chats.py` — всё про чаты: схема `ChatAnswer`, правила и промпт,
  вызов модели, строки переписки, `ChatService` (приём, согласие, разбор в
  фоне, отчёты, «ждёт ответа», стирание). Хранилище — протокол `ChatStore`
  поверх `db/chats.py`; Telegram — замыкания из `runner.py` (отправка
  владельцу, `getBusinessConnection`); тест подставляет свои.
- `handlers.py` — `build_business_router()`: четыре бизнес-обновления; чистая
  `chat_message_of(message, owner)` разбирает сообщение в поля; кнопки
  `consent:` и `drop:` — в основном роутере (нажимает владелец).
- `middlewares.py` — бизнес-обновления проходят `OwnerOnlyMiddleware` без
  проверки отправителя и **без ответа**: их отбирает роутер по подключению.
- `runner.py` — сборка `ChatService`, роутер, шаг тика.
- `services/reminders.py` — шаг `chats.tick(now)` после поисков; ушедшее
  считается в числе отправленного.
- `services/batches.py` — публичная `render_line` (строка «время имя:
  текст»), ею же пользуется `conversation_text`.
- `services/understanding.py` — публичная `open_task_lines` для короткого
  блока открытых задач.

## Решения по неоднозначностям

1. **Падеж имени и род.** «Из переписки с Игорем», «Вы не ответили Игорю — он
   спрашивал…» бот сам не склонит (Игорь, латиница, фамилии, род). Узкая
   схема получает два поля строкой: `with_whom` (творительный, после «с») и
   `to_whom` (дательный); фраза `waiting` — от третьего лица с местоимением по
   полу. Бот режет их по длине; пусто — имя как есть. Это не сверх схемы
   §25.3 по смыслу — те же «о чём», только с нужными формами.
2. **«(обещали вам)»** вместо «(обещал вам)»: без рода собеседника.
3. **Затих / 2 часа / 7 дней — по времени прихода** (`created_at`,
   `last_message_at`), не по времени площадки: у пересланного в MAX время
   площадки старое, и оно сразу бы «затихло» и стёрлось.
4. **`is_from_offline`** (автоответ, приветствие, отложенное) — пропуск, как
   `sender_business_bot`: это не владелец ответил.
5. **Разбор — в фоне**, по одному чату за раз (как поиски): тик только
   запускает обработчик, если он не идёт, и не ждёт модели. Отчёт шлёт тик —
   до минуты после разбора; так отправка одна, без гонок.
6. **Кусок.** Новые — до 50 старейших неразобранных; строки «раньше» (до 20
   разобранных) выпадают первыми, если текст длиннее 8000; новые берутся,
   пока влезают (хотя бы одно), остальные ждут следующего разбора. Новых со
   словами нет (всё стёрто удалением) — `skipped` без модели.
7. **«Ждёт ответа».** С какого времени — последнее `in` сообщение куска.
   Чат уже ждёт и не напомнено — время прежнее, фраза новая. Не пишется,
   если владелец писал в чат после этого времени. Разбор без «ждёт ответа»
   прежнее не снимает — снимает только сообщение владельца.
8. **Отчёт строится при отправке из базы** (`chat_report`), тем же кодом
   после «Убрать»: убранное — «— убрано», сделанное — «— сделано», кнопки
   только у активных. Одно дело — одной строкой после «записал:», кнопка
   «Убрать». Срок словами: «сегодня», «завтра», дальше день.
9. **Согласие** спрашивается без ночного окна: это ответ на действие
   владельца. Отказ и потом новое подключение (было выключено — включили) —
   спросить снова; согласие не переспрашивается. Кнопку можно нажать и
   после решения — решение меняется.
10. **Узкая схема:** без повтора, срочности, вида и `same_as`; дубль — по
    правилу промпта и блоку открытых задач. Дел не больше пяти (обрезает
    бот), пустая суть не пишется.
11. **Неудача** — только отказ модели (§5.4); сбой базы — следующий тик без
    счёта.
12. **Голосовое** расшифровывается при приходе: сообщение пишется с пустым
    текстом, потом расшифровка `set_chat_transcript`; подсказка — имя
    собеседника. Не вышло — пустой текст, в промпте «[голосовое, не
    расслышал]». «Прочее» — «[вложение]» с подписью.
13. **Стирание** — раз в час по памяти процесса.
14. **Журнал** — без текстов и имён: id, числа, статусы.
