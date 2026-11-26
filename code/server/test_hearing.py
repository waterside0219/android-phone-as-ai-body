"""Hearing endpoint contract; stdlib only, fake provider replies and key."""
import base64
import importlib.util
import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path
from unittest.mock import patch
import body_relay as relay

SPEC = importlib.util.find_spec('body_hearing')
if SPEC: import body_hearing as hearing


class HearingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.key = self.root/'key'; self.key.write_text('fake-hearing-key'); self.key.chmod(0o600)
        self.usage = self.root/'usage.jsonl'
        self.pcm = b'\x01\x00'*8000
        self.patches = [patch.object(relay, 'DEVICE_TOKEN', b'body-test-token')]
        if SPEC:
            self.patches += [patch.object(hearing, 'KEY_FILE', self.key), patch.object(hearing, 'USAGE_FILE', self.usage),
                             patch.object(hearing, 'SCRIBE_KEY_FILE', self.root/'no-scribe-key')]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        self.server = relay.ThreadingHTTPServer(('127.0.0.1', 0), relay.Handler); self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, data=None, auth=True, path='/agent?transcribe=1', content_type='application/octet-stream'):
        headers = {'Content-Type': content_type}
        if auth: headers['Authorization'] = 'Bearer body-test-token'
        req = urllib.request.Request(self.url+path, data=self.pcm if data is None else data, headers=headers)
        try:
            with self.opener.open(req, timeout=3) as response: return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error: return error.code, json.loads(error.read())


class HearingEndpointTest(HearingBase):
    def test_route_exists_authenticates_and_bounds_pcm(self):
        self.assertEqual(self.post(auth=False)[0], 401)
        for data in (b'', b'odd'):
            self.assertEqual(self.post(data)[0], 413)
        # The server rejects from headers before reading an oversized body.
        # Sending the entire body races its deliberate close and masks HTTP 413.
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        try:
            connection.putrequest('POST', '/agent?transcribe=1')
            for key, value in {'Authorization': 'Bearer body-test-token',
                               'Content-Type': 'application/octet-stream', 'Content-Length': '960002'}.items():
                connection.putheader(key, value)
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 413); response.read()
        finally:
            connection.close()
        self.assertEqual(self.post(content_type='text/plain')[0], 415)
        self.assertEqual(self.post(path='/agent?transcribe=1&extra=1')[0], 404)

    def test_wav_prompt_model_empty_text_usage_and_no_audio_storage(self):
        self.assertIsNotNone(SPEC, 'stdlib hearing module is missing')
        for text in ('更换 Shizuku，看见一只猫。', ''):
            body = {'choices': [{'finish_reason': 'stop', 'message': {'content': text}}], 'usage': {'cost': .00012}}
            opener = unittest.mock.Mock()
            opener.open.return_value = io.BytesIO(json.dumps(body).encode())
            with patch.object(hearing, 'build_opener', return_value=opener):
                status, result = self.post()
            self.assertEqual(status, 200, result); self.assertEqual(result['text'], text)
            request = opener.open.call_args.args[0]; payload = json.loads(request.data)
            self.assertEqual(request.full_url, 'https://openrouter.ai/api/v1/chat/completions')
            self.assertEqual(payload['models'], ['google/gemini-2.5-flash-lite', 'google/gemini-2.5-flash'])
            self.assertNotIn('model', payload)
            self.assertLessEqual(opener.open.call_args.kwargs['timeout'], 5)
            self.assertIn('简体', payload['messages'][0]['content'])
            self.assertIn('不补全', payload['messages'][0]['content'])
            audio = payload['messages'][1]['content'][0]['input_audio']
            self.assertEqual(audio['format'], 'wav')
            with wave.open(io.BytesIO(base64.b64decode(audio['data']))) as wav:
                self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1,2,16000))
                self.assertEqual(wav.readframes(wav.getnframes()), self.pcm)
        rows = [json.loads(line) for line in self.usage.read_text().splitlines()]
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['calls'], 2)
        self.assertAlmostEqual(rows[0]['cost_usd'], .00024)
        self.assertEqual(set(p.name for p in self.root.iterdir()), {'key','usage.jsonl'})
        self.assertNotIn('更换', self.usage.read_text()); self.assertNotIn('fake-hearing-key', self.usage.read_text())

    def test_provider_failures_are_sanitized_and_counted(self):
        self.assertIsNotNone(SPEC, 'stdlib hearing module is missing')
        for body in ({'error': {'message': 'private-provider-detail'}}, {'choices': [{'finish_reason': 'length', 'message': {'content': 'truncated'}}]}, {'choices': [{'finish_reason':'stop','message': {'content': None}}]}):
            opener = unittest.mock.Mock(); opener.open.return_value = io.BytesIO(json.dumps(body).encode())
            with patch.object(hearing, 'build_opener', return_value=opener):
                status, result = self.post()
            self.assertEqual(status, 502)
            self.assertNotIn('private-provider-detail', json.dumps(result))
        opener = unittest.mock.Mock(); opener.open.side_effect = urllib.error.HTTPError('https://openrouter.ai',401,'private-provider-detail',{},io.BytesIO(b'secret'))
        with patch.object(hearing, 'build_opener', return_value=opener):
            self.assertEqual(self.post()[0], 502)
        row = json.loads(self.usage.read_text())
        self.assertEqual(row['calls'], 4); self.assertEqual(row['failed'], 4); self.assertEqual(row['unknown_cost_calls'], 4)

    def test_busy_does_not_queue_another_provider_call(self):
        self.assertIsNotNone(SPEC, 'stdlib hearing module is missing')
        with hearing.LOCK:
            self.assertEqual(self.post()[0], 503)
        self.assertFalse(self.usage.exists())

    def test_usage_write_failure_is_sanitized_and_releases_lock(self):
        self.assertIsNotNone(SPEC, 'stdlib hearing module is missing')
        body = {'choices': [{'finish_reason': 'stop', 'message': {'content': '测试'}}]}
        opener = unittest.mock.Mock(); opener.open.return_value = io.BytesIO(json.dumps(body).encode())
        with patch.object(hearing, 'build_opener', return_value=opener), \
                patch.object(hearing, 'count_usage', side_effect=PermissionError('private-provider-detail')):
            status, result = self.post()
        self.assertEqual(status, 502); self.assertNotIn('private-provider-detail', json.dumps(result))
        self.assertFalse(hearing.LOCK.locked())


