# -*- coding: utf-8 -*-
"""Генератор артбордов прототипа 006: шапка, статус-бар и вкладки — одни на
все экраны, чтобы не разъехались. Запуск: python make-boards.py, затем
node build-canvas.mjs. Артборды *.dc.html — результат, править лучше здесь."""

HEAD = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<link rel="stylesheet" href="tokens.css">
</head>
<body class="dc">
<section class="board">
  <header class="board__head">
    <div class="board__no">{no}</div>
    <h1 class="board__title">{h1}</h1>
    <p class="board__note">{note}</p>
  </header>

  <div class="device">
    <div class="screen">

      <div class="status">
        <span>9:41</span>
        <span class="status__right">
          <span class="bars"><i></i><i></i><i></i><i></i></span>
          <span class="batt"><i></i></span>
        </span>
      </div>

      <div class="sheet">
        <div class="grab"><i></i></div>

        <div class="app-head">
          <span class="app-head__title">Соломон</span>
          <span class="app-head__tools"><span>⋯</span><span>✕</span></span>
        </div>

{body}
{tabs}
      </div>

    </div>
  </div>
</section>
</body>
</html>
"""

ICON_TASKS = (
    '<svg viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 5.5h11M8.5 11h11M8.5 16.5h7.5"/>'
    '<path d="M2.5 5.2 3.9 6.6 6.4 4"/><path d="M2.5 10.7 3.9 12.1 6.4 9.5"/></svg>'
)
CHEV_DOWN = (
    '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M3 5.2 7 9.2l4-4"/></svg>'
)
ICON_ME = (
    '<svg viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="7.6" r="3.7"/>'
    '<path d="M4.2 19c.4-3.7 3.2-6 6.8-6s6.4 2.3 6.8 6"/></svg>'
)


def tabs(active):
    def on(k):
        return " tab--on" if k == active else ""

    return f"""        <div class="tabs">
          <span class="tab{on('tasks')}">
            {ICON_TASKS}
            <span>Задачи</span>
          </span>
          <span class="tab{on('me')}">
            {ICON_ME}
            <span>О себе</span>
          </span>
        </div>"""


# ── 01: список задач из 005 + вкладки ──────────────────────────────
TASKS_BODY = """        <div class="app-body">
          <div>
            <h2 class="page-title">Задачи</h2>
            <p class="page-sub">7 активных · одна просрочена</p>
          </div>

          <div class="sec">
            <div class="sec__h sec__h--overdue"><span>Просрочено</span></div>
            <div class="card">
              <div class="row">
                <span class="row__mark row__mark--overdue"></span>
                <span class="row__body">
                  <span class="row__title">Перезвонить Сергею по договору</span>
                  <span class="row__meta">
                    <span class="row__when--overdue">понедельник, 28 сентября, 10:00</span>
                    <span class="chip chip--mine">Я обещал: Сергею</span>
                  </span>
                </span>
                <span class="row__chev">›</span>
              </div>
            </div>
          </div>

          <div class="sec">
            <div class="sec__h"><span>Сегодня</span><em>среда, 30 сентября</em></div>
            <div class="card">
              <div class="row">
                <span class="row__mark row__mark--high"></span>
                <span class="row__body">
                  <span class="row__title">Отправить Кузнецову расчёт по складу</span>
                  <span class="row__meta">
                    <span class="row__when--today">сегодня, 15:00</span>
                    <span class="chip chip--high">Высокий приоритет</span>
                    <span class="chip chip--mine">Я обещал: Кузнецову</span>
                  </span>
                </span>
                <span class="row__chev">›</span>
              </div>
            </div>
          </div>

          <div class="sec">
            <div class="sec__h"><span>На неделе</span></div>
            <div class="card">
              <div class="row">
                <span class="row__mark"></span>
                <span class="row__body">
                  <span class="row__title">Марина пришлёт договор на подпись</span>
                  <span class="row__meta">
                    <span>пятница, 2 октября</span>
                    <span class="chip chip--theirs">Обещали мне: Марина</span>
                  </span>
                </span>
                <span class="row__chev">›</span>
              </div>
              <div class="row">
                <span class="row__mark"></span>
                <span class="row__body">
                  <span class="row__title">Записаться к стоматологу</span>
                  <span class="row__meta">
                    <span>суббота, 3 октября, 11:00</span>
                    <span class="chip chip--review">Перепроверьте</span>
                  </span>
                </span>
                <span class="row__chev">›</span>
              </div>
            </div>
          </div>

          <div class="sec">
            <div class="sec__h"><span>Позже</span></div>
            <div class="card">
              <div class="row">
                <span class="row__mark"></span>
                <span class="row__body">
                  <span class="row__title">Продлить ОСАГО</span>
                  <span class="row__meta"><span>четверг, 15 октября</span></span>
                </span>
                <span class="row__chev">›</span>
              </div>
            </div>
          </div>

          <div class="sec">
            <div class="sec__h"><span>Без срока</span></div>
            <div class="card">
              <div class="row">
                <span class="row__mark"></span>
                <span class="row__body">
                  <span class="row__title">Купить лампочку в коридор</span>
                  <span class="row__meta"><span>записано 24 сентября</span></span>
                </span>
                <span class="row__chev">›</span>
              </div>
            </div>
          </div>

          <div class="sec">
            <div class="sec__h"><span>Идеи и желания</span></div>
            <div class="card">
              <div class="row">
                <span class="row__mark"></span>
                <span class="row__body">
                  <span class="row__title">Съездить в Карелию на выходные</span>
                  <span class="row__meta"><span class="chip chip--kind">Желание</span><span>записано 26 сентября</span></span>
                </span>
                <span class="row__chev">›</span>
              </div>
            </div>
          </div>
        </div>
