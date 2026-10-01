#!/usr/bin/env node
/**
 * Выкладка бота на сервер VoiceFin — techspec/16-server.md §16.4.
 *
 *   node scripts/deploy-bot.mjs            код, зависимости, служба, перезапуск, проверка
 *   node scripts/deploy-bot.mjs --env      то же и перед перезапуском — ключи из .env
 *   node scripts/deploy-bot.mjs --status   состояние на сервере, ничего не меняет
 *   node scripts/deploy-bot.mjs --setup    первая настройка: deploy/setup-server.sh от root
 *
 * Сервер берёт код с GitHub, поэтому до первого обращения к нему скрипт
 * отказывает, если рабочее дерево не чистое или HEAD не равен origin/main.
 * На сервер ходит `ssh voicefin-vps` (имя из ~/.ssh/config владельца —
 * адреса в репозитории нет) без вопросов и с таймаутом соединения.
 *
 * Аргумент ssh всегда `bash -s`: всё, что делается на сервере, уходит
 * скриптом через stdin — и ключи, и файл службы внутри него, в base64.
 * Так значения не попадают ни в аргументы команды, ни в список процессов.
 * Внутри папки бота root не работает: git, uv и запись .env — от имени
 * solomon (`runuser -u solomon --`); root только ставит файл службы из
 * копии владельца и управляет службой (§16.2).
 *
 * Чистые части — список ключей, отбор строк .env, сборка скриптов для
 * сервера, разбор ответа — экспортируются и проверяются тестом
 * scripts/deploy-bot.test.mjs; git, ssh и файлы приходят в main() снаружи.
 *
 * Код возврата: 0 — готово, 1 — отказ или бот не поднялся, 2 — не те флаги.
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/** Имя сервера из ~/.ssh/config владельца. */
export const HOST = "voicefin-vps";
const CONNECT_TIMEOUT_S = 15;

const APP_USER = "solomon";
const APP_HOME = "/opt/solomon";
const APP_DIR = `${APP_HOME}/secretar-solomon`;
const UNIT = "solomon-bot";
const UNIT_PATH = `/etc/systemd/system/${UNIT}.service`;

const UNIT_FILE = "deploy/solomon-bot.service";
const SETUP_FILE = "deploy/setup-server.sh";

/** Строка aiogram о начале опроса Telegram: бот получил себя через getMe. */
const POLLING_LINE = "Run polling for bot";
/** Второй экземпляр с тем же токеном (§16.3). */
const CONFLICT_LINE = "TelegramConflictError";
const WAIT_S = 60;
const JOURNAL_LINES = 30;

/** Граница между полями ответа сервера и хвостом журнала. */
export const JOURNAL_MARK = "--- journal ---";

