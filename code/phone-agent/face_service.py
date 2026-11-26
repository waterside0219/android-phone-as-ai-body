#!/usr/bin/env python3
import base64
import json
import math
import os
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone, timedelta
from remote_update import atomic_write

MAX_BODY = 64 * 1024
MAX_FRAME_BODY = 120 * 1024
MAX_JPEG = 80 * 1024
LOOK_WAIT_SECONDS = 8
FACE_STATES = {
    "在想": "在想",
    "在干活": "在干活",
    "在等": "在等",
    "卡住了": "卡住了",
    "thinking": "在想",
    "working": "在干活",
    "waiting": "在等",
    "stuck": "卡住了",
}
TOUCH_KINDS = {"tap", "chin", "hold", "two_finger_lock", "four_finger_unlock"}


class FaceService:
    def __init__(self, event_sender=None, privacy_path=None):
        self.event_sender = event_sender
        self.lock = threading.Lock()
        self.agent_connected = False
        self.page_ready = ""
        self.page_ready_at = 0.0
        self.page_display = None
        self.display_changed = threading.Event()
        self.release = "bootstrap"
        release_path = Path(__file__).resolve().parent.parent / '.body-update/current.json'
        if release_path.is_file():
            self.release = json.loads(release_path.read_text())["version"]
        self.state = "在等"
        self.subtitle = ""
        self.subtitle_until = 0.0
        self.speaking_until = 0.0
        self.speech = None
        self.speech_bytes = None
        self.speech_revision = 0
        self.speech_changed = threading.Condition(self.lock)
        self.closed_until = 0.0
        self.camera_enabled = True
        self.camera_override_until = 0.0
        self.camera_look_until = 0.0
        self.look_frame = None
        self.camera_changed = threading.Condition(self.lock)
        self.manual_mute = False
        self.unmuted_day = ""
        self.off_mode = False
        self.off_display = "eyes"
        self.last_controls = None
        self.notify_sadness = True
        self.companion_until = 0.0
        self.latest_frame = None
        self.gesture_frame = None
        self.gesture_result = None
        self.gesture_times = []
        self.gesture_limit_hits = 0
        self.frame_cutoff = 0.0
        self.camera_generation = uuid.uuid4().hex
        self.frame_capture_at = 0.0
        self.frame_selected_at = 0.0
        self.frame_hash = None
        self.motion_hash = None
        self.motion_since = None
        self.vision_returned_at = 0.0
        self.vision_absent_ms = 0
        self.presence_at = 0.0
        self.vision_present_since = None
        self.pending_events = []
        self.watch_periods = [
            {"days": "weekdays", "start": "07:00", "end": "17:10"},
            {"days": "weekdays", "start": "18:00", "end": "23:30"},
            {"days": "weekends", "start": "09:00", "end": "24:00"},
        ]
        self.work_silent_period = {"days":"weekdays","start":"08:00","end":"18:00"}
        self.privacy_path = Path(privacy_path) if privacy_path else None
        if self.privacy_path and self.privacy_path.exists():
            try:
                saved = json.loads(self.privacy_path.read_text())
                self.closed_until = float(saved["closed_until"])
                self.camera_enabled = saved.get("camera_enabled", True) is True
                self.camera_override_until = float(saved.get("camera_override_until", 0))
                self.work_silent_period = self.validate_periods([saved.get("work_silent_period", self.work_silent_period)])[0]
                self.manual_mute = saved.get("manual_mute", False) is True
                self.unmuted_day = str(saved.get("unmuted_day", ""))
                self.off_mode = saved.get("off_mode", False) is True
                self.off_display = "black" if saved.get("off_display") == "black" else "eyes"
                self.notify_sadness = saved.get('notify_sadness', True) is True
                self.companion_until = float(saved.get('companion_until', 0))
                if not math.isfinite(self.companion_until):
                    self.companion_until = 0.0
                if saved.get("schedule_revision") == 2:
                    self.watch_periods = self.validate_periods(saved.get("watch_periods", self.watch_periods))
                if not math.isfinite(self.closed_until) or not math.isfinite(self.camera_override_until):
                    raise ValueError("invalid closure time")
                if saved.get("schedule_revision") != 2:
                    self.save_settings()
            except (OSError, ValueError, KeyError, TypeError):
                self.closed_until = time.time() + 86400
        self.phone_call_active = False
        self.touch_locked = False
        self.presence = False
        self.face_present = False
        self.camera_foreground = None
        self.camera_last_frame_at = 0.0
        self.look = {"x": 0.0, "y": 0.0}
        self.absent_since = None
        self.seen_person = False
        self.leave_reported = False
        self.looking_reported = False
        self.hot_reported = False
        self.temperature_c = None
        self.thermal_hold = False
        self.cooling_recovering = False
        self.cooling_since = None
        self.cooling_recovery_at = 0.0
        self.messages = []
        self.messages_read_through = 0
        self.messages_path = self.privacy_path.with_name('messages.json') if self.privacy_path else None
        self.messages_error = None
        self.message_save_error = None
        if self.messages_path and self.messages_path.exists():
            try:
                history = json.loads(self.messages_path.read_text())
                self.messages = history['messages'][-30:]
                self.messages_read_through = int(history['read_through'])
                for item in self.messages:
                    if (type(item['id']) is not int or item['speaker'] not in ('AI', '我')
                            or not isinstance(item['text'], str) or len(item['text']) > 4096
                            or not math.isfinite(item['at'])):
                        raise ValueError('invalid message history')
                latest = next((m for m in reversed(self.messages) if m['speaker'] == 'AI'), None)
                if latest:
                    self.subtitle = latest['text']
                    self.subtitle_until = latest['at'] + 5
            except (OSError, ValueError, KeyError, TypeError):
                self.messages = []
                self.messages_error = 'message history unavailable; existing file preserved'
        self.server = None
        self.thread = None

    def snapshot(self):
        with self.lock:
            if not self.camera_allowed(time.time()) or (self.camera_look_until <= time.time() and self.presence_at and time.time() - self.presence_at > (45 if self.camera_probe_mode(time.time()) else 10)):
                self.clear_camera_frame()
                self.presence = False
                self.look = {"x": 0.0, "y": 0.0}
            return {
                "release": self.release,
                "agent_connected": self.agent_connected,
                "page_ready": self.page_ready if time.time() - self.page_ready_at < 10 else "",
                "page_display": self.page_display if time.time() - self.page_ready_at < 10 else None,
                "state": self.state,
                "phone_call_active": self.phone_call_active,
                "subtitle": self.subtitle,
                "subtitle_until": self.subtitle_until,
                "message_id": self.messages[-1]['id'] if self.messages else 0,
                "unread": sum(m['speaker'] == 'AI' and m['id'] > self.messages_read_through for m in self.messages),
                "latest_message": next((dict(m) for m in reversed(self.messages) if m['speaker'] == 'AI'), None),
                "speaking": self.speaking_until > time.time(),
                "speaking_until": self.speaking_until,
                "closed_until": self.closed_until,
                "touch_locked": self.touch_locked,
                "presence": self.presence,
                "present": self.presence,
                "face_present": self.face_present and self.presence,
                "look": dict(self.look),
                "camera_enabled": self.camera_enabled,
                "camera_mode": "off" if not self.camera_enabled else "now" if self.camera_override_until > time.time() else "auto",
                "camera_override_until": self.camera_override_until,
                "camera_look_until": self.camera_look_until,
                "watch_periods": self.watch_periods,
                "watch_active": self.in_watch_period(time.time()),
                "camera_allowed": self.camera_allowed(time.time()),
                "camera_active": self.camera_allowed(time.time()) and self.camera_stream_alive(time.time()),
                "camera_stream_alive": self.camera_stream_alive(time.time()),
                "camera_last_frame_at": self.camera_last_frame_at,
                "camera_probe_mode": self.camera_probe_mode(time.time()),
                "temperature_c": self.temperature_c,
                "thermal_hold": self.thermal_hold,
                "cooling_recovering": self.cooling_recovering,
                "system_notice": '降温休息中，降到37℃后确认镜头出画面再恢复' if self.thermal_hold else '',
                "muted": self.is_muted(),
                "manual_mute": self.manual_mute,
                "unmuted_day": self.unmuted_day,
                "work_silent": self.work_silent(time.time()),
                "work_silent_period": dict(self.work_silent_period),
                "off_mode": self.off_mode,
                "off_display": self.off_display,
                "proactive_enabled": not self.off_mode,
                "gesture_result": self.gesture_result,
                "gesture_candidates_hour": len([at for at in self.gesture_times if at > time.time()-3600]),
                "gesture_limit_hits": self.gesture_limit_hits,
                "notify_sadness": self.notify_sadness,
                "companion_until": self.companion_until if self.companion_until > time.time() else 0,
            }

    @staticmethod
    def validate_periods(periods):
        if not isinstance(periods, list) or not 1 <= len(periods) <= 4:
            raise ValueError("one to four watch periods required")
        for period in periods:
            if not isinstance(period, dict) or set(period) != {"days", "start", "end"} or period["days"] not in ("weekdays", "weekends", "daily"):
                raise ValueError("invalid watch period")
            for key in ("start", "end"):
                value = period[key]
                if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
                    raise ValueError("time must be HH:MM")
                hour, minute = value.split(":")
                if key == "end" and value == "24:00":
                    continue
                if not hour.isdigit() or not minute.isdigit() or not (0 <= int(hour) < 24 and 0 <= int(minute) < 60):
                    raise ValueError("invalid watch time")
            if period["start"] >= period["end"]:
                raise ValueError("end must follow start; split overnight periods")
        return [dict(period) for period in periods]

    def in_watch_period(self, now):
        local = datetime.fromtimestamp(now, timezone(timedelta(hours=8)))
        clock = local.strftime("%H:%M")
        return any(self.period_day(p, local) and p["start"] <= clock < p["end"] for p in self.watch_periods)

    @staticmethod
    def period_day(period, local):
        return period["days"] == "daily" or (local.weekday() < 5 if period["days"] == "weekdays" else local.weekday() >= 5)

    def next_watch_end(self, now):
        midnight = self.local_time(now).replace(hour=0, minute=0, second=0, microsecond=0)
        ends = []
        for offset in range(8):
            day = midnight + timedelta(days=offset)
            for period in self.watch_periods:
                if self.period_day(period, day):
                    hour, minute = map(int, period["end"].split(":"))
                    end = (day + timedelta(hours=hour, minutes=minute)).timestamp()
                    if end > now:
                        ends.append(end)
        return min(ends)

    def camera_allowed(self, now):
        return (self.camera_enabled and not self.off_mode and not self.phone_call_active
                and (not self.thermal_hold or self.cooling_recovering)
                and (self.camera_look_until > now and not self.thermal_hold
                     or self.closed_until <= now and (self.camera_override_until > now or self.in_watch_period(now))))

    def camera_probe_mode(self, now):
        return bool(self.camera_look_until <= now and self.seen_person and not self.presence and self.absent_since is not None and now - self.absent_since > 1200)

    def camera_stream_alive(self, now):
        display = self.page_display or {}
        return bool(now - self.page_ready_at < 10 and self.camera_foreground is not False
                    and display.get('visible') is True and display.get('camera_stream_alive') is True)

    def update_camera_status(self, display):
        """A fresh page report proves a new video stream, not just permission."""
        now = time.time()
        started = display.get('camera_started_at')
        ended = None
        with self.lock:
            if display.get('visible') is True and display.get('camera_stream_alive') is True:
                self.camera_last_frame_at = now
            if (self.cooling_recovering and self.camera_allowed(now)
                    and self.temperature_c is not None and self.temperature_c <= 37
                    and display.get('visible') is True and display.get('camera_stream_alive') is True
                    and display.get('detector_ready') is True
                    and type(started) in (int, float) and math.isfinite(started)
                    and self.cooling_recovery_at <= started <= now + 2):
                ended = {'temperature_c': self.temperature_c,
                         'duration_s': round(now - self.cooling_since, 1)}
                self.thermal_hold = self.cooling_recovering = False
                self.cooling_since = None
        if ended is not None:
            self.queue_event('cooling_end', ended)

    @staticmethod
    def local_time(now):
        return datetime.fromtimestamp(now, timezone(timedelta(hours=8)))

    def work_silent(self, now):
        local = self.local_time(now)
        return self.period_day(self.work_silent_period, local) and self.work_silent_period["start"] <= local.strftime("%H:%M") < self.work_silent_period["end"]

    def is_muted(self):
        now = time.time()
        return self.off_mode or self.manual_mute or (self.work_silent(now) and self.unmuted_day != self.local_time(now).strftime("%Y-%m-%d"))

    def report_controls(self, force=False):
        snapshot = self.snapshot()
        controls = {key: snapshot[key] for key in ("camera_enabled", "camera_active", "muted", "off_mode", "off_display", "proactive_enabled", "notify_sadness", "companion_until")}
        if (force or controls != self.last_controls) and self._send_event("controls", controls):
            self.last_controls = controls
        self.flush_events()

    def save_settings(self):
        if self.privacy_path:
            temporary = self.privacy_path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"closed_until": self.closed_until, "camera_enabled": self.camera_enabled, "camera_override_until": self.camera_override_until, "schedule_revision": 2, "watch_periods": self.watch_periods, "work_silent_period": self.work_silent_period, "manual_mute": self.manual_mute, "unmuted_day": self.unmuted_day, "off_mode": self.off_mode, "off_display": self.off_display, "notify_sadness": self.notify_sadness, "companion_until": self.companion_until}))
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.privacy_path)

    def configure(self, request):
        if not request or set(request) - {"camera_enabled", "camera_mode", "watch_periods", "work_silent_period", "muted", "off_mode", "off_display", "notify_sadness", "companion_hours"}:
            raise ValueError("invalid settings")
        if "camera_mode" in request and (request["camera_mode"] not in ("off", "auto", "now") or "camera_enabled" in request):
            raise ValueError("camera_mode must be off/auto/now without camera_enabled")
        if "camera_enabled" in request and not isinstance(request["camera_enabled"], bool):
            raise ValueError("camera_enabled must be boolean")
        for key in ("muted", "off_mode", "notify_sadness"):
            if key in request and not isinstance(request[key], bool):
                raise ValueError(key + " must be boolean")
        if "off_display" in request and request["off_display"] not in ("eyes", "black"):
            raise ValueError("off_display must be eyes or black")
        if 'companion_hours' in request and (type(request['companion_hours']) is not int or request['companion_hours'] not in (0, 1, 2)):
            raise ValueError('companion_hours must be 0, 1 or 2')
        periods = self.validate_periods(request["watch_periods"]) if "watch_periods" in request else None
        silent_period = self.validate_periods([request["work_silent_period"]])[0] if "work_silent_period" in request else None
        with self.lock:
            self.notify_sadness = request.get('notify_sadness', self.notify_sadness)
            if 'companion_hours' in request:
                self.companion_until = time.time() + request['companion_hours'] * 3600 if request['companion_hours'] else 0
            if silent_period is not None:self.work_silent_period = silent_period
            self.camera_enabled = request.get("camera_enabled", self.camera_enabled)
            if "camera_enabled" in request:
                self.camera_override_until = 0
            self.off_mode = request.get("off_mode", self.off_mode)
            if self.off_mode:
                self.pending_events.clear()
            self.off_display = request.get("off_display", self.off_display)
            if "muted" in request:
                self.manual_mute = request["muted"]
                self.unmuted_day = "" if request["muted"] else self.local_time(time.time()).strftime("%Y-%m-%d")
            if periods is not None:
                self.watch_periods = periods
            if "camera_mode" in request:
                self.camera_enabled = request["camera_mode"] != "off"
                self.camera_override_until = self.next_watch_end(time.time()) if request["camera_mode"] == "now" else 0
            if not self.camera_allowed(time.time()):
                self.clear_camera_frame()
                self.presence = False
                self.look = {"x": 0.0, "y": 0.0}
            self.save_settings()
        self.report_controls()
        return self.snapshot()

    def set_face_state(self, state):
        if not isinstance(state, str) or state not in FACE_STATES:
            raise ValueError("state must be 在想/在干活/在等/卡住了 or thinking/working/waiting/stuck")
        with self.lock:
            self.state = FACE_STATES[state]
        return self.snapshot()

    def set_subtitle(self, text):
        if not isinstance(text, str) or not text or len(text) > 4096 or "\x00" in text:
            raise ValueError("invalid subtitle")
        with self.lock:
            self.subtitle = text
            self.subtitle_until = time.time() + 5
            try:
                self._append_message('AI', text)
            except (OSError, ValueError):
                # History is secondary: a full disk must not swallow speech.
                self.message_save_error = '消息未能保存；字幕和声音继续'

    def _save_messages(self):
        if self.messages_error:
            raise ValueError(self.messages_error)
        if self.messages_path:
            atomic_write(self.messages_path, json.dumps({'messages': self.messages,
                         'read_through': self.messages_read_through}, ensure_ascii=False).encode(), 0o600)

    def _append_message(self, speaker, text):
        if self.messages_error:
            raise ValueError(self.messages_error)
        previous = self.messages
        self.messages = (previous + [{'id': previous[-1]['id'] + 1 if previous else 1,
                         'at': time.time(), 'speaker': speaker, 'text': text}])[-30:]
        try:
            self._save_messages()
            self.message_save_error = None
        except (OSError, ValueError):
            self.messages = previous
            raise

    def messages_snapshot(self):
        with self.lock:
            return {'messages': [dict(m) for m in self.messages], 'read_through': self.messages_read_through,
                    'unread': sum(m['speaker'] == 'AI' and m['id'] > self.messages_read_through for m in self.messages),
                    'error': self.messages_error or self.message_save_error}

    def mark_messages_read(self, through):
        if type(through) is not int or through < 0:
            raise ValueError('invalid read receipt')
        with self.lock:
            newest = self.messages[-1]['id'] if self.messages else 0
            self.messages_read_through = max(self.messages_read_through, min(through, newest))
            self._save_messages()
        return self.messages_snapshot()

    def submit_message(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 500 or '\x00' in text:
            raise ValueError('请输入1–500字')
        if self.messages_error:
            raise ValueError(self.messages_error)
        # Persist before sending, so a disk failure cannot invite duplicate sends.
        with self.lock:
            previous = self.messages
            self._append_message('我', text)
            if not self._send_event('message', {'text': text}):
                self.messages = previous
                self._save_messages()
                raise ValueError('暂未连上，请保留文字后重试')
        return self.messages_snapshot()

    def record_call_line(self, identifier, speaker, text):
        if (not isinstance(identifier, str) or not 1 <= len(identifier) <= 180
                or speaker not in ('AI', '我') or not isinstance(text, str)
                or not text.strip() or len(text) > 4096 or '\x00' in text):
            raise ValueError('invalid call line')
        with self.lock:
            previous = self.messages
            item = next((m for m in previous if m.get('call_line') == identifier), None)
            if item:
                if item['speaker'] != speaker:
                    raise ValueError('call line speaker mismatch')
                self.messages = [dict(m, text=text) if m is item else m for m in previous]
            else:
                self.messages = (previous + [{'id': previous[-1]['id'] + 1 if previous else 1,
                    'at': time.time(), 'speaker': speaker, 'text': text, 'call_line': identifier}])[-30:]
            try:
                self._save_messages()
            except (ValueError, OSError):
                self.messages = previous
                raise
            if speaker == 'AI':
                self.subtitle = text
                self.subtitle_until = time.time() + 5
        return self.messages_snapshot()

    def prepare_speech(self, path):
        # Only the active local track, never a caller-selected path or a log.
        try:
            with open(path, 'rb') as handle:
                data = handle.read(10 * 1024 * 1024 + 1)
        except OSError:
            self.finish_speech()
            return  # Lip-sync must not change the existing playback behavior.
        if not data or len(data) > 10 * 1024 * 1024:
            self.finish_speech()
            return
        with self.speech_changed:
            self.speech_bytes = data
            self.speech = {'id': uuid.uuid4().hex, 'state': 'preparing',
                           'position': 0.0, 'clock_at': 0.0}
            self.speech_revision += 1
            self.speech_changed.notify_all()

    def speech_started(self, at):
        with self.speech_changed:
            if self.speech is not None:
                self.speech.update(state='playing', position=0.0, clock_at=at)
                self.speech_revision += 1
                self.speech_changed.notify_all()

    def speech_playing(self, playing):
        with self.speech_changed:
            if self.speech is None:
                return
            state = 'playing' if playing else 'paused'
            if self.speech['state'] == state:
                return
            now = time.time()
            if self.speech['state'] == 'playing':
                self.speech['position'] += max(0.0, now - self.speech['clock_at'])
            self.speech.update(state=state, clock_at=now)
            self.speech_revision += 1
            self.speech_changed.notify_all()

    def finish_speech(self):
        with self.speech_changed:
            self.speech = None
            self.speech_bytes = None
            self.speech_revision += 1
            self.speech_changed.notify_all()

    def speech_snapshot(self):
        with self.lock:
            return dict(self.speech) if self.speech is not None else None

    def speech_audio(self, identifier):
        with self.lock:
            return self.speech_bytes if self.speech and self.speech['id'] == identifier else None

    def set_speaking(self, speaking):
        with self.lock:
            self.speaking_until = time.time() + 2 if speaking else 0

    def close_eyes(self, hours):
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours not in (0, 1, 4, 24):
            raise ValueError("hours must be 0, 1, 4, or 24")
        with self.lock:
            until = time.time() + hours * 3600 if hours else 0
            self.closed_until = until
            self.clear_camera_frame()
            self.save_settings()
            self.presence = False
            self.look = {"x": 0.0, "y": 0.0}
        return self.snapshot()

    def _send_event(self, name, args):
        if self.event_sender is None:
            return False
        try:
            return self.event_sender(name, args) is not False
        except (OSError, ConnectionError):
            return False

    def queue_event(self, name, args):
        with self.lock:
            # Keep a bounded short outage backlog; full-off cancels it.
            self.pending_events.append((name, args))
            self.pending_events = self.pending_events[-16:]
        self.flush_events()

    def flush_events(self):
        with self.lock:
            while self.pending_events and not self.off_mode:
                if not self._send_event(*self.pending_events[0]):
                    break
                self.pending_events.pop(0)

    def touch(self, kind):
        if kind not in TOUCH_KINDS:
            raise ValueError("kind must be tap/chin/hold/two_finger_lock/four_finger_unlock")
        with self.lock:
            if self.off_mode:
                return {"ok": True, "ignored": "off_mode"}
            if kind == "two_finger_lock":
                self.touch_locked = not self.touch_locked
            elif kind == "four_finger_unlock":
                self.touch_locked = False
        vibrated = None
        if kind == "tap":
            try:
                result = subprocess.run(
                    ["termux-vibrate", "-d", "60"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                vibrated = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                vibrated = False
        event_sent = self._send_event("touch", {"kind": kind})
        response = {"ok": True, "touch_locked": self.snapshot()["touch_locked"], "event_sent": event_sent}
        if vibrated is not None:
            response["vibrated"] = vibrated
        return response

    def update_presence(self, request):
        if set(request) - {"present", "look", "gesture", "looking", "face_present"}:
            raise ValueError("invalid presence fields")
        present = request.get("present")
        look = request.get("look")
        if not isinstance(present, bool) or not isinstance(look, dict) or set(look) != {"x", "y"}:
            raise ValueError("present and look:{x,y} are required")
        coordinates = []
        for axis in ("x", "y"):
            value = look[axis]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("look coordinates must be finite numbers")
            coordinates.append(float(value))
        gesture = request.get("gesture")
        looking = request.get("looking", False)
        face_present = request.get('face_present', present)
        if not isinstance(face_present, bool):
            raise ValueError('face_present must be boolean')
        if not isinstance(looking, bool):
            raise ValueError("looking must be boolean")
        if gesture is not None and (not isinstance(gesture, str) or not gesture or len(gesture) > 64):
            raise ValueError("invalid gesture")
        transitions = []
        now = time.time()
        with self.lock:
            if not self.camera_allowed(now):
                self.clear_camera_frame()
                return {"ok": True, "ignored": "camera_closed"}
            self.presence_at = now
            if self.presence and not present:
                self.camera_generation = uuid.uuid4().hex
            self.presence = present
            self.face_present = face_present
            self.look = {"x": coordinates[0], "y": coordinates[1]}
            if present:
                if self.vision_present_since is None:
                    self.vision_present_since = now
                if self.seen_person and self.absent_since is not None and now - self.absent_since >= 1200:
                    self.vision_absent_ms = round((now - self.absent_since) * 1000)
                    self.vision_returned_at = now
                if self.seen_person and self.absent_since is not None and now - self.absent_since >= 300:
                    transitions.append("arrive")
                self.seen_person = True
                self.absent_since = None
                self.leave_reported = False
                if looking and not self.looking_reported:
                    transitions.append("looking")
                    self.looking_reported = True
            else:
                self.latest_frame = self.gesture_frame = self.gesture_result = None
                self.motion_hash = self.motion_since = None
                self.vision_present_since = None
                if self.absent_since is None:
                    self.absent_since = now
                if self.seen_person and now - self.absent_since >= 120 and not self.leave_reported:
                    transitions.append("leave")
                    self.leave_reported = True
                    self.looking_reported = False
        for kind in transitions:
            self.queue_event("presence", {"kind": kind})
        event_sent = None
        if gesture is not None:
            event_sent = self._send_event("gesture", {"name": gesture})
        response = {"ok": True}
        if event_sent is not None:
            response["event_sent"] = event_sent
        return response

    def clear_camera_frame(self):
        """Called under self.lock; no pixels or presence history survive a privacy stop."""
        self.look_frame = None
        self.latest_frame = self.gesture_frame = self.gesture_result = None
        self.frame_cutoff = time.time()
        self.camera_generation = uuid.uuid4().hex
        self.frame_hash = self.motion_hash = self.motion_since = None
        self.frame_capture_at = self.frame_selected_at = 0.0
        self.vision_absent_ms = 0
        self.vision_returned_at = 0.0
        self.absent_since = None
        self.seen_person = False
        self.vision_present_since = None

    def store_camera_frame(self, frame):
        required = {'jpeg_base64', 'dhash', 'captured_at', 'reason'}
        candidate = frame.get('reason') == 'gesture_candidate'
        if set(frame) != required | ({'candidate_id'} if candidate else set()):
            raise ValueError('invalid frame fields')
        encoded, digest, captured, reason = (frame[k] for k in ('jpeg_base64', 'dhash', 'captured_at', 'reason'))
        now = time.time()
        if (not isinstance(encoded, str) or len(encoded) > (MAX_JPEG + 2) // 3 * 4
                or not isinstance(digest, str) or len(digest) != 16 or any(c not in '0123456789abcdef' for c in digest)
                or type(captured) not in (int, float) or not math.isfinite(captured) or not now - 90 <= captured <= now + 2
                or reason not in ('auto', 'manual', 'gesture_candidate', 'look')
                or candidate and (captured < now-10 or not isinstance(frame['candidate_id'], str) or not 1 <= len(frame['candidate_id']) <= 100)):
            raise ValueError('invalid frame')
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ValueError('invalid frame encoding') from error
        if len(raw) > MAX_JPEG or not (raw.startswith(b'\xff\xd8') and raw.endswith(b'\xff\xd9')):
            raise ValueError('invalid JPEG')
        del raw
        with self.lock:
            if not self.camera_allowed(now):
                self.clear_camera_frame()
                return {'ok': True, 'ignored': 'camera_closed'}
            if reason == 'look':
                if (self.camera_look_until <= now or self.thermal_hold
                        or not max(now - 8, self.camera_look_until - 60) <= captured <= now):
                    return {'ok': True, 'ignored': 'stale_look'}
                self.look_frame = dict(frame)
                self.camera_changed.notify_all()
                return {'ok': True, 'selected': 'look'}
            if not self.presence or now - self.presence_at > 10 or captured < self.frame_cutoff:
                self.latest_frame = None
                return {'ok': True, 'ignored': 'no_current_presence'}
            if candidate:
                if self.thermal_hold or self.temperature_c is not None and self.temperature_c >= 42:
                    return {'ok': True, 'ignored': 'thermal_hold'}
                self.gesture_times = [at for at in self.gesture_times if at > now-3600]
                blocked = ('gesture_hour_limit' if len(self.gesture_times) >= 60 else
                           'gesture_minute_limit' if sum(at > now-60 for at in self.gesture_times) >= 6 else
                           'gesture_cooldown' if self.gesture_times and captured-self.gesture_times[-1] < 4 else None)
                if blocked:
                    self.gesture_limit_hits += 1
                    return {'ok': True, 'ignored': blocked}
                self.gesture_times.append(captured)
                self.gesture_frame = dict(frame, camera_generation=self.camera_generation)
                return {'ok': True, 'selected': 'gesture_candidate'}
            if reason == 'auto' and captured - self.frame_capture_at < 60:
                return {'ok': True, 'ignored': 'capture_interval'}
            self.frame_capture_at = captured
            value = int(digest, 16)
            if self.motion_hash is None or (value ^ self.motion_hash).bit_count() >= 8:
                self.motion_hash, self.motion_since = value, now
            changed = self.frame_hash is None or (value ^ self.frame_hash).bit_count() >= 8
            returned = self.vision_returned_at > self.frame_selected_at
            companion = self.companion_until > now
            if reason == 'manual':
                selected = 'manual'
            elif returned:
                selected = 'returned'
            elif companion:
                selected = 'companion' if changed else 'periodic' if captured - self.frame_selected_at >= 60 else None
            else:
                selected = 'changed' if changed else 'periodic' if captured - self.frame_selected_at >= 60 else None
            if selected:
                self.frame_hash, self.frame_selected_at = value, captured
                self.latest_frame = dict(frame, reason=selected)
            else:
                # Preserve a pending explicit request until the consumer has had
                # its normal polling window, rather than replacing it with a skip.
                if not self.latest_frame or self.latest_frame['reason'] != 'manual' or now - self.latest_frame['captured_at'] > 90:
                    self.latest_frame = None
            return {'ok': True, 'selected': selected}

    def look_camera(self):
        # One relay action owns this in-memory lease. Never save it as a user setting.
        with self.camera_changed:
            started = time.time()
            deadline = time.monotonic() + LOOK_WAIT_SECONDS
            try:
                while True:
                    reason = ('camera_off_by_user' if not self.camera_enabled else
                              'off_mode' if self.off_mode else 'thermal' if self.thermal_hold else
                              'native_call' if self.phone_call_active else None)
                    if reason:
                        raise ValueError(reason)
                    if not self.camera_look_until:
                        self.look_frame = None
                        self.camera_look_until = started + 60
                    now = time.time()
                    if (self.look_frame and self.look_frame['jpeg_base64']
                            and max(started, now - 8) <= self.look_frame['captured_at'] <= now):
                        return dict(self.look_frame)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or now >= self.camera_look_until:
                        raise ValueError('camera_frame_timeout')
                    self.camera_changed.wait(min(.1, remaining))
            finally:
                self.camera_look_until = 0.0
                self.look_frame = None
                if not self.camera_allowed(time.time()):
                    self.clear_camera_frame()

    def camera_frame(self, mode='ordinary'):
        if mode == 'look':
            return self.look_camera()
        if mode not in ('ordinary', 'gesture', 'metadata'):
            raise ValueError('invalid frame mode')
        now = time.time()
        with self.lock:
            if not self.camera_allowed(now):
                self.clear_camera_frame()
                return {}
            if now - self.presence_at > (45 if self.camera_probe_mode(now) else 10):
                self.clear_camera_frame()
                return {}
            current = self.presence and now - self.presence_at <= 10
            if not current or self.latest_frame and now - self.latest_frame['captured_at'] > 90:
                self.latest_frame = None
            if (not current or self.thermal_hold or self.temperature_c is not None and self.temperature_c >= 42
                    or self.gesture_frame and now-self.gesture_frame['captured_at'] > 10):
                self.gesture_frame = None
            selected = self.gesture_frame if mode == 'gesture' else self.latest_frame if mode == 'ordinary' else None
            absent = round((now - self.absent_since) * 1000) if not current and self.absent_since is not None else self.vision_absent_ms
            return {**(selected or {}), 'presence': current, 'absent_ms': absent,
                    'frame_cutoff': self.frame_cutoff, 'camera_generation': self.camera_generation,
                    'returned_at': self.vision_returned_at, 'seen_person': self.seen_person,
                    'present_ms': round((now - self.vision_present_since) * 1000) if current and self.vision_present_since is not None else 0,
                    'stationary_ms': round((now - self.motion_since) * 1000) if current and self.motion_since is not None else 0,
                    'notify_sadness': self.notify_sadness,
                    'companion_until': self.companion_until if self.companion_until > now else 0}

    def receive_gesture_result(self, result):
        metadata = {'candidate_id','camera_generation','captured_at'}
        failed = isinstance(result, dict) and set(result) == metadata | {'error'} and result['error'] == 'classification_failed'
        if (not failed and (not isinstance(result, dict) or set(result) != metadata | {'gesture','doing','confidence'}
                or result['gesture'] not in ('heart_two_hand','heart_finger','wave','thumb_up','victory','none')
                or result['doing'] not in ('typing','holding_phone','eating','other')
                or type(result['confidence']) not in (int,float) or not math.isfinite(result['confidence'])
                or not 0 <= result['confidence'] <= 1)):
            raise ValueError('invalid gesture result')
        with self.lock:
            frame = self.gesture_frame
            now = time.time()
            if not frame or any(result[k] != frame[k] for k in metadata):
                return {'ok': True, 'ignored': 'stale_gesture'}
            self.gesture_frame = None  # No JPEG survives classification, failure or none.
            if (not self.camera_allowed(now) or not self.presence or now-self.presence_at > 10
                    or self.thermal_hold or self.temperature_c is not None and self.temperature_c >= 42
                    or now-frame['captured_at'] > 10 or frame['camera_generation'] != self.camera_generation):
                return {'ok': True, 'ignored': 'stale_gesture'}
            if failed:
                self.gesture_result = {**result, 'gesture':None, 'doing':'other', 'confidence':0, 'event':'none'}
                return {'ok':True, 'event_sent':False}
            name = result['gesture']
            if result['doing'] != 'other' or result['confidence'] < .7:
                name = 'none'
            event = 'heart' if name.startswith('heart_') else name
            self.gesture_result = {**result, 'event': event, 'received_at': now}
            sent = self._send_event('gesture', {'name': event}) if event != 'none' else False
            return {'ok': True, 'event_sent': sent}

    def temperature(self):
        try:
            result = subprocess.run(
                ["termux-battery-status"], capture_output=True, timeout=5, check=False
            )
            data = json.loads(result.stdout.decode("utf-8"))
            value = float(data["temperature"])
            if result.returncode != 0 or not math.isfinite(value) or not -20 <= value <= 90:
                raise ValueError("battery status failed")
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise ValueError("temperature unavailable")
        entered = False
        with self.lock:
            self.temperature_c = value
            if value >= 42:
                entered = not self.thermal_hold
                if entered:
                    self.cooling_since = time.time()
                self.thermal_hold = True
                self.cooling_recovering = False
                self.clear_camera_frame()
                self.presence = False
            elif value <= 37 and self.thermal_hold and not self.cooling_recovering:
                self.cooling_recovering = True
                self.cooling_recovery_at = time.time()
        if entered:
            self.queue_event('cooling_start', {'temperature_c': value})
        if value > 40 and not self.hot_reported:
            self.queue_event("temperature", {"temperature_c": value})
        self.hot_reported = value > 40
        return {"temperature_c": value, "thermal_hold": self.thermal_hold, "cooling_recovering": self.cooling_recovering}

    def start(self, host="127.0.0.1", port=8192):
        service = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "phone-face"

            def log_message(self, format_string, *args):
                return

            def allowed_origin(self):
                origin = self.headers.get('Origin')
                return (origin in ('http://127.0.0.1:8190', 'http://127.0.0.1:8191', 'http://127.0.0.1:8192')
                        or (origin is None and self.headers.get('Sec-Fetch-Site') not in ('cross-site', 'same-site')))

            def origin_headers(self):
                if self.allowed_origin() and self.headers.get('Origin'):
                    self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
                self.send_header('Vary', 'Origin')

            def check_origin(self):
                if self.allowed_origin():
                    return True
                self.close_connection = True
                self.json_response(403, {'ok': False, 'error': 'origin not allowed'})
                return False

            def json_response(self, status, value):
                body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.origin_headers()
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                if not self.check_origin(): return
                if self.path not in {"/api/touch", "/api/presence", "/api/eyes", "/api/settings", "/api/ready", "/api/messages", "/api/messages/read", "/api/call-line", "/api/camera-frame"}:
                    self.json_response(404, {"ok": False, "error": "not found"})
                    return
                self.send_response(204)
                self.origin_headers()
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if not self.check_origin(): return
                if self.path == '/api/speech-events':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.origin_headers()
                    self.end_headers()
                    self.close_connection = True
                    revision = -1
                    try:
                        while True:
                            with service.speech_changed:
                                service.speech_changed.wait_for(lambda: service.speech_revision != revision, timeout=10)
                                changed = service.speech_revision != revision
                                revision = service.speech_revision
                                payload = dict(service.speech) if service.speech else None
                            data = ('data: ' + json.dumps(payload, separators=(',', ':')) + '\n\n') if changed else ': alive\n\n'
                            self.wfile.write(data.encode())
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    return
                if self.path.startswith('/api/speech-audio/'):
                    data = service.speech_audio(self.path.removeprefix('/api/speech-audio/'))
                    if data is None:
                        self.json_response(404, {'ok': False})
                        return
                    self.send_response(200)
                    self.send_header('Content-Type', 'audio/mpeg')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Cache-Control', 'no-store')
                    self.origin_headers()
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if self.path == "/api/messages":
                    self.json_response(200, service.messages_snapshot())
                    return
                if self.path == "/api/temperature":
                    try:
                        response = service.temperature()
                    except ValueError as error:
                        self.json_response(503, {"ok": False, "error": str(error)})
                        return
                    self.json_response(200, response)
                    return
                if self.path != "/api/face":
                    self.json_response(404, {"ok": False, "error": "not found"})
                    return
                self.json_response(200, service.snapshot())

            def do_POST(self):
                if not self.check_origin(): return
                if self.path not in {"/api/touch", "/api/presence", "/api/eyes", "/api/settings", "/api/ready", "/api/messages", "/api/messages/read", "/api/call-line", "/api/camera-frame"}:
                    self.json_response(404, {"ok": False, "error": "not found"})
                    return
                if self.headers.get("Transfer-Encoding"):
                    self.json_response(400, {"ok": False, "error": "transfer encoding is not allowed"})
                    return
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    self.json_response(415, {"ok": False, "error": "application/json required"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    length = -1
                if not 0 < length <= (MAX_FRAME_BODY if self.path == '/api/camera-frame' else MAX_BODY):
                    self.json_response(413, {"ok": False, "error": "invalid or oversized request body"})
                    return
                try:
                    request = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.json_response(400, {"ok": False, "error": "invalid JSON"})
                    return
                if not isinstance(request, dict):
                    self.json_response(400, {"ok": False, "error": "invalid request"})
                    return
                try:
                    if self.path == '/api/camera-frame':
                        response = service.store_camera_frame(request)
                    elif self.path == "/api/messages":
                        if set(request) != {"text"}:
                            raise ValueError('message requires text')
                        response = service.submit_message(request['text'])
                    elif self.path == "/api/call-line":
                        if set(request) != {'id', 'speaker', 'text'}:
                            raise ValueError('call line requires id, speaker and text')
                        response = service.record_call_line(request['id'], request['speaker'], request['text'])
                    elif self.path == "/api/messages/read":
                        if set(request) != {"through"}:
                            raise ValueError('read receipt requires through')
                        response = service.mark_messages_read(request['through'])
                    elif self.path == "/api/ready":
                        if set(request) - {"version", "display"} or request.get("version") != service.release:
                            raise ValueError("page version mismatch")
                        changed = False
                        if "display" in request:
                            display = request['display']
                            if not isinstance(display, dict) or len(json.dumps(display, allow_nan=False)) > 8192:
                                raise ValueError('invalid page display')
                            changed = any((service.page_display or {}).get(k) != display.get(k) for k in ('visible', 'rest_stage'))
                            service.page_display = display
                            service.update_camera_status(display)
                        service.page_ready = request["version"]
                        service.page_ready_at = time.time()
                        if changed:service.display_changed.set()
                        response = {"ok": True}
                    elif self.path == "/api/touch":
                        if set(request) != {"kind"}:
                            raise ValueError("touch requires kind")
                        response = service.touch(request["kind"])
                    elif self.path == "/api/eyes":
                        if set(request) != {"hours"}:
                            raise ValueError("eyes requires hours")
                        response = service.close_eyes(request["hours"])
                    elif self.path == "/api/settings":
                        response = service.configure(request)
                    else:
                        response = service.update_presence(request)
                except (ValueError, OSError) as error:
                    self.json_response(400, {"ok": False, "error": str(error)})
                    return
                self.json_response(200, response)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="face-api", daemon=True)
        self.thread.start()
        return self.server

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
