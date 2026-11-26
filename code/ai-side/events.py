#!/usr/bin/env python3
"""AI 这头用的：把手机报来的事（被摸、挥手、回来了、打字留言、太烫了……）攒一攒，敲进 AI 的会话。

AI 跑在 tmux 里的一个终端客户端（比如 Claude Code）。这个脚本常驻，每 2 秒问一次中转有没有新事件，
同类合并，两次敲入至少隔 20 秒，不刷屏。本身不花模型额度。

    TMUX_TARGET=ai:0.0 events.py        常驻
    events.py status                    看状态

环境变量：RELAY_URL、TOKEN_FILE 同 phone.py；TMUX_TARGET 必填；TMUX_SOCKET 可选。
"""
import collections
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

RELAY = os.environ.get("RELAY_URL", "http://127.0.0.1:8832")
HERE = Path(__file__).resolve().parents[1]
TOKEN_FILE = Path(os.environ.get("TOKEN_FILE", HERE / ".private" / "body-device-token"))
STATE = Path(os.environ.get("EVENTS_STATE", HERE / ".private" / "events-state.json"))
POLL, WINDOW, MIN_GAP = 2, 4, 20   # 秒：轮询间隔；收到第一条后再攒几秒；两次敲入最少间隔
TEMP_GAP, CAMERA_GAP, GESTURE_GAP = 1800, 3600, 1200

TOUCH = {"tap": "戳了你", "chin": "挠你下巴", "hold": "按住你", "two_finger_lock": "两指锁/解锁了触摸"}
GESTURE = {"wave": "冲你挥手", "thumb_up": "冲你点赞", "victory": "冲你比耶", "iloveyou": "比了我爱你", "heart": "比心"}


def load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    os.replace(tmp, STATE)


def fetch(token, after):
    request = urllib.request.Request(f"{RELAY}/v1/events?after={after}", headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def summarize(events, state, now=None):
    now = time.time() if now is None else now
    touch, other = collections.Counter(), []
    for event in events:
        kind, args = event.get("event"), event.get("args") or {}
        if kind == "controls":
            state["controls"] = args
        elif kind == "touch":
            touch[TOUCH.get(args.get("kind"), str(args.get("kind")))] += 1
        elif kind == "presence":
            # "在看你""走开了"很频繁，每条都推会吃掉额度；只推"离开一阵又回来了"。
            if args.get("kind") == "arrive":
                other.append("回来了（离开一阵后又出现在镜头前）")
        elif kind == "gesture":
            # 打键盘的手会被认成比心，一上午能误报十来回；比心先不推。其余手势同一种 20 分钟只推一次。
            if args.get("name") == "heart":
                continue
            key = "last_gesture_" + str(args.get("name"))
            if key in state and now - state[key] < GESTURE_GAP:
                continue
            state[key] = now
            other.append(GESTURE.get(args.get("name"), str(args.get("name"))))
        elif kind == "message":
            text = str(args.get("text", "")).strip()[:500]
            if text:
                other.append(f"在手机的消息面板上打字对你说：「{text}」（回的话用 phone.py say，会出现在面板里）")
        elif kind == "cooling_start":
            other.append(f"手机太烫（{args.get('temperature_c')}℃），进入降温闭眼，镜头停了")
        elif kind == "cooling_end":
            state.pop("last_camera_lost", None)
            other.append(f"手机凉下来了（{args.get('temperature_c')}℃），睁眼重开镜头")
        elif kind == "camera_lost":
            # Shizuku 掉了时手机每十来分钟报一回；一小时内只推一次，恢复后重置。
            if "last_camera_lost" in state and now - state["last_camera_lost"] < CAMERA_GAP:
                continue
            state["last_camera_lost"] = now
            other.append("手机上的脸/镜头丢了，手机自己拉了 3 次没拉回来（可能要人把脸页点回前台）")
        elif kind == "shizuku_lost":
            other.append("手机的 Shizuku 掉了（远程点按、自己拉回前台会失灵，要人重新启动它）")
        elif kind == "shizuku_back":
            state.pop("last_camera_lost", None)
            other.append("手机的 Shizuku 恢复了")
        elif kind == "temperature":
            if "last_temp_note" not in state or now - state["last_temp_note"] > TEMP_GAP:
                state["last_temp_note"] = now
                other.append(f"手机温度 {args.get('temperature_c')}℃，偏烫")
    parts = [f"{name}{count}下" if count > 1 else name for name, count in touch.items()]
    return "、".join(parts + list(dict.fromkeys(other)))


def inject(text, controls):
    base = ["tmux"] + (["-S", os.environ["TMUX_SOCKET"]] if os.environ.get("TMUX_SOCKET") else [])
    target = os.environ["TMUX_TARGET"]
    if subprocess.run(base + ["has-session", "-t", target.split(":")[0]], capture_output=True).returncode:
        return False
    quiet = ""
    if controls.get("off_mode") or controls.get("proactive_enabled") is False:
        quiet = "对方开着全关，别出声别开口。"
    elif controls.get("muted"):
        quiet = "手机现在静音，回的话只出字幕。"
    message = (f"[手机 {time.strftime('%H:%M')}] 这是手机报来的事，不是对方在聊天里发的消息：对方{text}。{quiet}"
               "想回就用 phone.py say \"一句话\"；不必每次都回，别刷屏。")
    subprocess.run(base + ["send-keys", "-t", target, "-l", message], check=True)
    time.sleep(1)
    subprocess.run(base + ["send-keys", "-t", target, "Enter"], check=True)
    return True


def run():
    if not os.environ.get("TMUX_TARGET"):
        sys.exit("set TMUX_TARGET, e.g. TMUX_TARGET=ai:0.0")
    token = TOKEN_FILE.read_text().strip()
    state = load()
    pending, first_at = [], 0.0
    while True:
        try:
            if "last_id" not in state:          # 第一次跑：从现在开始，不补旧事件
                state["last_id"] = fetch(token, 0).get("last_id", 0)
            else:
                data = fetch(token, state["last_id"])
                last = data.get("last_id", 0)
                if last < state["last_id"]:       # 中转重启过，编号从头来
                    state["last_id"] = 0
                else:
                    if data.get("events"):
                        pending += data["events"]
                        first_at = first_at or time.time()
                    state["last_id"] = last
        except Exception as error:                # 中转暂时不通，接着等
            print("fetch failed:", error, flush=True)
        now = time.time()
        if pending and now - first_at >= WINDOW and now - state.get("last_inject", 0) >= MIN_GAP:
            text = summarize(pending, state, now)
            pending, first_at = [], 0.0
            if text and inject(text, state.get("controls") or {}):
                state["last_inject"] = now
        save(state)
        time.sleep(POLL)


if __name__ == "__main__":
    if sys.argv[1:] == ["status"]:
        print(json.dumps(load(), ensure_ascii=False, indent=1))
    else:
        run()
