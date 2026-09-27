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
| `kind` | text, `check in ('text', 'voice', 'video_note')` | вид сообщения (§9.1); фото добавит своё значение |
| `text` | text, `default ''` | текст сообщения как есть; у голоса — расшифровка, пустая до неё (§9.3) |
| `telegram_file_id` | text, nullable | у голоса и кружка — файл в Telegram, по нему можно скачать снова |
| `duration_seconds` | int, nullable | длительность звука — мера стоимости распознавания |
| `transcript_confidence` | numeric, nullable | уверенность распознавания 0–1 (§9.4) |
| `received_at` | timestamptz, `default now()` | когда бот его получил |
| `analysis` | jsonb, nullable | что модель поняла: её ответ по схеме §5.3 целиком |
| `ai_model` | text, nullable | какая модель разбирала |
| `ai_input_tokens` | int, nullable | токенов на входе — мера стоимости (`spec.md` §5) |
| `ai_output_tokens` | int, nullable | токенов на выходе |
| `reply` | text, nullable | что бот ответил на это сообщение |

`unique (owner_telegram_id, chat_id, telegram_message_id)` — long
polling может отдать обновление повторно, и второй раз сообщение не
заводится.

Последние пять колонок nullable осознанно: их заполняет второй шаг
приёма (§3.4). Пусто в `reply` — ответа ещё не было, и следующий заход
по тому же обновлению разбирает сообщение заново; пусто в `analysis` —
разбор не состоялся (`§5.4`), задача записана буквально.

### 3.3 `tasks` — задачи

Задачу заполняет разбор сообщения моделью (§5.3). «Следующий шаг» и
повторяемость из `spec.md` §3.3 не заводятся: их некому заполнять
надёжно, колонка без записи — догадка о будущем.

| Колонка | Тип | Что это |
| --- | --- | --- |
| `id` | uuid | ключ |
| `owner_telegram_id` | bigint | владелец (§3.1) |
| `title` | text | суть задачи |
| `kind` | text, `check in ('task', 'idea', 'wish')`, `default 'task'` | задача, идея или желание: одна таблица, разные виды |
| `status` | text, `check in ('active', 'done')`, `default 'active'` | активна или выполнена |
| `due_at` | timestamptz, nullable | срок; пусто — срок не назван |
| `due_precision` | text, `check in ('day', 'time')`, nullable | назван день или день и время |
| `priority` | text, `check in ('low', 'normal', 'high')`, `default 'normal'` | срочность по словам человека |
| `promise` | text, `check in ('mine', 'to_me')`, nullable | чьё обещание (`spec.md` §3.3); пусто — не обещание |
| `people` | text[], `default '{}'` | упомянутые люди, как названы |
| `needs_review` | boolean, `default false` | модель не уверена или не разобрала вовсе — человеку стоит взглянуть |
| `source_message_id` | uuid, `references messages(id)`, nullable | сообщение, из которого возникла; пусто у задач, заведённых из Mini App |
| `created_at` | timestamptz, `default now()` | |
| `updated_at` | timestamptz, `default now()` | обновляется триггером при любой правке |

При `due_precision = 'day'` в `due_at` лежит **18:00 того дня в поясе
владельца**: полночь начала дня рано, полночь конца — поздно, а конец
рабочего дня даёт напоминанию куда стучаться заранее. Правило живёт в
промпте (§5.2), база его не пересчитывает.

Индекс `(owner_telegram_id, status)` — под главный запрос «активные
задачи владельца».

### 3.4 Приём сообщения: две функции

Сообщение сохраняется **до** разбора моделью, разбор и задача — после,
одной транзакцией. Обе функции зовёт только бот (`service_role`;
`revoke execute … from public, anon, authenticated`).

```sql
record_message(owner_telegram_id bigint, chat_id bigint,
               telegram_message_id bigint, text text,
               kind text default 'text',
               telegram_file_id text default null,
               duration_seconds int default null)
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
                     reply text, task jsonb, reminders jsonb,
                     facts jsonb,
                     transcript text default null,
                     transcript_confidence numeric default null)
  returns tasks
```

В одной транзакции: пишет разбор и ответ в `messages`, вставляет записи
памяти из `facts` (§3.7 — для любого вида сообщения, раньше ранних
выходов), и если `task` не `null` — заводит строку в `tasks` с
`source_message_id` и строки напоминаний из `reminders` (§3.5). Задача
для этого сообщения уже есть — возвращает её, второй не заводит и
напоминаний не добавляет; записи памяти повтор не дублирует по `unique`.
`task` — `null` для `chat` / `about_me`; тогда функция возвращает `null`.
`owner_telegram_id` передаётся явно и сверяется с владельцем сообщения
(§4.3): чужое `message_id` — отказ.

Поля задачи берутся из `task` по именам колонок §3.3; `people` ждётся
массивом, всё остальное — строками. Прежняя `record_task` (этап 002)
удалена той же миграцией.

`transcript` и `transcript_confidence` (этап 007) — расшифровка голоса:
не `null` — становится `text` сообщения (§9.3); `null` — текст не трогается.
Прежние сигнатуры обеих функций без аргументов голоса удалены той же
миграцией: две перегрузки PostgREST различать нечем.

### 3.5 `reminders` — напоминания

