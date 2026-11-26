"""Update boundary/rollback checks using real temporary files; no device commands."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest

SPEC = importlib.util.find_spec('remote_update')
if SPEC:
    import remote_update as update


def manifest(files, version='test-1'):
    return {'version': version, 'files': [
        {'path': p, 'size': len(b), 'sha256': hashlib.sha256(b).hexdigest()}
        for p, b in files.items()
    ]}


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SPEC, 'B7 update transaction is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'face').mkdir()
        (self.root/'face/index.html').write_bytes(b'old face')
        (self.root/'phone-agent').mkdir()
        (self.root/'phone-agent/privacy.json').write_text('{"camera_enabled":false}')
        self.updater = update.Updater(self.root)

    def stage(self, files, version='test-1'):
        m = manifest(files, version)
        def fetch(path, limit):
            return json.dumps(m).encode() if path.endswith('manifest.json') else files[path.split('resource=files/', 1)[1]]
        return self.updater.stage(version, fetch)

    def test_bad_paths_secrets_duplicates_and_hash_are_rejected_before_writes(self):
        for path in ['../face/x', '/face/x', 'face/../x', 'face//x', 'face/./x',
                     'face/x\\y', 'home/x.py', 'phone-agent/config.json',
                     'phone-agent/privacy.json', 'call-web/server.config.json',
                     'phone-agent/pwa-shortcut.json', 'phone-agent/rish', 'face/.hidden']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                update.validate_manifest(manifest({path: b'new'}))
        m = manifest({'face/index.html': b'new'})
        m['files'].append(m['files'][0])
        with self.assertRaises(ValueError): update.validate_manifest(m)
        m = manifest({'face/index.html': b'new'})
        with self.assertRaises(ValueError):
            self.updater.stage('test-1', lambda path, limit: json.dumps(m).encode() if path.endswith('manifest.json') else b'bad')
        self.assertEqual((self.root/'face/index.html').read_bytes(), b'old face')

    def test_success_and_failed_health_restore_old_files_and_keep_preferences(self):
        staged = self.stage({'face/index.html': b'first', 'face/added.js': b'new'})
        self.updater.install(staged, lambda: None, lambda: True)
        self.assertEqual(self.updater.status()['state'], 'installed')
        self.assertEqual((self.root/'face/index.html').read_bytes(), b'first')
        staged = self.stage({'face/index.html': b'broken', 'face/new.js': b'void 0'}, 'test-2')
        checks = iter([False, True])
        self.updater.install(staged, lambda: None, lambda: next(checks))
        self.assertEqual(self.updater.status()['state'], 'rolled_back')
        self.assertEqual(self.updater.status()['current_version'], 'test-1')
        self.assertEqual((self.root/'face/index.html').read_bytes(), b'first')
        self.assertFalse((self.root/'face/new.js').exists())
        self.assertEqual(json.loads((self.root/'phone-agent/privacy.json').read_text()), {'camera_enabled': False})

    def test_interrupted_install_is_recoverable_and_symlinks_are_refused(self):
        staged = self.stage({'face/index.html': b'new', 'face/added.js': b'new'})
        def power_loss(): raise KeyboardInterrupt('power loss')
        with self.assertRaises(KeyboardInterrupt):
            self.updater.install(staged, power_loss, lambda: True)
        fresh = update.Updater(self.root)
        self.assertTrue(fresh.recover())
        self.assertEqual((self.root/'face/index.html').read_bytes(), b'old face')
        self.assertFalse((self.root/'face/added.js').exists())
        outside = self.root/'outside'; outside.mkdir()
        (self.root/'face/link').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError): self.stage({'face/link/file.js': b'bad'})
        self.assertFalse((outside/'file.js').exists())

    def test_rollback_restart_failure_is_reported_as_recovery_needed(self):
        staged = self.stage({'face/index.html': b'broken'})
        restarts = iter([None, OSError('restart failed')])
        def restart():
            error = next(restarts)
            if error:
                raise error
        self.updater.install(staged, restart, lambda: False)
        self.assertEqual((self.root/'face/index.html').read_bytes(), b'old face')
        self.assertEqual(self.updater.status()['state'], 'recovery_needed')

    def test_concurrent_atomic_writers_never_share_a_temporary_file(self):
        target = self.root/'agent.pid'
        barrier = threading.Barrier(2)
        replace = update.os.replace
        def synchronized_replace(source, destination):
            barrier.wait(timeout=2)
            return replace(source, destination)
        with patch('remote_update.os.replace', side_effect=synchronized_replace), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(update.atomic_write, target, str(pid).encode()) for pid in (1, 2)]
            for future in futures:
                future.result()
        self.assertIn(target.read_text(), ('1', '2'))

    def test_redirects_cannot_leak_auth_and_update_command_is_bounded(self):
        self.assertEqual(update.update_origin('wss://body.example/agent'), 'https://body.example')
        for url in ['ws://body.example/agent', 'https://evil.example', 'wss://a:b@body.example/agent']:
            with self.assertRaises(ValueError): update.update_origin(url)
        for version in ['../x', 'v1;sh', '', 'a'*81]:
            with self.assertRaises(ValueError): update.validate_version(version)
        self.assertIsNone(update.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example'))

if __name__ == '__main__': unittest.main()
