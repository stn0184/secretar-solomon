// Генерирует артборды прототипа 000-redesign, круг 2: одна разметка экранов,
// две темы — «Банк-люкс» (тёмная) и «Тихая роскошь» (светлая). Тема меняет
// только вид (переменные в styles.css), разметка у обеих одна и та же.
// Данные общие и выдуманные.
// Запуск: node prototype/000-redesign/make-boards.mjs
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DIR = path.dirname(fileURLToPath(import.meta.url));

/* ─── данные (выдуманы) ──────────────────────────────────────────── */

const S = {
  vf: { n: "VoiceFin", mono: "VF" },
  rv: { n: "РЕЙВА", mono: "РВ" },
  fam: { n: "Семья", mono: "СМ" },
  sp: { n: "Спорт", mono: "СП" },
  none: { n: "Без сферы", mono: "·" },
};
const c = (s) => `--c:var(--s-${s})`;

const min = (hm) => { const [h, m] = hm.split(":").map(Number); return h * 60 + m; };
const fmt = (m) => {
  const h = Math.floor(m / 60), r = m % 60;
  if (!h) return `${r} мин`;
  return r ? `${h} ч ${r} мин` : `${h} ч`;
};
const fmtShort = (m) => { const h = Math.floor(m / 60), r = m % 60; return `${h}:${String(r).padStart(2, "0")}`; };

// «Сегодня» — четверг, 8 октября, сейчас 16:40.
const NOW = "16:40";
const FEED = [
  { f: "07:30", to: "08:30", s: "sp", t: "Тренировка", k: "meet", st: "done" },
  { f: "09:00", s: "fam", t: "Позвонить маме", k: "point", st: "done" },
  { gap: "Свободно 1 ч" },
  { f: "10:00", to: "11:00", s: "rv", t: "Планёрка с отделом продаж", k: "meet", st: "past" },
  { f: "11:15", to: "11:50", s: "rv", t: "Переписка с Олегом", via: "Telegram", k: "chat", st: "past" },
  { gap: "Свободно 40 мин" },
  { f: "12:30", to: "12:50", s: "none", t: "Переписка «Соседи по дому»", via: "MAX", k: "chat", st: "past" },
  { gap: "Свободно 1 ч 10 мин" },
  { f: "14:00", to: "16:00", s: "vf", t: "Созвон с Игорем", k: "meet", st: "past" },
  { f: "16:10", to: "16:25", s: "vf", t: "Переписка с Игорем", via: "Telegram", k: "chat", st: "past" },
  { now: NOW },
  { gap: "Свободно 2 ч 20 мин" },
  { f: "19:00", to: "20:30", s: "fam", t: "Ужин с родителями", k: "meet", st: "future" },
];
const DAY = [
  { s: "vf", m: 135 },
  { s: "rv", m: 95 },
  { s: "sp", m: 60 },
  { s: "none", m: 20 },
];
const DAY_TOTAL = 310;

const WEEK = [
  { d: "Пн", n: 5, tot: 325, segs: [["sp", "07:30", "08:30"], ["vf", "10:00", "12:00"], ["rv", "13:00", "14:00"], ["vf", "15:00", "16:00"], ["rv", "17:20", "17:30", "chat"], ["none", "19:00", "19:15", "chat"]] },
  { d: "Вт", n: 6, tot: 370, segs: [["vf", "09:30", "11:00"], ["rv", "11:30", "13:30"], ["rv", "15:00", "16:00"], ["fam", "18:30", "19:30"], ["none", "21:00", "21:40", "chat"]] },
  { d: "Ср", n: 7, tot: 350, segs: [["sp", "07:30", "08:30"], ["vf", "10:00", "12:00"], ["none", "13:00", "13:10", "chat"], ["vf", "14:00", "14:40", "chat"], ["fam", "17:00", "19:00"]] },
  { d: "Чт", n: 8, tot: 310, today: true, segs: [["sp", "07:30", "08:30"], ["rv", "10:00", "11:00"], ["rv", "11:15", "11:50", "chat"], ["none", "12:30", "12:50", "chat"], ["vf", "14:00", "16:00"], ["vf", "16:10", "16:25", "chat"], ["fam", "19:00", "20:30", "future"]] },
  { d: "Пт", n: 9, segs: [["vf", "11:00", "12:00", "future"], ["rv", "15:00", "16:00", "future"]] },
  { d: "Сб", n: 10, segs: [["fam", "12:00", "15:00", "future"]] },
  { d: "Вс", n: 11, segs: [["sp", "09:00", "10:00", "future"]] },
];
const WEEK_SPH = [
  { s: "vf", m: 565, meet: 510, chat: 55 },
  { s: "rv", m: 345, meet: 300, chat: 45 },
  { s: "fam", m: 180, meet: 180, chat: 0 },
  { s: "sp", m: 180, meet: 180, chat: 0 },
  { s: "none", m: 85, meet: 0, chat: 85 },
];
const WEEK_TOTAL = 1355;

