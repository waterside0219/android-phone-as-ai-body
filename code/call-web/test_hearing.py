"""Cloud-first phone entry; synthetic PCM and a loopback relay, no real key/network."""
import asyncio
import contextlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
import server


class HearingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'call-web').mkdir(); (self.root / 'phone-agent').mkdir()
        (self.root / 'phone-agent/config.json').write_text(json.dumps({'token': 'body-test-token', 'url': 'wss://relay.example.com/agent'}))
        binary = self.root / 'whisper'
        import sys
        binary.write_text('#!' + sys.executable + '\nimport sys\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index("-of")+1])\np.with_suffix(".txt").write_text("本地兜底")\n')
        binary.chmod(0o700)
        self.config = {'temp_directory': str(self.root), 'whisper_binary': str(binary), 'whisper_model': 'tiny'}
        self.reply = {'text': '更换 Shizuku，看见一只猫。'}; self.status = 200
        self.delay = 0; self.requests = []; self.finished = asyncio.Event()
        async def handle(request):
            self.requests.append((request.path_qs, request.headers.get('Authorization'), await request.read()))
            if self.delay:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.finished.wait(), self.delay)
            return web.json_response(self.reply, status=self.status)
        app = web.Application(); app.router.add_post('/agent', handle)
        self.relay = TestServer(app); await self.relay.start_server()
        # Redirect only the test transport; production URL/token/body are asserted.
        def session(*args, **kwargs):
            real = ClientSession(*args, **kwargs); original = real.post
            def post(url, **options):
                self.assertEqual(url, 'https://relay.example.com/agent?transcribe=1')
                self.assertFalse(options.get('allow_redirects', True))
                return original(str(self.relay.make_url('/agent?transcribe=1')), **options)
            real.post = post
            return real
        self.patches = [patch.object(server, 'ROOT', self.root/'call-web'), patch.object(server, 'ClientSession', side_effect=session)]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        self.pcm = b'\x01\x00' * 8000

    async def asyncTearDown(self):
        self.finished.set(); await self.relay.close()

    async def transcribe(self, cancel=None):
        return await asyncio.to_thread(server.transcribe_pcm, self.pcm, self.config, cancel)

    async def test_cloud_first_has_no_audio_files_and_empty_is_success(self):
        with patch.object(server.tempfile, 'TemporaryDirectory', side_effect=AssertionError('cloud must not write audio')):
            self.assertEqual(await self.transcribe(), self.reply['text'])
            self.reply = {'text': ''}
            self.assertEqual(await self.transcribe(), '')
        self.assertEqual(self.requests[0], ('/agent?transcribe=1', 'Bearer body-test-token', self.pcm))

    async def test_failure_reaches_original_tiny_and_logs_no_content(self):
        for status, reply in [(401, {'error': 'private-provider-detail'}), (402, {}), (503, {}), (200, {}), (200, {'text': None}), (200, {'text': 'x'*2001})]:
            self.status, self.reply = status, reply
            with self.subTest(status=status, reply_type=type(reply.get('text')).__name__), self.assertLogs('call-web', level='WARNING') as logs:
                self.assertEqual(await self.transcribe(), '本地兜底')
            self.assertEqual(len(logs.output), 1)
            self.assertIn('fallback', logs.output[0]); self.assertNotIn('private-provider-detail', logs.output[0])
            self.assertNotIn('body-test-token', logs.output[0])
        self.assertEqual(list(self.root.glob('call-web-*')), [])

    async def test_connection_refused_falls_back_without_hanging_up(self):
        from aiohttp import ClientConnectorError
        # Keep a valid URL and close its listening socket to model no network.
        url = str(self.relay.make_url('/agent?transcribe=1'))
        await self.relay.close()
        original = ClientSession.post
        def refused(session, ignored_url, **kwargs):
            return original(session, url, **kwargs)
        with patch.object(server, 'ClientSession', ClientSession), patch.object(ClientSession, 'post', refused), \
                self.assertLogs('call-web', level='WARNING') as logs:
            self.assertEqual(await self.transcribe(), '本地兜底')
        self.assertIn(ClientConnectorError.__name__, logs.output[0])

    async def test_call_room_keeps_echo_empty_and_fallback_behavior(self):
        room = server.CallRoom({'config': self.config, 'whisper_semaphore': asyncio.Semaphore(1)}, 'test-call')
        room.upstream = AsyncMock()
        room.upstream.closed = False
        room.recent.append((time.monotonic(), self.reply['text']))
        await room.transcribe('echo', self.pcm)
        self.assertEqual(room.events[-1]['data']['type'], 'echo_dropped')
        room.upstream.send_json.assert_not_awaited()
        self.reply = {'text': ''}
        await room.transcribe('quiet', self.pcm)
        self.assertIn('轻声', room.events[-1]['data']['text'])
        self.assertIn('请轻轻接一下', room.upstream.send_json.call_args.args[0]['text'])
        self.status = 401
        with self.assertLogs('call-web', level='WARNING'):
            await room.transcribe('fallback', self.pcm)
        self.assertEqual(room.events[-1]['data']['text'], '本地兜底')
        self.assertEqual(room.upstream.send_json.call_args.args[0]['text'], '本地兜底')
        self.assertFalse(room.closed)

    async def test_stalled_response_reaches_tiny_before_six_seconds(self):
        self.delay = 20
        started = time.monotonic()
        with self.assertLogs('call-web', level='WARNING'):
            self.assertEqual(await self.transcribe(), '本地兜底')
        elapsed = time.monotonic()-started
        self.assertTrue(self.requests, 'must actually attempt the relay')
        self.assertLess(elapsed, 6, elapsed)
        print('synthetic stalled cloud → tiny: %.3fs' % elapsed)

    async def test_hangup_cancels_cloud_without_starting_tiny(self):
        self.delay = 20; cancel = threading.Event()
        with patch.object(server.subprocess, 'Popen', side_effect=AssertionError('hangup must not run tiny')):
            job = asyncio.create_task(self.transcribe(cancel))
            try:
                for _ in range(100):
                    if self.requests: break
                    if job.done(): await job
                    await asyncio.sleep(.01)
                self.assertTrue(self.requests)
                cancel.set()
                with self.assertRaises(server.WhisperFailure): await asyncio.wait_for(job, 1)
            finally:
                cancel.set(); await asyncio.gather(job, return_exceptions=True)


if __name__ == '__main__': unittest.main()
