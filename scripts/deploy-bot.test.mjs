/**
 * Выкладка бота на сервер — без сети: ssh и git подменены, `.env` владельца
 * подставлен строкой. Пункты «до выкладки» из specs 015 и §16.4.
 *
 * Запуск: node --test "scripts/*.test.mjs" (так же зовут ворота).
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import { test } from "node:test";

import {
  JOURNAL_MARK,
  botEnvKeys,
  deployScript,
  httpsRepoUrl,
  main,
  parseReport,
  selectEnv,
  sshArgs,
  statusScript,
  toLF,
  verifyScript,
} from "./deploy-bot.mjs";

const ROOT = new URL("../", import.meta.url);
const repoFile = (rel) => fs.readFileSync(new URL(rel, ROOT), "utf8");

const BOM = "\uFEFF";

const BOT_KEYS = [
  "TELEGRAM_BOT_TOKEN",
  "OWNER_TELEGRAM_ID",
  "OWNER_TIMEZONE",
  "SUPABASE_URL",
  "SUPABASE_SERVICE_ROLE_KEY",
  "ANTHROPIC_API_KEY",
  "ANTHROPIC_BASE_URL",
  "DEEPGRAM_API_KEY",
];

const HEAD = "0123456789abcdef0123456789abcdef01234567";
const OTHER = "fedcba9876543210fedcba9876543210fedcba98";

/** Значения выдуманные; ANTHROPIC_BASE_URL нет нарочно. */
const SECRETS = {
  TELEGRAM_BOT_TOKEN: "123456789:AAFakeTokenForTestsOnly_abcdefghijk",
  OWNER_TELEGRAM_ID: "555000111",
  OWNER_TIMEZONE: "Asia/Yekaterinburg",
  SUPABASE_URL: "https://fakeref.supabase.co",
  SUPABASE_SERVICE_ROLE_KEY: "service-role-secret-value",
  ANTHROPIC_API_KEY: "sk-ant-fake-secret-value",
  DEEPGRAM_API_KEY: "deepgram-fake-secret-value",
};

const FOREIGN = {
  VITE_SUPABASE_URL: "https://fakeref.supabase.co",
  VITE_SUPABASE_ANON_KEY: "anon-public-value",
  SUPABASE_ACCESS_TOKEN: "sbp_access_secret_value",
  SUPABASE_DB_PASSWORD: "db-password-secret-value",
  JWT_SIGNING_SECRET: "jwt-signing-secret-value",
  LOG_LEVEL: "DEBUG",
};

/** `.env` владельца, как его пишет Блокнот: CRLF, BOM, комментарий. */
const OWNER_ENV =
  `${BOM}# личный .env\r\n` +
  Object.entries({ ...SECRETS, ...FOREIGN })
    .map(([k, v]) => `${k}=${v}`)
    .join("\r\n") +
  "\r\n";

const UNIT_CRLF = "[Unit]\r\nDescription=Solomon\r\n\r\n[Service]\r\nUser=solomon\r\n";
const SETUP_CRLF = '#!/usr/bin/env bash\r\nset -euo pipefail\r\nmain() { :; }\r\nmain "$@" </dev/null\r\n';

function report({
  polling = "1",
  conflict = "0",
  active = "active",
  enabled = "enabled",
  commit = HEAD,
  memory = "88080384",
  started = "Thu 2026-10-01 10:00:00 UTC",
  journal = ["2026-10-01T10:00:01+00:00 host solomon-bot[1]: INFO aiogram.dispatcher: Run polling for bot @solomon_bot"],
} = {}) {
  return (
    [
      `polling=${polling}`,
      `conflict=${conflict}`,
      `active=${active}`,
      `enabled=${enabled}`,
      `commit=${commit}`,
      `memory=${memory}`,
      `started=${started}`,
      JOURNAL_MARK,
      ...journal,
    ].join("\n") + "\n"
  );
}

