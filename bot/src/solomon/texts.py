"""Тексты бота. Все на русском, обращение на «вы», без канцелярита.

Листовой модуль: из `solomon` не импортирует ничего, поэтому слова можно
править, не боясь за поведение. Здесь же — русские названия дней и месяцев:
дата нужна и в ответе человеку, и в контексте момента для модели, и список
месяцев должен быть один.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

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

# Повтор словами (`techspec/13-repeat.md` §13.7): «каждый понедельник»,
# «каждую среду», «каждое воскресенье» — род дня; «по понедельникам и
# пятницам» — дательный множественного.
EVERY_WEEKDAY = ("каждый", "каждый", "каждую", "каждый", "каждую", "каждую", "каждое")
WEEKDAYS_DATIVE_PLURAL = (
    "понедельникам",
    "вторникам",
    "средам",
    "четвергам",
    "пятницам",
    "субботам",
    "воскресеньям",
)
WORKDAYS = [1, 2, 3, 4, 5]
WEEKEND = [6, 7]
ALL_WEEK = [1, 2, 3, 4, 5, 6, 7]
# Единица шага: «каждый» при 1 и при 21, 31…; формы для 1, 2–4 и 5–20.
REPEAT_UNITS = {
    "day": ("каждый", "день", "дня", "дней"),
    "week": ("каждую", "неделю", "недели", "недель"),
    "month": ("каждый", "месяц", "месяца", "месяцев"),
    "year": ("каждый", "год", "года", "лет"),
}

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


def _listed(words: Sequence[str]) -> str:
    """«а», «а и б», «а, б и в»."""
    if len(words) == 1:
        return words[0]
    return f"{', '.join(words[:-1])} и {words[-1]}"


def _every(unit: str, interval: int) -> str:
    """Шаг правила: «каждую неделю», «каждые 2 недели», «каждые 5 недель», «каждую 21 неделю»."""
    every, one, few, many = REPEAT_UNITS[unit]
    if interval == 1:
        return f"{every} {one}"
    tens, ones = interval % 100, interval % 10
    if ones == 1 and tens != 11:
        return f"{every} {interval} {one}"
    if 2 <= ones <= 4 and not 12 <= tens <= 14:
        return f"каждые {interval} {few}"
    return f"каждые {interval} {many}"


def _weekdays_words(days: Sequence[int]) -> str:
    """Дни недели после шага: «по будням», «по выходным», «по вторникам», «по средам и пятницам»."""
    if list(days) == WORKDAYS:
        return "по будням"
    if list(days) == WEEKEND:
        return "по выходным"
    return "по " + _listed([WEEKDAYS_DATIVE_PLURAL[day - 1] for day in days])


def repeat_words(rule: Mapping[str, Any]) -> str:
    """Повтор словами без часа — час уже в сроке (§13.7).

    Правило — каноническое, как его хранит база (§13.2). Те же слова пишет
    приложение (`miniapp/src/lib/repeat.ts`); примеры в тестах общие.
    """
    every = str(rule.get("every"))
    interval = int(rule.get("interval") or 1)
    if every == "day":
        if interval == 2:
            return "через день"
        return _every("day", interval)
    if every == "week":
        days = sorted(int(day) for day in rule.get("weekdays") or [])
        if days == ALL_WEEK:
            return "каждый день" if interval == 1 else f"{_every('week', interval)}, каждый день"
        if interval == 1:
            if len(days) == 1:
                day = days[0] - 1
                return f"{EVERY_WEEKDAY[day]} {WEEKDAYS_ACCUSATIVE[day]}"
            return _weekdays_words(days)
        return f"{_every('week', interval)} {_weekdays_words(days)}"
    month_day = int(rule.get("month_day") or 1)
    if every == "month":
        if month_day == -1:
            if interval == 1:
                return "в последний день месяца"
            return f"{_every('month', interval)} в последний день"
        return f"{_every('month', interval)} {month_day}-го"
    if every == "year":
        month = int(rule.get("month") or 1)
        return f"{_every('year', interval)} {month_day} {MONTHS[month - 1]}"
    raise ValueError(f"Unknown repeat rule: {every}")


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
    "Задачу можно сделать повторяющейся словом: «каждый понедельник отправлять "
    "отчёт», «по будням в 9 планёрка», «10-го каждый месяц платить за "
    "квартиру». «Сделано» у такой задачи не закрывает её, а переводит на "
    "следующий раз; «в этот раз не надо» — пропуск раза. Правило меняется "
    "словом («теперь по вторникам», «больше не повторяй») и в приложении.\n\n"
    "Задачи видны в приложении — кнопка меню рядом с полем ввода. Там список "
    "по срокам и карточка с исходным сообщением (у голосового — с "
    "расшифровкой, у снимка — с прочитанным); задачу можно закрыть, изменить "
    "или удалить.\n\n"
    "Задачу можно перенести, поправить, закрыть или убрать и словом в чате: "
    "«встреча перенеслась на пять», «отчёт отправил», «это уже не нужно». "
    "Проще всего — ответом на напоминание или на своё сообщение о задаче. "
    "Не пойму, о какой задаче речь, — спрошу кнопками; закрыл по ошибке — "
    "под ответом есть кнопка «Вернуть».\n\n"
    "Фото и скриншоты тоже разбираю: приглашение, переписку, этикетку. Со "
    "снимка записываю одну задачу — ту, о которой подпись, — и называю, что "
    "на нём есть ещё. Сам снимок не храню. Файлы, стикеры и видео пока не "
    "понимаю."
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
    repeat: str | None = None,
) -> str:
    """Пересказ по частям: суть, повтор, срок, когда напомню, приоритет, последняя фраза.

    `repeat` — правило словами (`repeat_words`): строка «Повтор» встаёт после
    головы и перед «Срок:» (`techspec/13-repeat.md` §13.7); у разовой задачи
    её нет, и пересказ прежний.
    """
    parts = [head]
    if repeat:
        parts.append(f"Повтор: {repeat}")
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
    repeat: str | None = None,
) -> str:
    """Подтверждение записи: суть, повтор, срок, когда напомню, приоритет (если
    не обычный) и причина «перепроверьте».

    Строка «Напомню» есть только тогда, когда напоминание вправду
    запланировано (§6.4): обещать её без плана значило бы сказать о том,
    чего не будет (инвариант 4).

    Из ответа модели дословно уходит только `review_reason`
    (`techspec/05-ai.md` §5.4) — остальное собрано здесь.
    """
    head = RECORDED_BY_KIND.get(kind, RECORDED_BY_KIND["task"]).format(title=title)
    return _retold(head, due, remind_at, priority, review_reason, repeat)


def asked_reply(
    title: str,
    question: str,
    due: str | None = None,
    remind_at: str | None = None,
    repeat: str | None = None,
) -> str:
    """Запись с уточняющим вопросом (`techspec/10-dialog.md` §10.1).

    «Записал: отправить расчёт клиенту. К какому сроку?» — задача уже в базе,
    вторая фраза и есть вопрос. Приоритета и причины здесь нет: вопрос
    говорит, чего не хватает, а лишняя фраза перед ним его заслонила бы.
    Вопрос уходит дословно от модели, как `review_reason`.
    """
    return _retold(
        RECORDED_BY_KIND["task"].format(title=title), due, remind_at, "normal", question, repeat
    )


# Ответ на вопрос лёг в ту же задачу (§10.2): пересказ как при записи, но
# словом «Понял» — человек видит, что второй задачи не появилось.
UNDERSTOOD = "Понял: {title}"


def understood_reply(
    title: str,
    due: str | None = None,
    review_reason: str | None = None,
    priority: str = "normal",
    remind_at: str | None = None,
    repeat: str | None = None,
) -> str:
    """«Понял: отправить расчёт клиенту. Срок: пятница, 25 сентября. Напомню: …»."""
    return _retold(UNDERSTOOD.format(title=title), due, remind_at, priority, review_reason, repeat)


# Дубль (`techspec/15-duplicates.md` §15.3): новой задачи нет, ответ — о
# найденной. Кнопка под ним заводит задачу всё-таки отдельно (§15.4).
ALREADY_RECORDED = "Это уже записано: {title}"
APART_BUTTON = "Записать отдельно"


def duplicate_reply(title: str, due: str | None = None, repeat: str | None = None) -> str:
    """«Это уже записано: встреча с Ренатой. Срок: пятница, 2 октября, 17:00».

    Суть, повтор и срок — найденной задачи. Ни «Напомню», ни приоритета:
    напоминания у неё прежние, а срочность из сообщения в неё не пишется.
    """
    return _retold(ALREADY_RECORDED.format(title=title), due, None, "normal", None, repeat)


# Правка из приложения перенесла срок (`techspec/11-edit.md` §11.4): тот же
# пересказ, что у ответа на вопрос, но словом «Перенёс». Срок сняли — одна
# фраза, и обещаний больше нет.
MOVED = "Перенёс: {title}"
DUE_REMOVED = "Убрал срок: {title}. Напоминать не буду."


def moved_reply(
    title: str, due: str | None, remind_at: str | None, repeat: str | None = None
) -> str:
    """«Перенёс: отправить расчёт клиенту. Срок: пятница, 2 октября. Напомню: …».

    `due` пуст — срок снят, и строка «Убрал срок». «Напомню» только тогда,
    когда напоминание вправду впереди (§6.4). У повторяющейся задачи —
    строка «Повтор» (`techspec/13-repeat.md` §13.6).
    """
    if not due:
        return DUE_REMOVED.format(title=title)
    return _retold(MOVED.format(title=title), due, remind_at, "normal", None, repeat)


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

# Повторяющаяся задача (`techspec/13-repeat.md` §13.7): «сделал» и пропуск не
# закрывают её, а переводят на следующий раз; убрать — всю серию.
DONE_REPEAT = "Отметил: {title}"
SKIPPED = "Пропускаю этот раз: {title}"
REPEAT_REMOVED = "Больше не повторяю: {title}"
DUE_AND_REPEAT_REMOVED = "Убрал срок и повтор: {title}. Напоминать не буду."
CANCELLED_SERIES = "Убрал из списка со всеми повторами: {title}."
# Правило назвали, а первого раза нет: у задачи нет срока, и он не назван.
REPEAT_START = "С какого дня начать повтор?"
# «Вернуть» под «Отметил» и «Пропускаю»: задача уже на другом разе.
GONE_FURTHER = "Задача уже ушла дальше — ничего не менял."


def advanced_reply(head: str, next_due: str | None, remind_at: str | None) -> str:
    """«Отметил: …. Следующий раз: …. Напомню: …» — без строки «Повтор» (§13.7).

    Следующий раз — тем же видом, что срок; «Напомню» — только о напоминании,
    которое вправду впереди (§6.4).
    """
    parts = [head]
    if next_due:
        parts.append(f"Следующий раз: {next_due}")
    if remind_at:
        parts.append(f"Напомню: {remind_at}")
    return ". ".join(parts)


# Кнопки правки (§12.6): вопрос «какую задачу» называет действие.
PICK_MOVE = "Какую задачу перенести {target}?"
PICK_REMOVE_DUE = "С какой задачи снять срок?"
PICK_DONE = "Какую задачу закрыть?"
PICK_CANCEL = "Какую задачу убрать из списка?"
PICK_SKIP = "Какую задачу пропустить в этот раз?"
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
    repeat: str | None = None,
) -> str:
    """Ответ на правку словом: «Перенёс: …», «Поправил: …», «Вернул в работу: …».

    `priority` и `people` — только когда правка их сменила: срочность
    звучит словом и тогда, когда она вернулась к обычной, люди — списком
    целиком (§12.5). «Напомню» — только о напоминании, которое вправду
    впереди (§6.4). `repeat` — правило повторяющейся задачи словами: строка
    «Повтор» перед «Срок:» (`techspec/13-repeat.md` §13.7).
    """
    parts = [head]
    if repeat:
        parts.append(f"Повтор: {repeat}")
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
    repeat: str | None = None,
) -> str:
    """Перенос задачи, которой нет в списке, записан новой задачей (§12.3)."""
    return _retold(
        NOT_FOUND_RECORDED.format(title=title), due, remind_at, priority, review_reason, repeat
    )


# Напоминание и кнопка под ним (`techspec/06-reminders.md` §6.2, §6.3).
DONE_BUTTON = "Сделано"
DONE_MARK = "✓ Сделано"
DONE_ANSWER = "Задача закрыта."
DONE_UNKNOWN = "Не нашёл эту задачу."
# Повторяющаяся задача после «Сделано» (`techspec/13-repeat.md` §13.3): срок —
# тот, какой вернула база, и когда перевела она, и когда задача ушла раньше.
DONE_NEXT = "✓ Сделано. Следующий раз: {due}"
NEXT_ANSWER = "Следующий раз: {due}"


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


def done_message(text: str, mark: str = DONE_MARK) -> str:
    """Сообщение напоминания после нажатия кнопки: та же суть и отметка.

    Прежняя отметка заменяется: повтор нажатия у повторяющейся задачи
    называет срок, какой вернула база, а не дописывает вторую строку.
    """
    return f"{without_mark(text)}\n\n{mark}"


def without_mark(text: str) -> str:
    """Текст напоминания без отметки «✓ Сделано…», если она уже стоит."""
    head, separator, _ = text.rpartition(f"\n\n{DONE_MARK}")
    return head if separator else text


NOT_SAVED = "Не смог записать: база не ответила. Попробуйте ещё раз."

# Правило повтора не по форме (§13.5): задача записана разовой, причина — в
# пометке «Перепроверьте».
REPEAT_DROPPED = "Не разобрал повтор — записал разовой"

# Кнопку нажали, а база не ответила: задача не закрыта, и сказать об этом
# надо прямо (инвариант 4).
NOT_CLOSED = "Не смог закрыть задачу: база не ответила. Попробуйте ещё раз."

NOT_TEXT = (
    "Понимаю текст, голос и фото. Файлы, стикеры и видео пока не разбираю — "
    "напишите или надиктуйте."
)

# Картинка файлом не того вида или больше 3,5 МБ (`techspec/14-photo.md`
# §14.1): ничего не сохраняется, как при отказе `NOT_TEXT`.
FILE_REFUSED = "Этот файл не открою — пришлите снимок как фото, а не файлом."

# Речь не распозналась (`techspec/09-voice.md` §9.3): сообщение с файлом уже
# в базе, и об этом говорится честно — «Записал» без расшифровки не бывает
# (инвариант 4).
NOT_HEARD = "Не расслышал. Сообщение сохранил — повторите голосом или напишите текстом."

# Снимок (`techspec/14-photo.md` §14.4). Поручения нет — сказать прямо, как
# у текста; о себе — только словами: со снимка память не пишется (§14.3).
PHOTO_NO_ERRAND = "На снимке поручения не нашёл — ничего не записал."
PHOTO_ABOUT_ME = "Со снимка в память не записываю — скажите словами, что запомнить."

# Отказы снимка (§14.2): сообщение с файлом уже в базе, и «Записал» без
# разбора не говорится (инвариант 4).
PHOTO_NOT_OPENED = "Не смог открыть снимок. Сообщение сохранил — пришлите его ещё раз."
PHOTO_NOT_UNDERSTOOD = (
    "Не разобрал снимок. Сообщение сохранил — пришлите ещё раз или опишите словами."
)


def more_on_photo(items: Sequence[str]) -> str:
    """Второй абзац ответа: поручения снимка, которые не записаны (§14.4).

    Суть уходит человеку дословно от модели, как `review_reason` (§5.4): по
    ней он решает, диктовать ли поручение отдельно.
    """
    listed = ", ".join(f"«{item}»" for item in items)
    return f"На снимке ещё: {listed}. Нужны — напишите или надиктуйте отдельно."
