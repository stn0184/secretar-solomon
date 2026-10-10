// Собирает полотно из артбордов: один файл canvas.html, без iframe.
// Ряд — тема, слева направо — путь владельца по экранам; разметка у рядов одна.
// Запуск: node prototype/000-redesign/build-canvas.mjs (после make-boards.mjs)
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
// та же строка, что в make-boards.mjs (импорт оттуда заново запустил бы генератор)
const FONTS = "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600&family=Lora:ital,wght@0,500;1,400&family=Inter+Tight:wght@200;300;400;500;600&display=swap";

const DIR = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(DIR, "canvas.html");
const css = fs.readFileSync(path.join(DIR, "styles.css"), "utf8");

const ROWS = [
  {
    key: "bank",
    name: "Банк-люкс — тёмная тема",
    gist: "Включается сама, когда в Telegram тёмная тема. Чёрный с золотом: золото — у действия и единиц крупных цифр. Сферы — камни (сапфир, изумруд, гранат, аметист), на «Сферах» — карточки-карты из этого камня. Шрифт Inter Tight, крупные цифры тонкие. Движение — тёплый свет за цифрой и блик по картам.",
  },
  {
    key: "quiet",
    name: "Тихая роскошь — светлая тема",
    gist: "Включается сама, когда в Telegram светлая тема. Тёплая бумага, мягкие тени, заголовки и крупные цифры антиквой (Lora), текст — Inter. Сферы — приглушённые цвета, на «Сферах» — белые карточки с цветной полосой. Движение тише: два пятна еле дышат, блика нет.",
  },
];

const boardsOf = (key) => fs.readdirSync(DIR)
  .filter((f) => f.startsWith(key + "-") && f.endsWith(".dc.html"))
  .sort()
  .map((f) => {
    const m = fs.readFileSync(path.join(DIR, f), "utf8").match(/<section class="board">[\s\S]*?<\/section>/);
    if (!m) throw new Error("нет артборда в " + f);
    return m[0];
  });

let total = 0;
const rows = ROWS.map((r) => {
  const b = boardsOf(r.key);
  total += b.length;
  return `<section class="dir-row" id="${r.key}">
  <header class="dir-row__head"><h2>${r.name}</h2><p>${r.gist}</p><span>Экраны 01–05 — штатно, 06 — где переключается тема, 07–10 — пусто</span></header>
  <div class="canvas-scroll"><div class="canvas-row">
${b.join("\n")}
  </div></div>
</section>`;
}).join("\n\n");

const html = `<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Соломон — редизайн Mini App, круг 2: одна разметка, две темы</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="${FONTS}">
<style>
${css}

/* ─── полотно ─── */
body.dc { padding: 0; }
.canvas-head { padding: 28px 24px 8px; max-width: 980px; display: flex; flex-direction: column; gap: 12px; }
.canvas-head h1 { margin: 0; font: 600 22px/28px var(--font-chrome); }
.canvas-head p { margin: 0; font: 400 13px/19px var(--font-chrome); color: var(--chrome-ink-2); max-width: 90ch; }
.canvas-head p b { color: var(--chrome-ink); }
.cv-bar { display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: center; }
.cv-warn { padding: 5px 10px; border-radius: 8px; background: rgba(240, 180, 90, .14); color: #f0b45a; font: 500 11px/15px var(--font-chrome); letter-spacing: .05em; text-transform: uppercase; }
.cv-seg { display: flex; gap: 2px; padding: 2px; border-radius: 9px; background: var(--chrome-card); border: 1px solid var(--chrome-line); }
.cv-seg button, .cv-btn { appearance: none; border: 0; cursor: pointer; padding: 5px 11px; border-radius: 7px; background: transparent; color: var(--chrome-ink-2); font: 500 11px/15px var(--font-chrome); }
.cv-seg button[aria-pressed="true"] { background: var(--chrome-accent); color: #0d1117; }
.cv-btn { background: var(--chrome-card); border: 1px solid var(--chrome-line); color: var(--chrome-ink); }
.cv-lbl { font: 400 11px/15px var(--font-chrome); color: var(--chrome-ink-2); }
.toc { display: flex; gap: 14px; font: 500 12px/16px var(--font-chrome); }
.toc a { color: var(--chrome-accent); }
.dir-row { padding-top: 26px; border-top: 1px solid var(--chrome-line); margin-top: 18px; }
.dir-row__head { padding: 0 24px; max-width: 980px; display: flex; flex-direction: column; gap: 6px; }
.dir-row__head h2 { margin: 0; font: 600 19px/24px var(--font-chrome); }
.dir-row__head p { margin: 0; font: 400 13px/19px var(--font-chrome); color: var(--chrome-ink-2); max-width: 90ch; }
.dir-row__head span { font: 400 11px/15px var(--font-chrome); color: var(--chrome-accent); }
.canvas-scroll { overflow-x: auto; padding: 16px 0 40px; }
.canvas-row { display: flex; gap: 28px; padding: 0 24px; width: max-content; align-items: flex-start; }
</style>
</head>
<body class="dc">
<header class="canvas-head">
  <h1>Соломон — редизайн Mini App, круг 2: одна разметка, две темы</h1>
  <p>Решение владельца после круга 1: остаются <b>Банк-люкс</b> и <b>Тихая роскошь</b>, «Ночное стекло» снято. Экраны теперь <b>одни и те же</b> — тема меняет только цвета, шрифты, поверхности и характер движения. Два ряда — каждый экран в обеих темах: Сегодня → Неделя → Сферы → Дела → Карточка дела → Оформление, затем пустые состояния. По умолчанию тема — как в Telegram: тёмная → Банк-люкс, светлая → Тихая роскошь. Прототип не работает: кнопки не нажимаются, данных из базы нет. «Сегодня» — четверг, 8 октября, сейчас 16:40.</p>
  <div class="cv-bar">
    <span class="cv-warn">Данные выдуманы — часы и дела для примера</span>
    <span class="cv-lbl">Движение</span>
    <span class="cv-seg" role="group" aria-label="Движение">
      <button type="button" data-motion-set="auto" aria-pressed="true">Как в системе</button>
      <button type="button" data-motion-set="on" aria-pressed="false">Включено</button>
      <button type="button" data-motion-set="off" aria-pressed="false">Выключено (reduced motion)</button>
    </span>
    <button type="button" class="cv-btn" id="replay">↻ Повторить анимацию</button>
  </div>
  <div class="toc"><a href="#bank">Банк-люкс (тёмная)</a><a href="#quiet">Тихая роскошь (светлая)</a></div>
</header>

${rows}

<script>
(function () {
  var root = document.documentElement;
  var btns = [].slice.call(document.querySelectorAll("[data-motion-set]"));
  btns.forEach(function (b) {
    b.addEventListener("click", function () {
      var m = b.getAttribute("data-motion-set");
      if (m === "auto") root.removeAttribute("data-motion"); else root.setAttribute("data-motion", m);
      btns.forEach(function (x) { x.setAttribute("aria-pressed", String(x === b)); });
    });
  });
  document.getElementById("replay").addEventListener("click", function () {
    var d = [].slice.call(document.querySelectorAll(".device"));
    d.forEach(function (x) { x.classList.remove("mv"); });
    void document.body.offsetWidth;
    d.forEach(function (x) { x.classList.add("mv"); });
  });
})();
</script>
</body>
</html>
`;

fs.writeFileSync(OUT, html, "utf8");
console.log("собрано артбордов:", total);
console.log("размер:", (fs.statSync(OUT).size / 1024).toFixed(1), "КБ");
