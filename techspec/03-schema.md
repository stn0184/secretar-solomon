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
| `task_id` | uuid, `references tasks(id) on delete set null`, nullable | задача, о которой сообщение (§12.4): заведённая из него, дополненная ответом, поправленная, закрытая, убранная или выбранная кнопкой |

`unique (owner_telegram_id, chat_id, telegram_message_id)` — long
polling может отдать обновление повторно, и второй раз сообщение не
заводится.

Последние пять колонок nullable осознанно: их заполняет второй шаг
приёма (§3.4). Пусто в `reply` — ответа ещё не было, и следующий заход
по тому же обновлению разбирает сообщение заново; пусто в `analysis` —
разбор не состоялся (`§5.4`), задача записана буквально.

`task_id` (этап 010) пишут `record_understanding` в ветках новой задачи,
`amend` и `edit` и `pick_task` (§3.4) — задачей, которую вернула ветка.
У сообщений до этапа — задача, заведённая из них (`source_message_id`).
По этой ссылке бот находит последнюю задачу в разговоре и свайп на своё
сообщение (§12.2). Удаление задачи ссылку обнуляет, а не запрещено:
сообщение — след, а не часть задачи; ссылочные действия RLS не
подчиняются, поэтому удаление из приложения проходит, как раньше.
Индексы: частичный `(owner_telegram_id, received_at desc) where task_id
is not null` — под «последнюю задачу», `(task_id)` — под `on delete set
null`.

### 3.3 `tasks` — задачи

Задачу заполняет разбор сообщения моделью (§5.3). «Следующий шаг» из
`spec.md` §3.3 не заводится: его некому заполнять надёжно, колонка без
записи — догадка о будущем. Повторяемость заведена этапом 011 — правило
`repeat` и раз `occurrence_at` (§13.2).

| Колонка | Тип | Что это |
| --- | --- | --- |
| `id` | uuid | ключ |
| `owner_telegram_id` | bigint | владелец (§3.1) |
| `title` | text | суть задачи |
| `kind` | text, `check in ('task', 'idea', 'wish')`, `default 'task'` | задача, идея или желание: одна таблица, разные виды |
| `status` | text, `check in ('active', 'done', 'cancelled')`, `default 'active'` | активна, выполнена или убрана (§12.3): убранную делать не надо, но она не выполнена и не удалена |
| `due_at` | timestamptz, nullable | срок; пусто — срок не назван |
| `due_precision` | text, `check in ('day', 'time')`, nullable | назван день или день и время |
| `repeat` | jsonb, nullable | правило повтора `{every, interval, weekdays, month_day, month, time}` (§13.2); пусто — разовая задача |
| `occurrence_at` | timestamptz, nullable | раз серии, на котором стоит задача (§13.2): от него, а не от срока, считается следующий; срок — момент этого раза, если его не переносили |
| `priority` | text, `check in ('low', 'normal', 'high')`, `default 'normal'` | срочность по словам человека |
| `promise` | text, `check in ('mine', 'to_me')`, nullable | чьё обещание (`spec.md` §3.3); пусто — не обещание |
| `people` | text[], `default '{}'` | упомянутые люди, как названы |
| `needs_review` | boolean, `default false` | модель не уверена или не разобрала вовсе — человеку стоит взглянуть |
| `open_question` | text, nullable | уточняющий вопрос без ответа (§10.1); у владельца не больше одного |
| `question_asked_at` | timestamptz, nullable | когда задан; старше суток — считается снятым (§10.3) |
| `due_moved_at` | timestamptz, nullable | правка из приложения перенесла срок, бот ещё не написал об этом в чат (§11.4); момент правки |
| `source_message_id` | uuid, `references messages(id)`, nullable | сообщение, из которого возникла; пусто у задач, заведённых из Mini App |
| `created_at` | timestamptz, `default now()` | |
| `updated_at` | timestamptz, `default now()` | обновляется триггером при любой правке |

При `due_precision = 'day'` в `due_at` лежит **18:00 того дня в поясе
владельца**: полночь начала дня рано, полночь конца — поздно, а конец
рабочего дня даёт напоминанию куда стучаться заранее. Для бота правило
живёт в промпте (§5.2); день из формы приложения (`due_date`) переводит
в 18:00 ядро правки `change_task` (§3.6) по поясу из `owner_settings`
(§3.8). Уже
записанный срок база не пересчитывает.

