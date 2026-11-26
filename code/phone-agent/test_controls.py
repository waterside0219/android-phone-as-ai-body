import unittest,tempfile,json,time,types
from pathlib import Path
from unittest.mock import patch,Mock
from datetime import datetime
from face_service import FaceService
from agent import AudioPlayer, EventSender, serve

class ControlsTest(unittest.TestCase):
    def test_silent_period_is_configurable_and_persists(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'privacy.json'
            face=FaceService(privacy_path=path)
            period={'days':'weekdays','start':'08:30','end':'17:30'}
            face.configure({'work_silent_period':period})
            restored=FaceService(privacy_path=path)
            self.assertEqual(restored.snapshot()['work_silent_period'],period)
            from datetime import datetime,timezone,timedelta
            def at(hour,minute):return datetime(2026,10,26,hour,minute,tzinfo=timezone(timedelta(hours=8))).timestamp()
            self.assertFalse(restored.work_silent(at(8,29)))
            self.assertTrue(restored.work_silent(at(8,30)))
            self.assertFalse(restored.work_silent(at(17,30)))
            with self.assertRaises(ValueError):restored.configure({'work_silent_period':{'days':'weekdays','start':'18:00','end':'08:00'}})

    def test_work_silence_boundaries(self):
        face=FaceService()
        for day,hour,minute,expected in [(26,7,59,False),(26,8,0,True),(26,17,59,True),(26,18,0,False),(31,10,0,False)]:
            with self.subTest(day=day,hour=hour,minute=minute),patch('face_service.time.time',return_value=datetime(2026,10,day,hour,minute).timestamp()):
                self.assertEqual(face.is_muted(),expected)

    def test_workday_mute_manual_today_and_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'privacy.json'
            monday=datetime(2026,10,26,10,0).timestamp()
            with patch('face_service.time.time',return_value=monday):
                face=FaceService(privacy_path=path)
                self.assertTrue(face.snapshot().get('muted'))
                face.configure({'muted':False})
                self.assertFalse(FaceService(privacy_path=path).snapshot()['muted'])
            with patch('face_service.time.time',return_value=monday+86400):
                self.assertTrue(FaceService(privacy_path=path).snapshot()['muted'])
            with patch('face_service.time.time',return_value=datetime(2026,10,26,18,30).timestamp()):
                self.assertFalse(FaceService(privacy_path=path).snapshot()['muted'])

    def test_all_off_restores_prior_camera_and_sound(self):
        with patch('face_service.time.time',return_value=datetime(2026,10,26,10,0).timestamp()):
            events=[];face=FaceService(lambda *event:events.append(event))
            face.configure({'camera_enabled':True,'muted':False})
            face.configure({'off_mode':True})
            state=face.snapshot()
            self.assertFalse(state['camera_allowed']);self.assertFalse(state['camera_active']);self.assertTrue(state['muted']);self.assertFalse(state['proactive_enabled'])
            face.configure({'off_mode':False})
            self.assertTrue(face.snapshot()['camera_allowed']);self.assertFalse(face.snapshot()['muted'])
            self.assertFalse(face.snapshot()['camera_active'],'restoring permission does not prove camera frames')
            face.page_ready_at=time.time();face.page_display={'visible':True,'camera_stream_alive':True}
            self.assertTrue(face.snapshot()['camera_active'],'fresh visible stream confirms active camera')
            self.assertTrue(any(e[0]=='controls' for e in events))

    def test_reconnect_reports_full_off_to_new_relay(self):
        events=EventSender();face=FaceService(events.send)
        face.configure({'off_mode':True})
        device=types.SimpleNamespace(face=face,update_night_brightness=lambda:None)
        for _ in range(2):
            messages=[]
            ws=Mock(last_received=time.monotonic())
            ws.send.side_effect=lambda opcode,payload:messages.append(json.loads(payload)) if opcode==1 else None
            ws.receive.side_effect=ConnectionError('simulated disconnect')
            with patch('agent.connect',return_value=ws),patch('builtins.print'):
                with self.assertRaises(ConnectionError):serve({},device,events)
            self.assertEqual(len(messages),1,'each connection must receive current controls')
            self.assertEqual(messages[0]['event'],'controls')
            self.assertTrue(messages[0]['args']['off_mode'])
            self.assertFalse(messages[0]['args']['proactive_enabled'])

    def test_muted_player_never_starts_and_midplay_mute_stops(self):
        face=FaceService();face.configure({'muted':True})
        player=object.__new__(AudioPlayer);player.face=face
        with patch('agent.subprocess.run') as run:
            player._play('/test/audio.mp3');run.assert_not_called()
        face.configure({'muted':False});calls=[]
        def run(args,**kwargs):
            calls.append(args[1])
            if args[1]=='info':face.configure({'muted':True});return Mock(returncode=0,stdout=b'Status: Playing\n')
            return Mock(returncode=0)
        with patch('agent.subprocess.run',side_effect=run),patch('agent.time.sleep'):
            player._play('/test/audio.mp3')
        self.assertIn('stop',calls)
        self.assertFalse(face.snapshot()['speaking'])
if __name__=='__main__':unittest.main()
