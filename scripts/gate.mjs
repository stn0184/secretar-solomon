#!/usr/bin/env node
/**
 * Ворота сдачи изменения — одной командой, без зависимостей.
 *
 * Правило приёмки живёт здесь, а документы на него ссылаются: перечислять
 * проверки в CLAUDE.md не нужно, иначе два списка разойдутся.
 *
 * Проверки делятся на стековые и универсальные. Стековые (типы, линт,
 * тесты) — чужие команды из массива CHECKS ниже: скрипт их запускает и
 * собирает коды возврата; массив заполняется на этапе 001, когда выбран
 * стек. Универсальные — свои: состояние спек в specs/, одобрение
 * прототипов в prototype/ и незакрытые маркеры [ЗАПОЛНИТЬ: …]
 * в корневых документах.
 *
 * Запуск:
 *
 *   node scripts/gate.mjs --since <коммит>   полные ворота по диффу этапа
 *   node scripts/gate.mjs                    то же по дереву против HEAD
 *   node scripts/gate.mjs --only spec        только состояние спек
 *   node scripts/gate.mjs --only prototype   только связку спек с прототипами
 *
 * Код возврата: 1, если провалилась хоть одна проверка.
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const ROOT = process.cwd();

/**
 * Стековые проверки проекта: линт, типы, тесты и сборка каждой части.
 * Имя проверки — её адрес для `--only`, поэтому короткое и латиницей.
 *
 * Бот идёт через `uv run --directory bot`: окружение Python 3.12 ставится
 * из `bot/uv.lock`, отдельная активация venv не нужна. Части на Node
 * запускаются из корня через `npm --prefix`.
 */
const CHECKS = [
  { name: "bot:lint", cmd: "uv run --directory bot ruff check ." },
  { name: "bot:format", cmd: "uv run --directory bot ruff format --check ." },
  { name: "bot:types", cmd: "uv run --directory bot mypy" },
  { name: "bot:tests", cmd: "uv run --directory bot pytest -q" },
  { name: "miniapp:lint", cmd: "npm --prefix miniapp run lint" },
  { name: "miniapp:types", cmd: "npm --prefix miniapp run typecheck" },
  // Сборка Mini App — это и есть её тест: падает на ошибке типов или импорта.
  { name: "miniapp:build", cmd: "npm --prefix miniapp run build" },
  { name: "supabase:types", cmd: "npm --prefix supabase run typecheck" },
  { name: "supabase:tests", cmd: "npm --prefix supabase test" },
];

/* ------------------------------------------------------------------ разбор */

const argv = process.argv.slice(2);

function flag(name) {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 ? argv[i + 1] : undefined;
}

const since = flag("since");
const only = flag("only")
  ?.split(",")
  .map((s) => s.trim())
  .filter(Boolean);

/* --------------------------------------------------------------- проверки */

/** Чужая команда: запустить, собрать код возврата и хвост вывода. */
function runCommand(name, cmd) {
  // shell: true — на Windows команды почти всегда .cmd (pnpm, npm).
  const r = spawnSync(cmd, { cwd: ROOT, encoding: "utf8", shell: true });
  const ok = r.status === 0;
  const tail = `${r.stdout ?? ""}${r.stderr ?? ""}`
    .split("\n")
    .filter((l) => l.trim())
    .slice(-25)
    .join("\n");
  return { name, ok, findings: [], note: ok ? undefined : tail };
}

/** Номер строки по смещению в тексте — для находок в документах. */
function lineOf(src, index) {
  return src.slice(0, index).split("\n").length;
}

/**
 * Гасит код-спаны и блоки кода, сохраняя смещения — номера строк не съезжают.
 * Нужно, чтобы упоминание механизма не считалось его применением: документ,
 * который описывает маркер `[УТОЧНИТЬ: …]`, не должен на нём же и падать.
 */
