# 被系统关掉以后自己恢复

**问题**：手机里常驻的程序跑在 Termux 里。Termux 的卡片在最近任务里被划走（哪怕加了锁），或者被系统清理，里面所有进程一起没了。人看到的是：脸页打不开，AI 看不见也听不见了。以前每次都要人打开 Termux 敲一行命令。

**做法**：两层，互相不依赖。

| | 谁在跑 | 多快 | 重启后 |
|---|---|---|---|
| 定时任务 | 安卓的 JobScheduler，经 Termux:API 叫起 `heal.sh` | 最慢 15 分钟 | 还在（登记为 persisted） |
| 守护脚本 `watch.sh` | 一个 shell 身份的后台进程 | 20 到 40 秒 | 不在，要重新起 |

守护脚本不自己去启动程序，它只做一件事：用 `cmd jobscheduler run -f` 把那个定时任务立刻触发一次。这样程序始终是在 Termux 自己的环境里被拉起来的，不用往屏幕上打字，锁着屏也行。

## 装

需要：Termux、Termux:API、Termux:Boot，电脑上有 adb。

```sh
# 1. 把 heal.sh 放进 Termux 的家目录（Termux 里做，或者见下面的 run-as）
cp heal.sh ~/heal.sh && chmod 700 ~/heal.sh

# 2. 登记定时任务（Termux 里跑）
termux-job-scheduler --job-id 219 --script ~/heal.sh --period-ms 900000 --persisted true --battery-not-low false

# 3. 起守护脚本（电脑上跑）
adb shell mkdir -p /data/local/tmp/heal
adb push watch.sh /data/local/tmp/heal/
adb shell 'setsid nohup sh /data/local/tmp/heal/watch.sh >/dev/null 2>&1 < /dev/null &'
```

**不碰屏幕就在 Termux 身份下跑命令**：从 GitHub 下载的 Termux 是可调试版本，`adb shell run-as com.termux <命令>` 可以直接用它的身份读写它的家目录、跑它的程序。上面第 1、2 步我们就是这么做的，没有往手机屏幕上敲字：

```sh
adb shell 'run-as com.termux sh -c "export HOME=/data/data/com.termux/files/home PREFIX=/data/data/com.termux/files/usr PATH=/data/data/com.termux/files/usr/bin:/system/bin; termux-job-scheduler --job-id 219 --script \$HOME/heal.sh --period-ms 900000 --persisted true --battery-not-low false"'
```

（从 F-Droid 装的 Termux 不是可调试版本，`run-as` 不能用。）

## 验

把它杀掉，不碰，看它回不回来：

```sh
adb shell 'for p in $(pidof com.termux); do run-as com.termux kill -9 $p; done'
sleep 60
adb shell 'cat /data/local/tmp/heal/watch.log; ps -A | grep -E "python|termux"'
```

我们验过的（2026 年 11 月 25 日，小米 MIX Flip 2）：

| 情形 | 结果 |
|---|---|
| 线插着、屏亮着，用命令杀 | 38 秒后回来 |
| 熄屏锁着，用命令杀 | 回来了 |
| 拔了线，用手在最近任务里划掉 | 43 秒后回来 |

还没验的：手机重启之后；Shizuku 自己死掉时守护脚本能不能把它拉起来；长时间熄屏后定时任务还跑不跑。

## 为什么不用别的办法

- **从 shell 直接发 RUN_COMMAND 给 Termux**：shell 没有那个权限；用 `run-as` 以 Termux 的身份去发，系统报 `Can't start from invalid source`。
- **往 Termux 的终端里模拟打字**：能用，我们以前就是这么救的。但要亮屏、要解锁、会抢前台。
- **只靠系统的自启动、电池白名单、最近任务加锁**：都开着，卡片照样能被划掉。

`heal.sh` 是照我们在用的那份整理的，删掉了只跟我们自己另一个常驻程序有关的一段。
