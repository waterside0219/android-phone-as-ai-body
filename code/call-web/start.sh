#!/data/data/com.termux/files/usr/bin/bash
set -eu
umask 077

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=/data/data/com.termux/files/usr/bin/python
PID_FILE="$ROOT/.call-web.pid"
LOCK_DIR="$ROOT/.call-web-start.lock"
LOG_FILE="$ROOT/call-web.log"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "call-web 正在由另一个启动命令处理"
  exit 1
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE" 2>/dev/null || true)
  case "$PID" in
    ''|*[!0-9]*) PID= ;;
  esac
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    ARGS=$(tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true)
    case "$ARGS" in
      *"$ROOT/server.py"*)
        echo "call-web 已在运行"
        exit 0
        ;;
      *)
        echo "call-web 的进程编号文件指向其他进程，请先检查"
        exit 1
        ;;
    esac
  fi
  rm -f "$PID_FILE"
fi

nohup "$PYTHON" "$ROOT/server.py" >> "$LOG_FILE" 2>&1 &
PID=$!
printf '%s\n' "$PID" > "$PID_FILE"
sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "call-web 启动失败，请检查配置和日志权限"
  exit 1
fi

echo "call-web 已启动：http://127.0.0.1:8191"
