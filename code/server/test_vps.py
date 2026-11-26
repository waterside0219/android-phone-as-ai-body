"""Portable relay and release handoff; real loopback HTTP, no phone/VPS/provider."""
import base64
import hashlib
import io
import json
from pathlib import Path
import socket
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import body_relay as relay
import update_phone
from remote_update import Updater


def package(version='portable-1', files=None, corrupt=False, extra=None):
    files = files or {'face/index.html': b'<html>test</html>'}
    manifest = {'version': version, 'files': [
        {'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        for name, data in files.items()]}
    if corrupt:
        manifest['files'][0]['sha256'] = '0' * 64
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for name, data in [('manifest.json', json.dumps(manifest).encode()),
                           *[('files/' + name, data) for name, data in files.items()]]:
            info = tarfile.TarInfo(name); info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if extra:
            info = tarfile.TarInfo(extra); info.size = 1
            archive.addfile(info, io.BytesIO(b'x'))
    return output.getvalue()


class PortableRelayTest(unittest.TestCase):
    def test_muted_say_never_reads_key_or_calls_tts(self):
        relay.HUB.register(object())
        relay.HUB.add_event('controls', {'muted': True})
        with patch.object(relay, 'read_token', return_value=b'test-key') as key_read, \
                patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps({'base_resp': {'status_code': 0}, 'data': {'audio': b'ID3test'.hex()}}).encode())), \
                patch.object(relay.HUB, 'request', return_value=({'ok': True, 'result': {'queued': True}}, None)) as sent:
            # Use HTTPConnection so the provider mock cannot intercept the local request.
            import http.client
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
            conn.request('POST', '/v1/do', json.dumps({'action': 'say', 'args': {'text': '在呢'}}),
                         {'Authorization': 'Bearer test-device', 'Content-Type': 'application/json'})
            response = conn.getresponse(); status, body = response.status, response.read(); conn.close()
        self.assertEqual(status, 200, body)
        key_read.assert_not_called()
        self.assertEqual(sent.call_args.args, ('say', {'text': '在呢'}))
        relay.HUB.add_event('controls', {'muted': False})
        rendered = {'text': '有声音', 'audio': {'base64': 'SUQz', 'mime_type': 'audio/mpeg'}}
        with patch.object(relay, 'render_say', return_value=rendered) as synth, \
                patch.object(relay.HUB, 'request', return_value=({'ok': True}, None)) as sent:
            status, _ = self.http('/v1/do', json.dumps({'action': 'say', 'args': {'text': '有声音'}}).encode(), content_type='application/json')
        self.assertEqual(status, 200)
        synth.assert_called_once_with({'text': '有声音'})
        self.assertEqual(sent.call_args.args, ('say', rendered))

    def test_controls_do_not_leak_between_connections(self):
        phone = object(); relay.HUB.register(phone)
        relay.HUB.add_event('controls', {'muted': True})
        relay.HUB.unregister(phone); relay.HUB.register(object())
        self.assertIsNone(getattr(relay.HUB, 'controls', {}).get('muted'))

    def test_camera_frame_includes_last_successful_speech_without_public_events(self):
        relay.HUB.register(object())
        relay.HUB.add_event('controls', {'muted': True})
        with patch.object(relay.HUB, 'request', return_value=({'ok': True, 'result': {'queued': True}}, None)):
            self.http('/v1/do', json.dumps({'action':'say','args':{'text':'刚说过'}}).encode(), content_type='application/json')
        with patch.object(relay.HUB, 'request', return_value=({'ok': True, 'result': {'presence': True}}, None)):
            status, body=self.http('/v1/do', b'{"action":"camera_frame","args":{}}', content_type='application/json')
        self.assertEqual(status,200)
        self.assertGreater(json.loads(body)['result'].get('last_say_at',0),0)
        with patch.object(relay.HUB, 'request', return_value=({'ok': True, 'result': {}}, None)):
            _,body=self.http('/v1/do', b'{"action":"camera_frame","args":{}}', content_type='application/json')
        self.assertEqual(json.loads(body)['result'], {}, 'closed camera remains empty')
        self.assertEqual(len(relay.HUB.events),1,'frame/pixels/speech must not enter event history')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = self.root / 'releases'
        self.key = self.root / 'key'; self.key.write_text('test-key')
        self.token = self.root / 'device'; self.token.write_text('test-device')
        self.settings = {'updates_dir': str(self.store), 'tts': {
            'api_key_file': str(self.key), 'voice_id': 'test-voice',
            'model': 'speech-02-hd', 'endpoint': 'https://api.minimaxi.com/v1/t2a_v2'}}
        self.patches = [patch.object(relay, 'SETTINGS', self.settings, create=True),
                        patch.object(relay, 'DEVICE_TOKEN', b'test-device'),
                        patch.object(relay, 'HUB', relay.PhoneHub())]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        self.server = relay.ThreadingHTTPServer(('127.0.0.1', 0), relay.Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:%s' % self.server.server_port
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def http(self, path, data=None, auth=True, content_type='application/x-tar'):
        headers = {'Content-Type': content_type}
        if auth: headers['Authorization'] = 'Bearer test-device'
        try:
            with self.opener.open(urllib.request.Request(self.url + path, data=data, headers=headers), timeout=3) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as error:
            with error: return error.code, error.read()

    def test_portable_paths_and_loopback_only(self):
        config = self.root / 'relay.json'
        config.write_text(json.dumps({'token_file': 'device', 'updates_dir': 'releases',
                                      'tts': {**self.settings['tts'], 'api_key_file': 'key'}}))
        try:
            result = relay.read_config(config)
        except ValueError as error:
            self.fail('portable configuration was rejected: ' + str(error))
        self.assertEqual(Path(result['token_file']), self.token)
        self.assertEqual(Path(result['updates_dir']), self.store)
        self.assertEqual(Path(result['tts']['api_key_file']), self.key)
        for host in ('0.0.0.0', '192.0.2.1', '::1'):
            config.write_text(json.dumps({'host': host}))
            with self.assertRaises(ValueError): relay.read_config(config)

    def test_upload_download_and_existing_b7_stage_without_mac_store(self):
        status, result = self.http('/v1/releases', package())
        self.assertEqual(status, 201, result)
        self.assertEqual(json.loads(result)['version'], 'portable-1')
        (self.root / 'phone').mkdir()
        updater = Updater(self.root / 'phone')
        staged = updater.stage('portable-1', lambda path, limit: self.http(path)[1])
        self.assertEqual(staged['version'], 'portable-1')
        self.assertEqual((updater.txn / 'staged/face/index.html').read_bytes(), b'<html>test</html>')
        self.assertEqual(self.http('/agent?update=portable-1&resource=manifest.json', auth=False)[0], 401)
        self.assertEqual(self.http('/v1/releases', package())[0], 409)
        self.assertEqual(self.http('/agent?update=portable-1&resource=files/face/missing.js')[0], 404)
        self.assertEqual(list(self.store.glob('.staging-*')), [])

    def test_bad_packages_never_become_visible(self):
        self.assertEqual(self.http('/v1/releases', package(), auth=False)[0], 401)
        for raw in (package(corrupt=True), package(extra='../../escape'),
                    package(files={'phone-agent/config.json': b'{}'}), b'bad tar'):
            with self.subTest(kind=hashlib.sha256(raw).hexdigest()[:8]):
                self.assertEqual(self.http('/v1/releases', raw)[0], 400)
                self.assertFalse((self.store / 'portable-1').exists())
        self.assertFalse((self.root / 'escape').exists())
        self.assertEqual(list(self.store.glob('.staging-*')), [])

    def test_interrupted_upload_has_no_release(self):
        sock = socket.create_connection(('127.0.0.1', self.server.server_port))
        sock.sendall(b'POST /v1/releases HTTP/1.1\r\nHost: localhost\r\n'
                     b'Authorization: Bearer test-device\r\nContent-Type: application/x-tar\r\n'
                     b'Content-Length: 10240\r\n\r\npartial')
        sock.shutdown(socket.SHUT_WR)
        try:
            self.assertIn(b'400', sock.recv(4096).split(b'\r\n')[0])
        finally:
            sock.close()
        self.assertFalse((self.store / 'portable-1').exists())

    def test_tar_extensions_cannot_request_unbounded_allocation(self):
        info = tarfile.TarInfo('metadata')
        info.type = tarfile.XHDTYPE
        info.size = 1024 ** 3
        raw = info.tobuf(format=tarfile.USTAR_FORMAT) + b'\0' * 9728
        class GuardedInput(io.BytesIO):
            def read(self, size=-1):
                assert 0 <= size <= relay.MAX_MANIFEST, 'tar parser requested an unbounded allocation'
                return super().read(size)
        # Prevent the OLD implementation from actually allocating the claimed GiB.
        with patch('tarfile.bltn_open', return_value=GuardedInput(raw)):
            with self.assertRaises(ValueError):
                relay.receive_release(io.BytesIO(raw), len(raw))
        info.size = 11
        extension = info.tobuf(format=tarfile.USTAR_FORMAT) + b'11 x=aaaaa\n'.ljust(512, b'\0')
        chained = extension * 300 + package()
        with self.assertRaisesRegex(ValueError, 'metadata too large'):
            relay.receive_release(io.BytesIO(chained), len(chained))
        self.assertEqual(self.http('/v1/releases', raw)[0], 400)
        self.assertFalse((self.store / 'portable-1').exists())

    def test_direct_mp3_with_bounded_safe_provider_errors(self):
        audio = b'ID3test-mp3'
        response = {'base_resp': {'status_code': 0}, 'data': {'audio': audio.hex()}}
        with patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(response).encode())) as opened:
            with patch('subprocess.run', side_effect=AssertionError('Mac helper/ffmpeg must not run')):
                result = relay.render_say({'text': '合成测试'})
        payload = json.loads(opened.call_args.args[0].data)
        self.assertEqual(payload['audio_setting']['format'], 'mp3')
        self.assertEqual(payload['voice_setting']['voice_id'], 'test-voice')
        self.assertEqual(payload['output_format'], 'hex')
        self.assertEqual(base64.b64decode(result['audio']['base64']), audio)
        self.assertEqual(result['audio']['mime_type'], 'audio/mpeg')
        for response in ({'base_resp': {'status_code': 1, 'status_msg': 'secret-provider-detail'}},
                         {'base_resp': {'status_code': 0}, 'data': {'audio': 'zz'}}, []):
            with patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(response).encode())):
                with self.assertRaises(relay.VoiceError) as failure:
                    relay.render_say({'text': '合成测试'})
                self.assertNotIn('secret-provider-detail', str(failure.exception))
        for args in ({'text': ''}, {'text': 'x', 'voice_id': 'other'}):
            with self.assertRaises(relay.VoiceError): relay.render_say(args)

    def test_global_and_excessive_total_pax_metadata_are_rejected(self):
        info = tarfile.TarInfo('global')
        info.type = tarfile.XGLTYPE; info.size = 11
        raw = info.tobuf(format=tarfile.USTAR_FORMAT) + b'11 x=aaaaa\n'.ljust(512, b'\0') + package()
        self.assertEqual(self.http('/v1/releases', raw)[0], 400)
        files = {'face/f%d.js' % i: b'x' for i in range(36)}
        output = io.BytesIO()
        with tarfile.open(fileobj=io.BytesIO(package(files=files)), mode='r:') as source, \
                tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT) as destination:
            for member in source:
                if member.name != 'manifest.json': member.pax_headers = {'comment': 'x' * 60000}
                destination.addfile(member, source.extractfile(member))
        self.assertEqual(self.http('/v1/releases', output.getvalue())[0], 400)
        self.assertFalse((self.store / 'portable-1').exists())

    def test_mac_publisher_pushes_existing_package_over_selected_tunnel(self):
        self.assertTrue(hasattr(update_phone, 'upload'), 'Mac publisher has no remote upload path')
        local = self.root / 'mac/portable-1'; local.mkdir(parents=True)
        files = {'face/index.html': b'<html>test</html>', 'face/' + 'x' * 101 + '.png': b'long-name'}
        with tarfile.open(fileobj=io.BytesIO(package(files=files)), mode='r:') as archive:
            for member in archive:
                target = local / member.name; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        try:
            result = update_phone.upload('portable-1', self.url, self.token, store=local.parent)
        except ValueError as error:
            self.fail('a valid B7 release must upload, including long basenames: ' + str(error))
        self.assertEqual(result['version'], 'portable-1')
        self.assertEqual(self.http('/agent?update=portable-1&resource=files/face/index.html')[1], b'<html>test</html>')
        with self.assertRaises(ValueError):
            update_phone.upload('portable-1', 'http://example.com', self.token, store=local.parent)


if __name__ == '__main__':
    unittest.main()
