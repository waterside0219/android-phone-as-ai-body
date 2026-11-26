#!/data/data/com.termux/files/usr/bin/bash
# USB-installed entry. Remote packages cannot replace this recovery gate.
unset LD_LIBRARY_PATH
export PATH=/data/data/com.termux/files/usr/bin:/system/bin
export TZ=Asia/Shanghai
if [ -f "$HOME/.body-update/runner.py" ]; then
    python "$HOME/.body-update/runner.py" --root "$HOME" --recover || exit 1
fi
exec bash "$HOME/phone-agent/termux-boot.sh"
