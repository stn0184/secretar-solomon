// Собирает полотно канваса из артбордов: один файл, без iframe.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DIR = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(DIR, "canvas.html");

const tokens = fs.readFileSync(path.join(DIR, "tokens.css"), "utf8");
const files = fs.readdirSync(DIR).filter((f) => f.endsWith(".dc.html")).sort();

const boards = files.map((f) => {
  const src = fs.readFileSync(path.join(DIR, f), "utf8");
  const m = src.match(/<section class="board">[\s\S]*?<\/section>/);
  if (!m) throw new Error("нет артборда в " + f);
  return m[0];
});

const head = `<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Соломон — 005: список, карточка</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
${tokens}

/* ─── полотно ─────────────────────────────────────────────────────── */
body {
  margin: 0;
  background-color: var(--ground);
  background-image:
    linear-gradient(var(--ground-line) 1px, transparent 1px),
    linear-gradient(90deg, var(--ground-line) 1px, transparent 1px);
  background-size: 32px 32px;
  color: var(--chrome-ink);
  font-family: var(--font-chrome);
}

.canvas-head {
  padding-block: 28px 20px;
  padding-inline: 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-width: 860px;
}
.canvas-head h1 {
  margin: 0;
  font: 600 21px/27px var(--font-chrome);
  letter-spacing: -.01em;
  color: var(--chrome-ink);
  text-wrap: balance;
}
.canvas-head p { margin: 0; font: 400 13px/19px var(--font-chrome); color: var(--chrome-ink-2); max-width: 62ch; }
.canvas-head p b { color: var(--chrome-ink); font-weight: 600; }
.canvas-head code { font: 500 12px/1 var(--font-chrome); color: var(--chrome-ink); }

.canvas-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 18px; }
.warn {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 5px 10px;
  border-radius: 8px;
  background: var(--important-weak);
  color: var(--important);
  font: 500 11px/15px var(--font-chrome);
  letter-spacing: .04em;
  text-transform: uppercase;
}
.seg { display: flex; gap: 2px; padding: 2px; border-radius: 9px; background: var(--chrome-card); border: 1px solid var(--chrome-line); }
.seg button {
  appearance: none; border: 0; cursor: pointer;
  padding: 5px 11px;
  border-radius: 7px;
  background: transparent;
  color: var(--chrome-ink-2);
  font: 500 11px/15px var(--font-chrome);
}
.seg button[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); }
.seg button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.flow {
  display: flex; align-items: center; gap: 8px;
  font: 400 11px/15px var(--font-chrome);
  color: var(--chrome-ink-2);
}
.flow i { display: block; width: 26px; height: 1px; background: var(--chrome-line); }

.canvas-scroll { overflow-x: auto; padding-block: 8px 56px; }
.canvas-row { display: flex; align-items: flex-start; gap: 40px; padding-inline: 24px; width: max-content; }
.canvas-row .board__head { min-height: 140px; }

@media (max-width: 460px) {
  .canvas-head { padding-inline: 16px; }
  .canvas-row { gap: 28px; padding-inline: 16px; }
}
</style>
</head>
<body>`;

const body = `<header class="canvas-head">
  <h1>Соломон — этап 005: список задач и карточка</h1>
  <p>Прототип первых экранов Mini App. Слева направо — путь человека: <b>список → (тот же список, когда задач нет) → карточка задачи → подтверждение удаления</b>. Прототип не работает: кнопки не нажимаются, данных из базы нет.</p>
  <div class="canvas-bar">
    <span class="warn">Выдуманные данные</span>
    <span class="flow">Тема<i></i></span>
    <span class="seg" role="group" aria-label="Тема оформления">
      <button type="button" data-theme-set="auto" aria-pressed="true">Как в системе</button>
      <button type="button" data-theme-set="light" aria-pressed="false">Светлая</button>
      <button type="button" data-theme-set="dark" aria-pressed="false">Тёмная</button>
    </span>
  </div>
  <p>Mini App живёт внутри Telegram, поэтому цвета взяты из его темы: на реализации токены подставляются из <code>--tg-theme-*</code>. Переключатель показывает, как экраны выглядят в светлой и тёмной теме Telegram. «Сегодня» на экранах — среда, 30 сентября.</p>
</header>

<div class="canvas-scroll">
  <div class="canvas-row">
${boards.join("\n\n")}
  </div>
</div>

<script>
(function () {
  var root = document.documentElement;
  var buttons = Array.prototype.slice.call(document.querySelectorAll("[data-theme-set]"));
  buttons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      var mode = btn.getAttribute("data-theme-set");
      if (mode === "auto") root.removeAttribute("data-theme");
      else root.setAttribute("data-theme", mode);
      buttons.forEach(function (b) { b.setAttribute("aria-pressed", String(b === btn)); });
    });
  });
})();
</script>
</body>
</html>`;

fs.writeFileSync(OUT, head + "\n\n" + body + "\n", "utf8");
console.log("собрано артбордов:", files.length);
console.log(files.join("\n"));
console.log("размер:", (fs.statSync(OUT).size / 1024).toFixed(1), "КБ");