Когда и о чём стучаться (§6). Живут в базе, чтобы пережить перезапуск
бота (`spec.md` §3.4); рождаются в той же транзакции, что задача.

| Колонка | Тип | Что это |
| --- | --- | --- |
| `id` | uuid | ключ |
| `owner_telegram_id` | bigint | владелец (§3.1) |
| `task_id` | uuid, `references tasks(id) on delete cascade` | о какой задаче |
| `stage` | text, `check in ('before', 'due')` | заранее или к сроку (§6.1) |
| `fire_at` | timestamptz | когда стучаться |
| `sent_at` | timestamptz, nullable | когда отправлено; пусто — ещё ждёт |
| `telegram_message_id` | bigint, nullable | сообщение в Telegram, под которым кнопка |
| `created_at` | timestamptz, `default now()` | |

`unique (task_id, stage)`; частичный индекс
`(owner_telegram_id, fire_at) where sent_at is null` — под запрос тика.
RLS — как у остальных (§4.2).

Функции (только `service_role`, как §3.4):

- `record_understanding` получает ещё один аргумент `reminders jsonb`
  (список `{stage, fire_at}`, может быть пустым) и вставляет строки в
  той же транзакции, что задачу.
- `due_reminders(owner_telegram_id bigint, now timestamptz)` —
  созревшие напоминания владельца вместе с полями задачи, только по
  задачам `status = 'active'`; колонки `id, task_id, stage, fire_at,
  title, due_at, due_precision`, порядок по `fire_at`.
- `mark_reminders_sent(owner_telegram_id bigint, ids uuid[],
  telegram_message_id bigint)` — `returns void`; уже помеченные строки
  не трогает, поэтому `sent_at` остаётся временем первой отправки.
- `mark_task_done(owner_telegram_id bigint, task_id uuid) returns tasks` —
  `status = done` и удаление неотправленных напоминаний задачи одной
  транзакцией; уже закрытая задача — возвращается как есть, чужая или
  несуществующая — `null` и ни одной правки.

### 3.6 Действия из Mini App

Mini App ходит в базу под ролью `authenticated` (§4.1), владельца
узнаёт из токена, поэтому функции для неё **не** принимают
`owner_telegram_id` — берут его из `auth.jwt() ->> 'telegram_id'`
и работают под RLS (`security invoker`). Выдаются только
`authenticated`; `anon` и `public` — `revoke`.

- `complete_task(task_id uuid) returns tasks` — `status = done` и
  удаление неотправленных напоминаний одной транзакцией; то же, что
  `mark_task_done` для бота (§3.5), но владелец — из токена. Чужая или
  несуществующая задача — `null`, не ошибка. Токен без клейма
  `telegram_id` — тоже `null`. Заведена миграцией 005
  (`..._complete_task.sql`): `security invoker`, `revoke` у `public`
  и `anon`, `grant` только `authenticated`.
- Удаление задачи — обычный `delete from tasks where id = …` под RLS;
  напоминания уходят каскадом (§3.5), сообщение-источник остаётся:
  это след, а не часть задачи.
- Чтение карточки: `tasks` + `messages` (по `source_message_id`) +
  `reminders` задачи — обычные `select` под RLS, функций не нужно.
  Политики `messages`, `tasks` и `reminders` объявлены `for all`, чтение
  и удаление в них входят; каскад `reminders` при удалении задачи
  срабатывает и под `authenticated` — проверки ссылочной целостности
  RLS не подчиняются. Mini App держит задачу из списка и докачивает
  только сообщение и напоминания — два запроса вместо трёх.

### 3.7 `facts` — память о пользователе

Что помощник знает о человеке (§8). Одна строка — одно обстоятельство
одной фразой.

| Колонка | Тип | Что это |
| --- | --- | --- |
| `id` | uuid | ключ |
| `owner_telegram_id` | bigint | владелец (§3.1) |
| `category` | text, `check in ('family','home','car','work','habit','preference','other')` | для группировки на экране |
| `text` | text | сама запись: «Машина — Toyota Camry» |
| `status` | text, `check in ('fact','guess')` | сказано прямо или выведено (§8.1) |
| `source_message_id` | uuid, `references messages(id) on delete set null`, nullable | откуда взялось; сообщение — след, его удаление память не стирает |
| `created_at` | timestamptz, `default now()` | |
| `updated_at` | timestamptz, `default now()` | триггер, как у `tasks` |

`unique (owner_telegram_id, category, text)` (§8.3); индекс
`(owner_telegram_id, status)`. RLS — §4.2, политика `for all to
authenticated`: приложение подтверждает (`update status`) и удаляет под
ней, функций для него не нужно.

`record_understanding` получает аргумент `facts jsonb` (список
`{category, text, status}`, может быть пустым; статус уже проставлен
ботом, §8.2) и вставляет строки в той же транзакции, что задача и
напоминания: `on conflict (owner_telegram_id, category, text) do update
set status = 'fact', source_message_id = excluded.source_message_id
where excluded.status = 'fact' and facts.status = 'guess'` — статус
только растёт (§8.3), а источником становится сообщение, где человек
сказал это прямо: по нему приложение подписывает запись «с ваших слов».
Миграция 006 (`..._facts.sql`).
