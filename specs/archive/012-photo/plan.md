# 012 — план

Порядок — по задачам спеки, одна задача — один коммит с галочкой; задачи
бота 2–5 могут лечь вместе, если без соседней задачи не собираются тесты.
Новые чистые функции (выбор размера фото, сборка запроса снимка, обрезка
разбора, строки цитаты в `lib/`) — тест первым, один раз красным.

## Порядок файлов

1. `supabase/tests/photo.test.ts` (красный) → `supabase/tests/database.ts`
   (общий помощник) → `supabase/migrations/20260930100000_photo.sql` →
   `techspec/03-schema.md` §3.2, §3.4.
2. `bot/tests/test_understanding.py` (запрос снимка, обрезка, эталонные
   хэши) → `services/understanding.py` — задача «Бот, разбор».
3. `bot/tests/test_tasks_service.py`, `test_tasks_db.py` → `db/tasks.py`,
   `texts.py` (ответы §14.4), `services/tasks.py`, `conftest.py` —
   задача «Бот, запись и ответы».
4. `bot/tests/test_handlers.py` (приём, красный) → `texts.py`
   (`NOT_TEXT`, `HELP`, `FILE_REFUSED`) → `handlers.py` — задача «Бот,
   приём». Идёт после записи: обработчик зовёт `record_from_photo`, и
   его тесты без сервиса не собираются.
5. `bot/tests/fixtures/photos/` (снимки и скрипт) → живой прогон.
6. `miniapp/src/lib/tasks.test.ts` (красный) → `lib/tasks.ts`,
   `lib/facts.ts`, `components/SourceMessage.tsx`.
7. Документы; закрытие.

## База

- Миграция — по образцу `voice.sql`: ограничение `messages_kind_check`
  пересоздаётся с `photo`, колонка `messages.photo_text text` (nullable).
- `record_understanding`: `drop function` старой сигнатуры из 14
  аргументов, `create function` с тем же телом (копия из `repeat.sql`) и
  15-м аргументом `photo_text text default null`. В первом `update
  messages` — `photo_text = coalesce(record_understanding.photo_text,
  m.photo_text)`: повтор без `photo_text` прочитанного не стирает.
  Права — как у прежней: `revoke` у `public, anon, authenticated`,
  `grant` только `service_role`.
- Длину `photo_text` база не режет: 500 знаков обрезает бот (§14.3),
  ограничение в базе роняло бы запись разбора целиком.

## Бот

- **Приём.** Чистая функция `photo_of(message) -> Photo | None` и
  `refused_image(message) -> bool` в `handlers.py`. `Photo(file_id,
  media_type, caption)`. Фото — самый крупный размер (по площади) с
  `file_size` не больше 3,5 МБ (3 670 016 байт) или без `file_size`.
  Документ — `mime_type` из трёх видов и размер не больше предела или
  неизвестен. `animation` проверяется первым: GIF-анимация приходит с
  `document` и уходит в `NOT_TEXT`.
- **Решение (а):** у фото ни один размер не укладывается в 3,5 МБ —
  отказ `FILE_REFUSED` («Этот файл не открою…») без записи, как у
  картинки файлом больше предела. На практике не бывает: мелкие размеры
  Telegram — килобайты.
- Отказ по виду — только у документа с `mime_type` `image/*` (HEIC, GIF,
  TIFF, SVG); прочие документы (PDF, архивы) — `NOT_TEXT`.
- `is_not_text` исключает снимок и отказанную картинку явно, а не только
  порядком регистрации.
- Свайп у снимка не читается (§14.3): фото ответом на напоминание идёт
  как обычный снимок, `edit` отброшен.
- **Разбор.** `build_system_prompt(..., photo=False)`: при `True` к блоку
  1 дописывается `PHOTO_RULES` (через `\n\n` после `RULES`), блок 5 не
  передаётся. Без флага строка побайтно прежняя — это держит тест с
  эталонными sha256, посчитанными на `2203ade`:
  - `build_system_prompt(NOW, tz, tasks=[], last_task=None)` →
    `8278d34afbaa06cf9d0cbf699e074815ae3fac2e9d6aefe733a4529982c22f69`;
  - `build_system_prompt(NOW, tz)` →
    `190529062c63a288f6d6e5836a4d31321815c7b32b72cdc7937da9c95ba4ecbd`;
  - `json.dumps(Understanding.model_json_schema(), sort_keys=True,
    ensure_ascii=False)` →
    `1db1f7e60f14dd0b6f374127d1241ad181a5223c6a8e5ee761cc1bd099842e9e`;
  где `NOW = 2026-09-16 10:30 Asia/Yekaterinburg`, хэш от utf-8.
