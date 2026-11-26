#!/usr/bin/env python3
import base64
import hashlib
import json
import os
import queue
import random
import re
import shlex
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from uuid import UUID
import xml.etree.ElementTree as ET
from pathlib import Path

from face_service import FaceService
from remote_update import Updater, atomic_write, connection_config

MAX_CONFIG = 64 * 1024
MAX_MESSAGE = 16 * 1024 * 1024
MAX_PNG = 10 * 1024 * 1024
MAX_AUDIO = 10 * 1024 * 1024
MAX_XML = 4 * 1024 * 1024
MAX_PRESETS = 64 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
UI_PATH = "/data/local/tmp/android-body-ui.xml"
ADB_IME = "com.android.adbkeyboard/.AdbIME"
FALLBACK_IME = "com.sohu.inputmethod.sogou.xiaomi/.SogouIME"
IME_RE = re.compile(r"[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+")
FORBIDDEN_TAPS = ("支付", "付款", "转账", "确认收货", "免密")
ACTIONS = {
    "screenshot", "read_screen", "tap", "tap_text", "input_text", "open_app", "back", "home",
    "face_state", "say", "say_preset", "eyes_close", "update", "update_status", "call_incoming", "camera_frame",
}
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ActionError(Exception):
    pass


class WebSocket:
    def __init__(self, sock, initial=b""):
        self.sock = sock
        self.buffer = bytearray(initial)
        self.send_lock = threading.Lock()
        self.last_received = time.monotonic()

    def _read(self, size):
        while len(self.buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise ConnectionError("websocket closed")
            self.last_received = time.monotonic()
            self.buffer.extend(chunk)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def send(self, opcode, payload=b""):
        length = len(payload)
        if opcode >= 8 and length > 125:
            raise ValueError("oversized control frame")
        head = bytearray([0x80 | opcode])
        if length < 126:
            head.append(0x80 | length)
        elif length <= 65535:
            head.append(0x80 | 126)
            head.extend(struct.pack("!H", length))
        else:
            head.append(0x80 | 127)
            head.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        head.extend(mask)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self.send_lock:
            self.sock.sendall(head + masked)

    def receive(self):
        fragments = bytearray()
        message_opcode = None
        while True:
            first, second = self._read(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            if first & 0x70 or second & 0x80:
                raise ConnectionError("invalid websocket frame")
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
                if length >> 63:
                    raise ConnectionError("invalid frame length")
            if length > MAX_MESSAGE:
                raise ConnectionError("websocket message too large")
            if opcode >= 8 and (not fin or length > 125):
                raise ConnectionError("invalid control frame")
            payload = self._read(length)
            if opcode == 8:
                raise ConnectionError("websocket closed by peer")
            if opcode == 9:
                self.send(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 0:
                if message_opcode is None:
                    raise ConnectionError("unexpected continuation")
                fragments.extend(payload)
            elif opcode in (1, 2):
                if message_opcode is not None:
                    raise ConnectionError("interleaved messages")
                message_opcode = opcode
                fragments.extend(payload)
            else:
                raise ConnectionError("unsupported websocket opcode")
            if len(fragments) > MAX_MESSAGE:
                raise ConnectionError("websocket message too large")
            if fin:
                return message_opcode, bytes(fragments)


def read_config(path):
    if os.path.getsize(path) > MAX_CONFIG:
        raise ValueError("config too large")
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("invalid config")
    config = connection_config(Path(path).resolve().parents[1], config)
    for key in ("url", "token", "device_id", "rish", "rish_application_id"):
        value = config.get(key)
        if not isinstance(value, str) or not value or len(value) > 2048 or "\r" in value or "\n" in value:
            raise ValueError("invalid " + key)
    parsed = urllib.parse.urlsplit(config["url"])
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname or parsed.path != "/agent" or parsed.query or parsed.fragment:
        raise ValueError("url must be ws(s)://host[:port]/agent")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", config["device_id"]):
        raise ValueError("invalid device_id")
    if not PACKAGE_RE.fullmatch(config["rish_application_id"]):
        raise ValueError("invalid rish_application_id")
    apps = config.get("apps")
    if not isinstance(apps, dict) or not apps:
        raise ValueError("apps must be a non-empty object")
    for alias, package in apps.items():
        if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias) or not isinstance(package, str) or not PACKAGE_RE.fullmatch(package):
            raise ValueError("invalid apps mapping")
    return config


def connect(config):
    parsed = urllib.parse.urlsplit(config["url"])
    secure = parsed.scheme == "wss"
    port = parsed.port or (443 if secure else 80)
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    sock = socket.create_connection((parsed.hostname, port), timeout=10)
    if secure:
        context = ssl.create_default_context()
        sock = context.wrap_socket(sock, server_hostname=parsed.hostname)
    sock.settimeout(10)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    host = parsed.hostname
    if ":" in host:
        host = "[" + host + "]"
    if parsed.port:
        host += ":" + str(port)
    request = (
        "GET /agent HTTP/1.1\r\nHost: " + host + "\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: " + key + "\r\nSec-WebSocket-Version: 13\r\n"
        "Authorization: Bearer " + config["token"] + "\r\nX-Device-Id: " + config["device_id"] + "\r\n\r\n"
    ).encode("ascii")
    sock.sendall(request)
    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("handshake failed")
        response.extend(chunk)
        if len(response) > 16384:
            raise ConnectionError("handshake too large")
    raw_head, initial = bytes(response).split(b"\r\n\r\n", 1)
    lines = raw_head.split(b"\r\n")
    if lines[0] != b"HTTP/1.1 101 Switching Protocols":
        raise ConnectionError("handshake rejected")
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if not separator:
            raise ConnectionError("invalid handshake")
        headers[name.strip().lower()] = value.strip()
    expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest())
    if headers.get(b"sec-websocket-accept") != expected:
        raise ConnectionError("invalid handshake accept")
    sock.settimeout(1)
    return WebSocket(sock, initial)


class EventSender:
    def __init__(self):
        self.lock = threading.Lock()
        self.ws = None

    def attach(self, ws):
        with self.lock:
            self.ws = ws

    def detach(self, ws):
        with self.lock:
            if self.ws is ws:
                self.ws = None

    def send(self, name, args):
        with self.lock:
            ws = self.ws
        if ws is None:
            return False
        payload = json.dumps({"event": name, "args": args}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ws.send(1, payload)
        return True


class AudioPlayer:
    def __init__(self, face):
        self.face = face
        self.items = queue.Queue(maxsize=8)
        self.thread = threading.Thread(target=self._worker, name="audio-player", daemon=True)
        self.thread.start()

    def queue_audio(self, audio, text):
        temporary = tempfile.NamedTemporaryFile(prefix="body-say-", suffix=".mp3", delete=False)
        path = temporary.name
        try:
            temporary.write(audio)
            temporary.close()
            self.queue_file(path, text, remove=True)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise

    def queue_file(self, path, text, remove=False, active=None):
        if not isinstance(text, str) or not text or len(text) > 4096 or "\x00" in text:
            raise ActionError("invalid subtitle")
        try:
            self.items.put_nowait((path, text, remove, active))
        except queue.Full as error:
            raise ActionError("audio queue is full") from error

    def _play(self, path, active=None):
        if self.face.is_muted() or (active and not active()):
            return
        self.face.prepare_speech(path)
        try:
            result = subprocess.run(
                ["termux-media-player", "play", path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self.face.finish_speech()
            raise ActionError("media player unavailable") from error
        if result.returncode != 0:
            self.face.finish_speech()
            raise ActionError("media player rejected audio")
        self.face.speech_started(time.time())
        deadline = time.monotonic() + 600
        started_by = time.monotonic() + 4
        started = False
        try:
            time.sleep(0.25)
            while time.monotonic() < deadline:
                if self.face.is_muted() or (active and not active()):
                    subprocess.run(["termux-media-player", "stop"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
                    return
                try:
                    info_result = subprocess.run(
                        ["termux-media-player", "info"], capture_output=True, timeout=5, check=False
                    )
                    if info_result.returncode == 0:
                        raw = info_result.stdout.decode("utf-8").strip()
                        if raw.startswith("No track currently"):
                            if started or time.monotonic() > started_by:
                                return
                            time.sleep(0.25)
                            continue
                        if raw.startswith("{"):
                            info = json.loads(raw)
                        else:
                            match = re.search(r"^Status:\s*(\w+)", raw, re.MULTILINE)
                            info = {"status": match.group(1) if match else "unknown"}
                        status = str(info.get("status", "")).lower()
                        playing = info.get("playing", status == "playing")
                        self.face.set_speaking(bool(playing))
                        self.face.speech_playing(bool(playing))
                        if playing or status in {"paused", "buffering"}:
                            started = True
                        elif started or time.monotonic() > started_by:
                            return
                except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                    pass
                time.sleep(0.25)
            subprocess.run(
                ["termux-media-player", "stop"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
            )
        finally:
            self.face.set_speaking(False)
            self.face.finish_speech()

    def _worker(self):
        while True:
            path, text, remove, active = self.items.get()
            try:
                if active is None:
                    self.face.set_subtitle(text)
                self._play(path, active)
                if active is None and self.face.is_muted():
                    time.sleep(max(8, len(text) * .3))
            except (ActionError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                print("audio playback failed: " + str(error), flush=True)
            finally:
                if remove:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                self.items.task_done()


class Device:
    def __init__(self, config, face):
        self.face = face
        self.updater = Updater(Path(__file__).resolve().parent.parent, config)
        self.audio = AudioPlayer(face)
        self.rish = os.path.abspath(config["rish"])
        self.apps = dict(config["apps"])
        self.night_applied = None
        self.brightness_restore_path = Path(__file__).with_name("brightness-before-night.json")
        self.ime_restore_path = Path(__file__).with_name("ime-before-input.json")
        self.command_lock = threading.Lock()
        self.android_reads = {}
        self.env = os.environ.copy()
        self.env.pop("LD_LIBRARY_PATH", None)
        self.env["RISH_APPLICATION_ID"] = config["rish_application_id"]
        self.env["PATH"] = "/system/bin:/system/xbin:/apex/com.android.runtime/bin:" + self.env.get("PATH", "")
        if not os.path.isfile(self.rish) or not os.path.isfile(os.path.join(os.path.dirname(self.rish), "rish_shizuku.dex")):
            raise ValueError("rish and rish_shizuku.dex must be in the same directory")

    def _run_command(self, command, timeout):
        try:
            with self.command_lock:
                completed = subprocess.run(
                    [self.rish, "-c", command], env=self.env, capture_output=True, timeout=timeout, check=False
                )
        except (OSError, subprocess.TimeoutExpired):
            raise ActionError("Android command failed or timed out")
        query = command.removeprefix('set -o pipefail; ')
        output = completed.stdout
        if query.startswith('dumpsys '):
            # rish can deliver successful Android stdout in both subprocess pipes.
            output += b'\n' + completed.stderr
            data = output.decode(errors='replace')
            diagnostic = {'returncode':completed.returncode, 'stdout_bytes':len(completed.stdout),
                          'stderr_bytes':len(completed.stderr)}
            label = query.split(' | ', 1)[0].removesuffix(' 2>&1')
            if label == 'dumpsys telephony.registry':
                diagnostic['call_fields'] = re.findall(r'\bmCallState\s*=\s*[^\s,}]+', data)[:4]
            if label == 'dumpsys activity activities':
                diagnostic['resumed'] = re.findall(r'^.*[Rr]esumedActivity[^\n]*', data, re.M)[:4]
            self.android_reads[label] = diagnostic
        elif command.startswith('settings get ') and not output.strip():
            output = completed.stderr
        if completed.returncode != 0:
            raise ActionError("Android command failed")
        return output

    def run(self, *args, timeout=20):
        command = " ".join(shlex.quote(arg) for arg in args)
        fields = {('dumpsys','activity','activities'):'[Rr]esumedActivity',
                  ('dumpsys','telephony.registry'):'mCallState[[:space:]]*=',
                  ('dumpsys','device_state'):'mCommittedState',
                  ('dumpsys','power'):'mLastUserActivityTime'}
        if args in fields:
            command = 'set -o pipefail; ' + command + ' 2>&1 | grep -E ' + shlex.quote(fields[args])
        return self._run_command(command, timeout)

    def current_input_method(self):
        current = self.run("settings", "get", "secure", "default_input_method", timeout=5).decode().strip()
        if not IME_RE.fullmatch(current):
            raise ActionError("unable to read current input method: " + repr(current)[:240])
        return current

    def restore_input_method(self, method):
        self.run("ime", "set", method, timeout=5)
        if self.current_input_method() != method:
            raise ActionError("input method restoration was not confirmed")

    def recover_input_method(self):
        current = self.current_input_method()
        if current != ADB_IME:
            return current
        try:
            previous = json.loads(self.ime_restore_path.read_text())["input_method"]
            if not isinstance(previous, str) or not IME_RE.fullmatch(previous) or previous == ADB_IME:
                previous = FALLBACK_IME
        except (OSError, ValueError, KeyError, TypeError):
            previous = FALLBACK_IME
        self.restore_input_method(previous)
        return previous

    def update_night_brightness(self):
        self.brightness_status = {'face_foreground':False, 'top':None, 'cap':None}
        try:
            local = time.localtime()
            foreground = self.run('dumpsys', 'activity', 'activities', timeout=5).decode(errors='replace')
            top = re.findall(r'^\s*(?:mTopResumedActivity|topResumedActivity)[:=].*?\s([\w.]+/[\w.$]+)', foreground, re.M)
            if not top:
                top = re.findall(r'^\s*mResumedActivity[:=].*?\s([\w.]+/[\w.$]+)', foreground, re.M)
            visible = (self.face.page_display or {}).get('visible') is True and time.time() - self.face.page_ready_at < 10
            face_foreground = visible and len(top) == 1 and bool(re.fullmatch(r'com\.android\.chrome/.*WebappActivity\d*', top[0]))
            self.face.camera_foreground = face_foreground
            target = None
            device_state = ''
            if face_foreground:
                target = 30 if local.tm_wday < 5 and 8 <= local.tm_hour < 18 else 60
                if getattr(self.face, 'thermal_hold', False) or local.tm_hour >= 23 or local.tm_hour < 7 or (self.face.page_display or {}).get('rest_stage') in ('dim', 'doze'):
                    target = 0
            self.brightness_status = {'face_foreground': face_foreground, 'cap': target,
                                      'local_hour': local.tm_hour, 'weekday': local.tm_wday, 'device_state': device_state, 'top': top[0] if len(top)==1 else None}
            if target == self.night_applied and (target is not None or not self.brightness_restore_path.exists()):
                return
            if target is not None:
                if not self.brightness_restore_path.exists():
                    previous = {key: self.run("settings", "get", "system", key, timeout=5).decode().strip()
                                for key in ("screen_brightness", "screen_brightness_mode")}
                    if not all(value.isdigit() for value in previous.values()):
                        raise ValueError("brightness settings unavailable")
                    atomic_write(self.brightness_restore_path, json.dumps(previous).encode(), 0o600)
                previous = json.loads(self.brightness_restore_path.read_text())
                self.run("settings", "put", "system", "screen_brightness_mode", "0", timeout=5)
                self.run("settings", "put", "system", "screen_brightness", str(min(target, int(previous['screen_brightness']))), timeout=5)
            elif self.brightness_restore_path.exists():
                previous = json.loads(self.brightness_restore_path.read_text())
                for key in ("screen_brightness", "screen_brightness_mode"):
                    self.run("settings", "put", "system", key, str(int(previous[key])), timeout=5)
                actual = {key: self.run('settings', 'get', 'system', key, timeout=5).decode().strip() for key in previous}
                if actual != previous:
                    raise ValueError('brightness restoration unconfirmed; backup retained')
                self.brightness_restore_path.unlink()
            if target is not None:
                actual = {key: self.run('settings', 'get', 'system', key, timeout=5).decode().strip() for key in previous}
                if actual != {'screen_brightness_mode':'0', 'screen_brightness':str(min(target,int(previous['screen_brightness'])))}:
                    raise ValueError('brightness change unconfirmed')
            self.night_applied = target
        except (ActionError, OSError, ValueError, KeyError):
            self.face.camera_foreground = None
            print("face brightness update failed; will retry", flush=True)

    def resume_face(self, reload=False):
        completed = subprocess.run([os.sys.executable, str(Path(__file__).with_name('start-face.py')), '--reload' if reload else '--resume'],
                                   env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if completed.returncode:
            raise ActionError('face resume failed')

    def guard_camera(self, now):
        wall = time.time()
        if (not self.face.camera_allowed(wall)
                or self.face.thermal_hold and not self.face.cooling_recovering
                or getattr(self, 'native_call_active', False)
                or getattr(self, 'shizuku_available', None) is False):
            self.camera_lost_since = None
            self.camera_retries = 0
            self.camera_resume_pending = False
            return
        resume_pending = getattr(self, 'camera_resume_pending', False)
        state = self.face.snapshot()
        foreground = (getattr(self, 'brightness_status', None) or {}).get('face_foreground') is True
        display = self.face.page_display or {}
        intentional_pause = state['camera_probe_mode'] and display.get('camera_probe_sleeping') is True and wall - self.face.camera_last_frame_at <= 45
        detector_ready = display.get('detector_ready') is True
        if not resume_pending and foreground and (state['camera_stream_alive'] and detector_ready or intentional_pause):
            self.camera_lost_since = None
            self.camera_retries = 0
            return
        if getattr(self, 'camera_lost_since', None) is None:
            self.camera_lost_since = now
            self.camera_retries = 0
            self.camera_retry_at = now
        if not resume_pending and foreground and now - self.camera_lost_since < 60:
            return
        if not resume_pending and now < self.camera_retry_at:
            return
        # A cached successful probe cannot authorize retries after Shizuku dies.
        if not resume_pending:
            self.check_shizuku(now, force=True)
        if not self.shizuku_available:
            self.camera_lost_since = None
            self.camera_retries = 0
            return
        self.camera_resume_pending = False
        if self.camera_retries < 3:
            self.camera_retries += 1
            self.camera_retry_at = now + 20
            call = (self.face.page_display or {}).get('call') or {}
            try:
                # Reload only an idle face; never tear down a live call room.
                self.resume_face(reload=call.get('state', 'idle') in ('idle', 'ended'))
            except (ActionError, OSError, subprocess.TimeoutExpired):
                pass  # Only a fresh stream report proves that the attempt worked.
            return
        if now - getattr(self, 'camera_lost_reported_at', -1e9) >= 600:
            frame = self.face.camera_frame()
            # Reopening changes generation; only a frame captured since this
            # outage proves recovery. Never compare/reset generation as health.
            if (frame.get('jpeg_base64')
                    and frame.get('captured_at', 0) > wall - (now - self.camera_lost_since)):
                self.camera_lost_since = None
                self.camera_retries = 0
                return
            self.face.queue_event('camera_lost', {'attempts': 3, 'foreground': foreground,
                                                 'camera_stream_alive': state['camera_stream_alive']})
            self.camera_lost_reported_at = now
            self.camera_retries = 0
            self.camera_retry_at = now + 600

    def check_shizuku(self, now, force=False):
        if not force and now < getattr(self, 'next_shizuku_check', 0):
            return
        self.next_shizuku_check = now + 300
        try:
            self.run('dumpsys', 'battery', timeout=5)
            available = True
        except (ActionError, OSError, subprocess.TimeoutExpired):
            available = False
        previous = getattr(self, 'shizuku_available', None)
        self.shizuku_available = available
        if previous != available and (previous is not None or not available):
            diagnostic = getattr(self, 'android_reads', {}).get('dumpsys battery', {})
            self.face.queue_event('shizuku_back' if available else 'shizuku_lost',
                                 {'available': available, 'returncode': diagnostic.get('returncode')})
        if available and previous is False:
            self.camera_lost_since = None
            self.camera_retries = 0
            self.camera_resume_pending = True

    @staticmethod
    def call_request(path, data=None):
        request = urllib.request.Request('http://127.0.0.1:8191' + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Content-Type':'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            return json.load(response)

    def incoming_call(self, args):
        if set(args) != {'id'}:
            raise ActionError('call_incoming requires id')
        try:
            identifier = str(UUID(args['id']))
        except (ValueError, TypeError, AttributeError):
            raise ActionError('invalid call id')
        if self.face.off_mode or self.face.phone_call_active:
            raise ActionError('phone is unavailable')
        try:
            invitation = self.call_request('/api/incoming', {'id': identifier})['incoming']
            self.run('input', 'keyevent', 'KEYCODE_WAKEUP', timeout=5)
            self.resume_face()
            subprocess.run(['termux-vibrate', '-d', '400'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            path, text = self.preset(2)
            def active():
                if time.time() >= invitation['expires']:
                    return False
                try:
                    return (self.call_request('/api/incoming').get('incoming') or {}).get('id') == identifier
                except (OSError, ValueError):
                    return False
            self.audio.queue_file(path, text, active=active)
            return {'incoming': invitation, 'ring_muted': self.face.is_muted()}
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise ActionError('incoming call could not be presented') from error

    def observe_call_state(self, raw, now, idle_seconds=None):
        states = re.findall(r'\bmCallState\s*=\s*([012])\b', raw)
        if not states:
            self.call_state_status = 'unavailable'
            return
        active = any(value != '0' for value in states)
        previous = getattr(self, 'native_call_active', False)
        display = getattr(self, 'brightness_status', {})
        if display.get('face_foreground'):
            self.last_face_at = now
        if active and not previous:
            self.return_after_call = now - getattr(self, 'last_face_at', -10000) < 60
        if previous and not active and getattr(self, 'return_after_call', False):
            self.return_deadline = now + 90
        self.native_call_active = active
        self.face.phone_call_active = active
        self.call_state_status = 'active' if active else 'idle'
        if active or now > getattr(self, 'return_deadline', 0):
            return
        top = display.get('top') or ''
        safe = (top.split('/')[0] in ('com.miui.home', 'com.android.incallui')
                and idle_seconds is not None and idle_seconds >= 10)
        if safe:
            if getattr(self, 'return_top', None) != top:
                self.return_top, self.return_idle_since = top, now
            elif now - self.return_idle_since >= 10:
                self.resume_face()
                self.return_deadline = 0
        else:
            self.return_top = None
            if display.get('face_foreground'):
                self.return_deadline = 0

    def maintain_device(self, stopped=None):
        # Device safety/restoration must continue through relay reconnects.
        stopped = stopped or threading.Event()
        next_temperature = 0
        while not stopped.is_set():
            self.update_night_brightness()
            try:
                raw = self.run('dumpsys', 'telephony.registry', timeout=5).decode(errors='replace')
                idle_seconds = None
                if getattr(self, 'native_call_active', False) or time.monotonic() < getattr(self, 'return_deadline', 0):
                    try:
                        power = self.run('dumpsys', 'power', timeout=5).decode(errors='replace')
                        ages = re.findall(r'mLastUserActivityTime(?:NoChangeLights)?=\d+ \((\d+) ms ago\)', power)
                        if ages:
                            idle_seconds = min(int(age) for age in ages) / 1000
                    except (ActionError, OSError, subprocess.TimeoutExpired):
                        pass  # Unknown idle time blocks return, not the valid call state.

                self.observe_call_state(raw, time.monotonic(), idle_seconds)
            except (ActionError, OSError, subprocess.TimeoutExpired):
                self.call_state_status = 'unavailable'
            if time.monotonic() >= next_temperature:
                try:
                    self.face.temperature()
                except ValueError:
                    pass
                next_temperature = time.monotonic() + 20
            if hasattr(self.face, 'camera_allowed'):
                self.check_shizuku(time.monotonic())
                self.guard_camera(time.monotonic())
            changed = getattr(self.face, 'display_changed', None)
            if changed is None:
                stopped.wait(5)
            else:
                changed.wait(5)
                changed.clear()

    def screen_nodes(self):
        local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "android-body-ui.xml")
        output = self.run("uiautomator", "dump", UI_PATH, timeout=25)
        if b"dumped to:" not in output:
            raise ActionError("screen dump failed; stale XML refused")
        try:
            inner_command = "cat " + shlex.quote(UI_PATH) + " > " + shlex.quote(local_path)
            self._run_command("run-as com.termux sh -c " + shlex.quote(inner_command), 20)
            with open(local_path, "rb") as handle:
                data = handle.read(MAX_XML + 1)
            if len(data) > MAX_XML:
                raise ActionError("screen XML too large")
            try:
                root = ET.fromstring(data.decode("utf-8"))
            except (UnicodeDecodeError, ET.ParseError):
                raise ActionError("invalid screen XML")
            nodes = []
            for element in root.iter("node"):
                if len(nodes) >= 2000:
                    raise ActionError("too many screen nodes")
                bounds = element.get("bounds", "")
                match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                if not match:
                    continue
                coordinates = [int(value) for value in match.groups()]
                if max(coordinates) > 20000 or coordinates[0] > coordinates[2] or coordinates[1] > coordinates[3]:
                    continue
                nodes.append({
                    "text": element.get("text", ""),
                    "content_desc": element.get("content-desc", ""),
                    "bounds": coordinates,
                })
            return nodes
        finally:
            try:
                self.run("rm", "-f", UI_PATH)
            except ActionError:
                pass
            try:
                os.unlink(local_path)
            except OSError:
                pass

    def preset(self, preset_id):
        if isinstance(preset_id, bool) or not isinstance(preset_id, int) or not 1 <= preset_id <= 10:
            raise ActionError("preset id must be an integer from 1 to 10")
        directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")
        audio_path = os.path.join(directory, str(preset_id) + ".mp3")
        metadata_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.json")
        try:
            if not 0 < os.path.getsize(audio_path) <= MAX_AUDIO or os.path.getsize(metadata_path) > MAX_PRESETS:
                raise ActionError("invalid or oversized preset")
            with open(metadata_path, "r", encoding="utf-8") as handle:
                metadata = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ActionError("preset unavailable") from error
        if isinstance(metadata, dict) and "presets" in metadata:
            metadata = metadata["presets"]
        if isinstance(metadata, dict):
            entry = metadata.get(str(preset_id))
        elif isinstance(metadata, list) and preset_id <= len(metadata):
            entry = metadata[preset_id - 1]
        else:
            entry = None
        if isinstance(entry, dict):
            entry = entry.get("text", entry.get("subtitle"))
        if not isinstance(entry, str) or not entry:
            raise ActionError("preset subtitle unavailable")
        return audio_path, entry

    def execute(self, action, args):
        if action == "call_incoming":
            return self.incoming_call(args)
        if action == "update_status":
            if args:
                raise ActionError("update_status takes no arguments")
            result = {**self.updater.status(), "default_input_method": None,
                      "relay_url": (getattr(self.updater, 'config', None) or {}).get('url'),
                      "agent_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      "face": self.face.snapshot(), "native_call": getattr(self, "call_state_status", "unavailable"), "android_reads": getattr(self, "android_reads", {}), "brightness": getattr(self, 'brightness_status', None)}
            try:
                result["default_input_method"] = self.current_input_method()
            except ActionError as error:
                result["input_method_error"] = str(error)
            if result['brightness'] is not None:
                try:
                    result['brightness_current'] = {key: self.run('settings', 'get', 'system', key, timeout=5).decode().strip()
                                                    for key in ('screen_brightness', 'screen_brightness_mode')}
                except ActionError as error:
                    result['brightness_error'] = str(error)
            return result
        if action == "update":
            if set(args) != {"version"}:
                raise ActionError("update requires version only")
            if time.time() - self.face.page_ready_at > 10 or self.face.snapshot()["speaking"] or (self.face.page_display or {}).get("call", {}).get("state", "idle") != "idle":
                raise ActionError("keep the eyes page open and idle before updating")
            try:
                return self.updater.begin(args["version"])
            except (ValueError, OSError) as error:
                raise ActionError(str(error)) from error
        if action == "face_state":
            if set(args) != {"state"}:
                raise ActionError("face_state requires state")
            try:
                return self.face.set_face_state(args["state"])
            except ValueError as error:
                raise ActionError(str(error)) from error
        if action == 'camera_frame':
            try:
                if set(args) == {'gesture_result'}:
                    return self.face.receive_gesture_result(args['gesture_result'])
                if not args or set(args) == {'mode'}:
                    if args.get('mode') == 'look' and getattr(self, 'native_call_active', False):
                        raise ActionError('native_call')
                    return self.face.camera_frame(args.get('mode', 'ordinary'))
                raise ValueError('invalid camera_frame arguments')
            except ValueError as error:
                raise ActionError(str(error)) from error
        if action == "say":
            if set(args) not in ({"text"}, {"text", "audio"}):
                raise ActionError("say requires text and optional audio")
            text = args["text"]
            if not isinstance(text, str) or not text.strip() or len(text) > 4096 or "\x00" in text:
                raise ActionError("invalid say text")
            if 'audio' not in args:
                self.face.set_subtitle(text)
                return {"queued": True, "text_only": True}
            audio = args["audio"]
            if not isinstance(text, str) or not text or len(text) > 4096 or "\x00" in text or not isinstance(audio, dict) or set(audio) != {"base64", "mime_type"}:
                raise ActionError("invalid say arguments")
            if audio["mime_type"] != "audio/mpeg" or not isinstance(audio["base64"], str):
                raise ActionError("say audio must be MP3")
            try:
                decoded = base64.b64decode(audio["base64"], validate=True)
            except (ValueError, base64.binascii.Error) as error:
                raise ActionError("invalid audio encoding") from error
            if not decoded or len(decoded) > MAX_AUDIO:
                raise ActionError("invalid or oversized audio")
            try:
                self.audio.queue_audio(decoded, text)
            except OSError as error:
                raise ActionError("unable to store temporary audio") from error
            return {"queued": True}
        if action == "say_preset":
            if set(args) != {"id"}:
                raise ActionError("say_preset requires id")
            path, text = self.preset(args["id"])
            self.audio.queue_file(path, text)
            return {"queued": True, "id": args["id"]}
        if action == "eyes_close":
            if set(args) != {"hours"}:
                raise ActionError("eyes_close requires hours")
            try:
                return self.face.close_eyes(args["hours"])
            except ValueError as error:
                raise ActionError(str(error)) from error
        if action == "screenshot":
            state = self.run("dumpsys", "device_state")
            current = re.search(rb"mCommittedState=.*?name='([^']+)'", state)
            display = 1 if current and current.group(1) in (b"CLOSED", b"TENT") else 0
            output = self.run("dumpsys", "SurfaceFlinger", "--display-id")
            match = re.search(rb"Display (\d+) \(HWC display " + str(display).encode() + rb"\)", output) if current else None
            command = ["screencap"]
            if match:
                command.extend(["-d", match.group(1).decode("ascii")])
            command.append("-p")
            path = "/data/data/com.termux/files/home/android-body-shot-" + os.urandom(6).hex() + ".jpg"
            try:
                shell_command = " ".join(shlex.quote(arg) for arg in command)
                shell_command += " | run-as com.termux /data/data/com.termux/files/usr/bin/python /data/data/com.termux/files/home/phone-agent/image_convert.py " + shlex.quote(path)
                self._run_command(shell_command, 30)
                with open(path, "rb") as handle:
                    data = handle.read(MAX_PNG + 1)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            if len(data) > MAX_PNG or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
                raise ActionError("invalid or oversized JPEG")
            return {"base64": base64.b64encode(data).decode("ascii"), "mime_type": "image/jpeg"}
        if action == "read_screen":
            return {"nodes": self.screen_nodes()}
        if action == "tap":
            x, y = args.get("x"), args.get("y")
            if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
                raise ActionError("x and y must be integers")
            if not 0 <= x <= 20000 or not 0 <= y <= 20000:
                raise ActionError("coordinates out of range")
            self.run("input", "tap", str(x), str(y))
            return {"x": x, "y": y}
        if action == "tap_text":
            wanted = args.get("text")
            if not isinstance(wanted, str) or not wanted or len(wanted) > 200:
                raise ActionError("invalid text")
            if any(word in wanted for word in FORBIDDEN_TAPS):
                raise ActionError("refusing protected tap")
            nodes = self.screen_nodes()
            labels = []
            for node in nodes:
                for key in ("text", "content_desc"):
                    label = node[key]
                    if label:
                        labels.append((label, node["bounds"]))
            hit = next((item for item in labels if item[0] == wanted), None)
            if hit is None:
                hit = next((item for item in labels if wanted in item[0]), None)
            if hit is None:
                raise ActionError("text not found")
            label, bounds = hit
            if any(word in label for word in FORBIDDEN_TAPS):
                raise ActionError("refusing protected tap")
            x, y = (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2
            self.run("input", "tap", str(x), str(y))
            return {"text": label, "x": x, "y": y}
        if action == "input_text":
            text = args.get("text")
            if not isinstance(text, str) or not text or len(text) > 2048 or "\x00" in text:
                raise ActionError("invalid input text")
            previous = self.recover_input_method()
            atomic_write(self.ime_restore_path, json.dumps({"input_method": previous}).encode(), 0o600)
            try:
                self.run("ime", "enable", ADB_IME)
                self.run("ime", "set", ADB_IME)
                self.run("am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", text)
            finally:
                self.restore_input_method(previous)
            return {"length": len(text)}
        if action == "open_app":
            app = args.get("app")
            if not isinstance(app, str) or app not in self.apps:
                raise ActionError("app alias is not allowed")
            package = self.apps[app]
            self.run("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
            return {"app": app, "package": package}
        if action in ("back", "home"):
            if args:
                raise ActionError("action takes no arguments")
            self.run("input", "keyevent", "KEYCODE_BACK" if action == "back" else "KEYCODE_HOME")
            return {"action": action}
        raise ActionError("action is not allowed")


def valid_json(value, depth=0):
    if depth > 8:
        return False
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= 4096
    if isinstance(value, list):
        return len(value) <= 1000 and all(valid_json(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return len(value) <= 1000 and all(isinstance(key, str) and len(key) <= 128 and valid_json(item, depth + 1) for key, item in value.items())
    return False


def valid_action_args(action, args):
    if action != "say":
        return valid_json(args)
    if set(args) == {'text'}:
        return isinstance(args['text'], str) and 0 < len(args['text'].strip()) <= 4096 and '\x00' not in args['text']
    if set(args) != {"text", "audio"} or not isinstance(args["text"], str) or len(args["text"]) > 4096:
        return False
    audio = args["audio"]
    return (
        isinstance(audio, dict) and set(audio) == {"base64", "mime_type"}
        and audio["mime_type"] == "audio/mpeg" and isinstance(audio["base64"], str)
        and len(audio["base64"]) <= MAX_MESSAGE
    )


def serve(config, device, events):
    ws = connect(config)
    events.attach(ws)
    device.face.agent_connected = True
    device.face.report_controls(force=True)
    print("connected", flush=True)
    last_ping = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now - ws.last_received > 90:
                raise ConnectionError("websocket timed out")
            if now - last_ping >= 20:
                ws.send(9, b"body")
                last_ping = now
                device.face.report_controls()
            try:
                opcode, payload = ws.receive()
            except socket.timeout:
                continue
            if opcode != 1 or len(payload) > MAX_MESSAGE:
                raise ConnectionError("expected text JSON")
            request_id = None
            try:
                request = json.loads(payload.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ActionError("invalid request")
                request_id = request.get("id")
                action = request.get("action")
                args = request.get("args")
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                    raise ActionError("invalid request id")
                if action not in ACTIONS or not isinstance(args, dict) or not valid_action_args(action, args):
                    raise ActionError("invalid action or arguments")
                result = device.execute(action, args)
                response = {"id": request_id, "ok": True, "result": result}
            except ActionError as error:
                if request_id is None:
                    raise ConnectionError("request has no id")
                response = {"id": request_id, "ok": False, "error": str(error)[:500]}
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                if request_id is None:
                    raise ConnectionError("invalid request")
                response = {"id": request_id, "ok": False, "error": "invalid request"}
            ws.send(1, json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    finally:
        device.face.agent_connected = False
        events.detach(ws)
        try:
            ws.send(8, struct.pack("!H", 1000))
        except (OSError, ConnectionError):
            pass
        ws.sock.close()


def main():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    config = read_config(path)
    events = EventSender()
    face = FaceService(events.send, privacy_path=os.path.join(os.path.dirname(path), "privacy.json"))
    device = Device(config, face)
    atomic_write(device.updater.work / "agent.pid", str(os.getpid()).encode())
    face.start()
    threading.Thread(target=device.maintain_device, name='body-monitor', daemon=True).start()
    delay = 1
    while True:
        try:
            device.recover_input_method()
        except ActionError:
            print("input method recovery unconfirmed; retry on reconnect", flush=True)
        try:
            serve(config, device, events)
            delay = 1
        except (OSError, ConnectionError, ssl.SSLError, ValueError):
            print("connection failed; retrying", flush=True)
        time.sleep(delay + random.random())
        delay = min(delay * 2, 60)


if __name__ == "__main__":
    main()