/** Подменённый мир: git, ssh, файлы репозитория и вывод — всё записывается. */
function world({
  dirty = "",
  head = HEAD,
  originMain = HEAD,
  fetchFails = false,
  origin = "https://github.com/owner/secretar-solomon.git",
  files = {},
  remote = report(),
  sshStatus = 0,
} = {}) {
  const calls = { git: [], ssh: [], out: [] };
  const allFiles = {
    ".env.example": repoFile(".env.example"),
    ".env": OWNER_ENV,
    "deploy/solomon-bot.service": UNIT_CRLF,
    "deploy/setup-server.sh": SETUP_CRLF,
    ...files,
  };
  const deps = {
    git(args) {
      calls.git.push(args.join(" "));
      const [cmd] = args;
      const last = args.at(-1);
      if (cmd === "status") return { status: 0, stdout: dirty, stderr: "" };
      if (cmd === "fetch") {
        return { status: fetchFails ? 128 : 0, stdout: "", stderr: fetchFails ? "fatal: unable to access" : "" };
      }
      if (cmd === "rev-parse" && last === "HEAD") return { status: 0, stdout: `${head}\n`, stderr: "" };
      if (cmd === "rev-parse" && last.includes("origin/main")) return { status: 0, stdout: `${originMain}\n`, stderr: "" };
      if (cmd === "remote") return { status: 0, stdout: `${origin}\n`, stderr: "" };
      throw new Error(`unexpected git ${args.join(" ")}`);
    },
    ssh(args, { input, capture }) {
      calls.ssh.push({ args, input, capture });
      return { status: sshStatus, stdout: capture ? remote : "" };
    },
    read(rel) {
      return rel in allFiles ? allFiles[rel] : null;
    },
    log(line) {
      calls.out.push(line);
    },
  };
  return { deps, calls, output: () => calls.out.join("\n") };
}

/** Полезная нагрузка скрипта: `printf '%s' '<base64>' | base64 -d`. */
function payloads(script) {
  return [...script.matchAll(/printf '%s' '([A-Za-z0-9+/=]*)' \| base64 -d/g)].map((m) =>
    Buffer.from(m[1], "base64").toString("utf8"),
  );
}

/**
 * Скрипт без частей, что идут от имени solomon, — то, что делает root.
 * Полезная нагрузка тоже гасится: в base64 есть `+` и `/`, и случайное
 * «git» между ними читалось бы как слово.
 */