class ScribeTest(HearingBase):
    def setUp(self):
        super().setUp()
        self.scribe_key = self.root/'scribe-key'; self.scribe_key.write_text('fake-scribe-key')
        p = patch.object(hearing, 'SCRIBE_KEY_FILE', self.scribe_key); p.start(); self.addCleanup(p.stop)

    def reply(self, *bodies):
        opener = unittest.mock.Mock()
        opener.open.side_effect = [b if isinstance(b, Exception) else io.BytesIO(json.dumps(b).encode()) for b in bodies]
        return opener

    def test_scribe_first_with_pcm_events_and_keyterms(self):
        body = {'text': '[笑声] 鸡翅和冒菜', 'words': [{'type': 'audio_event', 'text': '[笑声]'}, {'type': 'word', 'text': '鸡翅'}]}
        opener = self.reply(body)
        with patch.object(hearing, 'build_opener', return_value=opener):
            status, result = self.post()
        self.assertEqual((status, result['text'], result['model']), (200, '[笑声] 鸡翅和冒菜', 'scribe_v2'))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.elevenlabs.io/v1/speech-to-text')
        self.assertEqual(request.headers['Xi-api-key'], 'fake-scribe-key')
        for part in (b'scribe_v2', b'pcm_s16le_16', b'tag_audio_events', b'Shizuku', self.pcm):
            self.assertIn(part, request.data)
        self.assertLessEqual(opener.open.call_args.kwargs['timeout'], 4)
        self.assertFalse(self.usage.exists())

    def test_events_only_is_not_speech(self):
        body = {'text': '[呼吸声]', 'words': [{'type': 'audio_event', 'text': '[呼吸声]'}]}
        with patch.object(hearing, 'build_opener', return_value=self.reply(body)):
            self.assertEqual(self.post()[1]['text'], '')

    def test_scribe_failure_falls_back_to_gemini(self):
        gemini = {'choices': [{'finish_reason': 'stop', 'message': {'content': '我在呢'}}]}
        failure = urllib.error.HTTPError('https://api.elevenlabs.io', 401, 'private-provider-detail', {}, io.BytesIO(b'secret'))
        with patch.object(hearing, 'build_opener', return_value=self.reply(failure, gemini)):
            status, result = self.post()
        self.assertEqual((status, result['text']), (200, '我在呢'))
        self.assertNotIn('private-provider-detail', json.dumps(result))


class NoiseTextTest(unittest.TestCase):
    def test_timestamp_only_output_is_dropped(self):
        for raw in ('00:00 00:01 00:01 00:01', ' 00:00 ', '0', '00:00。'):
            self.assertEqual(hearing.clean_text(raw), '')
        for raw in ('我在呢', '三点见', '10点20分开会'):
            self.assertEqual(hearing.clean_text(raw), raw)


if __name__ == '__main__':
    unittest.main()