function maskCode(src) {
  const blank = (m) => m.replace(/[^\n]/g, " ");
  return src
    .replace(/```[\s\S]*?```/g, blank)
    .replace(/``[\s\S]*?``/g, blank)
    .replace(/`[^`\n]*`/g, blank);
}

const STATUS_DONE = /^\*\*Статус:\*\*\s*сделано\s*$/m;

/** Папки с номером `NNN-…` в указанных корнях — этапы или прототипы. */
function numberedDirs(bases) {
  const dirs = [];
  for (const base of bases) {
    const abs = path.join(ROOT, base);
    if (!fs.existsSync(abs)) continue;
    for (const name of fs.readdirSync(abs)) {
      if (/^\d{3}-/.test(name)) dirs.push(`${base}/${name}`);
    }
  }
  return dirs;
}

/** Состояние очереди спек: маркеры, галочки, процедура закрытия. */
function checkSpec() {
  const findings = [];
  const dirs = numberedDirs(["specs", "specs/archive"]);

  for (const dir of dirs) {
    const file = `${dir}/spec.md`;
    const abs = path.join(ROOT, file);
    if (!fs.existsSync(abs)) continue;
    const src = fs.readFileSync(abs, "utf8");

    // Неясность, помеченная на проверке спеки, не доезжает до реализации.
    const prose = maskCode(src);
    const marker = /\[УТОЧНИТЬ:/g;
    let m;
    while ((m = marker.exec(prose))) {
      findings.push({ file, line: lineOf(src, m.index), message: "открытая неясность [УТОЧНИТЬ: …] — этап не сдаётся с ней" });
    }

    const open = src.split("\n").filter((l) => /^\s*- \[ \]/.test(l)).length;
    const done = src.split("\n").filter((l) => /^\s*- \[x\]/.test(l)).length;
    const isDone = STATUS_DONE.test(src);

    if (isDone && open > 0) {
      findings.push({ file, line: 0, message: `статус «сделано», а незакрытых задач ${open} — галочка ставится в коммите задачи` });
    }
    if (isDone && !dir.includes("archive/")) {
      findings.push({ file, line: 0, message: "статус «сделано», а папка не в specs/archive/ — закрытие в том же коммите" });
    }
    if (isDone) {
      const readme = path.join(ROOT, "specs", "README.md");
      if (fs.existsSync(readme)) {
        const idx = fs.readFileSync(readme, "utf8");
        const num = dir.match(/(\d{3})-/)?.[1];
        if (num && new RegExp(`^\\| ${num} \\|`, "m").test(idx)) {
          findings.push({ file: "specs/README.md", line: 0, message: `этап ${num} сделан, а строка всё ещё в таблице «Этапы»` });
        }
      }
    }
    if (!isDone && done + open === 0) {
      findings.push({ file, line: 0, message: "в спеке нет ни одной задачи — раздел «Задачи» обязателен" });
    }
  }

  return { name: "spec", ok: findings.length === 0, findings, note: dirs.length === 0 ? "этапов нет" : undefined };
}

/**
 * Прототипы: код не сдаётся раньше, чем человек одобрил визуал.
 *
 * Проверяется связка «поле шапки спеки ↔ папка прототипа»: этап,
 * который просил прототип, не может уехать в архив без одобрения, а
 * ссылка на одобренный прототип не может вести в пустоту.
 */
function checkPrototype() {
  const findings = [];
  const specs = numberedDirs(["specs", "specs/archive"]);
  const protos = new Map();
  for (const dir of numberedDirs(["prototype", "prototype/archive"])) {
    protos.set(dir.split("/").pop(), dir);
  }

  const APPROVED = /^\*{0,2}Статус:\*{0,2}\s*одобрен/m;
  let seen = 0;

  for (const dir of specs) {
    const file = `${dir}/spec.md`;
    const abs = path.join(ROOT, file);
    if (!fs.existsSync(abs)) continue;
    const src = fs.readFileSync(abs, "utf8");

    const field = src.match(/^\*\*Прототип:\*\*\s*(.+?)\s*$/m);
    // Поля нет — спека старше механики прототипов, это не находка.
    if (!field) continue;
    seen += 1;

    const value = field[1].replaceAll("`", "").trim();
    const line = lineOf(src, field.index);
    const isDone = STATUS_DONE.test(src);

    if (/^не нужен/i.test(value)) continue;

    if (/^нужен/i.test(value)) {
      if (isDone) {
        findings.push({ file, line, message: "этап сдан, а прототип так и не одобрен — визуал большого изменения смотрится до кода" });
      }
      continue;
    }

    if (!/^одобрен/i.test(value)) {
      findings.push({ file, line, message: `непонятное значение поля «Прототип»: «${value}» — нужен | одобрен prototype/NNN-<имя> | не нужен` });
      continue;
    }

    const name = value.match(/prototype\/(?:archive\/)?(\d{3}-[^\s/`]+)/)?.[1];
    if (!name) {
      findings.push({ file, line, message: "поле «Прототип» одобрено, но не называет папку — нужно «одобрен prototype/NNN-<имя>»" });
      continue;
    }

    const proto = protos.get(name);
    if (!proto) {
      findings.push({ file, line, message: `поле «Прототип» ссылается на prototype/${name}, а такой папки нет ни в prototype/, ни в prototype/archive/` });
      continue;
    }

    const readme = `${proto}/README.md`;
    const readmeAbs = path.join(ROOT, readme);
    if (!fs.existsSync(readmeAbs)) {
      findings.push({ file: readme, line: 0, message: "в папке прототипа нет README.md — статус одобрения и «что смотреть» живут там" });
    } else if (!APPROVED.test(fs.readFileSync(readmeAbs, "utf8"))) {
      findings.push({ file: readme, line: 0, message: `спека ${dir} говорит «одобрен», а в README прототипа нет строки «Статус: одобрен …»` });
    }

    if (isDone && !proto.startsWith("prototype/archive/")) {
      findings.push({ file: readme, line: 0, message: `этап закрыт, а прототип не уехал в prototype/archive/${name}/ — переезд идёт закрывающим коммитом` });
    }
  }

  return {
    name: "prototype",
    ok: findings.length === 0,
    findings,
    note: seen === 0 ? "поле «Прототип» ни в одной спеке" : undefined,
  };
}

/** Незакрытые заготовки шаблона в корневых документах. */
function checkDocs() {
  const findings = [];
  for (const name of ["CLAUDE.md", "idea.md", "spec.md", "techspec.md", "design.md", "README.md"]) {
    const abs = path.join(ROOT, name);
    if (!fs.existsSync(abs)) continue;
    const src = fs.readFileSync(abs, "utf8");
    const prose = maskCode(src);
    const marker = /\[ЗАПОЛНИТЬ:/g;
    let m;
    while ((m = marker.exec(prose))) {
      findings.push({ file: name, line: lineOf(src, m.index), message: "незакрытая заготовка [ЗАПОЛНИТЬ: …] — впиши ответ или убери раздел осознанно" });
    }
  }
  return { name: "docs", ok: findings.length === 0, findings };
}

/* ------------------------------------------------------------------- вывод */

function report(checks) {
  const width = Math.max(...checks.map((c) => c.name.length));
  for (const c of checks) {
    console.log(` ${c.name.padEnd(width)}  ${c.ok ? "ok" : "FAIL"}${c.note && c.ok ? `  (${c.note})` : ""}`);
  }
  for (const c of checks) {
    if (c.ok) continue;
    console.log(`\n── ${c.name} ─────────────────────────────`);
    if (c.note) console.log(c.note);
    for (const f of c.findings) {
      console.log(`  ${f.file}${f.line ? `:${f.line}` : ""}\n    ${f.message}`);
    }
  }
}

/* -------------------------------------------------------------------- main */

const want = (name) => !only || only.includes(name);
const checks = [];

for (const c of CHECKS) {
  if (want(c.name)) checks.push(runCommand(c.name, c.cmd));
}
if (want("spec")) checks.push(checkSpec());
if (want("prototype")) checks.push(checkPrototype());
if (want("docs")) checks.push(checkDocs());

console.log(`ворота: ${since ? `дифф от ${since}` : "рабочее дерево против HEAD"}\n`);
report(checks);

const failed = checks.filter((c) => !c.ok);
if (failed.length) {
  console.log(`\nпровалено: ${failed.map((c) => c.name).join(", ")}`);
  process.exit(1);
}
console.log("\nворота чисты");
