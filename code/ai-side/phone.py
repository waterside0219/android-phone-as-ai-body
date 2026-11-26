#!/usr/bin/env python3
"""AI 这头用的：让手机做一件事。

    phone.py say "一句话"            从手机喇叭说出来，屏上出字幕（手机静音时只出字幕）
    phone.py state 在想|在干活|在等|卡住了   换脸上的状态
    phone.py look                    要一帧镜头画面（结果里是 base64 JPEG）
    phone.py do <动作> '<JSON参数>'   直接发任意动作，比如 do tap_text '{"text":"设置"}'

环境变量：
    RELAY_URL   中转在本机的地址，默认 http://127.0.0.1:8832
    TOKEN_FILE  设备钥匙文件，默认 ../.private/body-device-token（相对本文件）
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

RELAY = os.environ.get("RELAY_URL", "http://127.0.0.1:8832")
TOKEN_FILE = Path(os.environ.get("TOKEN_FILE", Path(__file__).resolve().parents[1] / ".private" / "body-device-token"))
# say 要等手机念完才回；查状态、要画面这类快动作，手机没连上就立刻返回，不傻等。
QUICK = {"camera_frame": 12, "update_status": 10, "face_state": 10}


def phone_online():
    try:
        with urllib.request.urlopen(RELAY + "/v1/health", timeout=3) as response:
            return bool(json.load(response).get("phone_connected"))
    except (OSError, ValueError):
        return False


def do(action, args=None):
    args = args or {}
    if action in QUICK and not phone_online():
        return {"ok": False, "error": "phone_offline"}
    request = urllib.request.Request(
        RELAY + "/v1/do", data=json.dumps({"action": action, "args": args}).encode(),
        headers={"Authorization": "Bearer " + TOKEN_FILE.read_text().strip(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=QUICK.get(action, 90)) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return json.load(error)
    except (TimeoutError, urllib.error.URLError) as error:
        return {"ok": False, "error": "no_answer", "detail": str(error)[:80]}


def main(argv):
    if len(argv) == 2 and argv[0] == "say" and argv[1].strip():
        result = do("say", {"text": argv[1]})
    elif len(argv) == 2 and argv[0] == "state":
        result = do("face_state", {"state": argv[1]})
    elif argv == ["look"]:
        result = do("camera_frame", {})
    elif len(argv) in (2, 3) and argv[0] == "do":
        result = do(argv[1], json.loads(argv[2]) if len(argv) == 3 else {})
    else:
        sys.exit(__doc__)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
