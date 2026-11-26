"""Phone-only frame selection and privacy: synthetic bytes, no camera or network."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from face_service import FaceService
from agent import Device, ACTIONS

JPEG = base64.b64encode(b'\xff\xd8synthetic-test\xff\xd9').decode()


class CameraFrameTest(unittest.TestCase):
    def setUp(self):
        self.now = 1793008800.0
        self.clock = patch('face_service.time.time', side_effect=lambda: self.now)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.face = FaceService()
        self.face.watch_periods = [{'days': 'daily', 'start': '00:00', 'end': '24:00'}]
        self.assertTrue(hasattr(self.face, 'store_camera_frame'), 'phone has no memory frame handoff')
        self.present(True)

    def present(self, value):
        return self.face.update_presence({'present': value, 'look': {'x': 0, 'y': 0}})

    def frame(self, hash='0' * 16, reason='auto'):
        self.present(True)
        return self.face.store_camera_frame({'jpeg_base64': JPEG, 'dhash': hash,
                        'captured_at': self.now, 'reason': reason})

    def test_one_minute_changed_or_idle_and_return(self):
        self.frame()
        self.assertEqual(self.face.camera_frame()['reason'], 'changed')
        self.now += 30; self.frame('f' * 16)
        self.assertEqual(self.face.camera_frame()['dhash'], '0' * 16, 'automatic capture is at most once/minute')
        self.now += 30; self.frame()
        self.assertEqual(self.face.camera_frame()['reason'], 'periodic')
        self.now += 60; self.frame()
        self.assertEqual(self.face.camera_frame()['reason'], 'periodic', 'unchanged presence still selects each minute')
        self.now += 60; self.frame('f' * 16)
        self.assertEqual(self.face.camera_frame()['reason'], 'changed')
        self.now += 1; self.present(False)
        self.assertNotIn('jpeg_base64', self.face.camera_frame())
        self.now += 1201; self.present(False)
        self.assertGreaterEqual(self.face.camera_frame()['absent_ms'], 1200000)
        self.present(True); self.frame('f' * 16)
        self.assertEqual(self.face.camera_frame()['reason'], 'returned')
        self.assertGreaterEqual(self.face.camera_frame()['absent_ms'], 1200000)

    def test_companion_one_minute_changed_or_idle_manual_bypasses_cadence(self):
        self.face.configure({'companion_hours': 1})
        self.frame(); self.assertEqual(self.face.camera_frame()['reason'], 'companion')
        self.now += 60; self.frame('f' * 16)
        self.assertEqual(self.face.camera_frame()['reason'], 'companion')
        self.now += 59
        self.assertEqual(self.frame('f' * 16)['ignored'], 'capture_interval')
        self.now += 1; self.frame('f' * 16)
        self.assertEqual(self.face.camera_frame()['reason'], 'periodic', 'companion also selects unchanged presence each minute')
        self.now += 1; self.frame('f' * 16, 'manual')
        self.assertEqual(self.face.camera_frame()['reason'], 'manual')
        self.now += 3601
        self.assertEqual(self.face.snapshot()['companion_until'], 0)

    def test_minute_capture_survives_upload_latency_jitter(self):
        start = self.now
        for seconds, latency in ((0, .1), (60, .05), (120, .05)):
            with self.subTest(seconds=seconds):
                captured = start + seconds; self.now = captured + latency
                self.present(True)
                result = self.face.store_camera_frame({'jpeg_base64': JPEG, 'dhash': '0' * 16,
                                      'captured_at': captured, 'reason': 'auto'})
                self.assertEqual(result['selected'], 'changed' if seconds == 0 else 'periodic')

    def test_privacy_closes_memory_and_rejects_late_frames(self):
        for close in (lambda: self.face.close_eyes(1),
                      lambda: self.face.configure({'camera_mode': 'off'}),
                      lambda: self.face.configure({'off_mode': True}),
                      lambda: setattr(self.face, 'thermal_hold', True),
                      lambda: setattr(self.face, 'watch_periods', [{'days': 'daily', 'start': '00:00', 'end': '00:01'}])):
            self.setUpFace()
            self.frame(); close()
            self.assertEqual(self.face.camera_frame(), {})
            self.frame(reason='manual')
            self.assertEqual(self.face.camera_frame(), {})
            self.assertIsNone(self.face.latest_frame)
        self.setUpFace(); self.frame(); self.now += 91
        self.assertNotIn('jpeg_base64', self.face.camera_frame(), 'stale frame cannot be used')

    def setUpFace(self):
        self.face = FaceService()
        self.face.watch_periods = [{'days': 'daily', 'start': '00:00', 'end': '24:00'}]
        self.present(True)

    def test_settings_persist_and_frames_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'privacy.json'
            self.face.privacy_path = path
            self.face.configure({'notify_sadness': False, 'companion_hours': 2})
            self.frame(reason='manual')
            saved = path.read_text()
            self.assertNotIn(JPEG, saved)
            restored = FaceService(privacy_path=path)
            self.assertFalse(restored.snapshot()['notify_sadness'])
            self.assertEqual(restored.snapshot()['companion_until'], self.now + 7200)
            self.assertIsNone(restored.latest_frame)

    def test_stationary_timer_resets_on_absence_or_changed_scene(self):
        self.frame()
        self.now += 3660; self.frame()
        self.assertGreaterEqual(self.face.camera_frame()['stationary_ms'], 3600000)
        self.now += 60; self.frame('f' * 16)
        self.assertEqual(self.face.camera_frame()['stationary_ms'], 0)
        self.present(False); self.now += 60; self.frame('f' * 16)
        self.assertEqual(self.face.camera_frame()['stationary_ms'], 0)

    def test_action_and_input_boundaries(self):
        self.assertIn('camera_frame', ACTIONS)
        device = Device.__new__(Device); device.face = self.face
        self.frame()
        self.assertEqual(device.execute('camera_frame', {})['jpeg_base64'], JPEG)
        for update in ({'dhash': 'bad'}, {'captured_at': float('nan')},
                       {'jpeg_base64': 'a' * 800000}, {'reason': 'arbitrary'},
                       {'captured_at': self.now - 120}, {'captured_at': self.now + 10}):
            body = {'jpeg_base64': JPEG, 'dhash': '0' * 16, 'captured_at': self.now, 'reason': 'auto', **update}
            with self.assertRaises(ValueError):self.face.store_camera_frame(body)

    def test_stalled_detection_does_not_claim_long_absence(self):
        self.frame(); self.now+=1; self.present(False);self.now+=1300
        self.assertEqual(self.face.camera_frame(), {}, 'a stalled browser is not a measured absence')
        self.present(True);self.frame()
        self.assertEqual(self.face.camera_frame()['returned_at'],0, 'resuming after a detector outage is not a measured return')

    def test_controls_heartbeat_clears_stalled_camera_even_without_vision_poll(self):
        self.frame(); self.now+=11;self.face.snapshot()
        self.assertIsNone(self.face.latest_frame)

    def candidate(self, identifier='c1'):
        self.present(True)
        return self.face.store_camera_frame({'jpeg_base64': JPEG, 'dhash': '0'*16,
            'captured_at': self.now, 'reason': 'gesture_candidate', 'candidate_id': identifier})

    def test_gesture_roundtrip_uses_existing_action_and_drops_pixels(self):
        self.frame(reason='manual')
        self.assertEqual(self.candidate()['selected'], 'gesture_candidate')
        device=Device.__new__(Device);device.face=self.face
        frame=device.execute('camera_frame', {'mode':'gesture'})
        self.assertEqual(frame['candidate_id'],'c1')
        result={k:frame[k] for k in ('candidate_id','camera_generation','captured_at')}
        result.update(gesture='heart_two_hand',doing='other',confidence=.9)
        sent=[];self.face._send_event=lambda *args:sent.append(args) or True
        self.assertTrue(device.execute('camera_frame',{'gesture_result':result})['event_sent'])
        self.assertEqual(sent,[('gesture',{'name':'heart'})])
        self.assertNotIn('jpeg_base64',device.execute('camera_frame',{'mode':'gesture'}))
        self.assertEqual(device.execute('camera_frame',{'mode':'ordinary'})['reason'],'manual')
        self.assertEqual(self.face.snapshot()['gesture_result']['candidate_id'],'c1')
        device.execute('camera_frame',{'gesture_result':result})
        self.assertEqual(len(sent),1)

    def test_gesture_none_privacy_generation_and_activity_gates(self):
        for change in ('none','typing','holding_phone','eating','low','closed','restart','hot','stale'):
            with self.subTest(change=change):
                self.setUpFace();self.candidate()
                frame=self.face.camera_frame('gesture')
                reply={k:frame[k] for k in ('candidate_id','camera_generation','captured_at')}
                reply.update(gesture='wave',doing='other',confidence=.9)
                if change=='none':reply['gesture']='none'
                elif change in ('typing','holding_phone','eating'):reply['doing']=change
                elif change=='low':reply['confidence']=.69
                elif change=='closed':self.face.configure({'off_mode':True})
                elif change=='restart':self.face.clear_camera_frame()
                elif change=='hot':self.face.temperature_c=42
                elif change=='stale':self.now+=11;self.present(True)
                self.face._send_event=Mock()
                self.face.receive_gesture_result(reply)
                self.face._send_event.assert_not_called()
                self.assertNotIn('jpeg_base64',self.face.camera_frame('gesture'))

    def test_gesture_four_second_cooldown_uses_capture_time_not_upload_jitter(self):
        start=self.now
        for delta,latency in ((0,.1),(4,.05)):
            self.now=start+delta+latency;self.present(True)
            result=self.face.store_camera_frame({'jpeg_base64':JPEG,'dhash':'0'*16,
                'captured_at':start+delta,'reason':'gesture_candidate','candidate_id':str(delta)})
            self.assertEqual(result.get('selected'),'gesture_candidate')

    def test_gesture_failure_ack_discards_frame_without_fabricating_none(self):
        self.candidate()
        frame=self.face.camera_frame('gesture')
        result={k:frame[k] for k in ('candidate_id','camera_generation','captured_at')}
        result['error']='classification_failed'
        self.face._send_event=Mock()
        self.face.receive_gesture_result(result)
        self.assertIsNone(self.face.gesture_frame)
        self.assertIsNone(self.face.snapshot()['gesture_result']['gesture'])
        self.face._send_event.assert_not_called()

    def test_gesture_limits_survive_privacy_toggle_and_do_not_delay_auto_capture(self):
        for i in range(6):
            self.now+=4;self.candidate(str(i))
        self.now+=4
        self.assertEqual(self.candidate('seventh')['ignored'],'gesture_minute_limit')
        self.face.clear_camera_frame();self.present(True)
        self.assertEqual(self.candidate('after-reset')['ignored'],'gesture_minute_limit')
        self.assertIsNotNone(self.frame()['selected'])
        for close in ('camera','off','heat'):
            self.setUpFace()
            if close=='camera':self.face.configure({'camera_mode':'off'})
            elif close=='off':self.face.configure({'off_mode':True})
            else:self.face.temperature_c=42
            self.assertIn('ignored',self.candidate())


if __name__ == '__main__':unittest.main()
