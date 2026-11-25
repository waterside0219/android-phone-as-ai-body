#!/system/bin/sh
# 看门人（shell 身份常驻；重启后不在，重启后靠 Termux:Boot 和定时任务）。
# 每 20 秒看一眼：
#   Shizuku 没了          → 起它
#   agent 连着两回都没了  → 立刻触发 Termux 里的定时任务（heal.sh），不等 15 分钟
# 起：adb shell 'setsid nohup sh /data/local/tmp/heal/watch.sh >/dev/null 2>&1 < /dev/null &'
# 停：adb shell 'kill $(cat /data/local/tmp/heal/watch.pid)'
D=/data/local/tmp/heal
LOG=$D/watch.log
JOB=219                      # 和 termux-job-scheduler --job-id 用同一个数
if [ -f $D/watch.pid ] && kill -0 "$(cat $D/watch.pid)" 2>/dev/null; then exit 0; fi
echo $$ > $D/watch.pid
now() { date '+%F %T'; }
echo "$(now) watch start pid $$" >> $LOG
n=0
while true; do
    if ! pidof shizuku_server >/dev/null 2>&1; then
        S="$(pm path moe.shizuku.privileged.api | sed 's/package://; s/base.apk//')lib/arm64/libshizuku.so"
        echo "$(now) shizuku missing -> start" >> $LOG
        $S >> $LOG 2>&1
    fi
    if pgrep -f 'phone-agent/agent.py' >/dev/null 2>&1; then
        n=0
    else
        n=$((n+1))
        if [ $n -ge 2 ]; then
            if dumpsys telephony.registry 2>/dev/null | grep -q 'mCallState=[12]'; then
                echo "$(now) agent missing, phone call in progress, wait" >> $LOG
            else
                echo "$(now) agent missing -> run job $JOB" >> $LOG
                cmd jobscheduler run -f com.termux.api $JOB >> $LOG 2>&1
                sleep 60
            fi
            n=0
        fi
    fi
    sleep 20
done
