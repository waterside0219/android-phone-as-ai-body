# 代码

一共四块，分在三个地方跑。

```
手机（Termux 里）                         云上一台小机器                     AI 那头（tmux 里的 AI 会话）
┌──────────────────────────────┐        ┌───────────────────────────┐      ┌──────────────────────┐
│ phone-agent/  连云、执行动作  │◄──────►│ server/body_relay.py 中转  │◄────►│ ai-side/phone.py     │
│   face_service  脸的本机接口  │ 手机主动 │   say → 合成声音            │      │   让手机说话、点按    │
│   serve_face    出脸页        │ 连出去   │   ?transcribe=1 → 听写      │      │ ai-side/events.py    │
│ face/         脸页（网页）    │        │ server/body_vision.py 看图  │─────►│   把手机的事敲进会话  │
│ call-web/     网页电话        │        │ server/update_phone.py 发版 │      │                      │
└──────────────────────────────┘        └───────────────────────────┘      └──────────────────────┘
```

| 目录 | 跑在哪 | 干什么 |
|---|---|---|
| [`phone-agent/`](phone-agent) | 手机 Termux | `agent.py` 用 WebSocket 连到中转，收到动作就经 Shizuku 的 `rish` 以 shell 身份去做（截屏、读屏、点按、打字、开 App、说话、换表情、要一帧画面……），手机上发生的事（被摸、挥手、太烫、留言）往回报。`face_service.py` 是脸页用的本机接口（127.0.0.1:8192），`serve_face.py` 出脸页（127.0.0.1:8190），`start-face.py` 把脸页拉回前台，`remote_update.py` 收版本包、校验、装、坏了退回。`termux-boot.sh` 是开机入口。 |
| [`face/`](face) | 手机 Chrome | 脸页。眼睛（SVG）或 Live2D、字幕、触摸、设置、消息面板；`vision.js` + `vision-worker.js` 在手机本地用 MediaPipe 看人、认手势；防烧屏、降温、静音、全关都在这里。 |
| [`call-web/`](call-web) | 手机 Termux + Chrome | 网页电话。`server.py`（127.0.0.1:8191）一头接脸页，一头接语音网关；听写先走中转上的云听写，不通再用手机本地 whisper；回声过滤也在这里。`app.js` 是电话的网页部分：切句、上传、排队播放、断线重连、睡眠。 |
| [`server/`](server) | 云上 | `body_relay.py` 中转：只听 127.0.0.1:8832，外面只开放 `/agent` 给手机；本机的 `/v1/do`、`/v1/events`、`/v1/health` 给 AI 用。`body_hearing.py` 听写，`body_vision.py` 看图（常驻），`update_phone.py` 打版本包推给手机。 |
| [`ai-side/`](ai-side) | AI 那台机器 | `phone.py` 让手机做事，`events.py` 把手机的事敲进 AI 会话。 |
| [`deploy/`](deploy) | 云上 | 配置样例、systemd 单元、看图的启动脚本。 |

另外，仓库根目录的 [`keepalive/`](../keepalive) 是手机里的程序被系统关掉以后自己恢复的那一套。

## 端口

全部只在本机（127.0.0.1）上听，外面进不来：

| 端口 | 在哪 | 是什么 |
|---|---|---|
| 8190 | 手机 | 脸页 |
| 8191 | 手机 | 电话服务 |
| 8192 | 手机 | 脸的本机接口 |
| 8832 | 云上 | 中转。只有 `/agent` 经隧道对外 |

## 动作和事件

AI 能让手机做的（`/v1/do` 的 `action`）：

`say`（说话＋字幕）、`say_preset`（放一段预制语音）、`face_state`（在想/在干活/在等/卡住了）、`eyes_close`（闭眼）、`camera_frame`（要一帧画面）、`screenshot`、`read_screen`、`tap`、`tap_text`、`input_text`、`open_app`、`back`、`home`、`call_incoming`（AI 打电话给人）、`update`、`update_status`。

手机报回来的事件（`/v1/events`）：

`touch`、`gesture`、`presence`、`message`、`temperature`、`cooling_start`、`cooling_end`、`camera_lost`、`shizuku_lost`、`shizuku_back`、`controls`。

两条写死在代码里的线：`tap_text` 碰到"支付、付款、转账、确认收货、免密"这几个字不点，付钱的按钮留给人自己按；`open_app` 只开配置里列出的 App。

## 测试

```
pip install aiohttp pillow
cd phone-agent && python -m unittest discover -p 'test*.py'
cd ../server && python -m unittest discover -p 'test*.py'
cd ../call-web && python -m unittest discover -p 'test*.py'
cd ../ai-side && python -m unittest test_events
```

`face/` 和 `call-web/` 下的 `test-*.cjs` 是网页测试，要 Node 和 Playwright（`PLAYWRIGHT_PATH` 指向你装的 playwright）。用到 MediaPipe 或 Live2D 的几个，要先把对应文件放进 `face/vendor/`、`face/models/`、`face/runtime/`，见[搭建](../docs/搭建.md)。

已知：`call-web` 里有两三个按秒计时的测试（比如"6 秒内退到本地听写"），机器忙的时候会差零点几秒不过，原版也一样。
