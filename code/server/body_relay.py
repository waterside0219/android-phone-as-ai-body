#!/usr/bin/env python3
import argparse
import base64
import hashlib
import hmac
import io
import json
from pathlib import Path
import socket
import struct
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import body_hearing

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'phone-agent'))
from remote_update import (validate_manifest, validate_version, valid_path, safe_path,
                           NoRedirect, MAX_FILE, MAX_MANIFEST, MAX_TOTAL)
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"
DEFAULT_TOKEN_FILE = ROOT / ".private" / "body-device-token"
MAX_CONFIG = 64 * 1024
MAX_HTTP_BODY = 64 * 1024
MAX_MESSAGE = 16 * 1024 * 1024
MAX_PNG = 10 * 1024 * 1024
MAX_AUDIO = 10 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ACTIONS = {
    "screenshot", "read_screen", "tap", "tap_text", "input_text", "open_app", "back", "home",
    "face_state", "say", "say_preset", "eyes_close", "update", "update_status", "call_incoming", "camera_frame",
}
MAX_UPLOAD = MAX_TOTAL + MAX_MANIFEST + 1024 * 1024
SETTINGS = {}
UPLOAD_LOCK = threading.Lock()
VOICE_LOCK = threading.Lock()
HUB = None
DEVICE_TOKEN = b""


def token_matches(header):
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return False
    supplied = header[7:].encode("utf-8", "surrogatepass")
    return hmac.compare_digest(supplied, DEVICE_TOKEN)