const SPHERES = [
  { s: "vf", m: 565, meet: 510, open: 3, facts: ["Продаём подписку бухгалтерам", "Игорь — партнёр, отвечает за продукт", "Показы клиентам — по пятницам"] },
  { s: "rv", m: 345, meet: 300, open: 2, facts: ["В РЕЙВА я отвечаю за продажи", "Олег — коммерческий директор"] },
  { s: "fam", m: 180, meet: 180, open: 3, facts: ["Маше 8 лет, плавание по вторникам", "По четвергам — ужин с родителями"] },
  { s: "sp", m: 180, meet: 180, open: 2, facts: ["Тренируюсь утром, три раза в неделю"] },
];

const TASKS = [
  { g: "Просрочено", items: [
    { s: "fam", t: "Оплатить секцию Маше", w: "🔔 вторник, 6 октября", late: true, chips: [["high", "Высокий приоритет"]] },
  ] },
  { g: "Сегодня", items: [
    { s: "none", t: "Купить лампочку", w: "🔔 сегодня, без часа" },
    { s: "fam", t: "Ужин с родителями", w: "📅 19:00–20:30" },
  ] },
  { g: "Завтра, пятница", items: [
    { s: "rv", t: "Прислать расчёт Олегу", w: "🔔 до конца дня", chips: [["mine", "Я обещал: Олегу"], ["high", "Высокий приоритет"]] },
    { s: "vf", t: "Показ VoiceFin бухгалтерам", w: "📅 11:00–12:00" },
    { s: "rv", t: "Встреча с Олегом", w: "📅 15:00–16:00" },
  ] },
  { g: "Позже", items: [
    { s: "fam", t: "Поездка к родителям", w: "📅 сб, 10 октября, 12:00–15:00" },
    { s: "sp", t: "Тренировка", w: "📅 вс, 11 октября, 9:00–10:00" },
    { s: "vf", t: "Созвон с Игорем о тарифах", w: "📅 пн, 12 октября, 14:00–15:00", chips: [["guess", "Перепроверьте"]] },
    { s: "vf", t: "Игорь пришлёт макет лендинга", w: "🔔 ср, 14 октября", chips: [["kind", "Обещали мне: Игорь"]] },
  ] },
  { g: "Без срока", items: [
    { s: "sp", t: "Найти бассейн ближе к дому", w: "", chips: [["kind", "💡 Идея"]] },
  ] },
];

/* ─── общие куски ────────────────────────────────────────────────── */

// Число с прокруткой разрядов: каждая цифра — колонка 0–9.
function num(str, wait = 0) {
  let i = 0;
  return `<span class="num">${[...String(str)].map((ch) => {
    if (!/\d/.test(ch)) return ch;
    const col = Array.from({ length: 10 }, (_, k) => `<span>${k}</span>`).join("");
    return `<span class="dg"><span class="dg__col" style="--d:${ch};--i:${i++};--wait:${wait}ms">${col}</span></span>`;
  }).join("")}</span>`;
}
const numH = (m, wait = 0) => num(fmt(m), wait);

// Крупная цифра часов: «5 ч 10 мин», единицы — цветом акцента темы.
function big(m, wait = 0) {
  const h = Math.floor(m / 60), r = m % 60;
  return `<div class="big">${num(h, wait)}<span class="u">ч</span>${r ? `${num(String(r).padStart(2, "0"), wait + 80)}<span class="u">мин</span>` : ""}</div>`;
}

const pct = (hm) => ((min(hm) - 420) / 900) * 100; // ось 7:00–22:00
const chips = (list = []) => list.map(([k, l]) => `<span class="chip chip--${k}">${l}</span>`).join("");

