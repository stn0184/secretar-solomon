"""Тексты бота. Все на русском, обращение на «вы», без канцелярита.

Листовой модуль: из `solomon` не импортирует ничего, поэтому слова можно
править, не боясь за поведение. Здесь же — русские названия дней и месяцев:
дата нужна и в ответе человеку, и в контексте момента для модели, и список
месяцев должен быть один.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


# «Перенести на …» (`techspec/12-chat-edit.md` §12.6): день недели в
# винительном падеже.
WEEKDAYS_ACCUSATIVE = (
    "понедельник",
    "вторник",
    "среду",
    "четверг",
    "пятницу",
    "субботу",
    "воскресенье",
)

# Короткий срок на кнопке кандидата (§12.6): «2 окт».
MONTHS_SHORT = (
    "янв",
    "фев",
    "мар",
    "апр",
    "мая",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
)


def format_day(moment: datetime) -> str:
    """«пятница, 20 сентября» — как человек называет день вслух."""
    return f"{WEEKDAYS[moment.weekday()]}, {moment.day} {MONTHS[moment.month - 1]}"


def format_time(moment: datetime) -> str:
    """«19:00»."""
    return f"{moment:%H:%M}"


def format_date(moment: datetime) -> str:
    """«25 сентября» — день без дня недели, когда важна сама дата."""
    return f"{moment.day} {MONTHS[moment.month - 1]}"


def format_due(due_at: datetime, precision: str | None) -> str:
    """Срок словами: день, а со временем — и час.

    Время показывается, только когда человек его назвал: у срока «в пятницу»
    в базе стоит 18:00 (`techspec/03-schema.md` §3.3), и произносить этот час
    вслух значило бы приписать человеку то, чего он не говорил.
    """
    day = format_day(due_at)
    if precision == "time":
        return f"{day}, {format_time(due_at)}"
    return day


def format_remind_at(fire_at: datetime, now: datetime) -> str:
    """Когда бот постучится: «сегодня в 18:00», «25 сентября в 09:00» (§6.4).

    Одно правило на все случаи: дата и время ближайшего напоминания, какой бы
    ступенью оно ни было. Оба момента ждутся в поясе владельца.
    """
    day = "сегодня" if fire_at.date() == now.date() else format_date(fire_at)
    return f"{day} в {format_time(fire_at)}"


def format_short_due(due_at: datetime, precision: str | None) -> str:
    """Короткий срок на кнопке: «2 окт», со временем — «2 окт, 17:00» (§12.6).

    Час — только когда его назвал человек, как у `format_due`.
    """
    day = f"{due_at.day} {MONTHS_SHORT[due_at.month - 1]}"
    if precision == "time":
        return f"{day}, {format_time(due_at)}"
    return day


def format_move_target(due_at: datetime, precision: str | None, now: datetime) -> str:
    """Новый срок в вопросе «Какую задачу перенести …?» (§12.6).

    «на пятницу, 2 октября», «на сегодня в 17:00», «на среду, 30 сентября,
    в 09:00». Оба момента ждутся в поясе владельца.
    """
    if due_at.date() == now.date():
        day = "на сегодня"
        joint = " "
    else:
        weekday = WEEKDAYS_ACCUSATIVE[due_at.weekday()]
        day = f"на {weekday}, {due_at.day} {MONTHS[due_at.month - 1]}"
        joint = ", "
    if precision == "time":
        return f"{day}{joint}в {format_time(due_at)}"
    return day


def format_due_moment(due_at: datetime, now: datetime) -> str:
    """Срок в самом напоминании: «сегодня, 18:00», «пятница, 25 сентября, 18:00».

    Здесь час называется всегда, даже у срока «в пятницу»
    (`techspec/06-reminders.md` §6.2): бот стучится именно в этот час, и
    человеку важно видеть, о каком моменте речь.
    """
    day = "сегодня" if due_at.date() == now.date() else format_day(due_at)
    return f"{day}, {format_time(due_at)}"


START = (
    "Здравствуйте. Я Соломон, ваш секретарь.\n\n"
    "Напишите или надиктуйте, что нужно сделать, — я запишу это задачей и подтвержу. "
    "Список задач открывается в приложении.\n\n"
    "/help — что уже работает."
)

HELP = (
    "Напишите или надиктуйте, что нужно сделать, — запишу задачей и повторю "
    "своими словами, чтобы вы видели, что я понял. Голосовые и видео-кружки "
    "распознаю сам, пересланные — тоже. Не расслышал — скажу об этом, а "
    "сообщение сохраню.\n\n"
    "/start — начать разговор.\n"
    "/help — этот список.\n\n"
    "Разбираю срок, суть и срочность: «в пятницу отправить расчёт» станет "
    "задачей со сроком. Идею и желание отличаю от дела, разговор задачей не "
    "делаю. Не уверен — записываю и говорю, чего не понял.\n\n"
    "Запоминаю сведения о вас — машину, семью, график. Скажете прямо («у меня "
    "Toyota Camry») — отвечу «Запомнил»; замеченное мимоходом в поручении "
    "запишу как предположение. Всё это видно и правится на вкладке «О себе» "
    "в приложении.\n\n"
    "О задаче со сроком напомню заранее и в срок; под напоминанием кнопка "
    "«Сделано» — нажмёте, и задача закроется. Напоминания приходят, пока я "
    "запущен: пропущенные придут при следующем запуске.\n\n"
    "Задачи видны в приложении — кнопка меню рядом с полем ввода. Там список "
    "по срокам и карточка с исходным сообщением (у голосового — с "
    "расшифровкой); задачу можно закрыть, изменить или удалить.\n\n"
    "Фотографии и файлы пока не понимаю."
)

STRANGER = "Это личный помощник. Он отвечает только своему владельцу."

# Пересказ — не вежливость, а проверка: человек сразу видит, что понято
# не то (`spec.md` §2.1, шаг 4). Задача, идея и желание — одна таблица,
# разные виды, и человеку это видно по первому слову ответа.
RECORDED_BY_KIND = {
    "task": "Записал: {title}",
    "idea": "Записал идею: {title}",
    "wish": "Записал желание: {title}",
}

NO_ERRAND = "Это не похоже на поручение — ничего не записал. Если это задача, скажите прямо."

# Сведение о себе, которое уже есть в памяти: модель ничего нового не отдала.
ALREADY_KNOWN = "Это я уже знаю — в памяти есть. Посмотреть можно во вкладке «О себе»."

# Сведение о себе записано в память (`techspec/08-memory.md` §8.2). Текст
# записи уходит человеку дословно от модели, как `review_reason` (§5.4):
# по нему человек видит, что именно запомнено, и правит в приложении.
REMEMBERED = "Запомнил: {facts}"


def remembered(items: Sequence[str]) -> str:
    """«Запомнил: Машина — Toyota Camry»; несколько записей — через «; »."""
    return REMEMBERED.format(facts="; ".join(items))


# Модель не ответила (`techspec/05-ai.md` §5.4): поручение не теряется, но и
# делать вид, что оно разобрано, нельзя.
RECORDED_AS_IS = "Записал как есть: «{text}». Разобрать сейчас не смог."


# Срочность словами: в подтверждении звучит только необычная, а модели в
# блоке открытого вопроса (`techspec/05-ai.md` §5.2) называется любая.
PRIORITY_NAMES = {"high": "высокий", "normal": "обычный", "low": "низкий"}
PRIORITY_WORDS = {key: f"Приоритет: {PRIORITY_NAMES[key]}" for key in ("high", "low")}


def _retold(
    head: str,
    due: str | None,
    remind_at: str | None,
    priority: str,
    tail: str | None,
) -> str:
    """Пересказ по частям: суть, срок, когда напомню, приоритет, последняя фраза."""
    parts = [head]
    if due:
        parts.append(f"Срок: {due}")
    if remind_at:
        parts.append(f"Напомню: {remind_at}")
    if priority in PRIORITY_WORDS:
        parts.append(PRIORITY_WORDS[priority])
    if tail:
        parts.append(tail)
    return ". ".join(parts)


def recorded_reply(
    kind: str,
    title: str,
    due: str | None = None,
    review_reason: str | None = None,
    priority: str = "normal",
    remind_at: str | None = None,
) -> str:
    """Подтверждение записи: суть, срок, когда напомню, приоритет (если не
    обычный) и причина «перепроверьте».

    Строка «Напомню» есть только тогда, когда напоминание вправду
    запланировано (§6.4): обещать её без плана значило бы сказать о том,
    чего не будет (инвариант 4).

    Из ответа модели дословно уходит только `review_reason`
    (`techspec/05-ai.md` §5.4) — остальное собрано здесь.
    """
    head = RECORDED_BY_KIND.get(kind, RECORDED_BY_KIND["task"]).format(title=title)
    return _retold(head, due, remind_at, priority, review_reason)


def asked_reply(
    title: str, question: str, due: str | None = None, remind_at: str | None = None
) -> str:
    """Запись с уточняющим вопросом (`techspec/10-dialog.md` §10.1).

    «Записал: отправить расчёт клиенту. К какому сроку?» — задача уже в базе,
    вторая фраза и есть вопрос. Приоритета и причины здесь нет: вопрос
    говорит, чего не хватает, а лишняя фраза перед ним его заслонила бы.
    Вопрос уходит дословно от модели, как `review_reason`.
    """
    return _retold(RECORDED_BY_KIND["task"].format(title=title), due, remind_at, "normal", question)


# Ответ на вопрос лёг в ту же задачу (§10.2): пересказ как при записи, но
# словом «Понял» — человек видит, что второй задачи не появилось.
UNDERSTOOD = "Понял: {title}"


def understood_reply(
    title: str,
    due: str | None = None,
    review_reason: str | None = None,
    priority: str = "normal",
    remind_at: str | None = None,
) -> str:
    """«Понял: отправить расчёт клиенту. Срок: пятница, 25 сентября. Напомню: …»."""
    return _retold(UNDERSTOOD.format(title=title), due, remind_at, priority, review_reason)


# Правка из приложения перенесла срок (`techspec/11-edit.md` §11.4): тот же
# пересказ, что у ответа на вопрос, но словом «Перенёс». Срок сняли — одна
# фраза, и обещаний больше нет.
MOVED = "Перенёс: {title}"
DUE_REMOVED = "Убрал срок: {title}. Напоминать не буду."


def moved_reply(title: str, due: str | None, remind_at: str | None) -> str:
    """«Перенёс: отправить расчёт клиенту. Срок: пятница, 2 октября. Напомню: …».

    `due` пуст — срок снят, и строка «Убрал срок». «Напомню» только тогда,
    когда напоминание вправду впереди (§6.4).
    """
    if not due:
        return DUE_REMOVED.format(title=title)
    return _retold(MOVED.format(title=title), due, remind_at, "normal", None)


# Правка задачи словом (`techspec/12-chat-edit.md` §12.5): одной строкой,
# тем же видом, что запись и «Перенёс» из приложения. Суть — из задачи после
# правки, срок и «Напомню» — из того, что записано.
MOVED_BY_WORD = "Перенёс: {title}"
FIXED = "Поправил: {title}"
REOPENED = "Вернул в работу: {title}"
CLOSED = "Закрыл: {title}."
CANCELLED = "Убрал из списка: {title}."
UNCLEAR_EDIT = "Не понял, как поправить: {title}. {question}"
NOTHING_TO_CHANGE = "Не понял, что поменять в задаче «{title}» — ничего не менял."
NOT_FOUND_RECORDED = "Не нашёл открытой задачи — записал новую: {title}"
NOT_FOUND = "Не нашёл открытой задачи «{title}» — ничего не менял."

# Кнопки правки (§12.6): вопрос «какую задачу» называет действие.
PICK_MOVE = "Какую задачу перенести {target}?"
PICK_REMOVE_DUE = "С какой задачи снять срок?"
PICK_DONE = "Какую задачу закрыть?"
PICK_CANCEL = "Какую задачу убрать из списка?"
PICK_CHANGE = "Какую задачу поправить?"
REOPEN_BUTTON = "Вернуть"
PICKED_GONE = "Задачу уже закрыли или удалили — ничего не менял."
# Нажатие не записалось: вопрос с кнопками остаётся, и сказать об этом надо
# прямо (инвариант 4) — как «Сделано», когда база не ответила.
NOT_PICKED = "Не смог записать правку: база не ответила. Попробуйте ещё раз."
NOT_REOPENED = "Не смог вернуть задачу: база не ответила. Попробуйте ещё раз."
# Людей в правке не осталось: «Люди: …» с пустым списком читается как обрыв.
NOBODY = "никого"


def edited_reply(
    head: str,
    due: str | None,
    remind_at: str | None = None,
    priority: str | None = None,
    people: Sequence[str] | None = None,
) -> str:
    """Ответ на правку словом: «Перенёс: …», «Поправил: …», «Вернул в работу: …».

    `priority` и `people` — только когда правка их сменила: срочность
    звучит словом и тогда, когда она вернулась к обычной, люди — списком
    целиком (§12.5). «Напомню» — только о напоминании, которое вправду
    впереди (§6.4).
    """
    parts = [head]
    if due:
        parts.append(f"Срок: {due}")
    if remind_at:
        parts.append(f"Напомню: {remind_at}")
    if priority is not None:
        parts.append(f"Приоритет: {PRIORITY_NAMES.get(priority, priority)}")
    if people is not None:
        parts.append(f"Люди: {', '.join(people) if people else NOBODY}")
    return ". ".join(parts)


def not_found_reply(
    title: str,
    due: str | None = None,
    review_reason: str | None = None,
    priority: str = "normal",
    remind_at: str | None = None,
) -> str:
    """Перенос задачи, которой нет в списке, записан новой задачей (§12.3)."""
    return _retold(NOT_FOUND_RECORDED.format(title=title), due, remind_at, priority, review_reason)


# Напоминание и кнопка под ним (`techspec/06-reminders.md` §6.2, §6.3).
DONE_BUTTON = "Сделано"
DONE_MARK = "✓ Сделано"
DONE_ANSWER = "Задача закрыта."
DONE_UNKNOWN = "Не нашёл эту задачу."


def reminder(title: str, due: str | None, overdue: bool) -> str:
    """Текст напоминания: о чём и к какому сроку.

    `overdue` — срок уже прошёл в момент отправки (бот был выключен): тогда
    «Срок был», и человек видит, что напоминание догоняет, а не опережает.
    Опоздание самого напоминания на текст не влияет.
    """
    lines = [f"Напоминаю: {title}"]
    if due:
        lines.append(f"{'Срок был' if overdue else 'Срок'}: {due}")
    return "\n".join(lines)


def done_message(text: str) -> str:
    """Сообщение напоминания после нажатия кнопки: та же суть и отметка."""
    return f"{text}\n\n{DONE_MARK}"


NOT_SAVED = "Не смог записать: база не ответила. Попробуйте ещё раз."

# Кнопку нажали, а база не ответила: задача не закрыта, и сказать об этом
# надо прямо (инвариант 4).
NOT_CLOSED = "Не смог закрыть задачу: база не ответила. Попробуйте ещё раз."

NOT_TEXT = (
    "Пока понимаю только текст и голос — напишите или надиктуйте. "
    "Фотографии и файлы — в следующих версиях."
)

# Речь не распозналась (`techspec/09-voice.md` §9.3): сообщение с файлом уже
# в базе, и об этом говорится честно — «Записал» без расшифровки не бывает
# (инвариант 4).
NOT_HEARD = "Не расслышал. Сообщение сохранил — повторите голосом или напишите текстом."
