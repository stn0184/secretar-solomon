"""Имена для подсказок распознаванию: откуда берутся, как склоняются, сколько входит.

Чистые функции без базы и сети (`techspec/09-voice.md` §9.5): имя из текста
памяти и из людей задачи, шесть падежей по правилу окончаний и сборка
подсказок в пределах объёма.
"""

from __future__ import annotations

import pytest

from solomon.services.names import (
    Hints,
    fit,
    forms,
    known_names,
    memory_names,
    person_names,
    volume,
)

# ------------------------------------------------------------- имена из памяти


def test_memory_name_is_a_capital_word_not_at_the_sentence_start() -> None:
    """«Сына» начинает предложение и не берётся; «Юлай» — имя (§9.5)."""
    assert memory_names("Сына зовут Юлай") == ["Юлай"]


def test_memory_name_of_several_capital_words_is_one_name() -> None:
    """Подряд идущие слова с заглавной — одно имя; число и «года» его кончают."""
    assert memory_names("Машина — Volkswagen Polo 2015 года") == ["Volkswagen Polo"]


def test_memory_without_names_gives_nothing() -> None:
    assert memory_names("Женат") == []
    assert memory_names("Занимается бизнесом, несколько направлений") == []
    assert memory_names("") == []


def test_every_sentence_start_is_skipped() -> None:
    assert memory_names("Работает в Москве. Офис на Тверской") == ["Москве", "Тверской"]
    assert memory_names("Сын учится! Дочь Рената — в саду") == ["Рената"]


def test_names_apart_by_punctuation_are_two_names() -> None:
    assert memory_names("Дети — Юлай, Рената и Саша Уваров") == ["Юлай", "Рената", "Саша Уваров"]


def test_capital_word_after_the_sentence_start_is_still_a_name() -> None:
    """Имя, с которого начинается запись, не отличить от слова; следующее — имя."""
    assert memory_names("Юлай Петров учится в школе") == ["Петров"]


def test_single_letters_are_not_names() -> None:
    """Инициалы и «Я» — не имена: подсказка из одной буквы только мешала бы."""
    assert memory_names("Учитель сына — И Петрова") == ["Петрова"]


def test_hyphenated_word_is_one_word() -> None:
    assert memory_names("Дочь зовут Анна-Мария") == ["Анна-Мария"]


# --------------------------------------------------------- имена из людей задач


@pytest.mark.parametrize("person", ["мама", "брату", "клиент", ""])
def test_person_without_a_capital_word_gives_nothing(person: str) -> None:
    """«мама» и «брату» распознавание слышит и без подсказки (§9.5)."""
    assert person_names(person) == []


def test_person_keeps_only_the_capital_words() -> None:
    assert person_names("Анна Петровна из школы") == ["Анна Петровна"]


def test_person_name_at_the_start_is_taken() -> None:
    """В `people` предложений нет: первое слово — тоже имя."""
    assert person_names("Юлай") == ["Юлай"]
    assert person_names("Саша Уваров") == ["Саша Уваров"]


def test_person_with_two_names_gives_two() -> None:
    assert person_names("Рената и Антон") == ["Рената", "Антон"]


# --------------------------------------------------------------------- падежи


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Юлай", ["Юлай", "Юлая", "Юлаю", "Юлаем", "Юлае"]),
        ("Рената", ["Рената", "Ренаты", "Ренате", "Ренату", "Ренатой"]),
        (
            "Саша Уваров",
            [
                "Саша Уваров",
                "Саши Уварова",
                "Саше Уварову",
                "Сашу Уварова",
                "Сашей Уваровым",
                "Саше Уварове",
            ],
        ),
        ("Мария", ["Мария", "Марии", "Марию", "Марией"]),
        ("Айгуль", ["Айгуль"]),
        ("Volkswagen", ["Volkswagen"]),
    ],
)
def test_forms_from_the_acceptance(name: str, expected: list[str]) -> None:
    """Ровно эти подсказки — пункт «Приёмки» спеки этапа 014."""
    assert forms(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # -ий
        ("Дмитрий", ["Дмитрий", "Дмитрия", "Дмитрию", "Дмитрием", "Дмитрии"]),
        # -я
        ("Таня", ["Таня", "Тани", "Тане", "Таню", "Таней"]),
        # согласная
        ("Антон", ["Антон", "Антона", "Антону", "Антоном", "Антоне"]),
        # -а после г: родительный на -и, творительный на -ой
        ("Ольга", ["Ольга", "Ольги", "Ольге", "Ольгу", "Ольгой"]),
        # -а после з и ц: родительный на -ы; после ц творительный на -ей
        ("Лиза", ["Лиза", "Лизы", "Лизе", "Лизу", "Лизой"]),
        ("Птица", ["Птица", "Птицы", "Птице", "Птицу", "Птицей"]),
        # фамилии на -ев, -ёв, -ин, -ын — творительный на -ым
        ("Ширкалин", ["Ширкалин", "Ширкалина", "Ширкалину", "Ширкалиным", "Ширкалине"]),
        ("Соловьёв", ["Соловьёв", "Соловьёва", "Соловьёву", "Соловьёвым", "Соловьёве"]),
        ("Синицын", ["Синицын", "Синицына", "Синицыну", "Синицыным", "Синицыне"]),
        ("Тимофей", ["Тимофей", "Тимофея", "Тимофею", "Тимофеем", "Тимофее"]),
    ],
)
def test_forms_by_the_ending(name: str, expected: list[str]) -> None:
    """Таблица окончаний §9.5: шесть падежей, повторы убраны."""
    assert forms(name) == expected