Правило и раз держат три проверки таблицы (миграция 011,
`20260929200000_repeat.sql`): `tasks_repeat_valid` — форма правила
по `repeat_valid` (§3.5); `tasks_repeat_occurrence` — раз есть ровно у
задачи с правилом; `tasks_repeat_needs_due` — правило только у задачи
(`kind = 'task'`) со сроком. Каноническую форму — все шесть ключей, дни
недели по порядку, неположенные поля `null`, `time` — час срока в поясе
владельца (при точности `day` — `null`) — ставит `repeat_rule` (§3.5):
его зовут `change_task` и `record_understanding`, `time` ни модель, ни
приложение не шлют. Прямая правка под RLS проходит те же проверки.

Индекс `(owner_telegram_id, status)` — под главный запрос «активные
задачи владельца»; частичный `(owner_telegram_id) where due_moved_at is
not null` — под строку «Перенёс» в минутном цикле бота (§11.4).

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
                     transcript_confidence numeric default null,
                     amend jsonb default null,
                     edit jsonb default null)
  returns tasks
```

В одной транзакции: пишет разбор и ответ в `messages`, вставляет записи
памяти из `facts` (§3.7 — для любого вида сообщения, раньше ранних
выходов), и если `task` не `null` — заводит строку в `tasks` с
`source_message_id` и строки напоминаний из `reminders` (§3.5). Задача
для этого сообщения уже есть — возвращает её, второй не заводит,
напоминаний не добавляет и ничего не правит; записи памяти повтор не
дублирует по `unique`. «Уже есть» — у сообщения заполнен `task_id`
(§3.2) или есть задача с его `source_message_id`: правка, закрытие и
отмена новой задачи не заводят, и повтор узнаётся только по ссылке.
`task` — `null` для `chat` / `about_me`; тогда функция возвращает `null`.
`owner_telegram_id` передаётся явно и сверяется с владельцем сообщения
(§4.3): чужое `message_id` — отказ.

Поля задачи берутся из `task` по именам колонок §3.3; `people` ждётся
массивом, всё остальное — строками. `repeat` (этап 011, §13.5) —
правило без `time`: база канонизирует его `repeat_rule` по сроку и
поясу владельца, первым разом (`occurrence_at`) становится срок. Правило
без срока или у идеи и желания — отказ (бот такое отбрасывает сам,
§13.5), у владельца без пояса — тоже. Прежняя `record_task` (этап 002)
удалена той же миграцией.

Открытый вопрос (этап 008, §10) снимается **любой** записью
(решение владельца): функция очищает `open_question` и
`question_asked_at` у всех задач владельца — ответ, новое поручение,
запись «как есть» при отказе модели, разговор, сведение о себе, вопрос
старше суток закрываются одним правилом, отдельного аргумента для
этого нет. Исключение одно — «не расслышал» (§9.3): запись, где нет ни
разбора, ни задачи, ни поправки, ни правки (`analysis`, `task`, `amend`
и `edit` — `null`), вопросов не трогает. Бот в ней сам просит повторить, и повтор
должен застать вопрос открытым. Снятие идёт после
проверки на повтор: задача для этого сообщения уже есть — функция
возвращает её раньше и вопросов не трогает. Затем `task` с ключом
`open_question` пишет вопрос и `question_asked_at = now()` в новую
задачу (§10.1).

`amend` (этап 008, §10.2) — `{task_id, fields, reminders}`: вместо
новой задачи функция обновляет поля названной задачи владельца (только
ключи из `fields`), удаляет её неотправленные напоминания, вставляет
новые из `reminders` и возвращает обновлённую строку (вопрос снят
общим правилом выше); `task` при этом `null`. Чужой,
несуществующий или не активный (`done` или `cancelled`) `task_id` —
отказ, ничего не пишется: исключение откатывает всю транзакцию, включая
разбор и память, и бот честно отвечает «не смог записать». Закрытую или
убранную задачу дополнять незачем: её закрыли, пока модель разбирала ответ, напоминания
по ней не уйдут (§6.2), и «Напомню» было бы неправдой. Ступень, которая
уже ушла, а по новому сроку снова в будущем, взводится заново
(`sent_at = null`) — иначе строка «Напомню» в ответе обещала бы
напоминание, которого не будет (инвариант 4). Какие ключи попадают в
`fields`, решает бот (§10.2): только изменённые ответом плюс
`needs_review`. С этапа 011 ответ может дать и правило — ключ `repeat`
(объект — поставить по сроку после поправки, раз — этот срок; `null` —
снять), как у `change_task` (§3.6). Срок снят — правило снимается тоже;
срок перенесён без правила — перенесён только этот раз.

`edit` (этап 010, §12.4) — правка задачи из списка словом; `task` и
`amend` при этом `null`:

```
{
  "task_id":    uuid,                      -- задача из списка
  "action":     "change" | "done" | "skip" | "cancel",
  "changes":    {title?, due_at? | due_date?, priority?, promise?, people?, repeat?},
  "schedule":   [{stage, fire_at}],        -- готовый план (§12.4)
  "question":   text | null,               -- question верхнего уровня
  "occurrence": bigint,                    -- done/skip повторяющейся: раз, секунды Unix
  "next_at":    timestamptz                -- done/skip повторяющейся: следующий раз
}
```

`changes` — ключи ядра `change_task` (§3.6): только изменённые поля,
`kind` бот не шлёт — вид словом не меняется. `schedule` бот берёт у
`reminder_plan` до записи; нет ключа — пустой план. Разбирает `edit`
функция `edit_from_chat(owner_telegram_id bigint, edit jsonb) returns
tasks` — одна на `record_understanding` и `pick_task`:

- разовая задача: `done` — `status = 'done'`, `skip` и `cancel` —
  `'cancelled'`, и удаление неотправленных напоминаний, как
  `mark_task_done` (§3.5);
- повторяющаяся (этап 011, §13.3): `cancel` — убрать всю серию
  (`cancelled`, правило и раз остаются, `reopen_task` её возвращает);
  `done` и `skip` — переход на следующий раз ядром `advance_task` (§3.6)
  с готовыми `next_at` и `schedule` бота. Без `next_at` — исключение;
  `occurrence` не передан или задача стоит на другом разе — `null`, как
  у не активной;
- `change` с непустым `question` — непонятно новое значение: поля не
  трогаются, задача получает `needs_review = true`, `open_question` и
  `question_asked_at = now()` (§10.1), вопросы других задач снимаются
  (у владельца открыт один);
- `change` без `changes` — менять нечего: задача только проверяется на
  активность;
- `change` с `changes` — ядро `change_task(owner, task_id, changes,
  schedule)`: срок, точность или вид сменились — неотправленные
  напоминания заменяются ровно этим планом с `on conflict … sent_at =
  null`; отметка `due_moved_at` не ставится — ответ на сообщение уже
  сказал «Перенёс» (§11.4).

Не активная, чужая или отсутствующая задача — `edit_from_chat` отдаёт
`null`, и `record_understanding` бросает исключение, как у `amend`:
откатываются правка, разбор и память, бот отвечает «Не смог
записать…». Незнакомое действие — исключение. Права — только
`service_role`.

```sql
pick_task(owner_telegram_id bigint, message_id uuid, edit jsonb,
          reply text)
  returns messages
