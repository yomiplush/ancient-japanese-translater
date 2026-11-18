#!/usr/bin/env bash
# Launch the ancient-japanese-translater live GUI.
#
# Responsibilities, in order:
#   1. Reuse a server that is already up (opening the GUI is idempotent).
#   2. Otherwise boot one, waiting until it actually answers before opening a
#      browser - a menu click that lands on "connection refused" looks broken.
#   3. Bootstrap the virtualenv on first run, because .venv is gitignored and a
#      fresh clone has nothing to run.
#
# Everything is keyed on a loopback health check, which is also how we tell
# "our server" apart from some unrelated process holding the port.
set -uo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
DEFAULT_APP_DIR="$(cd "$(dirname "$SELF")/../.." 2>/dev/null && pwd)"
APP_DIR="${AJT_APP_DIR:-$DEFAULT_APP_DIR}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/ajt"
LOG="$STATE_DIR/server.log"
PID_FILE="$STATE_DIR/server.pid"
PORT="${AJT_PORT:-8765}"
PORT_LAST=$((PORT + 10))
START_TIMEOUT="${AJT_START_TIMEOUT:-90}"

mkdir -p "$STATE_DIR"

notify() {
    if command -v notify-send >/dev/null 2>&1; then
        notify-send -a "古文翻訳" -i "${ICON:-utilities-terminal}" "古文 → 現代口語" "$1" >/dev/null 2>&1 || true
    fi
    printf '%s\n' "$1" >&2
}

die() {
    notify "$1"
    exit 1
}

health() {
    curl -fsS --max-time 2 "http://127.0.0.1:$1/api/health" 2>/dev/null
}

port_busy() {
    # A successful connect means something is listening there.
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

token_for() {
    curl -fsS --max-time 3 "http://127.0.0.1:$1/api/pair" 2>/dev/null |
        python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' 2>/dev/null
}

open_gui() {
    local port="$1"
    local url="http://127.0.0.1:$port/"
    local token
    token="$(token_for "$port")"
    [ -n "$token" ] && url="${url}?t=${token}"
    xdg-open "$url" >/dev/null 2>&1 &
}

pids_on_port() {
    # Fallback discovery for servers started outside the launcher (e.g. from a
    # terminal), which have no PID file. `ss -p` needs the owning user's
    # privileges, which we have for our own processes.
    if command -v ss >/dev/null 2>&1; then
        ss -ltnp 2>/dev/null |
            awk -v p=":$1" '$4 ~ p {print}' |
            grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u
    fi
}

stop_server() {
    local stopped=0
    local pids=""

    if [ -f "$PID_FILE" ]; then
        local pid
        pid="$(cat "$PID_FILE" 2>/dev/null || true)"
        [ -n "${pid:-}" ] && pids="$pid"
        rm -f "$PID_FILE"
    fi

    # Also cover a server running on any port in our range without a PID file.
    local p
    for p in $(seq "$PORT" "$PORT_LAST"); do
        if health "$p" >/dev/null; then
            pids="$pids $(pids_on_port "$p")"
        fi
    done

    for pid in $pids; do
        [ -n "$pid" ] || continue
        kill -0 "$pid" 2>/dev/null || continue
        # SIGTERM lets uvicorn close cleanly; escalate if it ignores us.
        kill "$pid" 2>/dev/null && stopped=1
        for _ in $(seq 1 40); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.1
        done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    done

    if [ "$stopped" -eq 1 ]; then
        notify "サーバーを停止しました"
    else
        notify "起動中のサーバーはありません"
    fi
}

venv_python() {
    printf '%s\n' "$APP_DIR/.venv/bin/python"
}

env_ready() {
    local py
    py="$(venv_python)"
    [ -x "$py" ] && "$py" -c 'import ajt, rapidocr_onnxruntime, fastapi' >/dev/null 2>&1
}

bootstrap_env() {
    [ -d "$APP_DIR" ] || die "プロジェクトが見つかりません: $APP_DIR"
    notify "初回セットアップ中です（数分かかります）…"
    if command -v uv >/dev/null 2>&1; then
        (
            cd "$APP_DIR" || exit 1
            uv venv --python 3.12 >/dev/null 2>&1
            uv pip install -e . >/dev/null 2>&1
        )
    else
        python3 -m venv "$APP_DIR/.venv" >/dev/null 2>&1 &&
            "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR" >/dev/null 2>&1
    fi
    env_ready || die "依存関係のインストールに失敗しました。$LOG を確認してください"
}

start_server() {
    local port="$1"
    local py
    py="$(venv_python)"
    # Run in its own session so closing the browser (or the launcher exiting)
    # does not take the server down, but record a PID we can actually signal.
    # `setsid --wait` would fork; instead start normally in a subshell and
    # disown, keeping $! meaningful for --stop.
    (
        cd "$APP_DIR" || exit 1
        AJT_PORT="$port" nohup "$py" -m ajt.cli serve --port "$port" \
            >>"$LOG" 2>&1 </dev/null &
        echo $! >"$PID_FILE"
    )
    disown -a 2>/dev/null || true
}

wait_ready() {
    local port="$1"
    local waited=0
    while [ "$waited" -lt "$((START_TIMEOUT * 10))" ]; do
        if health "$port" >/dev/null; then
            return 0
        fi
        sleep 0.1
        waited=$((waited + 1))
    done
    return 1
}

main() {
    case "${1:-}" in
        --stop)
            stop_server
            exit 0
            ;;
        --status)
            for p in $(seq "$PORT" "$PORT_LAST"); do
                if health "$p" >/dev/null; then
                    printf 'running on %s\n' "$p"
                    exit 0
                fi
            done
            printf 'stopped\n'
            exit 1
            ;;
    esac

    # 1. Already running somewhere in our port range?
    local p
    for p in $(seq "$PORT" "$PORT_LAST"); do
        if health "$p" >/dev/null; then
            open_gui "$p"
            exit 0
        fi
    done

    # 2. Pick the first free port, so an unrelated service on 8765 is survivable.
    local chosen=""
    for p in $(seq "$PORT" "$PORT_LAST"); do
        if ! port_busy "$p"; then
            chosen="$p"
            break
        fi
    done
    [ -n "$chosen" ] || die "空きポートが見つかりません（$PORT〜$PORT_LAST）"

    # 3. Make sure there is something to run.
    env_ready || bootstrap_env

    start_server "$chosen"
    if ! wait_ready "$chosen"; then
        notify "起動に失敗しました。ログ: $LOG"
        exit 1
    fi
    open_gui "$chosen"
}

main "$@"