function rootPart(script) {
  return script
    .replace(/printf '%s' '[A-Za-z0-9+/=]*'/g, "<payload>")
    .replace(/runuser -u solomon -- [^']*'[^']*'/g, "<solomon>");
}

const escape = (s) => s.replace(/[.*+?^${}()|[\]\\%]/g, "\\$&");

/* ------------------------------------------------------------------ ключи */

test("ключи для сервера — ровно раздел бота из .env.example", () => {
  assert.deepEqual(botEnvKeys(repoFile(".env.example")), BOT_KEYS);
});

test("ключи других разделов .env.example в список не попадают", () => {
  const example = [
    "# шапка",
    "# --- Бот (что-то про сервер) ---",
    "# комментарий",
    "A_KEY=",
    "",
    "B_KEY=",
    "# --- Mini App ---",
    "VITE_X=",
    "# --- Edge Function telegram-auth ---",
    "#   supabase secrets set TELEGRAM_BOT_TOKEN=... ",
  ].join("\r\n");
  assert.deepEqual(botEnvKeys(example), ["A_KEY", "B_KEY"]);
});

test("нет раздела бота — ошибка, а не пустой список", () => {
  assert.throws(() => botEnvKeys("# --- Mini App ---\nVITE_X=\n"));
});

test(".env для сервера: только переменные из списка, с LF; отсутствующей нет", () => {
  const env = selectEnv(OWNER_ENV, BOT_KEYS);
  assert.equal(env.content.includes("\r"), false);
  assert.equal(env.content.includes(BOM), false);
  const names = env.content
    .split("\n")
    .filter(Boolean)
    .map((l) => l.split("=")[0]);
  assert.deepEqual(names, Object.keys(SECRETS));
  for (const name of Object.keys(FOREIGN)) assert.equal(env.content.includes(name), false, name);
  assert.deepEqual(env.names, Object.keys(SECRETS));
  assert.deepEqual(env.missing, ["ANTHROPIC_BASE_URL"]);
  assert.match(env.content, /^TELEGRAM_BOT_TOKEN=123456789:AAFakeTokenForTestsOnly_abcdefghijk$/m);
});

/* ------------------------------------------------------ отказы до сервера */

test("грязное дерево — отказ до первого ssh и подсказка", () => {
  const w = world({ dirty: " M bot/src/solomon/cli.py\n?? new.txt\n" });
  assert.equal(main([], w.deps), 1);
  assert.equal(w.calls.ssh.length, 0);
  assert.match(w.output(), /не чистое/);
  assert.match(w.output(), /git status/);
});

test("HEAD не равен origin/main — отказ до ssh; origin/main сначала обновляется", () => {
  const w = world({ originMain: OTHER });
  assert.equal(main(["--env"], w.deps), 1);
  assert.equal(w.calls.ssh.length, 0);
  const fetchAt = w.calls.git.findIndex((c) => c.startsWith("fetch"));
  const compareAt = w.calls.git.findIndex((c) => c.startsWith("rev-parse") && c.includes("origin/main"));
  assert.ok(fetchAt >= 0 && fetchAt < compareAt, w.calls.git.join(" | "));
  assert.match(w.output(), /push/);
  assert.match(w.output(), new RegExp(OTHER.slice(0, 7)));
});

test("origin/main не обновился — отказ до ssh", () => {
  const w = world({ fetchFails: true });
  assert.equal(main([], w.deps), 1);
  assert.equal(w.calls.ssh.length, 0);
  assert.match(w.output(), /fetch/);
});

test("--env без .env — отказ до ssh: пустой .env стёр бы ключи на сервере", () => {
  const w = world({ files: { ".env": null } });
  assert.equal(main(["--env"], w.deps), 1);
  assert.equal(w.calls.ssh.length, 0);
});

test("--env без единой переменной бота — отказ до ssh", () => {
  const w = world({ files: { ".env": "VITE_SUPABASE_URL=x\r\n" } });
  assert.equal(main(["--env"], w.deps), 1);
  assert.equal(w.calls.ssh.length, 0);
});

test("неизвестный флаг и несовместимые флаги — код 2 без ssh", () => {
  for (const argv of [["--force"], ["--status", "--env"], ["--setup", "--status"]]) {
    const w = world();
    assert.equal(main(argv, w.deps), 2, argv.join(" "));
    assert.equal(w.calls.ssh.length, 0);
  }
});

/* --------------------------------------------------------------- выкладка */

test("ssh — без вопросов и с таймаутом соединения", () => {
  const args = sshArgs(["bash", "-s"]);
  const opts = args.join(" ");
  assert.match(opts, /-o BatchMode=yes/);
  assert.match(opts, /-o ConnectTimeout=\d+/);
  assert.deepEqual(args.slice(-3), ["voicefin-vps", "bash", "-s"]);
});

test("--env: значения только в stdin ssh, в аргументах и выводе — только имена", () => {
  const w = world();
  assert.equal(main(["--env"], w.deps), 0, w.output());
  assert.ok(w.calls.ssh.length >= 2);
  for (const call of w.calls.ssh) {
    const args = call.args.join(" ");
    for (const value of Object.values(SECRETS)) {
      assert.equal(args.includes(value), false, `значение в аргументах: ${value}`);
      assert.equal(args.includes(Buffer.from(value).toString("base64")), false);
    }
  }
  const out = w.output();
  for (const value of Object.values(SECRETS)) assert.equal(out.includes(value), false, `значение в выводе: ${value}`);
  for (const name of Object.keys(SECRETS)) assert.match(out, new RegExp(name));
  assert.match(out, /ANTHROPIC_BASE_URL/);

  const sent = w.calls.ssh.flatMap((c) => payloads(c.input ?? ""));
  const envSent = sent.find((p) => p.includes("TELEGRAM_BOT_TOKEN="));
  assert.ok(envSent, "ключи не ушли через stdin");
  assert.equal(envSent, selectEnv(OWNER_ENV, BOT_KEYS).content);
  assert.equal(envSent.includes("\r"), false);
  for (const name of Object.keys(FOREIGN)) assert.equal(envSent.includes(name), false, name);
});

test("выкладка без --env ключей не кладёт и .env не читает", () => {
  const w = world({ files: { ".env": null } });
  assert.equal(main([], w.deps), 0, w.output());
  const script = w.calls.ssh.map((c) => c.input ?? "").join("\n");
  assert.equal(/\.env\b/.test(script), false);
});

test("git, uv и запись .env — от имени solomon; root их не зовёт", () => {
  const env = selectEnv(OWNER_ENV, BOT_KEYS);
  const script = deployScript({ unit: toLF(UNIT_CRLF), env: env.content });
  assert.doesNotMatch(rootPart(script), /\bgit\b|\buv\b|\.env\b/);
  assert.match(script, /runuser -u solomon -- [^']*'[^']*git pull --ff-only[^']*'/);
  assert.match(script, /runuser -u solomon -- [^']*'[^']*uv sync --frozen --no-dev[^']*'/);
  assert.match(script, /base64 -d \| runuser -u solomon -- [^']*'[^']*> [^']*\.env[^']*'/);
  for (const s of [verifyScript(), statusScript()]) {
    assert.doesNotMatch(rootPart(s), /\bgit\b|\buv\b|\.env\b/);
    assert.match(s, /runuser -u solomon -- [^']*'[^']*git -C [^']* rev-parse HEAD[^']*'/);
  }
});

test("файл службы ставится из копии владельца с LF, не из клона на сервере", () => {
  const w = world();
  assert.equal(main([], w.deps), 0, w.output());
  const script = w.calls.ssh.map((c) => c.input ?? "").join("\n");
  assert.ok(payloads(script).includes(toLF(UNIT_CRLF)), "файл службы не из копии владельца");
  assert.equal(toLF(UNIT_CRLF).includes("\r"), false);
  assert.match(rootPart(script), /\/etc\/systemd\/system\/solomon-bot\.service/);
  assert.doesNotMatch(script, /secretar-solomon\/deploy/);
  assert.match(script, /daemon-reload/);
  assert.match(script, /systemctl enable/);
});

test("порядок на сервере: код, зависимости, ключи, служба, перезапуск", () => {
  const w = world();
  assert.equal(main(["--env"], w.deps), 0, w.output());
  const script = w.calls.ssh.map((c) => c.input ?? "").join("\n");
  const order = [/git pull/, /uv sync/, /\.env\.new/, /\/etc\/systemd\/system/, /systemctl restart/].map((re) =>
    script.search(re),
  );
  assert.ok(
    order.every((i) => i >= 0),
    order.join(","),
  );
  assert.deepEqual(
    [...order].sort((a, b) => a - b),
    order,
  );
});

test("в аргументах ssh выкладки — только bash -s, скрипт идёт через stdin", () => {
  const w = world();
  main(["--env"], w.deps);
  for (const call of w.calls.ssh) assert.deepEqual(call.args.slice(-2), ["bash", "-s"]);
});

/* --------------------------------------------------------------- проверка */

test("перезапуск: ждёт строку начала опроса до 60 с с момента перезапуска", () => {
  const s = verifyScript();
  assert.match(s, /systemctl restart solomon-bot/);
  assert.match(s, /Run polling for bot/);
  assert.match(s, /--since/);
  assert.match(s, /\b60\b/);
});

test("служба active, строка есть, коммит равен HEAD — успех с коммитом", () => {
  const w = world();
  assert.equal(main([], w.deps), 0, w.output());
  assert.match(w.output(), new RegExp(HEAD.slice(0, 7)));
});

test("служба не active — ошибка и хвост журнала", () => {
  const journal = ["line one", "Traceback: boom"];
  const w = world({ remote: report({ active: "activating", polling: "0", journal }) });
  assert.equal(main([], w.deps), 1);
  assert.match(w.output(), /activating/);
  assert.match(w.output(), /Traceback: boom/);
});

test("за 60 с нет строки опроса — ошибка и хвост журнала", () => {
  const w = world({ remote: report({ polling: "0", journal: ["ConfigError: OWNER_TIMEZONE"] }) });
  assert.equal(main([], w.deps), 1);
  assert.match(w.output(), /Run polling for bot/);
  assert.match(w.output(), /ConfigError: OWNER_TIMEZONE/);
});

test("коммит на сервере не равен HEAD — ошибка", () => {
  const w = world({ remote: report({ commit: OTHER }) });
  assert.equal(main([], w.deps), 1);
  assert.match(w.output(), new RegExp(OTHER.slice(0, 7)));
});

test("шаг на сервере упал — ошибка без перезапуска", () => {
  const w = world({ sshStatus: 1 });
  assert.equal(main([], w.deps), 1);
  assert.equal(w.calls.ssh.length, 1);
  assert.doesNotMatch(w.calls.ssh[0].input, /systemctl restart/);
});

test("токен бота в журнале не печатается", () => {
  const token = "987654321:AAAnotherFakeTokenValue_abcdefghijk";
  const w = world({ remote: report({ polling: "0", journal: [`GET https://api.telegram.org/bot${token}/getMe`] }) });
  main([], w.deps);
  assert.equal(w.output().includes(token), false);
});

test("разбор ответа сервера: поля и журнал", () => {
  const parsed = parseReport(report({ journal: ["a=b", "c"] }));
  assert.equal(parsed.values.active, "active");
  assert.equal(parsed.values.commit, HEAD);
  assert.equal(parsed.values.polling, "1");
  assert.deepEqual(parsed.journal, ["a=b", "c"]);
});

/* --------------------------------------------------------------- --status */

test("--status ничего не меняет на сервере", () => {
  const w = world({ dirty: " M x\n", originMain: OTHER });
  assert.equal(main(["--status"], w.deps), 0, w.output());
  assert.equal(w.calls.ssh.length, 1);
  const script = w.calls.ssh[0].input.replace(/2>\/dev\/null/g, "").replace(/<\/dev\/null/g, "");
  assert.doesNotMatch(
    script,
    /\bpull\b|\bsync\b|\brestart\b|\bstart\b|\bstop\b|daemon-reload|\benable\b|\binstall\b|\bmv\b|\bcp\b|\brm\b|\btee\b|\bchmod\b|base64|mktemp|>/,
  );
  assert.equal(statusScript(), w.calls.ssh[0].input);
  assert.match(w.output(), new RegExp(HEAD.slice(0, 7)));
  assert.match(w.output(), /84 МБ/);
});

test("--status: коммит на сервере против HEAD", () => {
  const w = world({ head: OTHER });
  main(["--status"], w.deps);
  assert.match(w.output(), new RegExp(HEAD.slice(0, 7)));
  assert.match(w.output(), new RegExp(OTHER.slice(0, 7)));
  assert.match(w.output(), /не совпадает/);
});

/* ---------------------------------------------------------------- --setup */

test("--setup: скрипт из копии владельца с LF через stdin, адрес клона — HTTPS", () => {
  const w = world({ origin: "git@github.com:owner/secretar-solomon.git" });
  assert.equal(main(["--setup"], w.deps), 0, w.output());
  assert.equal(w.calls.ssh.length, 1);
  const [call] = w.calls.ssh;
  assert.equal(call.input, toLF(SETUP_CRLF));
  assert.deepEqual(call.args.slice(-4), ["bash", "-s", "--", "https://github.com/owner/secretar-solomon.git"]);
});

test("адрес репозитория приводится к HTTPS, чужой — отказ", () => {
  assert.equal(httpsRepoUrl("https://github.com/a/b.git"), "https://github.com/a/b.git");
  assert.equal(httpsRepoUrl("https://github.com/a/b"), "https://github.com/a/b.git");
  assert.equal(httpsRepoUrl("git@github.com:a/b.git"), "https://github.com/a/b.git");
  assert.equal(httpsRepoUrl("ssh://git@github.com/a/b"), "https://github.com/a/b.git");
  assert.equal(httpsRepoUrl("https://gitlab.com/a/b.git"), null);
  assert.equal(httpsRepoUrl("https://github.com/a/b.git; rm -rf /"), null);
});

/* ------------------------------------------------------------ файлы deploy/ */

test("служба: пользователь, запуск, перезапуск, пределы, защита", () => {
  const unit = repoFile("deploy/solomon-bot.service");
  for (const line of [
    "User=solomon",
    "Restart=always",
    "RestartSec=10",
    "MemoryMax=300M",
    "CPUQuota=100%",
    "WantedBy=multi-user.target",
    "NoNewPrivileges=yes",
    "PrivateTmp=yes",
    "ProtectSystem=full",
    "Environment=PYTHONUNBUFFERED=1",
  ]) {
    assert.match(unit, new RegExp(`^${escape(line)}\\r?$`, "m"), line);
  }
  assert.match(unit, /^ExecStart=\S*\/bot\/\.venv\/bin\/solomon-bot\r?$/m);
});

test("первая настройка: uv той же версии, что в §1, Python — как в bot/.python-version", () => {
  const setup = repoFile("deploy/setup-server.sh");
  const stackUv = repoFile("techspec/01-stack.md").match(/^\| uv \| ([\d.]+) \|/m)?.[1];
  assert.ok(stackUv);
  assert.match(setup, new RegExp(`^readonly UV_VERSION=${escape(stackUv)}\\r?$`, "m"));
  const python = repoFile("bot/.python-version").trim();
  assert.match(setup, new RegExp(`^readonly PYTHON_VERSION=${escape(python)}\\r?$`, "m"));
});

test("первая настройка не ставит пакетов, службы и не запускает бота", () => {
  const code = repoFile("deploy/setup-server.sh")
    .split("\n")
    .filter((l) => !l.trimStart().startsWith("#"))
    .join("\n");
  assert.doesNotMatch(code, /\bapt(-get)?\b|\bsystemctl\b|solomon-bot\.service|\.env\b/);
  assert.match(code, /nologin/);
  assert.match(code, /"\$UV_BIN" sync --frozen --no-dev/);
});

test("toLF: CRLF и BOM уходят", () => {
  assert.equal(toLF(`${BOM}a\r\nb\rc\n`), "a\nb\nc\n");
});
