# §3. Схема данных

Таблицы заводятся вместе с кодом, который их заполняет, а не впрок:
колонка без записи — это догадка о будущем. Изменение схемы = миграция
и правка этого раздела **в одном коммите** (`techspec.md`
§«Правила правки»).

### 3.1 Общие правила

- Имена таблиц и колонок — `snake_case`, английские, таблицы во
  множественном числе.
- Ключ — `id uuid primary key default gen_random_uuid()`.
- **Каждая таблица с данными человека несёт `owner_telegram_id bigint
  not null`** — тот же Telegram-id, что в `OWNER_TELEGRAM_ID` и в клейме
  `telegram_id` токена. Это колонка, по которой фильтрует бот и по которой
  режут правила доступа (§4). Без неё таблица не заводится.
- Время — `timestamptz`, `created_at` с `default now()`.
- Колонка, без которой строка бессмысленна, — `not null`: пустое значение
  в ней означает не «неизвестно», а недописанный код. Nullable заводится
  осознанно и помечается в таблице раздела.
- Перечисления — `text` с `check (... in (...))`, не `enum`: значение
  добавляется миграцией без пересоздания типа.
- Миграции лежат в `supabase/migrations/<YYYYMMDDHHMMSS>_<что>.sql`,
  применяются `supabase db push` или тем же SQL в редакторе панели
  Supabase (`supabase/README.md`). Миграция не правится после того,
  как применена: следующая правка — следующий файл.

### 3.2 `messages` — входящие сообщения

Исходное сообщение хранится целиком (`spec.md` §3.1): из него сделан
разбор, и к нему возвращаются, когда разбор оказался неверным.

| Колонка | Тип | Что это |
| --- | --- | --- |
| `id` | uuid | ключ |
| `owner_telegram_id` | bigint | владелец (§3.1) |
| `chat_id` | bigint | чат Telegram, откуда пришло |
| `telegram_message_id` | bigint | id сообщения в этом чате |
| `kind` | text, `check in ('text')` | вид: пока только текст; голос и фото добавят свои значения |
| `text` | text | текст сообщения как есть |
| `received_at` | timestamptz, `default now()` | когда бот его получил |

`unique (owner_telegram_id, chat_id, telegram_message_id)` — long
polling может отдать обновление повторно, и второй раз сообщение не
заводится.

### 3.3 `tasks` — задачи

Колонки из `spec.md` §3.3 (срок, приоритет, следующий шаг, чьё
обещание, повторяемость) появятся вместе с этапом понимания поручений,
который их заполняет. Сейчас задача — это буквально текст сообщения.

| Колонка | Тип | Что это |
| --- | --- | --- |
| `id` | uuid | ключ |
| `owner_telegram_id` | bigint | владелец (§3.1) |
| `title` | text | суть задачи |
| `status` | text, `check in ('active', 'done')`, `default 'active'` | активна или выполнена |
| `source_message_id` | uuid, `references messages(id)`, nullable | сообщение, из которого возникла; пусто у задач, заведённых из Mini App |
| `created_at` | timestamptz, `default now()` | |
| `updated_at` | timestamptz, `default now()` | обновляется триггером при любой правке |

Индекс `(owner_telegram_id, status)` — под главный запрос «активные
задачи владельца».

### 3.4 Приём сообщения: две функции

Сообщение сохраняется **до** разбора моделью, разбор и задача — после,
одной транзакцией. Обе функции зовёт только бот (`service_role`;
`revoke execute … from public, anon, authenticated`).

```sql
record_message(owner_telegram_id bigint, chat_id bigint,
               telegram_message_id bigint, text text)
  returns messages
```

Вставляет строку в `messages`; если такая уже есть (`unique` §3.2) —
возвращает существующую, ничего не меняя. По возвращённой строке бот
видит повтор: у неё заполнен `reply` — ответ уже давался, модель не
зовётся, тот же текст отправляется снова. `reply` пуст — первый заход
упал между шагами, разбираем заново.

```sql
record_understanding(message_id uuid, owner_telegram_id bigint,
                     analysis jsonb, ai_model text,
                     ai_input_tokens int, ai_output_tokens int,
                     reply text, task jsonb)
  returns tasks
```

В одной транзакции: пишет разбор и ответ в `messages`, и если `task`
не `null` — заводит строку в `tasks` с `source_message_id`. Задача для
этого сообщения уже есть — возвращает её, второй не заводит. `task` —
`null` для `chat` / `about_me`; тогда функция возвращает `null`.
`owner_telegram_id` передаётся явно и сверяется с владельцем сообщения
(§4.3): чужое `message_id` — отказ.

Прежняя `record_task` (этап 002) удаляется той же миграцией.

Колонки `messages` под это (в дополнение к §3.2): `analysis jsonb`,
`ai_model text`, `ai_input_tokens int`, `ai_output_tokens int`,
`reply text` — все nullable: заполняются вторым шагом.