@pytest.mark.parametrize("name", ["Игорь", "Отто", "Анри", "СТО", "ЖК", "Polo"])
def test_soft_sign_vowels_latin_and_caps_stay_as_is(name: str) -> None:
    """-ь, гласная кроме -а и -я, латиница, аббревиатура — только как есть."""
    assert forms(name) == [name]


def test_name_of_several_words_is_declined_word_by_word() -> None:
    assert forms("Volkswagen Polo") == ["Volkswagen Polo"]
    assert forms("Анна Петровна") == [
        "Анна Петровна",
        "Анны Петровны",
        "Анне Петровне",
        "Анну Петровну",
        "Анной Петровной",
    ]


# ------------------------------------------------------- откуда и в каком порядке


def test_memory_names_come_first_then_task_names_freshest_first() -> None:
    memory = ["Сына зовут Юлай", "Машина — Volkswagen Polo 2015 года"]
    people = [("мама", "брату"), ("Анна Петровна из школы",), ("Рената", "Юлай")]

    assert known_names(memory, people) == ["Юлай", "Volkswagen Polo", "Анна Петровна", "Рената"]


def test_known_names_have_no_repeats_regardless_of_case() -> None:
    memory = ["Дочь зовут Рената", "Подруга дочери — РЕНАТА"]
    people = [("Рената",), ("рената Иванова",), ("Анна",), ("Анна",)]

    assert known_names(memory, people) == ["Рената", "Иванова", "Анна"]


def test_nothing_known_gives_no_names() -> None:
    assert known_names([], []) == []
    assert known_names(["Женат"], [("мама",)]) == []


# --------------------------------------------------------------- сборка в пределе


def test_volume_counts_the_signs_of_every_hint() -> None:
    assert volume([]) == 0
    assert volume(["Юлай"]) < volume(["Юлай", "Юлая"])
    assert volume(["Саша Уваров"]) > volume(["Саша"])


def test_everything_fits_within_a_big_limit() -> None:
    hints = fit(["Юлай", "Рената"], limit=10_000)

    assert hints == Hints(
        terms=(
            "Юлай",
            "Юлая",
            "Юлаю",
            "Юлаем",
            "Юлае",
            "Рената",
            "Ренаты",
            "Ренате",
            "Ренату",
            "Ренатой",
        ),
        names=2,
        left_out=0,
    )


def test_name_goes_with_all_its_forms_or_not_at_all() -> None:
    """Предел на знак меньше нужного: Рената не входит вовсе, а не частью."""
    limit = volume(forms("Юлай") + forms("Рената")) - 1

    hints = fit(["Юлай", "Рената"], limit=limit)

    assert hints.terms == tuple(forms("Юлай"))
    assert hints.names == 1
    assert hints.left_out == 1


def test_first_names_take_the_place_first() -> None:
    limit = volume(forms("Рената"))

    hints = fit(["Рената", "Юлай"], limit=limit)

    assert hints.terms == tuple(forms("Рената"))
    assert hints.left_out == 1


def test_shorter_name_after_a_left_out_one_still_fits() -> None:
    """Не вошло длинное имя — следующие пробуются: предел не пропадает зря."""
    limit = volume(forms("Юлай") + forms("Айгуль"))

    hints = fit(["Юлай", "Саша Уваров", "Айгуль"], limit=limit)

    assert hints.terms == (*forms("Юлай"), "Айгуль")
    assert hints.names == 2
    assert hints.left_out == 1


def test_forms_shared_by_two_names_go_once() -> None:
    """«Рената» — и имя, и родительный «Ренат»: подсказка одна (без повторов)."""
    hints = fit(["Ренат", "Рената"], limit=10_000)

    assert len(hints.terms) == len(set(hints.terms))
    assert hints.terms.count("Рената") == 1
    assert hints.names == 2


def test_no_names_give_no_hints() -> None:
    assert fit([], limit=10_000) == Hints(terms=(), names=0, left_out=0)


def test_zero_limit_leaves_everything_out() -> None:
    assert fit(["Юлай"], limit=0) == Hints(terms=(), names=0, left_out=1)