"""


# ── записи памяти ──────────────────────────────────────────────────
def fact_row(text, src, guess=False, open_more=None):
    chip = (
        '\n                    <span class="chip chip--guess">Предположение</span>'
        if guess
        else ""
    )
    cls = "row row--fact" + (" row--open" if open_more else "")
    more = ""
    if open_more:
        when, msg, buttons = open_more
        more = f"""
                <span class="row__more">
                  <span class="source__top">
                    <span>Исходное сообщение</span>
                    <span>{when}</span>
                  </span>
                  <span class="source__box">
                    <span class="source__text">{msg}</span>
                  </span>
                  <span class="actions">
{buttons}
                  </span>
                </span>"""
    return f"""              <div class="{cls}">
                <span class="row__body">
                  <span class="row__title">{text}</span>
                  <span class="row__meta">
                    <span class="row__src">{src}</span>{chip}
                  </span>
                </span>
                <span class="row__chev row__chev--down">{CHEV_DOWN}</span>{more}
              </div>"""


def sec(title, rows):
    body = "\n".join(rows)
    return f"""          <div class="sec">
            <div class="sec__h"><span>{title}</span></div>
            <div class="card">
{body}
            </div>
          </div>"""


def memory_body(open_family=False):
    family_guess_more = None
    if open_family:
        family_guess_more = (
            "четверг, 24 сентября, 17:40",
            "Завтра в пять забрать Мишу из садика, а потом заехать в аптеку за каплями.",
            """                    <span class="btn btn--sm">Подтвердить</span>
                    <span class="btn btn--sm btn--danger">Удалить</span>""",
        )
    sections = [
        sec("Семья", [
            fact_row("Жена — Марина", "с ваших слов · 12 сентября"),
            fact_row("Сын Миша ходит в садик", "из сообщения · 24 сентября",
                     guess=True, open_more=family_guess_more),
        ]),
        sec("Дом", [
            fact_row("Живёт в Казани, на Дубравной", "с ваших слов · 2 сентября"),
        ]),
        sec("Машина", [
            fact_row("Машина — Toyota Camry", "с ваших слов · 2 сентября"),
        ]),
        sec("Работа", [
            fact_row("Работа заканчивается в 18:00", "с ваших слов · 5 сентября"),
            fact_row("По пятницам бывает в офисе на Петербургской",
                     "из сообщения · 25 сентября", guess=True),
        ]),
        sec("Привычки", [
            fact_row("Спортзал по вторникам и четвергам",
                     "из сообщения · 22 сентября", guess=True),
        ]),
    ]
    joined = "\n\n".join(sections)
    return f"""        <div class="app-body">
          <div>
            <h2 class="page-title">О себе</h2>
            <p class="page-sub">7 записей · 3 предположения</p>
          </div>

