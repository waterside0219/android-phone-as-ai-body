import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from face_service import FaceService

class WatchTest(unittest.TestCase):
    def test_schedule_and_manual_close_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'privacy.json'
            face=FaceService(privacy_path=path)
            monday=datetime(2026,10,26,9,0).timestamp()
            with patch('face_service.time.time',return_value=monday):
                self.assertTrue(face.snapshot()['camera_allowed'])
                face.configure({'camera_enabled':False})
                self.assertFalse(FaceService(privacy_path=path).snapshot()['camera_allowed'])
                face.configure({'camera_enabled':True})
                face.close_eyes(1)
                self.assertFalse(face.snapshot()['camera_allowed'])
            with patch('face_service.time.time',return_value=datetime(2026,10,26,17,11).timestamp()):
                self.assertFalse(FaceService(privacy_path=path).snapshot()['camera_allowed'])

    def test_new_schedule_boundaries_and_saved_default_migration(self):
        cases = [(26,6,59,False),(26,7,0,True),(26,17,9,True),(26,17,10,False),
                 (26,18,0,True),(26,23,29,True),(26,23,30,False),
                 (31,8,59,False),(31,9,0,True),(31,23,59,True)]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'privacy.json'
            path.write_text(json.dumps({'closed_until':0,'camera_enabled':True,'manual_mute':True,
                'watch_periods':[{'days':'weekdays','start':'09:00','end':'19:00'},
                                 {'days':'daily','start':'20:00','end':'23:30'}]}))
            face=FaceService(privacy_path=path)
            for day,hour,minute,expected in cases:
                with self.subTest(day=day,hour=hour,minute=minute), patch('face_service.time.time',return_value=datetime(2026,10,day,hour,minute).timestamp()):
                    self.assertEqual(face.snapshot()['camera_allowed'],expected)
            self.assertTrue(FaceService(privacy_path=path).manual_mute)
            self.assertEqual(json.loads(path.read_text())['watch_periods'],face.watch_periods)

    def test_now_expires_at_next_period_end_and_privacy_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'privacy.json'
            with patch('face_service.time.time',return_value=datetime(2026,10,26,17,15).timestamp()):
                face=FaceService(privacy_path=path)
                face.configure({'camera_mode':'now'})
                self.assertTrue(face.snapshot()['camera_allowed'])
                self.assertEqual(FaceService(privacy_path=path).snapshot()['camera_mode'],'now')
                self.assertEqual(face.snapshot()['camera_override_until'],datetime(2026,10,26,23,30).timestamp())
                face.close_eyes(1)
                self.assertFalse(face.snapshot()['camera_allowed'])
                face.close_eyes(0)
                face.configure({'off_mode':True})
                self.assertFalse(face.snapshot()['camera_allowed'])
                face.configure({'off_mode':False})
            with patch('face_service.time.time',return_value=datetime(2026,10,26,23,30).timestamp()):
                self.assertFalse(face.snapshot()['camera_allowed'])
                self.assertEqual(face.snapshot()['camera_mode'],'auto')
            with patch('face_service.time.time',return_value=datetime(2026,10,27,6,30).timestamp()):
                face.configure({'camera_mode':'now'})
                face.configure({'camera_mode':'off'})
                self.assertFalse(face.snapshot()['camera_allowed'])
            with patch('face_service.time.time',return_value=datetime(2026,10,27,9,0).timestamp()):
                self.assertFalse(FaceService(privacy_path=path).snapshot()['camera_allowed'])
                face.configure({'camera_mode':'auto'})
                self.assertTrue(face.snapshot()['camera_allowed'])

    def test_weekend_end_midnight_and_next_end(self):
        face=FaceService()
        with patch('face_service.time.time',return_value=datetime(2026,10,31,8,30).timestamp()):
            face.configure({'camera_mode':'now'})
            self.assertEqual(face.snapshot()['camera_override_until'],datetime(2026,11,1,0,0).timestamp())
        with self.assertRaises(ValueError):
            face.configure({'watch_periods':[{'days':'weekends','start':'24:00','end':'24:00'}]})

    def test_camera_permission_and_person_are_not_stream_health(self):
        now=datetime(2026,10,26,10,0).timestamp();face=FaceService()
        with patch('face_service.time.time',side_effect=lambda:now):
            self.assertTrue(face.snapshot()['camera_allowed'])
            self.assertFalse(face.snapshot()['camera_active'])
            face.update_presence({'present':True,'face_present':False,'look':{'x':0,'y':0}})
            self.assertTrue(face.snapshot()['present']);self.assertFalse(face.snapshot()['face_present'])
            self.assertFalse(face.snapshot()['camera_active'],'presence is not camera health evidence')
            face.page_ready_at=now;face.page_display={'visible':True,'camera_stream_alive':True}
            self.assertTrue(face.snapshot()['camera_active'])
            face.page_display['visible']=False
            self.assertFalse(face.snapshot()['camera_active'],'hidden page cannot report active')
            face.page_display['visible']=True;face.camera_foreground=False
            self.assertFalse(face.snapshot()['camera_active'],'native foreground evidence also applies')
            face.camera_foreground=True;face.page_display['camera_stream_alive']=False
            self.assertFalse(face.snapshot()['camera_active'])
            self.assertTrue(face.snapshot()['present'],'loss of stream differs from loss of presence')
            face.page_display['camera_stream_alive']=True;now+=11
            self.assertFalse(face.snapshot()['camera_active'],'stale page report cannot prove a live stream')

    def test_presence_transitions_and_once_per_stay(self):
        events=[]; face=FaceService(lambda *event:events.append(event))
        now=datetime(2026,10,26,10,0).timestamp()
        def update(seconds,present,looking=False):
            with patch('face_service.time.time',return_value=now+seconds):
                return face.update_presence({'present':present,'looking':looking,'look':{'x':.2,'y':-.1}})
        update(0,True,True);update(1,True,True)
        update(2,False);update(121,False)
        self.assertEqual(events,[('presence',{'kind':'looking'})])
        update(122,False);update(302,True,True);update(303,True,True)
        self.assertEqual(events,[('presence',{'kind':'looking'}),('presence',{'kind':'leave'}),('presence',{'kind':'arrive'}),('presence',{'kind':'looking'})])
        face.configure({'camera_enabled':False})
        update(304,True,True)
        self.assertFalse(face.snapshot()['presence'])

    def test_failed_transitions_and_heat_retry_after_reconnect(self):
        delivered=[]; online=False
        def send(name,args):
            if not online:return False
            delivered.append((name,args));return True
        face=FaceService(send)
        now=datetime(2026,10,26,10,0).timestamp()
        for seconds,present,looking in [(0,True,True),(1,False,False),(121,False,False),(301,True,True)]:
            with patch('face_service.time.time',return_value=now+seconds):
                face.update_presence({'present':present,'looking':looking,'look':{'x':0,'y':0}})
        with patch('face_service.subprocess.run') as run:
            run.return_value.returncode=0;run.return_value.stdout=b'{"temperature":42}'
            face.temperature()
        online=True
        face.report_controls(force=True);face.report_controls()
        observed=[args['kind'] for name,args in delivered if name=='presence']
        self.assertEqual(observed,['looking','leave','arrive','looking'])
        self.assertEqual(sum(name=='temperature' for name,args in delivered),1)
        self.assertEqual(sum(name=='cooling_start' for name,args in delivered),1)

    def test_heat_event_once_until_cool(self):
        events=[]; face=FaceService(lambda *event:events.append(event))
        with patch('face_service.subprocess.run') as run:
            run.return_value.returncode=0
            for temperature in (41,42,39,41):
                run.return_value.stdout=('{"temperature":%s}'%temperature).encode()
                face.temperature()
        self.assertEqual(events,[('temperature',{'temperature_c':41.0}),
                                 ('cooling_start',{'temperature_c':42.0}),
                                 ('temperature',{'temperature_c':41.0})])
        self.assertTrue(face.thermal_hold,'39 degrees must not end the42/37 cooling cycle')

if __name__=='__main__': unittest.main()
