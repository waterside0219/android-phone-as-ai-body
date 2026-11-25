#!/data/data/com.termux/files/usr/bin/bash
# 自己恢复（Termux 里跑）：三样常驻的少了哪样补哪样。
# 谁来叫它：
#   1. termux-job-scheduler 的定时任务，每 15 分钟一回，重启后还在
#   2. watch.sh 发现 agent 没了的时候，立刻触发同一个任务
# 把下面三处路径换成你自己的常驻程序。
unset LD_LIBRARY_PATH
export HOME=/data/data/com.termux/files/home PREFIX=/data/data/com.termux/files/usr
export PATH=$PREFIX/bin:/system/bin TMPDIR=$PREFIX/tmp
LOG=$HOME/heal.log
now() { date '+%F %T'; }
if pgrep -f '[p]ython.*phone-agent/agent.py' >/dev/null; then
    # 主程序还在：只补掉了的小的。不去拉前台，免得每 15 分钟抢一次屏幕
    if ! pgrep -f '[p]ython.*phone-agent/serve_face.py' >/dev/null; then
        cd "$HOME/phone-agent" && setsid nohup python "$HOME/phone-agent/serve_face.py" >> "$HOME/face.log" 2>&1 < /dev/null &
        echo "$(now) face server missing -> restarted" >> "$LOG"
    fi
    exit 0
fi
echo "$(now) agent missing -> boot" >> "$LOG"
# 和开机时走同一个入口（Termux:Boot 的脚本），它自己会防重复
bash "$HOME/.termux/boot/body-agent" >> "$LOG" 2>&1
echo "$(now) boot rc=$?" >> "$LOG"
