import asyncio
import base64
import binascii
import collections
import difflib
import time
import json
import logging
import math
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
from uuid import UUID, uuid4
import wave

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
GATEWAY_TIMEOUT = ClientTimeout(total=15)
RECOVERY_SECONDS = 90  # 与网关 expire_recovery 90 秒对齐
MAX_BROWSER_MESSAGE_BYTES = 1024 * 1024
MAX_GATEWAY_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_PCM_PACKET_BYTES = 32768
MAX_TEXT_BYTES = 14000
LOGGER = logging.getLogger("call-web")


class WhisperFailure(Exception):
    pass


def load_config():
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("config.json must contain an object")
    gateway = config.get("gateway", "")
    if not isinstance(gateway, str) or not gateway.startswith(("http://", "https://")):
        raise ValueError("gateway must be an http(s) URL")
    if config.get("host") != "127.0.0.1":
        raise ValueError("host must be 127.0.0.1")
    if type(config.get("port")) is not int or not 1 <= config["port"] <= 65535:
        raise ValueError("invalid port")
    for key in ("device_token_file", "whisper_binary", "whisper_model", "temp_directory"):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
            raise ValueError(f"{key} must be an absolute path")
    seconds = config.get("max_audio_seconds", 30)
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= 120:
        raise ValueError("max_audio_seconds must be between 1 and 120")
    timeout = config.get("whisper_timeout_seconds", 90)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 5 <= timeout <= 300:
        raise ValueError("whisper_timeout_seconds must be between 5 and 300")
    chars = config.get("max_transcript_chars", 2000)
    if type(chars) is not int or not 1 <= chars <= MAX_TEXT_BYTES:
        raise ValueError("invalid max_transcript_chars")
    return config


def load_device_token(path_value):
    path = Path(path_value)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("device token file must be a regular file with mode 0600")
    if info.st_size < 1 or info.st_size > 4096:
        raise ValueError("device token file has an invalid size")
    token = path.read_text(encoding="utf-8").strip()
    if not token or len(token) > 512 or any(character.isspace() for character in token):
        raise ValueError("device token file contains an invalid token")
    return token


def error(status, message):
    return web.json_response({"error": message}, status=status)


def gateway_http(config, suffix):
    return config["gateway"].rstrip("/") + suffix


def gateway_ws(config, call_id):
    base = config["gateway"].rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    else:
        base = "ws://" + base[len("http://"):]
    return f"{base}/calls/{call_id}/stream"


def valid_identifier(value):
    return isinstance(value, str) and 1 <= len(value) <= 100


def valid_text(value):
    return isinstance(value, str) and len(value.encode("utf-8")) <= MAX_TEXT_BYTES


def validate_browser_message(message):
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ValueError("invalid message")
    kind = message["type"]
    if kind in {"media_ready", "played"}:
        if kind == "played" and type(message.get("generation")) is not int:
            raise ValueError("invalid generation")
        return {"type": kind, **({"generation": message["generation"]} if kind == "played" else {})}
    if kind in {"ping", "pong"}:
        return {"type": kind}
    if kind == "audio":
        encoded = message.get("pcm")
        if not isinstance(encoded, str) or len(encoded) > 45000:
            raise ValueError("invalid audio")
        try:
            pcm = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid audio") from None
        if not pcm or len(pcm) > MAX_PCM_PACKET_BYTES or len(pcm) % 2:
            raise ValueError("invalid audio")
        result = {"type": "audio", "pcm": encoded}
        if "time" in message:
            value = message["time"]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid audio time")
            result["time"] = value
        return result
    if kind in {"draft", "submit"}:
        if not valid_identifier(message.get("id")) or not valid_text(message.get("text")):
            raise ValueError("invalid utterance")
        result = {"type": kind, "id": message["id"], "text": message["text"]}
        if kind == "submit":
            if message.get("manual") is not True:
                raise ValueError("browser submissions must be manual")
            result["manual"] = True
        return result
    if kind == "audio_ack":
        if type(message.get("audio_sequence")) is not int or message["audio_sequence"] < 0:
            raise ValueError("invalid audio cursor")
        return {"type": kind, "audio_sequence": message["audio_sequence"]}
    if kind == "played_part":
        if (
            type(message.get("part")) is not int
            or message["part"] < 1
            or type(message.get("generation")) is not int
            or not valid_identifier(message.get("id"))
            or not valid_text(message.get("text"))
        ):
            raise ValueError("invalid played part")
        return {
            "type": kind,
            "part": message["part"],
            "generation": message["generation"],
            "id": message["id"],
            "text": message["text"],
        }
    if kind == "finalize_deferred":
        if not valid_identifier(message.get("request")):
            raise ValueError("invalid finalize request")
        return {"type": kind, "request": message["request"]}
    raise ValueError("unsupported message")


