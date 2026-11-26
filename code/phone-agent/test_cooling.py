import json
import tempfile
import types
import unittest
import time
from pathlib import Path
from unittest.mock import patch
from agent import Device
from face_service import FaceService


class CoolingTest(unittest.TestCase):
    def test_cooling_and_restore_continue_without_relay_connection(self):
        device=Device.__new__(Device);calls=[]
        device.face=types.SimpleNamespace(agent_connected=False,temperature=lambda:calls.append('temperature'))
        device.update_night_brightness=lambda:calls.append('brightness')
        device.run=lambda *a,**kw:b'unavailable'
        stop=types.SimpleNamespace(is_set=lambda:len(calls)>=3,wait=lambda seconds:None)
        device.maintain_device(stop)
        self.assertEqual(calls,['brightness','temperature','brightness'])

    def test_hot_camera_stops_until_cool_without_changing_user_choice(self):
        face=FaceService();face.watch_periods=[{'days':'daily','start':'00:00','end':'24:00'}]
        now=1793008800.
        with patch('face_service.time.time',side_effect=lambda:now),patch('face_service.subprocess.run') as run:
            run.return_value.returncode=0
            for value,allowed in [(41.9,True),(42,False),(41.5,False),(40,False),(37.1,False),(37,True)]:
                now+=10
                run.return_value.stdout=json.dumps({'temperature':value}).encode()
                face.temperature()
                self.assertEqual(face.snapshot()['camera_allowed'],allowed)
                self.assertFalse(face.snapshot()['camera_active'],'no stream has reported frames')
                self.assertEqual(face.snapshot()['temperature_c'],value)
                self.assertTrue(face.camera_enabled)
            self.assertTrue(face.thermal_hold,'37 degrees permits restart, but does not prove recovery')
            face.update_camera_status({'visible':True,'camera_stream_alive':True,'detector_ready':True,'camera_started_at':now-60})
            self.assertTrue(face.thermal_hold,'old stream cannot finish recovery')
            face.update_camera_status({'visible':True,'camera_stream_alive':False,'detector_ready':True,'camera_started_at':now})
            self.assertTrue(face.thermal_hold,'new stream must actually produce frames')
            face.update_camera_status({'visible':True,'camera_stream_alive':True,'detector_ready':False,'camera_started_at':now})
            self.assertTrue(face.thermal_hold,'detector must also be ready')
            face.update_camera_status({'visible':True,'camera_stream_alive':True,'detector_ready':True,'camera_started_at':now})
            self.assertFalse(face.thermal_hold)
            face.configure({'camera_mode':'off'})
            run.return_value.stdout=b'{"temperature":39}'
            face.temperature()
            self.assertFalse(face.snapshot()['camera_allowed'])
            self.assertFalse(face.snapshot()['camera_active'])

    def test_brightness_is_scoped_to_face_and_restored_after_other_app(self):
        with tempfile.TemporaryDirectory() as directory:
            values={'screen_brightness':'120','screen_brightness_mode':'1'}
            foreground=['com.android.chrome/org.chromium.chrome.browser.webapps.WebappActivity']
            folded=['TENT']
            def run(*args,**kwargs):
                if args[:2]==('dumpsys','activity'):return ('mResumedActivity: ActivityRecord{a u0 '+foreground[0]+' t1}').encode()
                if args[:2]==('dumpsys','device_state'):return ("mCommittedState=Optional[DeviceState{identifier=0, name='%s'}]"%(folded[0] or 'OPENED')).encode()
                if args[:2]==('settings','get'):return values[args[3]].encode()
                if args[:2]==('settings','put'):values[args[3]]=args[4];return b''
                self.fail(str(args))
            device=Device.__new__(Device);device.run=run;device.night_applied=None
            device.face=types.SimpleNamespace(page_display={'visible':True},page_ready_at=time.time())
            device.brightness_restore_path=Path(directory)/'restore.json'
            with patch('agent.time.localtime',return_value=types.SimpleNamespace(tm_hour=12,tm_wday=0)):
                device.update_night_brightness()
                self.assertEqual(values,{'screen_brightness':'30','screen_brightness_mode':'0'})
                device.face.page_display={'visible':False}
                device.update_night_brightness()
                self.assertEqual(values['screen_brightness'],'120','another PWA is not the face')
                device.face.page_display={'visible':True}
                device.update_night_brightness()
                device.face.page_display['rest_stage']='dim'
                device.update_night_brightness()
                self.assertEqual(values['screen_brightness'],'0','10-minute absence uses the same native brightness owner')
                device.face.page_display['rest_stage']='active'
                device.update_night_brightness()
                self.assertEqual(values['screen_brightness'],'30','return restores weekday work cap')
                foreground[0]='com.tencent.mm/.ui.LauncherUI'
                device.update_night_brightness()
                self.assertEqual(values,{'screen_brightness':'120','screen_brightness_mode':'1'})
                foreground[0]='com.android.chrome/org.chromium.chrome.browser.webapps.WebappActivity'
                folded[0]=False;device.update_night_brightness()
                self.assertEqual(values['screen_brightness'],'30')
                folded[0]='CLOSED';device.update_night_brightness()
                self.assertEqual(values['screen_brightness'],'30')
            for hour,weekday in ((18,0),(12,5)):
                with patch('agent.time.localtime',return_value=types.SimpleNamespace(tm_hour=hour,tm_wday=weekday)):
                    device.update_night_brightness()
                    self.assertEqual(values['screen_brightness'],'60','non-work daytime cap stays60')
            with patch('agent.time.localtime',return_value=types.SimpleNamespace(tm_hour=23,tm_wday=0)):
                device.update_night_brightness()
                self.assertEqual(values['screen_brightness'],'0')
                foreground[0]='com.tencent.mm/.ui.LauncherUI'
                device.update_night_brightness()
                self.assertEqual(values['screen_brightness'],'120')
