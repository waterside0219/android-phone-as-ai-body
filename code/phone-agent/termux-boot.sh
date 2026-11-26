#!/data/data/com.termux/files/usr/bin/bash
set -u
# app_process/rish must use Android's system libraries.
unset LD_LIBRARY_PATH
export TZ=Asia/Shanghai
export PATH=/data/data/com.termux/files/usr/bin:/system/bin
termux-wake-lock
AGENT_DIR="$HOME/phone-agent"
cd "$AGENT_DIR" || exit 1
if ! pgrep -f '[p]ython.*phone-agent/agent.py' >/dev/null; then
    setsid nohup python "$AGENT_DIR/agent.py" >> agent.log 2>&1 < /dev/null &
fi
if ! pgrep -f '[p]ython.*phone-agent/serve_face.py' >/dev/null; then
    setsid nohup python "$AGENT_DIR/serve_face.py" >> "$HOME/face.log" 2>&1 < /dev/null &
fi
if [ -f "$HOME/call-web/start.sh" ]; then
    "$HOME/call-web/start.sh" >> "$HOME/call-web-start.log" 2>&1 || true
fi
# Bounded boot recovery, not a second resident supervisor.
for attempt in 1 2 3 4 5 6; do
    if python "$AGENT_DIR/start-face.py"; then
        echo 'face launch intent sent; verify foreground in boot acceptance'
        exit 0
    fi
    sleep 5
done
echo 'face not launched: Shizuku/startup needs attention'