def transcribe_pcm(pcm, config, cancelled=None):
    maximum_chars = config.get("max_transcript_chars", 2000)
    cancelled = cancelled or threading.Event()
    if cancelled.is_set():
        raise WhisperFailure('transcription cancelled')

    async def cloud_request():
        # Body relay authentication is separate from the voice gateway's device token.
        phone = json.loads((ROOT.parent / 'phone-agent/config.json').read_text(encoding='utf-8'))
        token = phone['token']
        if not isinstance(token, str) or not token or len(token) > 2048 or any(c.isspace() for c in token):
            raise ValueError('invalid relay token')
        # Leave scheduling headroom below six seconds; disable aiohttp rounding.
        async with ClientSession(timeout=ClientTimeout(total=5.5, ceil_threshold=10)) as session:
            async with session.post(phone['url'].replace('wss://', 'https://', 1) + '?transcribe=1',
                    data=pcm, headers={'Authorization': 'Bearer ' + token,
                                      'Content-Type': 'application/octet-stream'},
                    allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError('cloud transcription failed')
                raw = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    raw.extend(chunk)
                    if len(raw) > maximum_chars * 4 + 1024:
                        raise ValueError('oversized cloud transcript')
                body = json.loads(raw)
                text = body.get('text') if isinstance(body, dict) else None
                if not isinstance(text, str) or len(text) > maximum_chars or '\x00' in text:
                    raise ValueError('invalid cloud transcript')
                return text.strip()

    async def cloud_until_cancelled():
        task = asyncio.create_task(cloud_request())
        try:
            while not task.done():
                if cancelled.is_set():
                    raise WhisperFailure('transcription cancelled')
                await asyncio.wait({task}, timeout=.05)
            return task.result()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    started = time.monotonic()
    try:
        text = asyncio.run(cloud_until_cancelled())
        if cancelled.is_set():
            raise WhisperFailure('transcription cancelled')
        LOGGER.info('hearing cloud elapsed_ms=%d', (time.monotonic() - started) * 1000)
        return text  # Empty is a successful nonverbal result, not a fallback.
    except WhisperFailure:
        raise
    except Exception as exc:
        if cancelled.is_set():
            raise WhisperFailure('transcription cancelled') from None
        # Exception messages may contain URLs, tokens or provider content.
        LOGGER.warning('hearing fallback reason=%s elapsed_ms=%d',
                       type(exc).__name__, (time.monotonic() - started) * 1000)
    with tempfile.TemporaryDirectory(prefix="call-web-", dir=config["temp_directory"]) as directory:
        directory = Path(directory)
        audio_path = directory / "speech.wav"
        output_base = directory / "transcript"
        with wave.open(str(audio_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(pcm)
        if cancelled.is_set():
            raise WhisperFailure('transcription cancelled')
        try:
            with subprocess.Popen([
                    config['whisper_binary'], '-m', config['whisper_model'], '-f', str(audio_path),
                    '-l', 'zh', '-nt', '-otxt', '-of', str(output_base)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
                deadline = time.monotonic() + config.get('whisper_timeout_seconds', 90)
                while process.poll() is None:
                    if cancelled.is_set() or time.monotonic() >= deadline:
                        process.terminate()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=1)
                        raise WhisperFailure('transcription cancelled or timed out')
                    cancelled.wait(.1)
                if process.returncode != 0:
                    raise WhisperFailure('whisper failed')
        except (OSError, subprocess.SubprocessError) as exc:
            raise WhisperFailure('whisper failed') from exc
        transcript_path = output_base.with_suffix(".txt")
        try:
            with transcript_path.open("rb") as handle:
                raw = handle.read(maximum_chars * 4 + 1)
        except OSError as exc:
            raise WhisperFailure("missing transcript") from exc
        if len(raw) > maximum_chars * 4:
            raw = raw[:maximum_chars * 4]
        return raw.decode("utf-8", errors="replace").strip()[:maximum_chars]



def is_echo(text, recent):
    clean = lambda value: ''.join(c.lower() for c in value if c.isalnum())
    text = clean(text)
    if len(text) < 4:
        return False
    for previous in recent:
        previous = clean(previous)
        # 只把几乎一模一样、长短相近的当回声；"我也爱你"回"我也爱你呀"不许被吞。
        if previous and .75 <= len(text) / len(previous) <= 1.33 and difflib.SequenceMatcher(None, text, previous).ratio() >= .9:
            return True
    return False


class CallRoom:
    def __init__(self, app, identifier, incoming=False):
        self.app, self.id, self.incoming = app, identifier, incoming
        self.closed = False
        self.end_confirmed = False
        self.cancel_transcription = threading.Event()
        self.upstream = None
        self.reader = None
        self.jobs = set()
        self.events = collections.deque()
        self.event_bytes = 0
        self.cursor = 0
        self.changed = asyncio.Event()
        self.recent = collections.deque(maxlen=12)
        self.utterance = None
        self.finished_utterances = collections.deque(maxlen=32)
        self.last_poll = time.monotonic()
        self.accepted = False
        self.gateway_cursor = self.audio_cursor = 0
        self.recovering = False
        self.connected = asyncio.Event()

    def emit(self, data):
        size = len(json.dumps(data, ensure_ascii=False).encode())
        self.cursor += 1
        self.events.append({'seq': self.cursor, 'data': data, 'size': size})
        self.event_bytes += size
        while len(self.events) > 3000 or self.event_bytes > 16 * 1024 * 1024:
            self.event_bytes -= self.events.popleft()['size']
        self.changed.set()

    async def notify_end(self):
        try:
            async with self.app['session'].post(gateway_http(self.app['config'], f'/calls/{self.id}/end'),
                    json={'state': 'ended'}, headers=self.app['gateway_headers'], timeout=ClientTimeout(total=5)) as response:
                return response.status == 200
        except (ClientError, OSError, asyncio.TimeoutError):
            return False

    async def close(self, notify=True):
        if self.closed:
            if notify and not self.end_confirmed:
                self.end_confirmed = await self.notify_end()
            return self.end_confirmed if notify else True
        self.closed = True
        self.connected.set()  # Release senders waiting for a transport that will never return.
        self.utterance = None
        self.emit({'type': 'call', 'state': 'ended'})
        self.cancel_transcription.set()
        # Keep the semaphore until the actual process has stopped, including hangup.
        if self.jobs:
            await asyncio.wait(list(self.jobs), timeout=2)
        if self.upstream:
            await self.upstream.close()
        if self.reader and self.reader is not asyncio.current_task():
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if notify:
            self.end_confirmed = await self.notify_end()
        return self.end_confirmed if notify else True

    async def read_upstream(self):
        deadline = None
        try:
            while not self.closed:
                try:
                    while not self.closed:
                        remaining = None if deadline is None else max(.001, deadline-time.monotonic())
                        message = await asyncio.wait_for(self.upstream.receive(), remaining)
                        if message.type != WSMsgType.TEXT:
                            break
                        data = json.loads(message.data)
                        if not isinstance(data, dict):
                            raise ValueError('invalid gateway event')
                        sequence, audio_sequence = data.get('sequence'), data.get('audio_sequence')
                        if type(sequence) is int and sequence <= self.gateway_cursor:
                            continue
                        if type(audio_sequence) is int and audio_sequence <= self.audio_cursor:
                            continue
                        if data.get('type') == 'ready':
                            if self.recovering:
                                # Same socket preserves order: restore the gateway state before queued submits.
                                await self.upstream.send_json({'type': 'media_ready'})
                            deadline = None
                            self.recovering = False
                            self.connected.set()
                        line = data.get('line', data)
                        if isinstance(line, dict) and (line.get('role') == 'assistant' or data.get('type') == 'audio_part_end'):
                            if isinstance(line.get('text'), str):
                                self.recent.append((time.monotonic(), line['text']))
                        if data.get('type') == 'finalize':
                            await self.upstream.send_json(validate_browser_message({'type': 'finalize_deferred', 'request': data.get('request')}))
                            continue
                        if data.get('type') == 'ping':
                            await self.upstream.send_json({'type': 'pong'})
                            continue
                        self.emit(data)
                        if type(sequence) is int: self.gateway_cursor = sequence
                        if type(audio_sequence) is int: self.audio_cursor = audio_sequence
                        if data.get('type') == 'call' and data.get('state') in ('ended', 'failed', 'cancelled', 'missed'):
                            await self.close(notify=False)
                            return
                except (ClientError, OSError, ValueError, asyncio.TimeoutError):
                    pass
                if self.closed:
                    return
                if deadline is None:
                    deadline = time.monotonic()+RECOVERY_SECONDS
                    self.recovering = True
                    self.connected.clear()
                    self.emit({'type':'call', 'state':'recovering', 'timeout_seconds':RECOVERY_SECONDS})
                await self.upstream.close()
                backoff = 1
                while not self.closed:
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        self.emit({'type':'error', 'code':'reconnect_failed', 'message':f'连接中断，{RECOVERY_SECONDS:g}秒内未能重连，请重新拨号'})
                        return
                    await asyncio.sleep(min(backoff, remaining))
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        continue
                    try:
                        url = gateway_ws(self.app['config'], self.id)+f'?after={self.gateway_cursor}&audio_after={self.audio_cursor}'
                        self.upstream = await asyncio.wait_for(self.app['session'].ws_connect(url,
                            headers=self.app['gateway_headers'], heartbeat=30, max_msg_size=MAX_GATEWAY_MESSAGE_BYTES), min(5, remaining))
                        if self.closed:
                            await self.upstream.close()
                            return
                        break
                    except (ClientError, OSError, asyncio.TimeoutError):
                        backoff = min(backoff*2, 5)
        finally:
            if not self.closed:
                await self.close()

    async def forward(self, data, timeout=4):
        """Retry identity-bearing controls only; raw microphone chunks are never replayed."""
        async with asyncio.timeout(timeout):
            while not self.closed:
                if self.recovering or not self.upstream or self.upstream.closed:
                    await self.connected.wait()
                    if self.closed: break
                try:
                    await self.upstream.send_json(data)
                    return
                except (ClientError, OSError):
                    self.connected.clear()
                    await self.upstream.close()  # Wake the single reader/recovery owner.
                    await self.connected.wait()
        raise ValueError('通话已断开')

    async def transcribe(self, identifier, pcm):
        try:
            async with self.app['whisper_semaphore']:
                if self.closed:
                    return
                text = await asyncio.to_thread(transcribe_pcm, pcm, self.app['config'], self.cancel_transcription)
            if self.closed:
                return
            recent = [text for at, text in self.recent if time.monotonic() - at < 60]
            if is_echo(text, recent):
                self.emit({'type': 'echo_dropped'})
                return
            display_text = text or '（听到轻声，未识别出文字）'
            submitted = text or '[语音提示：对方发出了一点声音，没有识别出文字。请轻轻接一下，不要追问。]'
            await self.forward({'type': 'draft', 'id': identifier, 'text': submitted}, RECOVERY_SECONDS)
            await self.forward({'type': 'submit', 'id': identifier, 'text': submitted, 'manual': True}, RECOVERY_SECONDS)
            self.emit({'type': 'transcript', 'id': identifier, 'role': 'you', 'text': display_text})
        except (WhisperFailure, ClientError, OSError, asyncio.TimeoutError, ValueError):
            if self.closed:
                return
            self.emit({'type': 'error', 'message': '这句话未能识别或发送，请重说或打字'})

    async def send(self, data):
        if self.closed:
            raise ValueError('通话已断开')
        if not isinstance(data, dict):
            raise ValueError('invalid event')
        kind = data.get('type')
        if kind == 'utterance_start':
            if valid_identifier(data.get('id')) and self.utterance is not None and self.utterance['id'] != data['id']:
                self.utterance = None  # 网页开了新的一句，旧的那句已经死了，丢掉旧的接新的，不拒绝
            if not valid_identifier(data.get('id')) or self.utterance is not None or len(self.jobs) >= 8:  # 人想到了还想说：排队转写（semaphore 保证按顺序），不拦
                raise ValueError('上一句仍在处理中')
            self.utterance = {'id': data['id'], 'pcm': bytearray()}
        elif kind == 'utterance_audio':
            if self.utterance is None or data.get('id') != self.utterance['id']:
                raise ValueError('invalid utterance')
            packet = validate_browser_message({'type': 'audio', 'pcm': data.get('pcm')})
            pcm = base64.b64decode(packet['pcm'])
            if len(self.utterance['pcm']) + len(pcm) > 30 * 16000 * 2:
                self.utterance = None
                raise ValueError('语音超过30秒')
            self.utterance['pcm'].extend(pcm)
            if self.upstream and not self.upstream.closed and not self.recovering:
                try:
                    await self.upstream.send_json(packet)
                except (ClientError, OSError):
                    await self.upstream.close()
        elif kind == 'utterance_end':
            if not valid_identifier(data.get('id')) or type(data.get('discard')) is not bool:
                raise ValueError('invalid utterance')
            if data['id'] in self.finished_utterances:
                return  # Lost HTTP responses must not duplicate transcription or consume newer capture.
            if data['discard'] and self.utterance is None:
                self.finished_utterances.append(data['id'])
                return
            if self.utterance is None or data['id'] != self.utterance['id']:
                raise ValueError('invalid utterance')
            utterance, self.utterance = self.utterance, None
            self.finished_utterances.append(data['id'])
            if not data['discard'] and len(utterance['pcm']) >= 8000:
                task = asyncio.create_task(self.transcribe(utterance['id'], bytes(utterance['pcm'])))
                self.jobs.add(task)
                task.add_done_callback(self.jobs.discard)
        else:
            await self.forward(validate_browser_message(data))


@web.middleware
async def local_origin(request, handler):
    origin = request.headers.get('Origin')
    allowed = origin in ('http://127.0.0.1:8190', 'http://127.0.0.1:8191')
    native = origin is None and request.headers.get('Sec-Fetch-Site') in (None, 'none', 'same-origin')
    if request.path.startswith('/api/') and not (allowed or native):
        return error(403, 'origin not allowed')
    if request.method == 'OPTIONS':
        response = web.Response(status=204, headers={'Access-Control-Allow-Methods': 'GET, POST, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type'})
    else:
        response = await handler(request)
    if allowed:
        response.headers['Access-Control-Allow-Origin'] = origin
    response.headers['Vary'] = 'Origin'
    response.headers['Cache-Control'] = 'no-store'
    return response


def requested_room(request):
    try:
        identifier = str(UUID(request.match_info['call_id']))
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text='invalid call id')
    return identifier, request.app['rooms'].get(identifier)


async def create_call(request):
    try:
        data = await request.json()
        identifier = str(UUID(data['id']))
    except (ValueError, TypeError, KeyError, AttributeError):
        return error(400, '无效通话编号')
    app = request.app
    room = app['rooms'].get(identifier)
    if room and room.closed:
        return error(410, '拨号已取消')
    if room and room.accepted:
        return web.json_response({'id': identifier})
    room = room or CallRoom(app, identifier)
    app['rooms'][identifier] = room
    room.accepted = True
    for previous in list(app['rooms'].values()):
        if previous.id != identifier and not previous.closed:
            await previous.close()
    if room.closed:
        return error(410, '拨号已取消')
    if app['incoming'] and app['incoming']['id'] == identifier:
        app['incoming'] = None
    try:
        async with asyncio.timeout(14):
            if not room.incoming:
                async with app['session'].post(gateway_http(app['config'], '/calls'),
                        json={'id': identifier, 'profile': app['config'].get('profile', 'default')}, headers=app['gateway_headers'], timeout=GATEWAY_TIMEOUT) as response:
                    if response.status != 200 or (await response.json()).get('id') != identifier:
                        raise ValueError('网关没有接受拨号')
            if room.closed:
                await room.notify_end()
                return error(410, '拨号已取消')
            room.upstream = await app['session'].ws_connect(gateway_ws(app['config'], identifier),
                headers=app['gateway_headers'], heartbeat=30, max_msg_size=MAX_GATEWAY_MESSAGE_BYTES)
            if room.closed:
                await room.upstream.close()
                await room.notify_end()
                return error(410, '拨号已取消')
        room.reader = asyncio.create_task(room.read_upstream())
        return web.json_response({'id': identifier})
    except (ClientError, OSError, ValueError, asyncio.TimeoutError):
        await room.close()
        return error(502, '通话未接通，请稍后再试')


async def end_call(request):
    identifier, room = requested_room(request)
    if room is None:
        room = CallRoom(request.app, identifier)
        room.closed = True
        room.end_confirmed = True  # No gateway create has started.
        # Cancel may reach the service before create does.
        request.app['rooms'][identifier] = room
        confirmed = True
    else:
        confirmed = await room.close()
    if request.app['incoming'] and request.app['incoming']['id'] == identifier:
        request.app['incoming'] = None
    return web.json_response({'ok': True, 'remote_confirmed': confirmed})


async def call_events(request):
    _, room = requested_room(request)
    if room is None:
        return error(404, '通话不存在')
    if request.method == 'POST':
        try:
            await room.send(await request.json())
        except (ClientError, OSError, asyncio.TimeoutError):
            return error(503, '连接中断，正在重连')
        except (ValueError, TypeError) as exc:
            return error(400, str(exc))
        return web.json_response({'ok': True})
    try:
        after = int(request.query.get('after', '0'))
        if not 0 <= after <= room.cursor:
            raise ValueError()
    except ValueError:
        return error(400, 'invalid event cursor')
    room.last_poll = time.monotonic()
    if after == room.cursor and not room.closed:
        room.changed.clear()
        try:
            await asyncio.wait_for(room.changed.wait(), 20)
        except asyncio.TimeoutError:
            pass
    # 网页落后太多（旧事件被挤掉）就从还留着的最早一条接着给，不再 409 挂电话。
    return web.json_response({'events': [{'seq': item['seq'], 'data': item['data']} for item in room.events if item['seq'] > after],
                             'cursor': room.cursor, 'closed': room.closed})


async def incoming(request):
    app = request.app
    if request.method == 'POST':
        if request.headers.get('Origin') or request.headers.get('Sec-Fetch-Site'):
            return error(403, 'incoming calls require the device agent')
        try:
            identifier = str(UUID((await request.json())['id']))
        except (ValueError, TypeError, KeyError, AttributeError):
            return error(400, 'invalid incoming call')
        existing = app['rooms'].get(identifier)
        if existing and existing.closed:
            return error(410, 'incoming call already ended')
        if app['incoming'] and app['incoming']['id'] == identifier:
            return web.json_response({'incoming': app['incoming']})
        if any(not room.closed for room in app['rooms'].values()):
            return error(409, 'phone is busy')
        app['rooms'][identifier] = CallRoom(app, identifier, incoming=True)
        app['incoming'] = {'id': identifier, 'expires': time.time() + 30}
    return web.json_response({'incoming': app['incoming']})


async def maintenance(app):
    while True:
        await asyncio.sleep(2)
        invitation = app['incoming']
        if invitation and invitation['expires'] <= time.time():
            app['incoming'] = None
            await app['rooms'][invitation['id']].close()
            try:
                async with app['session'].post('http://127.0.0.1:8192/api/call-line',
                        json={'id': invitation['id'], 'speaker': 'AI', 'text': '未接来电'}, timeout=ClientTimeout(total=2)) as response:
                    await response.read()
            except (ClientError, OSError, asyncio.TimeoutError):
                LOGGER.warning('missed call could not be saved')
        for room in list(app['rooms'].values()):
            if not room.closed and room.accepted and time.monotonic() - room.last_poll > 120:  # 锁屏冻结网页时多等一会儿
                await room.close()
        # Retain cancellation tombstones long enough for an in-flight create.
        for identifier, room in list(app['rooms'].items()):
            if room.closed and time.monotonic() - room.last_poll > 120:
                app['rooms'].pop(identifier, None)


async def index(request):
    raise web.HTTPFound('http://127.0.0.1:8190/')


async def javascript(request):
    return web.FileResponse(ROOT / 'app.js')


async def start_session(app):
    app['session'] = ClientSession()
    app['maintenance'] = asyncio.create_task(maintenance(app))


async def close_session(app):
    app['maintenance'].cancel()
    await asyncio.gather(app['maintenance'], return_exceptions=True)
    for room in app['rooms'].values():
        await room.close()
    await app['session'].close()


def create_app(config, token):
    app = web.Application(client_max_size=MAX_BROWSER_MESSAGE_BYTES, middlewares=[local_origin])
    app['config'] = config
    app['gateway_headers'] = {'Authorization': f'Bearer {token}', 'User-Agent': 'phone-body-call/1.0', 'Accept': 'application/json'}
    app['whisper_semaphore'] = asyncio.Semaphore(1)
    app['rooms'] = {}
    app['incoming'] = None
    app.router.add_get('/', index)
    app.router.add_get('/app.js', javascript)
    app.router.add_post('/api/calls', create_call)
    app.router.add_post('/api/calls/{call_id}/end', end_call)
    app.router.add_get('/api/calls/{call_id}/events', call_events)
    app.router.add_post('/api/calls/{call_id}/events', call_events)
    app.router.add_get('/api/incoming', incoming)
    app.router.add_post('/api/incoming', incoming)
    app.router.add_route('OPTIONS', '/{path:.*}', incoming)
    app.on_startup.append(start_session)
    app.on_cleanup.append(close_session)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config()
    token = load_device_token(config['device_token_file'])
    Path(config['temp_directory']).mkdir(mode=0o700, parents=True, exist_ok=True)
    app = create_app(config, token)
    LOGGER.info('call-web listening on http://%s:%s', config['host'], config['port'])
    web.run_app(app, host=config['host'], port=config['port'], access_log=None, print=None)


if __name__ == '__main__':
    main()
