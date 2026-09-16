# Соломон

Личный ИИ-секретарь в Telegram: принимает поручения голосом, текстом и
фотографией, понимает их обычным языком, сохраняет и напоминает в срок —
чтобы человек не держал свои дела в голове.

Документы: `idea.md` (зачем) → `spec.md` (что) → `techspec.md` (как) →
`design.md` (как выглядит). Правила работы — `CLAUDE.md`. Очередь работ —
`specs/README.md`. Визуал большого изменения одобряется прототипом до
кода — `prototype/README.md`.

## Как запустить

Нужны [uv](https://docs.astral.sh/uv/) и Node 24. Перед первым запуском
скопируйте `.env.example` в `.env` и заполните значения — без них процесс
не стартует и скажет, какой переменной не хватает.

```bash
uv run --directory bot solomon-bot     # бот: long polling, Ctrl+C останавливает
npm --prefix miniapp run dev           # Mini App: адрес печатает Vite
```

Жива ли база — `uv run --directory bot solomon-health`.
Проверки перед коммитом — `node scripts/gate.mjs` (см. `CLAUDE.md`
§«Сдача изменения»).