const ICON = {
  today: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/></svg>',
  week: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M3.5 6h9M8 12h11M5.5 18h7"/></svg>',
  spheres: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="8.5" cy="8.5" r="4.5"/><circle cx="15.5" cy="8.5" r="4.5"/><circle cx="12" cy="15.5" r="4.5"/></svg>',
  tasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6.5l1.6 1.6L8.5 5M4 13l1.6 1.6L8.5 11.5M4 19.5h.01M11.5 7h8.5M11.5 13.5h8.5M11.5 19.5h8.5"/></svg>',
};
const tabs = (on) => [["today", "Сегодня"], ["week", "Неделя"], ["spheres", "Сферы"], ["tasks", "Дела"]]
  .map(([k, l]) => `<span class="tab${k === on ? " tab--on" : ""}">${ICON[k]}${l}</span>`).join("");

const status = () => `<div class="status"><span>16:40</span><span class="status__r"><span class="bars"><i></i><i></i><i></i><i></i></span><span class="batt"><i></i></span></span></div>`;
const tg = (back) => `<div class="tg"><span class="tg__l">${back ? "‹ Назад" : "Закрыть"}</span><span class="tg__t">Соломон</span><span class="tg__r">⋯</span></div>`;
const BGFX = `<div class="bgfx"><div class="blob b1"></div><div class="blob b2"></div></div>`;

function device(skin, { body, tab, foot = "", back = false }) {
  return `<div class="device skin-${skin} mv">
    <div class="screen">
      ${BGFX}
      <div class="island"></div>
      ${status()}
      ${tg(back)}
      <main class="body">
${body}
      </main>
      ${tab ? `<nav class="tabs">${tabs(tab)}</nav>` : foot}
      <div class="home-ind"></div>
    </div>
  </div>`;
}

function board({ no, title, note, motion = [] }, dev) {
  return `<section class="board">
  <header class="board__head">
    <div class="board__no">${no}</div>
    <h1 class="board__title">${title}</h1>
    <p class="board__note">${note}</p>
    ${motion.length ? `<ul class="board__motion">${motion.map((m) => `<li>${m}</li>`).join("")}</ul>` : ""}
    <div class="board__fake">Данные выдуманы — пример</div>
  </header>
  ${dev}
</section>`;
}

const FONTS = "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600&family=Lora:ital,wght@0,500;1,400&family=Inter+Tight:wght@200;300;400;500;600&display=swap";
const page = (titleText, section) => `<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${titleText} — Соломон, редизайн</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="${FONTS}">
<link rel="stylesheet" href="styles.css">
</head>
<body class="dc">
${section}
</body>
</html>
`;

/* ─── пустые состояния ───────────────────────────────────────────── */

const EMPTY = {
  today: {
    eyebrow: "Сегодня · четверг, 8 октября", title: "",
    t: "Сегодня свободно",
    p: "Ни встреч, ни дел на сегодня. Часы появятся сами — из встреч с длительностью и из переписки в личных чатах.",
    ex: ["Созвон с Игорем завтра с 14 до 16", "Тренировка в субботу в 9"],
    hint: "Напишите или наговорите боту — дело встанет в ленту.",
    cta: "Написать Соломону",
  },
  week: {
    eyebrow: "Неделя · 5 – 11 октября", title: "",
    t: "За неделю часов нет",
    p: "Соломон считает часы по встречам, у которых есть время, и по переписке в личных чатах. Таймеров нет — ничего засекать не нужно.",
    ex: ["Встреча с Олегом в среду с 11 до 13", "Это по VoiceFin: созвон с Игорем"],
    hint: "Встреча без конца займёт час — поправить можно словом: «до 16».",
    cta: "Написать Соломону",
  },
  spheres: {
    eyebrow: "Ваши сферы", title: "Сферы",
    t: "Сфер пока нет",
    p: "Назовите в чате свои сферы — бизнесы, семью, интересы. Соломон будет раскладывать по ним дела, переписку и часы.",
    ex: ["Мои сферы: работа, семья, спорт", "Это по работе: отвечаю за продажи"],
    hint: "Сфер может быть до 12. Дело без сферы — тоже нормально.",
    cta: "Вернуться в чат",
    blank: `<div class="blank"><b>Первая сфера</b>Появится здесь, когда вы назовёте её в чате</div>`,
  },
  tasks: {
    eyebrow: "Открытых нет", title: "Дела",
    t: "Дел пока нет",
    p: "Напишите Соломону — и дело появится здесь. Сферу и срок он поставит сам, поправить можно словом.",
    ex: ["Купить лампочку", "Прислать расчёт Олегу до пятницы"],
    hint: "Голосовые и пересланные сообщения тоже подойдут.",
    cta: "Вернуться в чат",
  },
};