// Не `\b`: без флага u граница слова видит только латиницу.
const ENV_BOT_SECTION = /^#\s*---\s*Бот(?=\s|$)/;
const ENV_ANY_SECTION = /^#\s*---/;
const ENV_EXAMPLE_KEY = /^([A-Z][A-Z0-9_]*)=/;
const ENV_LINE_KEY = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=/;
/** Токен бота по форме — тот же, что ищет check-dist.mjs. */
const BOT_TOKEN = /(?<!\d)\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])/g;
const GITHUB_REPO =
  /^(?:https:\/\/github\.com\/|git@github\.com:|ssh:\/\/git@github\.com\/)([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?\/?$/;

/* ------------------------------------------------------------ чистые части */

/** Переводы строк — LF, BOM в начале убран: bash и systemd иного не понимают. */
export function toLF(text) {
  const body = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  return body.replace(/\r\n?/g, "\n");
}

/**
 * Имена переменных раздела бота из `.env.example` — от строки `# --- Бот`
 * до следующей `# ---`. Список не дублируется в скрипте: новая переменная
 * бота попадает на сервер, как только её завели в образце.
 */
export function botEnvKeys(example) {
  const lines = toLF(example).split("\n");
  const start = lines.findIndex((l) => ENV_BOT_SECTION.test(l));
  if (start < 0) throw new Error(".env.example: bot section '# --- Бот' not found");
  const keys = [];
  for (const line of lines.slice(start + 1)) {
    if (ENV_ANY_SECTION.test(line)) break;
    const m = line.match(ENV_EXAMPLE_KEY);
    if (m) keys.push(m[1]);
  }
  if (!keys.length) throw new Error(".env.example: bot section has no variables");
  return keys;
}

/**
 * `.env` для сервера: строки владельца ровно с переменными из списка, как
 * они записаны, с LF. Переменной нет — её нет и в результате: обязательные
 * проверит сам бот при старте, и проверка выкладки увидит отказ.
 */
export function selectEnv(envText, keys) {
  const wanted = new Set(keys);
  const found = new Set();
  const lines = [];
  for (const line of toLF(envText).split("\n")) {
    const m = line.match(ENV_LINE_KEY);
    if (!m || !wanted.has(m[1])) continue;
    lines.push(line);
    found.add(m[1]);
  }
  return {
    content: lines.length ? `${lines.join("\n")}\n` : "",
    names: keys.filter((k) => found.has(k)),
    missing: keys.filter((k) => !found.has(k)),
  };
}

/** Аргументы ssh: без запроса пароля и вопросов, с таймаутом соединения. */
export function sshArgs(remote) {
  return ["-o", "BatchMode=yes", "-o", `ConnectTimeout=${CONNECT_TIMEOUT_S}`, HOST, ...remote];
}

/** Адрес origin → HTTPS-адрес GitHub для клона без ключа; чужой — null. */
export function httpsRepoUrl(url) {
  const m = url.trim().match(GITHUB_REPO);
  return m ? `https://github.com/${m[1]}/${m[2]}.git` : null;
}

/**
 * Команда от имени solomon в чистом окружении: uv — в PATH, Python — только
 * из uv (системный 3.14 боту не подходит, §16.2).
 */
function asSolomon(script) {
  if (script.includes("'")) throw new Error("solomon script must not contain single quotes");
  const env = [
    `HOME=${APP_HOME}`,
    `PATH=${APP_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin`,
    "LANG=C.UTF-8",
    "UV_MANAGED_PYTHON=1",
  ].join(" ");
  return `runuser -u ${APP_USER} -- env -i ${env} sh -c '${script}'`;
}

/** Текст внутрь скрипта для сервера: base64 не ломает кавычки и строки. */
function payload(text) {
  return `printf '%s' '${Buffer.from(text, "utf8").toString("base64")}' | base64 -d`;
}

/**
 * Тело скрипта уходит через `bash -s`: оно в фигурных скобках, чтобы bash
 * прочёл его целиком до запуска, и с закрытым stdin — иначе команды съели
 * бы недочитанный остаток.
 */
function remoteScript(header, body) {
  return `${[...header, "cd /", "{", ...body, "} </dev/null"].join("\n")}\n`;
}

/**
 * Выкладка до перезапуска: код, зависимости, ключи (если даны), файл
 * службы из копии владельца. Ошибка любого шага останавливает скрипт —
 * бот не перезапускается.
 */
export function deployScript({ unit, env }) {
  const body = [
    "echo '== код с GitHub'",
    asSolomon(`cd ${APP_DIR} && git pull --ff-only`),
    "echo '== зависимости бота'",
    asSolomon(`cd ${APP_DIR}/bot && uv sync --frozen --no-dev`),
  ];
  if (env !== undefined) {
    const target = `${APP_DIR}/.env`;
    body.push(
      "echo '== ключи'",
      `${payload(env)} | ${asSolomon(
        `umask 077 && cat > ${target}.new && chmod 600 ${target}.new && mv -f ${target}.new ${target}`,
      )}`,
    );
  }
  body.push(
    "echo '== служба'",
    "unit=$(mktemp)",
    `${payload(unit)} > "$unit"`,
    `if cmp -s "$unit" ${UNIT_PATH}; then`,
    "  echo 'файл службы не изменился'",
    "else",
    `  install -m 644 "$unit" ${UNIT_PATH}`,
    "  systemctl daemon-reload",
    "  echo 'файл службы поставлен'",
    "fi",
    'rm -f "$unit"',
    `systemctl enable --quiet ${UNIT}`,
  );
  return remoteScript(["set -euo pipefail"], body);
}

/** Поля состояния и хвост журнала — только чтение. */
function reportLines() {
  return [
    `printf 'active=%s\\n' "$(systemctl is-active ${UNIT} 2>/dev/null)"`,
    `printf 'enabled=%s\\n' "$(systemctl is-enabled ${UNIT} 2>/dev/null)"`,
    `printf 'commit=%s\\n' "$(${asSolomon(`git -C ${APP_DIR} rev-parse HEAD`)} 2>/dev/null)"`,
    `printf 'memory=%s\\n' "$(systemctl show -p MemoryCurrent --value ${UNIT} 2>/dev/null)"`,
    `printf 'started=%s\\n' "$(systemctl show -p ActiveEnterTimestamp --value ${UNIT} 2>/dev/null)"`,
    `echo '${JOURNAL_MARK}'`,
    `journalctl -u ${UNIT} -n ${JOURNAL_LINES} --no-pager -o short-iso 2>/dev/null`,
  ];
}

/**
 * Перезапуск и проверка: до WAIT_S секунд ждёт в журнале службы, начиная с
 * момента перезапуска, строку начала опроса Telegram; затем — состояние.
 */
export function verifyScript() {
  const sinceRestart = `journalctl -u ${UNIT} --since "@$since" --no-pager -o cat 2>/dev/null`;
  return remoteScript(
    ["set -u"],
    [
      "since=$(date +%s)",
      `systemctl restart ${UNIT} || true`,
      "i=0",
      `while [ "$i" -lt ${WAIT_S} ]; do`,
      `  if ${sinceRestart} | grep -qF '${POLLING_LINE}'; then break; fi`,
      "  sleep 1",
      "  i=$((i + 1))",
      "done",
      `printf 'polling=%s\\n' "$(${sinceRestart} | grep -cF '${POLLING_LINE}')"`,
      `printf 'conflict=%s\\n' "$(${sinceRestart} | grep -cF '${CONFLICT_LINE}')"`,
      ...reportLines(),
    ],
  );
}

/** `--status`: ничего не меняет — ни pull, ни sync, ни записи, ни перезапуска. */
export function statusScript() {
  return remoteScript(["set -u"], reportLines());
}

/** Ответ сервера: `ключ=значение` до JOURNAL_MARK, после — строки журнала. */
export function parseReport(text) {
  const values = {};
  const journal = [];
  let inJournal = false;
  for (const line of toLF(text).split("\n")) {
    if (inJournal) {
      journal.push(line);
    } else if (line === JOURNAL_MARK) {
      inJournal = true;
    } else {
      const i = line.indexOf("=");
      if (i > 0) values[line.slice(0, i)] = line.slice(i + 1).trim();
    }
  }
  while (journal.length && journal.at(-1) === "") journal.pop();
  return { values, journal };
}

/** Токен бота в журнале не печатается, даже если бот его туда записал. */
function redact(line) {
  return line.replace(BOT_TOKEN, "<токен бота>");
}

function short(sha) {
  return sha ? sha.slice(0, 7) : "";
}

function megabytes(bytes) {
  const n = Number(bytes);
  return bytes && Number.isFinite(n) && n > 0 ? `${Math.round(n / 1048576)} МБ` : "нет данных";
}

/* ----------------------------------------------------------------- шаги */

const USAGE = [
  "Выкладка бота на сервер (techspec/16-server.md §16.4):",
  "  node scripts/deploy-bot.mjs            код, зависимости, служба, перезапуск, проверка",
  "  node scripts/deploy-bot.mjs --env      то же и ключи раздела бота из .env",
  "  node scripts/deploy-bot.mjs --status   состояние на сервере, ничего не меняет",
  "  node scripts/deploy-bot.mjs --setup    первая настройка сервера",
].join("\n");

function revParse(deps, ref) {
  const r = deps.git(["rev-parse", "--verify", "--quiet", ref]);
  return r.status === 0 ? (r.stdout ?? "").trim() : "";
}

function sshFailure(r, message) {
  if (r.error) return `ssh не запустился (${r.error.message}): нужен клиент OpenSSH и запись ${HOST} в ~/.ssh/config.`;
  if (r.status === 255) return `Не получилось зайти на ${HOST} (ssh, код 255): проверьте ~/.ssh/config и ключ.`;
  return `${message} (код ${r.status})`;
}

function showJournal(deps, journal) {
  deps.log(`Последние строки журнала (journalctl -u ${UNIT}):`);
  for (const line of journal.length ? journal : ["(пусто)"]) deps.log(`  ${redact(line)}`);
}

/** Дерево чистое и HEAD = свежий origin/main — иначе null, сервер не трогаем. */
function checkTree(deps) {
  const st = deps.git(["status", "--porcelain"]);
  if (st.status !== 0) {
    deps.log(`git status не ответил: ${(st.stderr ?? "").trim() || st.error?.message || "без причины"}`);
    return null;
  }
  if ((st.stdout ?? "").trim()) {
    deps.log("Рабочее дерево не чистое: сервер берёт код с GitHub, и незакоммиченное туда не попадёт.");
    deps.log("Закоммитьте или уберите изменения (git status), сделайте push в main и запустите снова.");
    return null;
  }
  const fetch = deps.git(["fetch", "--quiet", "origin", "main"]);
  if (fetch.status !== 0) {
    deps.log("Не получилось обновить origin/main (git fetch origin main) — сравнить HEAD с GitHub не с чем.");
    const why = (fetch.stderr ?? "").trim();
    if (why) deps.log(why);
    return null;
  }
  const head = revParse(deps, "HEAD");
  const remote = revParse(deps, "origin/main");
  if (!head || head !== remote) {
    deps.log(`HEAD ${short(head) || "?"} не совпадает с origin/main ${short(remote) || "?"}: сервер берёт код с GitHub.`);
    deps.log("Сделайте push в main с чистыми воротами (или git pull, если отстали) и запустите снова.");
    return null;
  }
  return head;
}

/** Ключи для сервера из .env владельца — или null с причиной. */
function envForServer(deps) {
  let keys;
  try {
    keys = botEnvKeys(deps.read(".env.example") ?? "");
  } catch {
    deps.log("В .env.example нет раздела бота («# --- Бот …»): список ключей для сервера берётся оттуда.");
    return null;
  }
  const text = deps.read(".env");
  if (text == null) {
    deps.log("Нет файла .env — ключам взяться неоткуда. Пустой .env стёр бы ключи на сервере, выкладка не начата.");
    return null;
  }
  const env = selectEnv(text, keys);
  if (!env.names.length) {
    deps.log(`В .env нет ни одной переменной бота (${keys.join(", ")}) — выкладка не начата: пустой .env стёр бы ключи на сервере.`);
    return null;
  }
  deps.log(`Ключи на сервер (${env.names.length} из ${keys.length}, значения не печатаются): ${env.names.join(", ")}`);
  if (env.missing.length) deps.log(`Нет в .env, не копируются: ${env.missing.join(", ")}`);
  return env;
}

function deploy(deps, { withEnv }) {
  const head = checkTree(deps);
  if (!head) return 1;

  const env = withEnv ? envForServer(deps) : undefined;
  if (env === null) return 1;

  const unit = deps.read(UNIT_FILE);
  if (unit == null) {
    deps.log(`Нет ${UNIT_FILE} в копии репозитория.`);
    return 1;
  }

  deps.log(`Выкладка ${short(head)} на ${HOST}`);
  const run = deps.ssh(sshArgs(["bash", "-s"]), {
    input: deployScript({ unit: toLF(unit), env: env?.content }),
    capture: false,
  });
  if (run.status !== 0) {
    deps.log(sshFailure(run, "Выкладка остановилась на сервере — причина в выводе выше; бот не перезапускался"));
    deps.log("Сервер ещё не настроен? Первая настройка — node scripts/deploy-bot.mjs --setup");
    return 1;
  }

  deps.log(`== перезапуск: жду в журнале «${POLLING_LINE}» до ${WAIT_S} с`);
  const check = deps.ssh(sshArgs(["bash", "-s"]), { input: verifyScript(), capture: true });
  if (check.status !== 0) {
    deps.log(sshFailure(check, "Проверка после перезапуска не ответила"));
    return 1;
  }

  const { values, journal } = parseReport(check.stdout ?? "");
  const problems = [];
  if (values.active !== "active") problems.push(`служба не active: ${values.active || "нет ответа"}`);
  if (!(Number(values.polling) > 0)) {
    problems.push(`за ${WAIT_S} с в журнале нет строки «${POLLING_LINE}» — опрос Telegram не начался`);
  }
  if (values.commit !== head) {
    problems.push(`коммит на сервере ${short(values.commit) || "не прочитан"}, а HEAD ${short(head)}`);
  }
  if (problems.length) {
    deps.log("Бот на сервере не поднялся:");
    for (const p of problems) deps.log(`  - ${p}`);
    showJournal(deps, journal);
    deps.log("Отката нет: исправление — новым коммитом, push и выкладкой (§16.4).");
    return 1;
  }

  if (Number(values.conflict) > 0) {
    deps.log(`Внимание: в журнале ${CONFLICT_LINE} — бот с этим токеном запущен где-то ещё; остановите второй (§16.3).`);
  }
  deps.log(`Готово: бот на сервере работает и опрашивает Telegram, коммит ${short(head)}, автозапуск: ${values.enabled || "?"}.`);
  return 0;
}

function status(deps) {
  const head = revParse(deps, "HEAD");
  const r = deps.ssh(sshArgs(["bash", "-s"]), { input: statusScript(), capture: true });
  if (r.status !== 0) {
    deps.log(sshFailure(r, "Сервер не ответил на запрос состояния"));
    return 1;
  }
  const { values, journal } = parseReport(r.stdout ?? "");
  const commit = values.commit ?? "";
  const same = commit !== "" && commit === head;
  deps.log(`Служба ${UNIT}: ${values.active || "нет"}, автозапуск: ${values.enabled || "нет"}`);
  deps.log(
    `Коммит на сервере: ${short(commit) || "не прочитан"} — ${same ? "совпадает с HEAD" : `не совпадает с HEAD ${short(head) || "?"}`}`,
  );
  deps.log(`Память службы: ${megabytes(values.memory)}`);
  deps.log(`Запущена: ${values.started || "—"}`);
  showJournal(deps, journal);
  return values.active === "active" ? 0 : 1;
}

function setup(deps) {
  const script = deps.read(SETUP_FILE);
  if (script == null) {
    deps.log(`Нет ${SETUP_FILE} в копии репозитория.`);
    return 1;
  }
  const origin = deps.git(["remote", "get-url", "origin"]);
  const url = origin.status === 0 ? httpsRepoUrl(origin.stdout ?? "") : null;
  if (!url) {
    deps.log("origin не похож на репозиторий GitHub: сервер клонирует код по HTTPS с github.com.");
    return 1;
  }
  deps.log(`Первая настройка ${HOST}: ${SETUP_FILE} от root, клон ${url}`);
  const r = deps.ssh(sshArgs(["bash", "-s", "--", url]), { input: toLF(script), capture: false });
  if (r.status !== 0) {
    deps.log(sshFailure(r, "Первая настройка остановилась — причина в выводе выше; повторный запуск продолжит с несделанного шага"));
    return 1;
  }
  deps.log("Дальше: остановите бота на компьютере (процесс бота один, §16.3) и выложите с ключами —");
  deps.log("  node scripts/deploy-bot.mjs --env");
  return 0;
}

/** Точка входа без побочных эффектов: git, ssh, файлы и вывод — в deps. */
export function main(argv, deps) {
  if (argv.includes("--help") || argv.includes("-h")) {
    deps.log(USAGE);
    return 0;
  }
  const flags = new Set(argv);
  const unknown = argv.filter((a) => !["--env", "--status", "--setup"].includes(a));
  const alone = flags.has("--status") || flags.has("--setup");
  if (unknown.length || (alone && flags.size > 1)) {
    deps.log(unknown.length ? `Не знаю флаг: ${unknown.join(" ")}` : "--status и --setup запускаются отдельно.");
    deps.log(USAGE);
    return 2;
  }
  if (flags.has("--status")) return status(deps);
  if (flags.has("--setup")) return setup(deps);
  return deploy(deps, { withEnv: flags.has("--env") });
}

/* ---------------------------------------------------------------- запуск */

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function realDeps(root) {
  return {
    git: (args) => spawnSync("git", args, { cwd: root, encoding: "utf8" }),
    ssh: (args, { input, capture }) =>
      spawnSync("ssh", args, {
        input,
        encoding: "utf8",
        stdio: ["pipe", capture ? "pipe" : "inherit", "inherit"],
        maxBuffer: 16 * 1024 * 1024,
      }),
    read: (rel) => {
      try {
        return fs.readFileSync(path.join(root, rel), "utf8");
      } catch (error) {
        if (error.code === "ENOENT") return null;
        throw error;
      }
    },
    log: (line) => console.log(line),
  };
}

if (import.meta.main) {
  process.exitCode = main(process.argv.slice(2), realDeps(ROOT));
}
