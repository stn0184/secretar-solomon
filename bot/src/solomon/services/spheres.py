"""Сферы жизни владельца — чистые правила (`techspec/30-spheres.md`).

Здесь решается всё, что не требует ни базы, ни модели: название сферы из
ответа модели, сфера по списку, сфера в разборе (`settle`) и план записи —
какие сферы завести, какие убрать, что уже есть и что не влезло в предел
(`plan_spheres`). Чтение — `db/spheres.py`, запись — `record_understanding`
одной транзакцией с разбором, блок промпта — `services/understanding.py`.

Правило §30.2, которое держит код, а не промпт: сфера дела по смыслу — только
из списка (нет такой — без сферы), а новое название бывает лишь там, где
владелец назвал его сам: «заведи сферу X», «это по X: …», «это по X» о деле.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from solomon import texts
from solomon.services.understanding import (
    SPHERE_ACTION,
    SPHERE_KINDS,
    TASK_KINDS,
    ConversationUnderstanding,
    KnownSphere,
    MessageUnderstanding,
    PhotoUnderstanding,
    SphereStep,
    TaskItem,
    Understanding,
    sphere_steps,
)

# Живых сфер у владельца не больше 12, название — до 40 знаков (§30.1). Тот
# же предел держит база (`sphere_id_of`), бот — раньше неё, чтобы ответить
# словами.
SPHERE_LIMIT = 12
NAME_LIMIT = 40
KNOWLEDGE_KIND = "about_me"
# Кавычки вокруг названия модель иногда переписывает из сообщения.
QUOTES = "«»\"'„“”‘’"


def clean_name(raw: str | None) -> str | None:
    """Название сферы одной строкой: без кавычек и пробелов по краям, до 40
    знаков; пустое — `None`."""
    if raw is None:
        return None
    name = " ".join(raw.split()).strip(QUOTES).strip()
    name = name[:NAME_LIMIT].rstrip()
    return name or None


def _key(name: str) -> str:
    """Ключ сравнения: без регистра, «ё» — как «е»."""
    return name.casefold().replace("ё", "е")


def find(spheres: Sequence[KnownSphere], name: str | None) -> KnownSphere | None:
    """Сфера из списка по названию без учёта регистра; нет — `None`."""
    cleaned = clean_name(name)
    if cleaned is None:
        return None
    key = _key(cleaned)
    return next((sphere for sphere in spheres if _key(sphere.name) == key), None)


def _listed(name: str | None, spheres: Sequence[KnownSphere]) -> str | None:
    """Только сфера из списка, его написанием; иначе — без сферы."""
    found = find(spheres, name)
    return found.name if found is not None else None


def _named(name: str | None, spheres: Sequence[KnownSphere]) -> str | None:
    """Сфера, которую назвал владелец: из списка — его написанием, новая —
    очищенной."""
    found = find(spheres, name)
    return found.name if found is not None else clean_name(name)


def _top_sphere(understanding: Understanding, spheres: Sequence[KnownSphere]) -> str | None:
    """Сфера верхних полей (§30.2). Правка сферы, знание и «заведи» — названное
    владельцем; поручение, а также верх правки срока («как новое поручение»,
    §12.1) — только из списка; у снимка и переписки — только из списка;
    у разговора и поиска сферы нет."""
    edit = understanding.edit
    own = not isinstance(understanding, PhotoUnderstanding | ConversationUnderstanding)
    if own and edit is not None and edit.action == SPHERE_ACTION:
        return _named(understanding.sphere, spheres)
    if own and edit is None and understanding.kind == KNOWLEDGE_KIND:
        return _named(understanding.sphere, spheres)
    if own and understanding.kind in SPHERE_KINDS:
        return _named(understanding.sphere or understanding.title, spheres)
    if understanding.kind in TASK_KINDS:
        return _listed(understanding.sphere, spheres)
    return None


def _settle_item(item: TaskItem, spheres: Sequence[KnownSphere]) -> TaskItem:
    return item.model_copy(update={"sphere": _listed(item.sphere, spheres)})


def settle[U: Understanding](understanding: U, spheres: Sequence[KnownSphere]) -> U:
    """Сфера в разборе — как её запишет бот (§30.2): названия — написанием
    списка, дела по смыслу — только из списка, шаги «заведи» и «убери» —
    очищенными, пустые выпадают. Остальные поля не трогаются."""
    update: dict[str, object] = {"sphere": _top_sphere(understanding, spheres)}
    if isinstance(understanding, MessageUnderstanding):
        update["also"] = [_settle_item(item, spheres) for item in understanding.also]
        steps = []
        for step in understanding.more_spheres:
            name = _named(step.name, spheres)
            if name is not None:
                steps.append(SphereStep(drop=step.drop, name=name))
        update["more_spheres"] = steps
    return understanding.model_copy(update=update)


@dataclass(frozen=True, slots=True)
class SpherePlan:
    """Что сообщение делает со сферами (§30.2) — для базы и для ответа.

    `add` — завести (новые из «заведи сферу»), `drop` — убрать (живые, их
    написанием); `known` — уже есть, `unknown` — убирать нечего, `refused` —
    не влезли в предел. `fact_sphere` — сфера знания «это по X: …»;
    `created` — такая сфера заведётся вместе с записью.
    """

    add: tuple[str, ...] = ()
    drop: tuple[str, ...] = ()
    known: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    fact_sphere: str | None = None
    created: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """Сообщение сфер не трогает — ни записи, ни абзаца ответа."""
        return self == SpherePlan()


def _append(names: list[str], name: str) -> None:
    """Название в список один раз, без учёта регистра."""
    if all(_key(name) != _key(present) for present in names):
        names.append(name)


def plan_spheres(understanding: Understanding, spheres: Sequence[KnownSphere]) -> SpherePlan:
    """План по разбору, прошедшему `settle` (§30.2).

    Сначала «убери» — освобождает место, потом «заведи» по порядку, потом
    знание. Уже живая не заводится второй раз, тринадцатая — не заводится
    вовсе (`refused`), знание тогда пишется без сферы. Снимок, переписка,
    ответ на вопрос и правка сфер не заводят.
    """
    if isinstance(understanding, PhotoUnderstanding | ConversationUnderstanding):
        return SpherePlan()
    steps = sphere_steps(understanding)
    drop: list[str] = []
    unknown: list[str] = []
    for step in steps:
        if not step.drop:
            continue
        found = find(spheres, step.name)
        if found is None:
            _append(unknown, step.name)
        else:
            _append(drop, found.name)
    dropped = {_key(name) for name in drop}
    alive = [sphere for sphere in spheres if _key(sphere.name) not in dropped]
    count = len(alive)
    add: list[str] = []
    known: list[str] = []
    refused: list[str] = []
    for step in steps:
        if step.drop:
            continue
        found = find(alive, step.name)
        if found is not None:
            _append(known, found.name)
        elif any(_key(step.name) == _key(name) for name in (*add, *refused)):
            continue
        elif count >= SPHERE_LIMIT:
            refused.append(step.name)
        else:
            add.append(step.name)
            count += 1
    fact_sphere = None
    created: list[str] = []
    knowledge = understanding.sphere if understanding.kind == KNOWLEDGE_KIND else None
    if knowledge is not None and understanding.edit is None and not understanding.answers_question:
        found = find(alive, knowledge)
        if found is not None:
            fact_sphere = found.name
        elif any(_key(knowledge) == _key(name) for name in add):
            fact_sphere = knowledge
        elif count >= SPHERE_LIMIT:
            _append(refused, knowledge)
        else:
            fact_sphere = knowledge
            created.append(knowledge)
    return SpherePlan(
        add=tuple(add),
        drop=tuple(drop),
        known=tuple(known),
        unknown=tuple(unknown),
        refused=tuple(refused),
        fact_sphere=fact_sphere,
        created=tuple(created),
    )


def sphere_note(plan: SpherePlan) -> tuple[str, str] | None:
    """Абзац ответа о сферах (§30.3) и его значок — или `None`, если сказать
    нечего.

    По порядку: убрано (✏️), убирать нечего (⚠️), заведено — и со знанием
    (✅), уже было (✅), не влезло в предел (⚠️). Значок — первой части:
    одно сообщение — один значок (`techspec/29-icons.md` §29.2).
    """
    parts: list[tuple[str, str]] = []
    if plan.drop:
        parts.append((texts.ICON_EDIT, texts.spheres_dropped(plan.drop)))
    if plan.unknown:
        parts.append((texts.ICON_TROUBLE, texts.spheres_missing(plan.unknown)))
    added = (*plan.add, *plan.created)
    if added:
        parts.append((texts.ICON_RECORDED, texts.spheres_added(added)))
    if plan.known:
        parts.append((texts.ICON_RECORDED, texts.spheres_exist(plan.known)))
    if plan.refused:
        parts.append((texts.ICON_TROUBLE, texts.spheres_full(plan.refused, SPHERE_LIMIT)))
    if not parts:
        return None
    return parts[0][0], " ".join(text for _, text in parts)