/* ─── экраны: одна разметка на обе темы ──────────────────────────── */

const SKIN_NAME = { bank: "Банк-люкс", quiet: "Тихая роскошь" };

const screens = {
  today(skin) {
    const stripe = DAY.map((d, i) => `<i class="grow" style="${c(d.s)};flex:${d.m} 0 0;--wait:${150 + i * 70}ms"></i>`).join("");
    const legend = DAY.map((d) => `<span style="${c(d.s)}"><i></i>${S[d.s].n} <b>${fmt(d.m)}</b></span>`).join("");
    const thread = FEED.map((e) => {
      if (e.gap) return `<div class="gap"><div></div><div class="gap__rail"></div><p>${e.gap}</p></div>`;
      if (e.now) return `<div class="now"><span>${e.now}</span><i></i></div>`;
      const dur = e.to ? min(e.to) - min(e.f) : 0;
      const h = e.k === "point" ? 30 : Math.max(40, Math.round(40 + (dur - 30) * 0.32));
      const cap = ["cap", e.k === "chat" ? "cap--chat" : "", e.k === "point" ? "cap--point" : "", e.st === "future" ? "cap--future" : ""].join(" ");
      const ic = e.k === "chat" ? "💬" : e.k === "point" ? "📞" : "📅";
      const range = e.to ? `${e.f}–${e.to} · ${fmt(dur)}` : `${e.f} · без длительности`;
      const meta = `${range} · ${S[e.s].n}${e.via ? ` · ${e.via}` : ""}${e.st === "future" ? " · впереди" : ""}`;
      const cls = e.st === "done" ? " ti--done" : e.st === "future" ? " ti--future" : "";
      return `<div class="ti${cls}" style="${c(e.s)};min-height:${h + 12}px">
        <span class="ti__time">${e.f}</span>
        <span class="ti__rail"><span class="${cap}" style="height:${h}px">${ic}</span></span>
        <span class="ti__b"><span class="ti__t">${e.t}</span><span class="ti__m">${meta}</span></span>
        ${e.st === "done" ? `<span class="done"></span>` : "<span></span>"}
      </div>`;
    }).join("\n");
    return device(skin, {
      tab: "today",
      body: `<div class="hd"><span class="eyebrow">Сегодня · четверг, 8 октября</span>${big(DAY_TOTAL, 100)}<span class="big-sub">примерно — из встреч и переписки · впереди ещё <b>1 ч 30 мин</b></span></div>
<div class="split"><div class="stripe">${stripe}</div><div class="legend">${legend}</div></div>
<div class="sec">Без часа<em>1 дело</em></div>
<div class="inbox card" style="${c("none")}"><span class="inbox__ic">🔔</span><span><div class="inbox__t">Купить лампочку</div><div class="inbox__m">Без сферы</div></span><span class="chev">›</span></div>
<div class="sec">День<em>сейчас ${NOW}</em></div>
<div class="thread">${thread}</div>`,
    });
  },

  week(skin) {
    const ax = [8, 11, 14, 17, 20].map((h) => `<span style="left:${((h * 60 - 420) / 900) * 100}%">${h}</span>`).join("");
    const days = WEEK.map((d, di) => {
      const bars = d.segs.map((sg) => `<i class="bar grow${sg[3] ? ` bar--${sg[3]}` : ""}" style="${c(sg[0])};left:${pct(sg[1]).toFixed(2)}%;width:${(pct(sg[2]) - pct(sg[1])).toFixed(2)}%;--wait:${di * 60}ms"></i>`).join("");
      const now = d.today ? `<i class="nowline" style="left:${pct(NOW).toFixed(2)}%"></i>` : "";
      const v = d.tot ? `<span class="wd__v">${num(fmtShort(d.tot), 120 + di * 60)}</span>` : `<span class="wd__v wd__v--muted">впереди</span>`;
      return `<div class="wd${d.today ? " wd--today" : ""}"><div class="wd__l">${d.d}<small>${d.n} окт</small></div><div class="track">${bars}${now}</div>${v}</div>`;
    }).join("");
    const maxM = WEEK_SPH[0].m;
    const hb = WEEK_SPH.map((w, i) => `<div class="hb" style="${c(w.s)}"><span class="hb__n"><i></i>${S[w.s].n}</span><span class="hb__v">${numH(w.m, 200 + i * 80)}</span><span class="hb__track"><i class="grow" style="width:${((w.m / maxM) * 100).toFixed(1)}%;--wait:${200 + i * 80}ms"></i></span><span class="hb__sub">${[w.meet ? `встречи ${fmt(w.meet)}` : "", w.chat ? `переписка ${fmt(w.chat)}` : ""].filter(Boolean).join(" · ")}</span></div>`).join("");
    return device(skin, {
      tab: "week",
      body: `<div class="hd"><span class="eyebrow">Неделя · 5 – 11 октября</span>${big(WEEK_TOTAL, 100)}<span class="big-sub">примерно — из встреч и переписки, с понедельника</span></div>
<div class="sec">По дням<em>сейчас ${NOW}</em></div>
<div class="wk card">
  <div class="wk__axis"><span></span><div>${ax}</div><span></span></div>
  ${days}
</div>
<div class="sec">По сферам<em>с понедельника</em></div>
<div class="hbars">${hb}</div>`,
    });
  },

  spheres(skin) {
    const maxM = SPHERES[0].m;
    const cards = SPHERES.map((sp, i) => `<div class="sph sph--${sp.s}" style="${c(sp.s)};--wait:${500 + i * 160}ms">
  <div class="sph__top"><span class="sph__chip"></span><span class="sph__name">${S[sp.s].n}</span><span class="sph__h">${numH(sp.m, 150 + i * 80)}</span></div>
  <div class="sph__bar"><i class="grow" style="width:${((sp.m / maxM) * 100).toFixed(1)}%;--wait:${150 + i * 80}ms"></i></div>
  <div class="sph__meta">${sp.open} ${sp.open === 1 ? "открытое дело" : "открытых дела"} · встречи ${fmt(sp.meet)}</div>
  <div class="sph__fact"><b>${sp.facts[0]}</b>${sp.facts.length > 1 ? ` · ещё ${sp.facts.length - 1}` : ""}</div>
</div>`).join("");
    return device(skin, {
      tab: "spheres",
      body: `<div class="hd" style="padding-bottom:12px"><span class="eyebrow">4 из 12 · часы за неделю</span><h2 class="h1">Сферы</h2></div>
${cards}
<div class="sph sph--none sph--plain" style="${c("none")}"><div class="sph__top"><span class="sph__name" style="font-size:16px">Без сферы и о себе</span><span class="sph__h" style="font-size:16px">${fmt(85)}</span></div><div class="sph__meta">1 дело · 6 записей о вас</div></div>`,
    });
  },

  tasks(skin) {
    const groups = TASKS.map((g) => `<div class="sec">${g.g}<em>${g.items.length}</em></div>
<div class="group card">${g.items.map((it) => `<div class="task" style="${c(it.s)}"><span class="mark">${S[it.s].mono}</span><span class="task__b"><span class="task__t">${it.t}</span><span class="task__m"><span>${S[it.s].n}</span>${it.w ? `<span class="${it.late ? "task__late" : ""}">${it.w}</span>` : ""}</span>${it.chips ? `<span class="task__m">${chips(it.chips)}</span>` : ""}</span><span class="chev">›</span></div>`).join("")}</div>`).join("");
    return device(skin, {
      tab: "tasks",
      body: `<div class="hd"><span class="eyebrow">11 открытых · одно просрочено</span><h2 class="h1">Дела</h2></div>
<div class="seg"><span class="on">По сроку</span><span>По сферам</span></div>
${groups}`,
    });
  },

  card(skin) {
    return device(skin, {
      back: true,
      body: `<div class="ch card" style="${c("vf")}">
  <span class="ch__s"><i></i>VoiceFin</span>
  <h2 class="ch__t">Созвон с Игорем о тарифах</h2>
  <span class="ch__time">14:00–15:00</span>
  <span class="ch__d">понедельник, 12 октября · 1 ч</span>
</div>
<div class="kv card">
  <div class="kv__r"><span class="kv__k">Разбор</span><span class="kv__v"><span class="chip chip--guess">Перепроверьте</span><small>Конец встречи не назван — отвёл час. Скажите «до 16», если дольше.</small></span></div>
  <div class="kv__r"><span class="kv__k">Сфера</span><span class="kv__v">VoiceFin<small>Соломон отнёс сам: Игорь — по VoiceFin</small></span></div>
  <div class="kv__r"><span class="kv__k">Люди</span><span class="kv__v">Игорь</span></div>
  <div class="kv__r"><span class="kv__k">Напоминания</span><span class="kv__v">пн, 9:00 — в утреннем плане<small>пн, 13:45 — за 15 минут · оба ждут</small></span></div>
</div>
<div class="src">
  <span class="src__h">Голосовое · 0:12 · четверг, 8 октября, 16:26</span>
  <p class="src__q">«Созвониться с Игорем в понедельник в два, обсудить тарифы»</p>
</div>`,
      foot: `<div class="actions"><span class="btn btn--main">Сделано</span><span class="btn">Изменить</span><span class="btn btn--danger">Удалить</span></div>`,
    });
  },

  settings(skin) {
    const now = skin === "bank" ? "Сейчас в Telegram тёмная тема — Банк-люкс" : "Сейчас в Telegram светлая тема — Тихая роскошь";
    return device(skin, {
      back: true,
      body: `<div class="hd" style="padding-bottom:14px"><span class="eyebrow">Настройки</span><h2 class="h1">Оформление</h2></div>
<div class="group card" style="margin:0 14px">
  <div class="opt"><span class="pv pv--auto"></span><span><div class="opt__t">Как в Telegram</div><div class="opt__m">${now}</div></span><span class="radio radio--on"></span></div>
  <div class="opt"><span class="pv pv--quiet"><i></i><i></i><i></i><i></i></span><span><div class="opt__t">Тихая роскошь</div><div class="opt__m">Светлая: бумага, воздух, антиква</div></span><span class="radio"></span></div>
  <div class="opt"><span class="pv pv--bank"><i></i><i></i><i></i><i></i></span><span><div class="opt__t">Банк-люкс</div><div class="opt__m">Тёмная: чёрный, золото, сферы-камни</div></span><span class="radio"></span></div>
</div>
<p class="note">«Как в Telegram» меняется само: тёмная тема Telegram — <b>Банк-люкс</b>, светлая — <b>Тихая роскошь</b>. Экраны те же, меняется только вид.</p>
<div class="sec">Движение</div>
<p class="note" style="margin-top:0">Цифры прокручиваются, фон медленно плывёт. Если в телефоне включено «Уменьшить движение», всё стоит на месте.</p>`,
    });
  },

  empty(skin, k) {
    const e = EMPTY[k];
    return device(skin, {
      tab: k,
      body: `<div class="hd" style="padding-bottom:14px"><span class="eyebrow">${e.eyebrow}</span>${e.title ? `<h2 class="h1">${e.title}</h2>` : ""}</div>
<div class="empty">
  ${e.blank || ""}
  <h2 class="empty__t">${e.t}</h2>
  <p class="empty__p">${e.p}</p>
  <div class="empty__ex">${e.ex.map((x) => `<span>«${x}»</span>`).join("")}</div>
  <p class="empty__hint">${e.hint}</p>
  <div class="cta">${e.cta}</div>
</div>`,
    });
  },
};

