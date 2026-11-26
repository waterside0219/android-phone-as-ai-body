"""Synthetic cross-layer acceptance; no real camera or provider requests."""
import base64,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
import body_vision as vision
from face_service import FaceService
from agent import Device
JPEG=base64.b64encode(b'\xff\xd8synthetic\xff\xd9').decode()

class GestureRoundtripTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=1800000000.0
        self.store=vision.Store(self.tmp.name,clock=lambda:self.now)
        self.engine=vision.Engine(self.store,Mock(),Mock(),Mock(),clock=lambda:self.now)

    def test_existing_phone_action_to_provider_to_event_roundtrip(self):
        with patch('face_service.time.time',side_effect=lambda:self.now):
            face=FaceService();face.watch_periods=[{'days':'daily','start':'00:00','end':'24:00'}]
            device=Device.__new__(Device);device.face=face
            events=[];face._send_event=lambda *event:events.append(event) or True
            provider=vision.Provider('unused',self.store,clock=lambda:self.now)
            self.engine.classify=provider.classify_gesture
            self.engine.current=lambda:device.execute('camera_frame',{'mode':'metadata'})
            self.engine.gesture_result=lambda result:device.execute('camera_frame',{'gesture_result':result})
            for i,name in enumerate(('heart_two_hand','heart_finger','wave','thumb_up','victory','none')):
                self.now+=4;face.update_presence({'present':True,'look':{'x':0,'y':0}})
                face.store_camera_frame({'jpeg_base64':JPEG,'dhash':'0'*16,'captured_at':self.now,
                    'reason':'gesture_candidate','candidate_id':str(i)})
                provider.request=Mock(return_value={'choices':[{'message':{'content':json.dumps(
                    {'gesture':name,'doing':'holding_phone' if name=='none' else 'other','confidence':.9})}}]})
                self.assertEqual(self.engine.process_gesture(device.execute('camera_frame',{'mode':'gesture'})),'gesture_result')
                self.assertIsNone(face.gesture_frame)
            self.assertEqual(events,[('gesture',{'name':name}) for name in ('heart','heart','wave','thumb_up','victory')])
            self.assertEqual(self.store.notes(),[]);self.assertEqual(self.store.images(),[])

if __name__=='__main__':unittest.main()
