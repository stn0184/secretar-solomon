"""Имена для подсказок распознаванию: откуда берутся, как склоняются, сколько входит.

Источник правды — `techspec/09-voice.md` §9.5. Редкое имя распознавание
слышит как похожее слово («Юлаю» → «илая»), поэтому имена, которые бот уже
знает, уходят в запрос подсказками — каждое всеми падежами: распознавание
ищет слово так, как оно написано.

Здесь только чистые функции: имена из текста памяти и из людей задачи,
падежи по правилу окончаний (без словаря и без угадывания рода) и сборка
подсказок в пределах объёма. Сколько объёма даёт провайдер, решает модуль
распознавания (`services/transcription.py`), откуда читаются имена —
`services/tasks.py` через `db/`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# Слово — буквы, можно через дефис («Анна-Мария»). Цифры и знаки слово
# кончают: «Polo 2015 года» — имя «Polo», а не «Polo 2015».
_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")
# Знаки, после которых начинается предложение: слово с заглавной там — не
# имя, а просто начало фразы («Сына зовут Юлай»).
_SENTENCE_END = frozenset(".!?…")

# Падежей шесть: именительный, родительный, дательный, винительный,
# творительный, предложный.
CASES = 6
_CONSONANTS = frozenset("бвгджзклмнпрстфхцчшщ")
# -а после этих букв даёт родительный на -и (Саша → Саши, Ольга → Ольги).
_GENITIVE_I = frozenset("гкхжшщч")
# -а после этих букв даёт творительный на -ей (Саша → Сашей, Птица → Птицей).
_INSTRUMENTAL_EI = frozenset("жшщчц")
# Фамилии с творительным на -ым (Уваров → Уваровым, Ширкалин → Ширкалиным).
_SURNAME_ENDINGS = ("ов", "ев", "ёв", "ин", "ын")


@dataclass(frozen=True, slots=True)
class Hints:
    """Подсказки распознаванию и что с ними вышло — числа для журнала.

    `names` — сколько имён вошло, `left_out` — сколько не уместилось в
    предел. Самих имён в журнал не пишут (§9.5), поэтому и здесь их нет.
    """

    terms: tuple[str, ...]
    names: int
    left_out: int


def _capital_runs(text: str, *, skip_sentence_start: bool) -> list[str]:
    """Подряд идущие слова с заглавной буквы — по имени на каждую цепочку.

    Цепочку рвёт слово со строчной, знак или число между словами. Слово из
    одной буквы — инициал или «Я» — не имя. `skip_sentence_start` — слово в
    начале предложения не берётся: так читается текст памяти.
    """
    names: list[str] = []
    run: list[str] = []
    previous_end = 0
    sentence_start = True
    for match in _WORD.finditer(text):
        gap = text[previous_end : match.start()]
        if any(sign in _SENTENCE_END for sign in gap):
            sentence_start = True
        word = match.group()
        capital = len(word) > 1 and word[0].isupper()
        taken = capital and not (skip_sentence_start and sentence_start)
        if run and (not taken or gap.strip()):
            names.append(" ".join(run))
            run = []
        if taken:
            run.append(word)
        previous_end = match.end()
        sentence_start = False
    if run:
        names.append(" ".join(run))
    return names


def memory_names(text: str) -> list[str]:
    """Имена из записи памяти (§8.1): слова с заглавной не в начале предложения.

    «Сына зовут Юлай» — «Юлай»; «Машина — Volkswagen Polo 2015 года» —
    «Volkswagen Polo». Имя, с которого начинается запись, от обычного слова
    не отличить — его даст `people` задачи.
    """
    return _capital_runs(text, skip_sentence_start=True)


def person_names(person: str) -> list[str]:
    """Имена из одного значения `people` задачи: только слова с заглавной.

    «Анна Петровна из школы» — «Анна Петровна»; «мама» и «брату» не берутся:
    такие слова распознавание слышит и без подсказки. Значение считается
    именительным падежом — модель пишет людей в начальной форме.
    """
    return _capital_runs(person, skip_sentence_start=False)


def known_names(memory: Iterable[str], people: Iterable[Iterable[str]]) -> list[str]:
    """Имена в порядке, в котором они занимают место в подсказках (§9.5).

    Сначала из памяти — их человек назвал о себе сам, — затем из людей задач
    в том порядке, в каком задачи пришли (свежие первыми). Повтор — то же
    имя без учёта регистра — не берётся.
    """
    found = [name for text in memory for name in memory_names(text)]
    found += [name for task in people for person in task for name in person_names(person)]
    names: list[str] = []
    seen: set[str] = set()
    for name in found:
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            names.append(name)
    return names


def _cases(word: str) -> tuple[str, ...]:
    """Шесть падежей одного слова по окончанию (таблица §9.5).

    Окончание сверяется строчными буквами: латиница, аббревиатура («ЖК»),
    мягкий знак и прочие гласные остаются как есть. Ошибка правила в редком
    имени ничего не ломает — такая форма просто не поможет.
    """
    if word.endswith("ий") and len(word) > 2:
        stem = word[:-2]
        return (word, stem + "ия", stem + "ию", stem + "ия", stem + "ием", stem + "ии")
    if word.endswith("й") and len(word) > 1:
        stem = word[:-1]
        return (word, stem + "я", stem + "ю", stem + "я", stem + "ем", stem + "е")
    if word.endswith("ия") and len(word) > 2:
        stem = word[:-2]
        return (word, stem + "ии", stem + "ии", stem + "ию", stem + "ией", stem + "ии")
    if word.endswith("я") and len(word) > 1:
        stem = word[:-1]
        return (word, stem + "и", stem + "е", stem + "ю", stem + "ей", stem + "е")
    if word.endswith("а") and len(word) > 1:
        stem = word[:-1]
        before = stem[-1].lower()
        genitive = "и" if before in _GENITIVE_I else "ы"
        instrumental = "ей" if before in _INSTRUMENTAL_EI else "ой"
        return (word, stem + genitive, stem + "е", stem + "у", stem + instrumental, stem + "е")
    if word[-1] in _CONSONANTS:
        instrumental = "ым" if word.endswith(_SURNAME_ENDINGS) else "ом"
        return (word, word + "а", word + "у", word + "а", word + instrumental, word + "е")
    return (word,) * CASES


def forms(name: str) -> list[str]:
    """Имя всеми падежами, именительный первым, повторы убраны.

    Имя из нескольких слов склоняется по словам в одном падеже: «Саша
    Уваров» → «Саши Уварова», «Сашей Уваровым».
    """
    declined = [_cases(word) for word in name.split()]
    if not declined:
        return []
    result: list[str] = []
    for case in range(CASES):
        form = " ".join(word[case] for word in declined)
        if form not in result:
            result.append(form)
    return result


def volume(terms: Iterable[str]) -> int:
    """Объём подсказок в знаках: каждая со своим разделителем.

    Провайдер считает свои токены, а бот меряет знаками — предел в знаках
    подобран живым запросом и взят с запасом (§9.5).
    """
    return sum(len(term) + 1 for term in terms)


def fit(names: Sequence[str], limit: int) -> Hints:
    """Подсказки из имён по порядку, не больше `limit` знаков (§9.5).

    Имя входит всеми формами или не входит; не вошедшее не останавливает
    сборку — следующее, покороче, может уместиться. Форма, которая уже есть
    (без учёта регистра), второй раз не кладётся и места не занимает.
    """
    terms: list[str] = []
    seen: set[str] = set()
    used = taken = left_out = 0
    for name in names:
        new = [form for form in forms(name) if form.casefold() not in seen]
        cost = volume(new)
        if used + cost > limit:
            left_out += 1
            continue
        terms += new
        seen.update(form.casefold() for form in new)
        used += cost
        taken += 1
    return Hints(terms=tuple(terms), names=taken, left_out=left_out)