/* ─── подписи артбордов: разметка общая, движение — у темы ───────── */

const NOTES = {
  today: ["Сегодня: крупная цифра и нить дня", "Сверху — <b>итог дня крупной цифрой</b> (из Банк-люкса) и доли сфер полосой с подписями. Ниже — дела без часа и <b>нить дня с капсулами</b> (из Тихой роскоши): высота капсулы растёт с длительностью, между ними «Свободно 40 мин», линия «сейчас». Встреча — заливка, переписка — тонкая заливка с кантом, впереди — контур. Простое дело с часом — маленькая капсула «без длительности», в часы не идёт. ✓ — только состояние, нажимать нечего."],
  week: ["Неделя: строка на день, часы справа", "Итог недели крупной цифрой. День — <b>одна строка</b> блоками цвета сферы, справа — <b>часы дня цифрами</b>, будущие дни — «впереди». Ниже — сферы полосами с разбивкой «встречи · переписка»."],
  spheres: ["Сферы: все сразу, чтобы сравнить", "Все сферы одним списком — <b>видно четыре разом</b>. У сферы — часы за неделю, полоса относительно самой большой, открытые дела, встречи и первая запись «что Соломон знает» (остальные — «ещё 2», раскрываются по нажатию). Внизу — «Без сферы и о себе»: сюда переехала вкладка «О себе»."],
  tasks: ["Дела: по сроку, переключатель «По сферам»", "Группы по сроку, как сейчас. Сверху — <b>«По сроку / По сферам»</b>: тот же список, сгруппированный по сферам. Строка — отметка сферы, суть, сфера и срок со значком из чата, метки вторым рядом, «›» — открыть карточку. <b>Кружка «Сделано» в строке нет</b>: «Сделано» — в карточке."],
  card: ["Карточка дела", "Шапка: сфера, суть, <b>время крупно</b>, дата и длительность. Ниже — поля стопкой (подпись над значением), «Перепроверьте» с вопросом бота, напоминания, исходное голосовое цитатой. Кнопки «Сделано / Изменить / Удалить» прилипают к низу; «назад» — у Telegram."],
  settings: ["Где переключается тема", "Вход — <b>меню ⋯ Telegram → «Настройки»</b> (кнопка настроек Mini App). По умолчанию «Как в Telegram»: тёмная тема Telegram — Банк-люкс, светлая — Тихая роскошь; вручную можно закрепить любую. Разметка одна — переключение меняет только цвета, шрифты, поверхности и характер движения."],
};
const MOTION = {
  bank: {
    today: ["«5 ч 10 мин»: часы и минуты прокручиваются, 320 мс, шаг 40 мс", "за цифрой медленно плывёт тёплый свет, 30–36 с", "доли сфер вырастают, 380 мс", "reduced motion: всё в конечном виде"],
    week: ["итог и часы дней прокручиваются лесенкой", "полосы дней вырастают, 380 мс"],
    spheres: ["по картам-камням один раз проходит блик, по очереди", "часы прокручиваются, полосы вырастают"],
    tasks: [], card: [], settings: [],
  },
  quiet: {
    today: ["«5 ч 10 мин»: прокрутка разрядов, 320 мс", "фон: два тёплых пятна еле дышат, 40–46 с", "доли сфер вырастают, 380 мс", "reduced motion: всё в конечном виде"],
    week: ["итог и часы дней прокручиваются", "полосы дней — лесенкой, 380 мс"],
    spheres: ["часы прокручиваются, полосы вырастают; блика нет — тише"],
    tasks: [], card: [], settings: [],
  },
};
const EMPTY_NOTE = {
  today: "«Сегодня», когда нет ни встреч, ни дел: крупной цифры и полосы нет вовсе — вместо нулей объяснение, откуда берутся часы, две фразы-примера и кнопка в чат.",
  week: "«Неделя» без часов: ни цифры, ни пустой сетки — объяснение, что часы считаются сами, без таймеров, и как встреча попадёт на неделю.",
  spheres: "«Сферы», когда сфер нет: пунктирное место под первую сферу и подсказка, как их назвать; «без сферы» — нормально.",
  tasks: "«Дела», когда дел нет: текст из design.md §2 со словом «дела» и двумя примерами.",
};

