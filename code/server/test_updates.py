import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'phone-agent'))
import body_relay as relay
import agent
from face_service import FaceService

class UpdateIntegrationTest(unittest.TestCase):
    def test_update_files_require_auth_and_exact_manifest_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); package=root/'.private/updates/v1'
            (package/'files/face').mkdir(parents=True)
            (package/'files/face/index.html').write_bytes(b'new')
            (package/'files/face/secret.json').write_bytes(b'private')
            manifest={'version':'v1','files':[{'path':'face/index.html','size':3,'sha256':hashlib.sha256(b'new').hexdigest()}]}
            (package/'manifest.json').write_text(json.dumps(manifest))
            with patch.object(relay,'ROOT',root),patch.object(relay,'DEVICE_TOKEN',b'test-only'):
                server=ThreadingHTTPServer(('127.0.0.1',0),relay.Handler)
                server.daemon_threads=True
                threading.Thread(target=server.serve_forever,daemon=True).start()
                try:
                    base='http://127.0.0.1:%s/agent?update=v1&resource='%server.server_port
                    def get(path, auth=True):
                        headers={'Authorization':'Bearer test-only'} if auth else {}
                        try:
                            with urllib.request.urlopen(urllib.request.Request(base+path,headers=headers),timeout=2) as r:return r.status,r.read()
                        except urllib.error.HTTPError as e:
                            with e:return e.code,e.read()
                    self.assertEqual(get('manifest.json')[0],200,'authenticated update manifest is missing')
                    self.assertEqual(get('files/face/index.html')[1],b'new')
                    self.assertEqual(get('manifest.json',False)[0],401)
                    self.assertEqual(get('files/face/secret.json')[0],404)
                    self.assertNotEqual(get('files/face/../manifest.json')[0],200)
                finally:server.shutdown();server.server_close()

    def test_agent_has_bounded_update_and_page_health_commands(self):
        self.assertIn('update',agent.ACTIONS)
        self.assertIn('update_status',agent.ACTIONS)
        self.assertIn('update',relay.ACTIONS)
        face=FaceService()
        self.assertIn('release',face.snapshot())
        self.assertFalse(face.snapshot()['agent_connected'])
        self.assertEqual(face.snapshot()['page_ready'],'')

if __name__=='__main__':unittest.main()