{joined}
        </div>
"""


EMPTY_BODY = """        <div class="app-body app-body--center">
          <div class="empty">
            <span class="empty__mark"><i></i></span>
            <h2 class="empty__h">Пока ничего о вас не знаю</h2>
            <p class="empty__p">Расскажите боту между делом — какая машина, во сколько заканчиваете работу, как зовут детей. Он запомнит и не будет переспрашивать.</p>
            <div class="hints">
              <div class="hint">«У меня Toyota Camry»</div>
              <div class="hint">«Работаю до шести, по пятницам до пяти»</div>
            </div>
            <span class="btn btn--wide">Вернуться в чат</span>
            <p class="note" style="margin-top: 4px; text-align: center">Что помощник поймёт из поручений сам, будет помечено как предположение — его можно подтвердить или удалить.</p>
          </div>
        </div>
"""

BOARDS = [
    ("01-tasks-tabs.dc.html", dict(
        title="01 · Задачи с вкладками — Соломон",
        no="Экран 01 · Mini App",
        h1="Список задач — тот же, что в 005, плюс нижние вкладки",
        note="Содержимое списка не меняется. Внизу появляются <b>две вкладки «Задачи | О себе»</b> — приложение теперь из двух экранов. Вкладки — корень: кнопки «назад» Telegram здесь нет, она остаётся только внутри карточки задачи. Список стал короче на высоту вкладок и уходит под них прокруткой.",
        body=TASKS_BODY, tabs=tabs("tasks"))),
    ("02-about-me.dc.html", dict(
        title="02 · О себе — Соломон",
        no="Экран 02 · Mini App",
        h1="«О себе» — что помощник знает, по категориям",
        note="Строка записи повторяет строку задачи: суть, под ней <b>источник и дата</b> — «с ваших слов» (сказано прямо, факт) или «из сообщения» (выведено). У выведенного — метка <b>«Предположение»</b> тем же фиолетовым, что «Перепроверьте» у задачи. Категории — из семи заданных, пустые скрыты. Стрелка вниз — раскрыть на месте, перехода нет.",
        body=memory_body(), tabs=tabs("me"))),
    ("03-about-me-open.dc.html", dict(
        title="03 · Раскрытая запись — Соломон",
        no="Экран 03 · Mini App",
        h1="Касание по записи — исходное сообщение и действия",
        note="Запись раскрывается на месте, остальные остаются вокруг: видно, <b>из какого сообщения</b> помощник это вывел, и два действия — «Подтвердить» (только у предположения: метка уходит, запись становится фактом) и «Удалить» (с системным окном Telegram, как у задачи в 005). У факта — только «Удалить». Правки текста нет.",
        body=memory_body(open_family=True), tabs=tabs("me"))),
    ("04-about-me-empty.dc.html", dict(
        title="04 · О себе пусто — Соломон",
        no="Экран 04 · Mini App",
        h1="Записей нет — первое, что видит новый человек на вкладке",
        note="Тот же приём, что у пустого списка задач: объяснение, <b>две фразы, которые можно так и написать боту</b>, и кнопка «Вернуться в чат». Ниже одна строка — обещание, что додуманное помощником будет помечено; человек узнаёт это до того, как увидит первую метку.",
        body=EMPTY_BODY, tabs=tabs("me"))),
]

if __name__ == "__main__":
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    for fname, kw in BOARDS:
        with open(os.path.join(here, fname), "w", encoding="utf-8") as f:
            f.write(HEAD.format(**kw))
        print("written", fname)
