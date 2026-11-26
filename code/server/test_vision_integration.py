"""Loopback-only phone/relay and standalone daemon/CLI acceptance."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import body_relay as relay
import body_vision as vision
import agent
from face_service import FaceService


class VisionIntegrationTest(unittest.TestCase):
    def test_phone_frame_crosses_http_websocket_and_relay_without_image_events(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory)/'token';token.write_text('test-token')
            with patch.object(relay, 'HUB', relay.PhoneHub()), patch.object(relay, 'DEVICE_TOKEN', b'test-token'):
                server = relay.ThreadingHTTPServer(('127.0.0.1',0),relay.Handler)
                server.daemon_threads=True
                threading.Thread(target=server.serve_forever,daemon=True).start()
                face=FaceService();face.watch_periods=[{'days':'daily','start':'00:00','end':'24:00'}]
                face.manual_mute=True
                local=face.start(port=0)
                ws=agent.connect({'url':'ws://127.0.0.1:%d/agent'%server.server_port,'token':'test-token','device_id':'test'})
                events=agent.EventSender();events.attach(ws);face.event_sender=events.send;face.report_controls(force=True)
                device=agent.Device.__new__(agent.Device);device.face=face
                def respond():
                    try:
                        while True:
                            opcode,payload=ws.receive()
                            if opcode!=1:continue
                            request=json.loads(payload)
                            result=device.execute(request['action'],request['args'])
                            ws.send(1,json.dumps({'id':request['id'],'ok':True,'result':result}).encode())
                    except (OSError,ConnectionError):pass
                worker=threading.Thread(target=respond,daemon=True);worker.start()
                config={'relay_url':'http://127.0.0.1:%d'%server.server_port,'token_file':str(token)}
                try:
                    face.update_presence({'present':True,'look':{'x':0,'y':0}})
                    raw=b'\xff\xd8'+os.urandom(75000)+b'\xff\xd9'
                    packet={'jpeg_base64':base64.b64encode(raw).decode(),'dhash':'0'*16,'captured_at':time.time(),'reason':'manual'}
                    # Same >64 KiB HTTP payload sent by the real web page.
                    result=vision.request_json('http://127.0.0.1:%d/api/camera-frame'%local.server_port,'test-token',packet)
                    self.assertEqual(result['selected'],'manual')
                    result=vision.relay_frame(config)
                    self.assertEqual(base64.b64decode(result['jpeg_base64']),raw)
                    deadline=time.monotonic()+2
                    while relay.HUB.controls.get('muted') is not True and time.monotonic()<deadline:time.sleep(.01)
                    with patch.object(relay,'read_token',side_effect=AssertionError('muted speech must not access TTS')):
                        result=vision.request_json(config['relay_url']+'/v1/do','test-token',{'action':'say','args':{'text':'本机测试字幕'}})
                    self.assertTrue(result['result']['text_only'])
                    self.assertEqual(face.snapshot()['subtitle'],'本机测试字幕')
                    face.close_eyes(1)
                    self.assertEqual(vision.relay_frame(config),{})
                    self.assertNotIn(packet['jpeg_base64'],json.dumps(relay.HUB.events_after(0)))
                finally:
                    ws.sock.shutdown(2);ws.sock.close();worker.join(timeout=2)
                    face.stop();server.shutdown();server.server_close()

    def test_live_daemon_commands_and_expiry_without_any_phone_or_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            config={'relay_url':'http://127.0.0.1:8832','token_file':'unused-device',
                    'api_key_file':'unused-key','state_dir':directory,'tmux_target':'test:0.0','tmux_command':'claude'}
            config_file=Path(directory)/'config.json';config_file.write_text(json.dumps(config))
            script='''import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import body_vision as v
Original=v.Store
class Seeded(Original):
    def __init__(self,*a,**kw):
        super().__init__(*a,**kw)
        now=time.time()
        self.records=[{'at':now-7197,'time':v.beijing(now-7197),'description':'synthetic note'}]
        v.atomic_json(self.note_path,self.records,lines=True)
        self.frames['test-image']={'id':'test-image','captured_at':now-597,'expires_at':now+3,'raw':b'fake-jpeg'}
v.Store=Seeded
v.relay_frame=lambda config: {}
v.serve(json.loads(Path(sys.argv[2]).read_text()))
'''
            process=subprocess.Popen([sys.executable,'-B','-c',script,str(Path(__file__).parent),str(config_file)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+5
                while not (Path(directory)/'vision.sock').exists():
                    if process.poll() is not None:self.fail(process.stderr.read().decode())
                    self.assertLess(time.monotonic(),deadline);time.sleep(.02)
                self.assertIn(b'synthetic note',vision.client(config,'notebook'))
                self.assertEqual(vision.client(config,'image','test-image'),b'fake-jpeg')
                command=[sys.executable,'-B',str(Path(__file__).with_name('body_vision.py')),'--config',str(config_file),'images']
                result=subprocess.run(command,capture_output=True,timeout=5,check=True)
                self.assertEqual(json.loads(result.stdout)[0]['id'],'test-image')
                # Expiration runs independently of input frames/inference work.
                time.sleep(3.3)
                self.assertEqual(vision.client(config,'notebook'),b'')
                self.assertEqual(json.loads(vision.client(config,'images')),[])
                with self.assertRaises(ValueError):vision.client(config,'image','test-image')
                self.assertEqual((Path(directory)/'notebook.jsonl').read_text(),'')
                self.assertFalse(list(Path(directory).glob('*.jpg')))
            finally:
                process.terminate();process.wait(timeout=5)
                errors=process.stderr.read().decode();process.stderr.close()
                self.assertEqual(errors,'')


if __name__=='__main__':unittest.main()