class WebSocket:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = bytearray()
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
            head.append(length)
        elif length <= 65535:
            head.append(126)
            head.extend(struct.pack("!H", length))
        else:
            head.append(127)
            head.extend(struct.pack("!Q", length))
        with self.send_lock:
            self.sock.sendall(head + payload)

    def receive(self):
        fragments = bytearray()
        message_opcode = None
        while True:
            first, second = self._read(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            if first & 0x70 or not second & 0x80:
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
            mask = self._read(4)
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(self._read(length)))
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

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class PhoneHub:
    def __init__(self):
        self.condition = threading.Condition()
        self.phone = None
        self.pending = {}
        self.events = deque(maxlen=100)
        self.next_event_id = 1
        self.controls = {}
        self.last_say_at = 0.0

    def register(self, phone):
        with self.condition:
            if self.phone is not None:
                return False
            self.phone = phone
            self.controls = {}
            return True

    def unregister(self, phone):
        with self.condition:
            if self.phone is phone:
                self.phone = None
                self.controls = {}
            for event, _ in self.pending.values():
                event.set()
            self.condition.notify_all()

    def connected(self):
        with self.condition:
            return self.phone is not None

    def respond(self, response):
        request_id = response.get("id")
        if not isinstance(request_id, str):
            return
        with self.condition:
            pending = self.pending.get(request_id)
            if pending is not None:
                pending[1] = response
                pending[0].set()

    def add_event(self, name, args):
        with self.condition:
            if name == 'controls':
                self.controls = dict(args)
            event = {"id": self.next_event_id, "event": name, "args": args}
            self.next_event_id += 1
            self.events.append(event)
            return event

    def events_after(self, after):
        with self.condition:
            return [dict(event) for event in self.events if event["id"] > after], self.next_event_id - 1

    def request(self, action, args, timeout=120):
        request_id = uuid.uuid4().hex
        event = threading.Event()
        with self.condition:
            phone = self.phone
            if phone is None:
                return None, "phone not connected"
            self.pending[request_id] = [event, None]
        try:
            payload = json.dumps({"id": request_id, "action": action, "args": args}, separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_MESSAGE:
                return None, "phone request too large"
            phone.send(1, payload)
            if not event.wait(timeout):
                return None, "phone response timed out"
            with self.condition:
                response = self.pending[request_id][1]
            return response, None if response is not None else "phone disconnected"
        finally:
            with self.condition:
                self.pending.pop(request_id, None)


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


class VoiceError(Exception):
    pass


def render_say(args):
    if set(args) != {"text"}:
        raise VoiceError("say requires text")
    text = args["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 4096 or "\x00" in text:
        raise VoiceError("invalid say text")
    # The phone reports effective mute (manual, schedule and full-off) on connect
    # and whenever it changes. Gate before reading a key or spending on synthesis.
    if HUB is not None:
        with HUB.condition:
            if HUB.controls.get('muted') is True:
                return {"text": text}
    settings = SETTINGS.get('tts')
    if not settings:
        raise VoiceError('TTS is not configured')
    if not VOICE_LOCK.acquire(blocking=False):
        raise VoiceError('voice rendering busy')
    try:
        key = read_token(Path(settings['api_key_file'])).decode('utf-8')
        request = urllib.request.Request(settings['endpoint'], data=json.dumps({
            'model': settings['model'], 'text': text, 'stream': False, 'output_format': 'hex',
            'voice_setting': {'voice_id': settings['voice_id'], 'speed': 1.0, 'vol': 1.0, 'pitch': 0},
            'audio_setting': {'sample_rate': 32000, 'bitrate': 128000, 'format': 'mp3'},
        }).encode(), headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        limit = MAX_AUDIO * 2 + MAX_HTTP_BODY
        with opener.open(request, timeout=60) as response:
            raw = response.read(limit + 1)
        if len(raw) > limit:
            raise VoiceError('oversized voice response')
        body = json.loads(raw)
        if body['base_resp']['status_code'] != 0:
            raise VoiceError('voice provider rejected request')
        encoded = body['data']['audio']
        if not isinstance(encoded, str) or len(encoded) > MAX_AUDIO * 2:
            raise VoiceError('invalid voice response')
        audio = bytes.fromhex(encoded)
        if not audio or len(audio) > MAX_AUDIO:
            raise VoiceError("invalid or oversized voice output")
        if not (audio.startswith(b'ID3') or (len(audio) >= 2 and audio[0] == 255 and audio[1] & 224 == 224)):
            raise VoiceError('voice response is not MP3')
        return {"text": text, "audio": {"base64": base64.b64encode(audio).decode("ascii"), "mime_type": "audio/mpeg"}}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise VoiceError('voice rendering failed') from error
    finally:
        VOICE_LOCK.release()


def updates_dir():
    return Path(SETTINGS.get('updates_dir', ROOT / '.private/updates'))


class ReleaseReader(io.BufferedReader):
    # tarfile consumes PAX/GNU metadata before member validation. Bound both a
    # single claimed allocation and chained extension headers for each member.
    metadata_remaining = MAX_MANIFEST
    total_metadata_remaining = 2 * 1024 * 1024

    def read(self, size=-1):
        if not 0 <= size <= MAX_MANIFEST:
            raise ValueError('archive read too large')
        if self.metadata_remaining is not None:
            self.metadata_remaining -= size
            self.total_metadata_remaining -= size
            if self.metadata_remaining < 0 or self.total_metadata_remaining < 0:
                raise ValueError('archive metadata too large')
        return super().read(size)


def receive_release(stream, length):
    """Stage a bounded uncompressed tar; only validated manifest members become visible."""
    store = updates_dir()
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(store).free < length * 2 + 16 * 1024 * 1024:
        raise ValueError('not enough release storage')
    with tempfile.TemporaryDirectory(prefix='.staging-', dir=store) as directory:
        staging = Path(directory)
        archive_path = staging / 'upload.tar'
        with archive_path.open('wb') as output:
            remaining = length
            while remaining:
                data = stream.read(min(64 * 1024, remaining))
                if not data:
                    raise ValueError('incomplete upload')
                output.write(data)
                remaining -= len(data)
        release = staging / 'release'
        release.mkdir(mode=0o700)
        with ReleaseReader(archive_path.open('rb', buffering=0)) as reader, \
                tarfile.open(fileobj=reader, mode='r:') as archive:
            first = archive.next()
            archive.members.clear()
            if archive.pax_headers:
                raise ValueError('global archive metadata is not supported')
            if (first is None or first.name != 'manifest.json' or not first.isfile()
                    or first.issparse() or not 0 < first.size <= MAX_MANIFEST):
                raise ValueError('manifest must be first')
            reader.metadata_remaining = None
            raw = archive.extractfile(first).read(MAX_MANIFEST + 1)
            manifest = json.loads(raw)
            files = validate_manifest(manifest)
            expected = {'files/' + item['path']: item for item in files}
            seen = set()
            while True:
                reader.metadata_remaining = MAX_MANIFEST
                member = archive.next()
                archive.members.clear()
                if archive.pax_headers:
                    raise ValueError('global archive metadata is not supported')
                if member is None:
                    break
                item = expected.get(member.name)
                if (item is None or member.name in seen or not member.isfile()
                        or member.issparse() or member.size != item['size']):
                    raise ValueError('unexpected archive member')
                reader.metadata_remaining = None
                seen.add(member.name)
                path = release / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with archive.extractfile(member) as source, path.open('wb') as output:
                    while data := source.read(64 * 1024):
                        digest.update(data)
                        output.write(data)
                if digest.hexdigest() != item['sha256']:
                    raise ValueError('release SHA-256 mismatch')
            if seen != set(expected):
                raise ValueError('missing archive members')
        (release / 'manifest.json').write_bytes(raw)
        destination = store / manifest['version']
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('version already published')
        release.rename(destination)
        return {'ok': True, 'version': manifest['version'], 'files': len(files)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "body-relay"

    def log_message(self, format_string, *args):
        return

    def json_response(self, status, value):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def reject(self, status, message):
        self.close_connection = True
        self.json_response(status, {"ok": False, "error": message})

    def do_GET(self):
        if self.path == "/v1/health":
            self.json_response(200, {"phone_connected": HUB.connected()})
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == '/agent' and parsed.query:
            if not token_matches(self.headers.get('Authorization')):
                self.reject(401, 'unauthorized')
                return
            try:
                query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                if set(query) != {'update', 'resource'} or any(len(v) != 1 for v in query.values()):
                    raise ValueError('invalid update query')
                version, resource = query['update'][0], query['resource'][0]
                validate_version(version)
                base = updates_dir()
                manifest_path = safe_path(base, version + '/manifest.json')
                if manifest_path.stat().st_size > MAX_MANIFEST:
                    raise ValueError('manifest too large')
                raw = manifest_path.read_bytes()
                manifest = json.loads(raw)
                files = validate_manifest(manifest)
                if manifest['version'] != version:
                    raise ValueError('version mismatch')
                if resource == 'manifest.json':
                    content_type, size = 'application/json', len(raw)
                else:
                    if not resource.startswith('files/'):
                        raise ValueError('invalid resource')
                    relative = valid_path(resource[len('files/'):])
                    item = next((f for f in files if f['path'] == relative), None)
                    if item is None:
                        raise ValueError('not in manifest')
                    path = safe_path(base, version + '/files/' + relative)
                    if path.stat().st_size != item['size'] or path.stat().st_size > MAX_FILE:
                        raise ValueError('file size mismatch')
                    content_type, size = 'application/octet-stream', item['size']
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(size))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                if resource == 'manifest.json':
                    self.wfile.write(raw)
                else:
                    with path.open('rb') as content:
                        shutil.copyfileobj(content, self.wfile, 64 * 1024)
            except (ValueError, OSError, KeyError):
                self.reject(404, 'update resource not found')
            return
        if parsed.path == "/v1/events":
            if not token_matches(self.headers.get("Authorization")):
                self.reject(401, "unauthorized")
                return
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if set(query) - {"after"} or any(len(values) != 1 for values in query.values()):
                self.reject(400, "invalid events query")
                return
            after = query.get("after", ["0"])[0]
            if not after.isdigit():
                self.reject(400, "after must be a non-negative integer")
                return
            events, last_id = HUB.events_after(int(after))
            self.json_response(200, {"events": events, "last_id": last_id})
            return
        if self.path != "/agent":
            self.reject(404, "not found")
            return
        if not token_matches(self.headers.get("Authorization")):
            self.reject(401, "unauthorized")
            return
        if self.headers.get("Upgrade", "").lower() != "websocket" or "upgrade" not in self.headers.get("Connection", "").lower():
            self.reject(400, "websocket upgrade required")
            return
        if self.headers.get("Sec-WebSocket-Version") != "13":
            self.reject(400, "unsupported websocket version")
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        try:
            decoded_key = base64.b64decode(key, validate=True)
        except (ValueError, base64.binascii.Error):
            decoded_key = b""
        if len(decoded_key) != 16:
            self.reject(400, "invalid websocket key")
            return
        phone = WebSocket(self.connection)
        if not HUB.register(phone):
            self.reject(409, "another phone is connected")
            return
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        self.connection.settimeout(1)
        stop = threading.Event()

        def ping_loop():
            while not stop.wait(20):
                try:
                    phone.send(9, b"body")
                except OSError:
                    phone.close()
                    return

        ping_thread = threading.Thread(target=ping_loop, daemon=True)
        ping_thread.start()
        try:
            while True:
                if time.monotonic() - phone.last_received > 90:
                    raise ConnectionError("websocket timed out")
                try:
                    opcode, payload = phone.receive()
                except socket.timeout:
                    continue
                if opcode != 1 or len(payload) > MAX_MESSAGE:
                    raise ConnectionError("expected text JSON")
                try:
                    response = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ConnectionError("invalid response JSON")
                if not isinstance(response, dict):
                    raise ConnectionError("invalid response")
                if "event" in response:
                    name = response.get("event")
                    args = response.get("args")
                    if name not in {"touch", "gesture", "temperature", "presence", "controls", "message", "cooling_start", "cooling_end", "camera_lost", "shizuku_lost", "shizuku_back"} or not isinstance(args, dict) or not valid_json(args):
                        raise ConnectionError("invalid event")
                    if name == "message" and (set(args) != {"text"} or not isinstance(args['text'], str)
                                              or not args['text'].strip() or len(args['text']) > 500 or '\x00' in args['text']):
                        raise ConnectionError("invalid message event")
                    HUB.add_event(name, args)
                    continue
                if not isinstance(response.get("id"), str) or not response["id"] or len(response["id"]) > 128:
                    raise ConnectionError("invalid response")
                HUB.respond(response)
        except (OSError, ConnectionError):
            pass
        finally:
            stop.set()
            HUB.unregister(phone)
            phone.close()

    def do_POST(self):
        if self.path == '/agent?transcribe=1':
            self.transcribe_audio()
            return
        if self.path == '/v1/releases':
            self.upload_release()
            return
        if self.path != "/v1/do":
            self.reject(404, "not found")
            return
        if not token_matches(self.headers.get("Authorization")):
            self.reject(401, "unauthorized")
            return
        if self.headers.get("Transfer-Encoding"):
            self.reject(400, "transfer encoding is not allowed")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.reject(415, "application/json required")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_HTTP_BODY:
            self.reject(413, "invalid or oversized request body")
            return
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.reject(400, "invalid JSON")
            return
        if not isinstance(request, dict):
            self.reject(400, "invalid request")
            return
        action = request.get("action")
        args = request.get("args")
        if action not in ACTIONS or not isinstance(args, dict) or not valid_json(args):
            self.reject(400, "invalid action or arguments")
            return
        if action == "say":
            if not HUB.connected():
                self.json_response(503, {"ok": False, "error": "phone not connected"})
                return
            try:
                args = render_say(args)
            except VoiceError as error:
                self.json_response(502, {"ok": False, "error": str(error)})
                return
        try:
            response, error = HUB.request(action, args)
        except OSError:
            response, error = None, "phone disconnected"
        if error:
            self.json_response(503 if error == "phone not connected" else 504 if error == "phone response timed out" else 502, {"ok": False, "error": error})
            return
        if not isinstance(response.get("ok"), bool):
            self.json_response(502, {"ok": False, "error": "invalid phone response"})
            return
        if response['ok'] and action in ('say', 'say_preset'):
            HUB.last_say_at = time.time()
        if response['ok'] and action == 'camera_frame' and response.get('result'):
            response['result']['last_say_at'] = HUB.last_say_at
        if action == "screenshot" and response["ok"]:
            result = response.get("result")
            encoded = result.get("base64") if isinstance(result, dict) else None
            if not isinstance(encoded, str) or len(encoded) > MAX_MESSAGE:
                self.json_response(502, {"ok": False, "error": "invalid screenshot response"})
                return
            try:
                image = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error):
                self.json_response(502, {"ok": False, "error": "invalid screenshot encoding"})
                return
            mime_type = result.get("mime_type") if isinstance(result, dict) else None
            valid_png = image.startswith(PNG_MAGIC) and image.endswith(b"IEND\xaeB`\x82")
            valid_jpeg = image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9")
            if len(image) > MAX_PNG or (mime_type == "image/png" and not valid_png) or (mime_type == "image/jpeg" and not valid_jpeg) or mime_type not in {"image/png", "image/jpeg"}:
                self.json_response(502, {"ok": False, "error": "invalid image"})
                return
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(image)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(image)
            return
        self.json_response(200, response)

    def transcribe_audio(self):
        # Existing public /agent ingress; never expose the local control routes.
        self.close_connection = True
        if not token_matches(self.headers.get('Authorization')):
            self.reject(401, 'unauthorized')
            return
        if self.headers.get('Transfer-Encoding'):
            self.reject(400, 'transfer encoding is not allowed')
            return
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/octet-stream':
            self.reject(415, 'application/octet-stream required')
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            length = -1
        if not 0 < length <= body_hearing.MAX_PCM or length % 2:
            self.reject(413, 'invalid PCM length')
            return
        try:
            self.connection.settimeout(5)
            pcm = self.rfile.read(length)
            if len(pcm) != length:
                self.reject(400, 'incomplete PCM')
                return
            self.json_response(200, body_hearing.transcribe(pcm))
        except body_hearing.HearingBusy:
            self.reject(503, 'hearing busy')
        except body_hearing.HearingFailure:
            self.reject(502, 'cloud transcription unavailable')
        except OSError:
            # Client hung up/timed out; do not print a request-bearing traceback.
            pass

    def upload_release(self):
        # Publishing uses an SSH forward, never the public /agent ingress.
        self.close_connection = True
        if not token_matches(self.headers.get('Authorization')):
            self.reject(401, 'unauthorized')
            return
        if self.headers.get('Transfer-Encoding'):
            self.reject(400, 'transfer encoding is not allowed')
            return
        if self.headers.get('Content-Type') != 'application/x-tar':
            self.reject(415, 'application/x-tar required')
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_UPLOAD:
            self.reject(413, 'invalid or oversized release')
            return
        if not UPLOAD_LOCK.acquire(blocking=False):
            self.reject(409, 'another upload is in progress')
            return
        try:
            self.connection.settimeout(30)
            result = receive_release(self.rfile, length)
            self.json_response(201, result)
        except FileExistsError:
            self.reject(409, 'version already published; use a new version')
        except (ValueError, OSError, tarfile.TarError, RecursionError):
            self.reject(400, 'release rejected; no version published')
        finally:
            UPLOAD_LOCK.release()


def read_config(path):
    path = path.expanduser().resolve()
    settings = {"host": "127.0.0.1", "port": 8832, "token_file": str(DEFAULT_TOKEN_FILE),
                'updates_dir': str(ROOT / '.private/updates'), 'tts': None}
    if path.is_file():
        if path.stat().st_size > MAX_CONFIG:
            raise ValueError("config too large")
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict) or set(loaded) - set(settings):
            raise ValueError("invalid config")
        settings.update(loaded)
    if not isinstance(settings["host"], str) or isinstance(settings["port"], bool) or not isinstance(settings["port"], int):
        raise ValueError("invalid host or port")
    if settings['host'] != '127.0.0.1':
        raise ValueError('relay must listen on 127.0.0.1')
    if not 1 <= settings["port"] <= 65535 or not isinstance(settings["token_file"], str):
        raise ValueError("invalid port or token_file")
    def resolve(value):
        if not isinstance(value, str) or not value or '\x00' in value:
            raise ValueError('invalid configured path')
        target = Path(value).expanduser()
        return str(target if target.is_absolute() else path.parent / target)
    for key in ('token_file', 'updates_dir'):
        settings[key] = resolve(settings[key])
    if settings['tts'] is not None:
        tts = settings['tts']
        if (not isinstance(tts, dict) or not {'api_key_file', 'voice_id'} <= set(tts)
                or set(tts) - {'api_key_file', 'voice_id', 'model', 'endpoint'}):
            raise ValueError('invalid TTS config')
        tts = {'model': 'speech-02-hd', 'endpoint': 'https://api.minimaxi.com/v1/t2a_v2', **tts}
        for key in ('model', 'voice_id', 'endpoint'):
            if not isinstance(tts[key], str) or not tts[key] or len(tts[key]) > 2048 or any(c in tts[key] for c in '\r\n\x00'):
                raise ValueError('invalid TTS setting')
        endpoint = urllib.parse.urlsplit(tts['endpoint'])
        if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.fragment or endpoint.query):
            raise ValueError('TTS requires an HTTPS endpoint without credentials')
        tts['api_key_file'] = resolve(tts['api_key_file'])
        settings['tts'] = tts
    return settings


def read_token(path):
    if not path.is_absolute():
        path = ROOT / path
    if path.stat().st_size > 4096:
        raise ValueError("token file too large")
    token = path.read_text(encoding="utf-8").strip()
    if not token or len(token) > 2048 or "\r" in token or "\n" in token:
        raise ValueError("invalid token")
    return token.encode("utf-8")


def main():
    global HUB, DEVICE_TOKEN, SETTINGS
    parser = argparse.ArgumentParser(description="Local phone body relay")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    arguments = parser.parse_args()
    settings = read_config(arguments.config)
    if arguments.host is not None:
        settings["host"] = arguments.host
        if settings['host'] != '127.0.0.1':
            raise ValueError('relay must listen on 127.0.0.1')
    if arguments.port is not None:
        if not 1 <= arguments.port <= 65535:
            raise ValueError("invalid port")
        settings["port"] = arguments.port
    DEVICE_TOKEN = read_token(arguments.token_file or Path(settings["token_file"]))
    SETTINGS = settings
    HUB = PhoneHub()
    server = ThreadingHTTPServer((settings["host"], settings["port"]), Handler)
    server.daemon_threads = True
    print("body relay listening on %s:%d" % (settings["host"], settings["port"]), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