const SCREENS = [
  ["01-today", "today"], ["02-week", "week"], ["03-spheres", "spheres"], ["04-tasks", "tasks"], ["05-card", "card"], ["06-settings", "settings"],
  ["07-today-empty", "empty:today"], ["08-week-empty", "empty:week"], ["09-spheres-empty", "empty:spheres"], ["10-tasks-empty", "empty:tasks"],
];

// убрать старые артборды, чтобы переименование не оставляло хвостов
for (const f of fs.readdirSync(DIR)) if (f.endsWith(".dc.html")) fs.unlinkSync(path.join(DIR, f));

let n = 0;
for (const skin of ["bank", "quiet"]) {
  SCREENS.forEach(([file, what], i) => {
    const no = `${SKIN_NAME[skin]} · ${String(i + 1).padStart(2, "0")}`;
    let meta, dev;
    if (what.startsWith("empty:")) {
      const k = what.slice(6);
      const titles = { today: "Сегодня — пусто", week: "Неделя — пусто", spheres: "Сферы — пусто", tasks: "Дела — пусто" };
      meta = { no, title: titles[k], note: EMPTY_NOTE[k] };
      dev = screens.empty(skin, k);
    } else {
      const [title, note] = NOTES[what];
      meta = { no, title, note, motion: MOTION[skin][what] };
      dev = screens[what](skin);
    }
    fs.writeFileSync(path.join(DIR, `${skin}-${file}.dc.html`), page(`${no} · ${meta.title}`, board(meta, dev)), "utf8");
    n++;
  });
}
console.log("артбордов:", n);
