#!/usr/bin/env bash
# Соломон — первая настройка сервера (techspec/16-server.md §16.2).
#
# Запускается от root одной командой с компьютера владельца:
#
#   node scripts/deploy-bot.mjs --setup
#
# Скрипт уходит на сервер через stdin ssh (`bash -s`), адрес репозитория
# на GitHub — первым аргументом. Что делает: пользователь solomon без входа
# с папкой /opt/solomon, uv и Python 3.12 от его имени, клон репозитория по
# HTTPS, окружение бота по замку. Шаг, который уже сделан, пропускается:
# повторный запуск ничего не переделывает.
#
# Чего не делает: не ставит пакеты системы — git и curl на сервере хозяйство
# VoiceFin; не кладёт ключи, не ставит службу и не запускает бота — это
# делает выкладка (§16.4). Адреса сервера и ключей здесь нет.

set -euo pipefail

readonly APP_USER=solomon
readonly APP_HOME=/opt/solomon
readonly APP_DIR=$APP_HOME/secretar-solomon
# Та же версия, что в techspec/01-stack.md §1.1 — тест выкладки их сверяет.
readonly UV_VERSION=0.11.21
readonly UV_BIN=$APP_HOME/.local/bin/uv
# Как в bot/.python-version.
readonly PYTHON_VERSION=3.12

say() { printf '%s\n' "$*"; }
skip() { say "  уже сделано: $*"; }
fail() {
  say "Остановка: $*" >&2
  exit 1
}

# Команда от имени solomon в чистом окружении. Root внутри папки бота не
# работает (§16.2): git под root выполнил бы хуки чужого клона, uv — сборку
# пакета, оба с правами root. UV_MANAGED_PYTHON — Python только из uv:
# системный 3.14 боту не подходит.
as_solomon() {
  runuser -u "$APP_USER" -- env -i \
    HOME="$APP_HOME" \
    PATH="$APP_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    LANG=C.UTF-8 \
    UV_MANAGED_PYTHON=1 \
    "$@"
}

check_tools() {
  local tool missing=()
  for tool in git curl; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  if ((${#missing[@]})); then
    fail "на сервере нет: ${missing[*]}. Пакеты системы этот скрипт не ставит — это хозяйство VoiceFin. Поставьте и запустите снова."
  fi
}

ensure_user() {
  say "Пользователь $APP_USER"
  if id -u "$APP_USER" >/dev/null 2>&1; then
    local home
    IFS=: read -r _ _ _ _ _ home _ < <(getent passwd "$APP_USER")
    [ "$home" = "$APP_HOME" ] ||
      fail "пользователь $APP_USER уже есть, но его папка $home, а не $APP_HOME — разберитесь руками."
    [ -d "$APP_HOME" ] || fail "у пользователя $APP_USER нет папки $APP_HOME — разберитесь руками."
    skip "есть, папка $APP_HOME"
    return
  fi
  local shell
  shell=$(command -v nologin || echo /usr/sbin/nologin)
  useradd --system --user-group --create-home --home-dir "$APP_HOME" --shell "$shell" "$APP_USER"
  say "  заведён: без входа ($shell), папка $APP_HOME"
}

uv_version() {
  local out=""
  [ -x "$UV_BIN" ] && out=$(as_solomon "$UV_BIN" --version 2>/dev/null || true)
  # «uv 0.11.21 (5aa65dd7a 2026-06-11 …)» → «0.11.21»
  out=${out#uv }
  printf '%s' "${out%% *}"
}

ensure_uv() {
  say "uv $UV_VERSION"
  if [ "$(uv_version)" = "$UV_VERSION" ]; then
    skip "стоит в $UV_BIN"
    return
  fi
  # Установщик uv той же версии, без правки профилей: входа у solomon нет.
  as_solomon bash -c "set -euo pipefail; curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh | env UV_INSTALL_DIR=$APP_HOME/.local/bin UV_NO_MODIFY_PATH=1 sh"
  [ "$(uv_version)" = "$UV_VERSION" ] || fail "uv поставился, но $UV_BIN не отвечает версией $UV_VERSION."
  say "  поставлен в $UV_BIN"
}

ensure_python() {
  say "Python $PYTHON_VERSION"
  if as_solomon "$UV_BIN" python find "$PYTHON_VERSION" >/dev/null 2>&1; then
    skip "есть: $(as_solomon "$UV_BIN" python find "$PYTHON_VERSION")"
    return
  fi
  as_solomon "$UV_BIN" python install "$PYTHON_VERSION"
}

ensure_clone() {
  local repo_url=$1
  say "Клон репозитория"
  if [ -d "$APP_DIR/.git" ]; then
    skip "есть в $APP_DIR"
    return
  fi
  [ ! -e "$APP_DIR" ] || fail "$APP_DIR есть, но это не клон git — разберитесь руками."
  as_solomon git clone --branch main "$repo_url" "$APP_DIR"
}

ensure_venv() {
  say "Окружение бота"
  if [ -x "$APP_DIR/bot/.venv/bin/solomon-bot" ]; then
    skip "есть в $APP_DIR/bot/.venv"
    return
  fi
  (cd "$APP_DIR/bot" && as_solomon "$UV_BIN" sync --frozen --no-dev)
}

main() {
  [ "$(id -u)" -eq 0 ] || fail "запускать от root."
  local repo_url=${1:-}
  case "$repo_url" in
    https://github.com/*/*) ;;
    *) fail "первым аргументом нужен адрес репозитория вида https://github.com/<владелец>/<репозиторий>.git" ;;
  esac
  check_tools

  # Из папки root (/root) solomon ничего не прочитает, а uv ищет
  # .python-version от текущей папки — работаем из своей.
  cd /
  ensure_user
  cd "$APP_HOME"
  ensure_uv
  ensure_python
  ensure_clone "$repo_url"
  ensure_venv

  say ""
  say "Готово: пользователь, uv, Python, клон и окружение на месте."
  say "Службу ставит и бота запускает выкладка: node scripts/deploy-bot.mjs --env"
  say "Бот на компьютере перед ней останавливается — процесс бота один (§16.3)."
}

# Скрипт приходит через stdin (bash -s): тело целиком в функции, а stdin
# командам закрыт — иначе они съели бы недочитанный остаток скрипта.
main "$@" </dev/null