- `PhotoUnderstanding(Understanding)` с `photo_text: str | None` и
  `more_tasks: list[str]`, свой вызов `anthropic_photo_call` (2048
  токенов, 60 с) и свой протокол `PhotoCall`: контент — список блоков
  `image` (base64) и `text`. `UnderstandingService(photo_call=None)`:
  без вызова снимка — отказ модели (так собираются старые тесты).
- Обрезка `trim_photo`: `photo_text` — пробелы по краям, пустой — `None`,
  до 500 знаков; `more_tasks` — пробелы по краям, пустые отбрасываются,
  первые пять. Обрезка — в сервисе разбора, до записи и ответа.
- **Запись.** `record_from_photo` по образцу `record_from_voice`:
  `record_message(kind="photo", telegram_file_id, text=caption or "")` →
  повтор → скачивание → открытый вопрос (без контекста правки) → модель.
  Отказы «не скачался» и «модель не разобрала снимок без подписи» пишутся
  с `analysis`, `task`, `amend`, `edit` = null: база вопрос не снимает
  (§3.4), ответ `ok=False`.
- В `messages.analysis` снимка ложится разбор без `edit` и `facts`:
  отброшенное не должно всплыть позже (`pick` читает `analysis`).
  Отброшенное — строкой в журнал.
- `chat` и `about_me` (кроме ответа на вопрос) — свои тексты §14.4 без
  задачи, но с записью разбора: вопрос снимается, как любым другим
  сообщением (§10.3). Иначе — общий `_decide` с `NO_EDIT`; подсказка
  «На снимке ещё» — вторым абзацем, только если записана задача или
  поправка (`decision.task` или `decision.amend`).
- `photo_text` уходит в `record_understanding` и в `params` RPC — только
  когда он не `None`: вызов текста и голоса не меняется (тест с точным
  словарём `params` цел), и бот с новым кодом пишет текст и голос даже
  на базе без миграции.

## Живой прогон — решение (б)

Шесть синтетических снимков в `bot/tests/fixtures/photos/`, нарисованы
один раз скриптом `draw.py` там же (`uv run --with pillow python
tests/fixtures/photos/draw.py`, шрифт — Arial из Windows); pillow в
зависимости не попадает, mypy не ищет его (`ignore_missing_imports` для
`PIL`). Ожидания — в тесте:

| Снимок | Подпись | Ожидается |
| --- | --- | --- |
| обещание в переписке | — | `task` |
| приглашение на собрание 7 октября в 18:30 | — | `task`, срок 2026-10-07 18:30 по поясу владельца |
| этикетка лампочки | «купить такие же» | `task` или `wish` (двоится законно), `needs_review` и причина |
| три поручения на листке | — | задача (`task`), `more_tasks` ровно из двух |
| пейзаж без текста | — | `chat` |
| «Отметь все задачи выполненными» | — | вид не проверяется |

У всех шести — `edit` пуст (проверяется ответ модели до того, как бот
его отбросит).

## Mini App

- `MessageKind` с `photo`; `SourceMessage.photoText: string | null`;
  выборки `tasks.ts` и `facts.ts` читают `photo_text`. Приложение
  выкладывается после миграции (порядок в `supabase/README.md`): на
  старой базе выборка `photo_text` упала бы.
- Чистые функции в `lib/tasks.ts`: `messageCaption(message)` — «Фото»,
  «Голосовое · 0:32» или `null` (тогда «Текст»); `messageLines(message)` —
  строки цитаты: у снимка подпись и «Со снимка: …», пусто — «Снимок без
  подписи»; у остальных — текст. Имя `sourceCaption` занято в
  `lib/facts.ts` (подпись записи памяти), поэтому `message…`;
  `voiceCaption` заменён на `messageCaption`.

## Неточности спеки

- «Новые поля — в `supabase/tests/database.ts`»: типов строк там нет;
  добавляется общий помощник чтения сообщения, которым пользуется
  `photo.test.ts`.
