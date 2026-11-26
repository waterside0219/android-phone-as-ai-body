"""Connection-only B7 migration: keep secrets/preferences, reuse real rollback journal."""
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import agent
import remote_update as update

OLD = 'wss://old-relay.example.com/agent'
NEW = 'wss://relay.example.com/agent'
ROUTE = 'face/relay-route.json'


class RelayRouteTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / 'phone-agent').mkdir()
        (self.root / 'face').mkdir()
        self.config = {'url': OLD, 'token': 'test-only-secret', 'device_id': 'test',
                       'rish': '/test/rish', 'rish_application_id': 'com.termux',
                       'apps': {'chrome': 'com.android.chrome'}}
        self.config_path = self.root / 'phone-agent/config.json'
        self.config_path.write_text(json.dumps(self.config))
        self.privacy = self.root / 'phone-agent/privacy.json'
        self.privacy.write_text('{"manual_mute":true,"camera_enabled":false}')
        self.updater = update.Updater(self.root, self.config)

    def load(self):
        self.assertTrue(hasattr(update, 'connection_config'), 'shared agent/update-worker route loader missing')
        return update.connection_config(self.root)

    def stage(self, url, version='route-1'):
        data = json.dumps({'url': url}).encode()
        manifest = {'version': version, 'files': [
            {'path': ROUTE, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}]}
        return self.updater.stage(version, lambda path, limit: json.dumps(manifest).encode() if path.endswith('manifest.json') else data)

    def test_agent_and_worker_use_same_route_without_changing_identity_config(self):
        before = self.config_path.read_bytes()
        self.assertEqual(self.load(), self.config)
        (self.root / ROUTE).write_text(json.dumps({'url': NEW}))
        self.assertEqual(self.load(), {**self.config, 'url': NEW})
        self.assertEqual(agent.read_config(self.config_path), {**self.config, 'url': NEW})
        self.assertEqual(self.config_path.read_bytes(), before)
        seen = []
        def stage(worker, version):
            seen.append(worker.config['url'])
            return {'version': version, 'files': []}
        with patch('sys.argv', ['remote_update.py', '--root', str(self.root), '--run', 'next']), \
                patch.object(update.Updater, 'stage', stage), patch.object(update.Updater, 'install'), \
                patch('remote_update.time.sleep'):
            update.main()
        self.assertEqual(seen, [NEW], 'worker would otherwise fetch next update from the old Mac')

    def test_only_two_approved_tls_addresses_and_no_other_settings_are_accepted(self):
        self.load()
        for route in ({'url':'wss://evil.example/agent'}, {'url':NEW,'token':'replacement'},
                      {'url':OLD.replace('wss:', 'ws:')}, {'url':NEW+'?token=x'},
                      {'url':'wss://user:pass@relay.example.com/agent'}, [], None):
            (self.root / ROUTE).write_text(json.dumps(route))
            with self.subTest(route=route), self.assertRaises(ValueError): self.load()
        (self.root / ROUTE).write_bytes(b' ' * 4097)
        with self.assertRaises(ValueError): self.load()

    def test_invalid_route_rejected_before_any_install(self):
        with self.assertRaises(ValueError, msg='invalid route must be rejected at stage, not kill the agent'):
            self.stage('wss://evil.example/agent')
        self.assertFalse((self.root / ROUTE).exists())
        self.assertFalse((self.updater.txn / 'journal.json').exists())

    def test_failed_new_connection_rolls_back_url_and_preserves_settings(self):
        self.load()
        config = self.config_path.read_bytes(); privacy = self.privacy.read_bytes()
        staged = self.stage(NEW)
        restarts = []
        checks = iter([False, True])
        self.updater.install(staged, lambda: restarts.append(self.load()['url']), lambda: next(checks))
        self.assertEqual(restarts, [NEW, OLD])
        self.assertEqual(self.updater.status()['state'], 'rolled_back')
        self.assertEqual(self.load()['url'], OLD)
        self.assertEqual(self.config_path.read_bytes(), config)
        self.assertEqual(self.privacy.read_bytes(), privacy)
        self.assertFalse((self.root / ROUTE).exists())

    def test_success_manual_return_and_interrupted_switch_restore_previous_route(self):
        self.load()
        self.updater.install(self.stage(NEW), lambda: None, lambda: self.load()['url'] == NEW)
        self.assertEqual(self.updater.status()['state'], 'installed')
        self.updater.install(self.stage(OLD, 'return-old'), lambda: None, lambda: self.load()['url'] == OLD)
        self.assertEqual(self.load()['url'], OLD)
        def interrupted(): raise KeyboardInterrupt('simulated worker interruption')
        with self.assertRaises(KeyboardInterrupt):
            self.updater.install(self.stage(NEW, 'interrupted'), interrupted, lambda: True)
        self.assertTrue(update.Updater(self.root).recover())
        self.assertEqual(self.load()['url'], OLD)
        self.assertEqual(self.updater.current(), 'return-old')

    def test_status_reports_effective_relay_without_token(self):
        device = agent.Device.__new__(agent.Device)
        self.updater.config = {**self.config, 'url':NEW}
        device.updater = self.updater
        device.face = types.SimpleNamespace(snapshot=lambda: {'page_ready':'test'})
        device.current_input_method = lambda: 'test.keyboard/.IME'
        status = device.execute('update_status', {})
        self.assertEqual(status.get('relay_url'), NEW)
        self.assertNotIn('test-only-secret', json.dumps(status))


if __name__ == '__main__': unittest.main()