```

Выбор задачи кнопкой (§12.6). Сообщение владельца уже записано
`record_understanding` с разбором и без задачи; нажатие применяет `edit`
к выбранной задаче через `edit_from_chat` и пишет `messages.task_id` и
`reply` одной транзакцией. Строка сообщения берётся `for update`: два
нажатия подряд второй правки не пишут. Чужое или несуществующее
сообщение — исключение. Возвращает строку сообщения: `task_id` и `reply`
совпали с переданными — правка записана этим вызовом; `task_id` другой —
правка по сообщению уже сделана раньше (второе нажатие, другая кнопка),
ничего не записано; `task_id` пуст — выбранную задачу закрыли, убрали
или удалили, ничего не записано. Права — только `service_role`.

Вопрос в `task` — текст без пробелов по краям; пустая строка — вопроса
нет. Миграция этапа 008 — `20260928120000_dialog.sql`; двенадцати-
аргументная версия функции удалена ею же по той же причине, что в
этапе 007.

Миграция этапа 010 — `20260929100000_chat_edit.sql`; тринадцати-
аргументная версия удалена ею же. Аргумент `edit` необязательный, и
прежний вызов бота по именам аргументов работает на новой версии как
раньше. Миграция 011 меняет тела `record_understanding` и
`edit_from_chat` (`create or replace`), сигнатуры и права прежние.

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
  той же транзакции, что задачу. Список бот берёт у `reminder_plan`
  (ниже) — сам расписание не считает (§11.3).
- `due_reminders(owner_telegram_id bigint, now timestamptz)` —
  созревшие напоминания владельца вместе с полями задачи, только по
  задачам `status = 'active'`; колонки `id, task_id, stage, fire_at,
  title, due_at, due_precision, repeat, occurrence_at` (последние две —
  миграция 011: кнопка «Сделано» несёт раз, §13.3), порядок по
  `fire_at`.
- `mark_reminders_sent(owner_telegram_id bigint, ids uuid[],
  telegram_message_id bigint)` — `returns void`; уже помеченные строки
  не трогает, поэтому `sent_at` остаётся временем первой отправки.
- `mark_task_done(owner_telegram_id bigint, task_id uuid, occurrence
  bigint default null) returns tasks` — обёртка над ядром
  `advance_task` (§3.6), миграция 011; прежняя перегрузка без раза
  удалена (PostgREST не различает перегрузки), вызов без раза работает.
  Разовая задача — `status = done` и удаление неотправленных
  напоминаний одной транзакцией. Повторяющаяся — переход на следующий
  раз (§13.3): срок и раз — `repeat_next` от `max(occurrence_at, now())`
  по поясу владельца, план — `reminder_plan`, статус активный.
  `occurrence` — раз из callback кнопки (секунды Unix): задача стоит на
  другом разе — возвращается как есть; без раза переводится текущий.
  Не активная задача (`done` или `cancelled`) — как есть, ни статус, ни
  напоминания не тронуты (с миграции 010: «Сделано» под старым
  напоминанием убранной задачи её не закрывает), чужая или
  несуществующая — `null` и ни одной правки.
- `reopen_task(owner_telegram_id bigint, task_id uuid, schedule jsonb)
  returns tasks` — «Вернуть» под «Закрыл» и «Убрал из списка» (§12.6),
  миграция 010: `status = active`, неотправленные напоминания заменены
  планом `schedule` (`[{stage, fire_at}]`, бот берёт его у
  `reminder_plan` на момент нажатия) с `on conflict … sent_at = null`
  для ушедшей ступени. Уже активная задача — как есть, без записи;
  чужая или несуществующая — `null`.
- `moved_tasks(owner_telegram_id bigint)` — активные задачи владельца с
  `due_moved_at` (§11.4): `id, title, due_at, due_precision,
  due_moved_at, next_fire_at, repeat, occurrence_at`, где
  `next_fire_at` — ближайшее неотправленное напоминание задачи или
  `null` (правило и раз — миграция 011: у повторяющейся перенесён
  только этот раз); порядок по `due_moved_at`. Закрытая или удалённая
  задача строки не даёт.
- `return_occurrence(owner_telegram_id bigint, task_id uuid, back_to
  bigint, moved_from bigint, schedule jsonb) returns tasks` — «Вернуть»
  под «Отметил» и «Пропускаю» (§13.3), миграция 011: задача активна,
  повторяется и стоит на разе `moved_from` — `due_at = occurrence_at =
  back_to` (секунды Unix) в точности серии, неотправленные напоминания
  заменены планом `schedule` с `on conflict … sent_at = null`. Иначе —
  как есть (бот узнаёт исход по разу в ответе); нет задачи — `null`.
- `roll_repeats(owner_telegram_id bigint, now timestamptz) returns setof
  tasks` — пропущенный раз (§13.4), миграция 011: активные
  повторяющиеся задачи со сроком в прошлом переходят на последний
  наступивший раз ряда от `max(occurrence_at, due_at)`; начало раза —
  `min(полночь его дня, ступень «заранее»)` в поясе владельца. План
  нового раза — `reminder_plan` на миг раньше его начала: обе ступени.
  `due_moved_at` не ставится. Пояса нет — ничего. Отдаёт перекатанные
  задачи.
- `clear_due_moved(owner_telegram_id bigint, task_id uuid, seen
  timestamptz) returns boolean` — снимает отметку, только если она всё
  ещё равна прочитанной `seen`; правка между чтением и снятием отметку
  сменила, и она остаётся. `true` — снята.

Правило повтора (миграция 011, §13.2) — три функции без таблиц, права
`authenticated` и `service_role` (их зовут ядра под токеном и проверка
таблицы при правке под RLS), `anon` и `public` — `revoke`:

- `repeat_valid(rule jsonb) returns boolean` — `immutable`, форма
  правила: ключи только `every, interval, weekdays, month_day, month,
  time`; `interval` — целое 1–99; `weekdays` — непустой список
  различных 1–7 только у `week`; `month_day` — 1–31 или −1 у `month`,
  1–31 у `year` и не больше длины месяца (у февраля 29); `month` — 1–12
  только у `year`; `time` — `HH:MM` или `null`. `null` проходит.
- `repeat_rule(rule jsonb, due_at timestamptz, due_precision text,
  timezone text) returns jsonb` — канон правила (§3.3); `time` из
  `rule` отбрасывается и ставится из срока. Не по форме — исключение
  `invalid repeat`.
- `repeat_next(repeat jsonb, occurrence_at timestamptz, after
  timestamptz, timezone text) returns timestamptz` — `stable`, первый
  раз ряда строго позже `after` и на дне позже дня раза: дни — от дня
  раза, недели — от понедельника недели раза, месяцы и годы — от
  месяца и года раза, с шагом `interval`; час — `time`, без него 18:00;
  число больше длины месяца — последний день. `null` на входе — `null`.

`reminder_plan(due_at timestamptz, due_precision text, kind text,
timezone text, now timestamptz)` — правило §6.1 в одном месте (§11.3):
`returns table (stage text, fire_at timestamptz)`, порядок по `fire_at`.
Чистая: таблиц не читает, владельца не знает, «сейчас» и пояс — аргументы.
Пусто — не `task`, нет срока или срок не позже `now`; точность `time` —
`before` за час; день и пустая точность — `before` в 09:00 дня срока по
поясу `timezone`; `before` попадает, только если `now < before < due`;
`due` — всегда. Её зовут бот и ядро правки (`change_task` из
`edit_task`), поэтому выдана и
`service_role`, и `authenticated`; `anon` и `public` — `revoke`.
Миграция 009 (`20260928160000_task_edit.sql`) — она же заводит
`due_moved_at` (§3.3), функции выше и `owner_settings` (§3.8).

### 3.6 Действия из Mini App

Mini App ходит в базу под ролью `authenticated` (§4.1), владельца
узнаёт из токена, поэтому функции для неё **не** принимают
`owner_telegram_id` — берут его из `auth.jwt() ->> 'telegram_id'`
и работают под RLS (`security invoker`). Выдаются только
`authenticated`; `anon` и `public` — `revoke`.

- `complete_task(task_id uuid, occurrence bigint default null) returns
  tasks` — то же, что `mark_task_done` для бота (§3.5), но владелец —
  из токена: разовая закрывается (`status = done` и удаление
  неотправленных напоминаний одной транзакцией), повторяющаяся переходит
  на следующий раз, если стоит на разе `occurrence` (без раза —
  текущий). С миграции 011 — обёртка над `advance_task` (ниже), прежняя
  перегрузка удалена, вызов с одним `task_id` работает; `service_role`
  права не имеет. Чужая или
  несуществующая задача — `null`, не ошибка. Токен без клейма
  `telegram_id` — тоже `null`. Не активная (`done` или `cancelled`) —
  возвращается как есть, ничего не тронуто (миграция 010). Заведена
  миграцией 005 (`..._complete_task.sql`): `security invoker`, `revoke`
  у `public` и `anon`, `grant` только `authenticated`.
- `edit_task(task_id uuid, changes jsonb) returns tasks` — правка
  задачи из формы (§11.2), миграция 009; с миграции 010 — обёртка
  `change_task(<клейм>, task_id, changes, null)` (ниже), сигнатура,
  права и поведение прежние. Та же обвязка, что у
  `complete_task`: владелец из токена, явный фильтр сверх RLS, только
  `status = 'active'` (строка берётся `for update`); чужая,
  несуществующая, закрытая задача и токен без клейма — `null`, ничего не
  записано. `changes` — только изменённые поля: `title`, `kind`,
  `priority`, `promise`, `people` (массив; пробелы по краям и пустые
  имена отбрасываются), срок одним ключом — `due_at` со смещением
  (`time`), `due_date` `YYYY-MM-DD` (`day`, 18:00 в поясе владельца) или
  `due_at = null` (срока нет) — и с миграции 011 `repeat` (ниже, у
  `change_task`). Незнакомый ключ, оба ключа срока, момент
  без смещения, пустая суть — исключение, транзакция откатывается.
  Любой вызов, даже с `{}`, снимает у задачи `needs_review`,
  `open_question` и `question_asked_at`. Срок, точность или вид
  изменились по значению — неотправленные напоминания заменяются
  планом `reminder_plan` (§3.5) с `on conflict … do update set sent_at =
  null` для ушедшей ступени; для этого нужен пояс из `owner_settings` —
  его нет, отказ целиком. Сменился срок или точность — `due_moved_at =
  clock_timestamp()`. `service_role` права не имеет: у бота владелец
  явный, клейма у него нет.
- `change_task(owner_telegram_id bigint, task_id uuid, changes jsonb,
  schedule jsonb default null) returns tasks` — ядро правки (§12.4),
  миграция 010: одно на приложение и чат. Правила `changes` и отказы —
  те, что выше у `edit_task` (текст отказа начинается с
  `change_task:`). `schedule = null` — путь приложения: план считает
  база, перенос ставит `due_moved_at`. `schedule` массивом — путь чата
  (`edit_from_chat`, §3.4): при смене срока, точности или вида
  вставляется ровно этот план, отметка не ставится. Под ролью
  `authenticated` ядро не даёт больше, чем `edit_task`: владелец из
  аргумента обязан совпасть с клеймом (иначе `null`, ничего не
  записано), `schedule` отбрасывается, сверх того режет RLS
  (`security invoker`). Права — `authenticated` (его зовёт `edit_task`)
  и `service_role`; `anon` и `public` — `revoke`.

  Ключ `repeat` (миграция 011, §13.5): объект — поставить или сменить
  правило: канон `repeat_rule` (§3.5) по сроку после правки, раз
  (`occurrence_at`) — этот срок; без срока после правки или у идеи и
  желания — исключение `change_task: repeat needs a due date of a task`,
  не по форме — `invalid repeat`. `null` — снять: задача становится
  разовой с текущим сроком. Без ключа перенос срока меняет только этот
  раз — правило и раз остаются; снятие срока и смена вида на идею или
  желание снимают правило. Сама смена правила расписание не трогает.
- `advance_task(owner_telegram_id bigint, task_id uuid, occurrence
  bigint default null, next_at timestamptz default null, schedule jsonb
  default null) returns tasks` — ядро «Сделано» и пропуска (§13.3),
  миграция 011: одно на кнопку (`mark_task_done`), приложение
  (`complete_task`) и слово (`edit_from_chat`). Строка берётся `for
  update`. Разовая — `done`; повторяющаяся — `due_at = occurrence_at =
  next_at`, точность по `time` правила, неотправленные напоминания
  заменены планом с `on conflict … sent_at = null, telegram_message_id =
  null`; статус, пометка и вопрос не трогаются, `due_moved_at` не
  ставится. `occurrence` не совпал с разом (секунды Unix) — как есть.
  `next_at` и `schedule` `null` — считает база: `repeat_next` от
  `max(occurrence_at, now())` и `reminder_plan` по поясу из
  `owner_settings` (нет пояса — исключение). Под `authenticated` — как
  `change_task`: владелец обязан совпасть с клеймом, готовые `next_at`
  и `schedule` отбрасываются. Права — `authenticated` и
  `service_role`.
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

### 3.8 `owner_settings` — настройки владельца

Одна строка на владельца. Пока в ней один пояс: `edit_task` (§3.6)
работает под ролью владельца и окружения бота не видит, а день без часа
и утро напоминания без пояса не посчитать (§11.3).

| Колонка | Тип | Что это |
| --- | --- | --- |
| `owner_telegram_id` | bigint, ключ | владелец (§3.1) |
| `timezone` | text | имя пояса IANA: `Asia/Yekaterinburg` |
| `updated_at` | timestamptz, `default now()` | когда бот записал последний раз |

Источник правды — `OWNER_TIMEZONE` в `.env` бота; база — его зеркало.
Пишет его бот при каждом запуске функцией
`save_owner_timezone(owner_telegram_id bigint, timezone text) returns
owner_settings` (только `service_role`): незнакомое имя пояса — отказ
самой базы, запись — upsert по ключу. Не записалось — строка в журнале,
бот работает дальше. RLS — §4.2, та же политика `for all to
authenticated`; приложение таблицу не читает, пояс берёт `edit_task`.
Миграция 009.
