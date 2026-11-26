"""K: relay look uses a new frame, independently of schedule, presence and AI calls."""
import base64
import concurrent.futures
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

from agent import ActionError, Device
from face_service import FaceService

JPEG = base64.b64encode(b'\xff\xd8look-test\xff\xd9').decode()


class CameraLookTest(unittest.TestCase):
    def setUp(self):
        self.started = time.monotonic()
        self.night = datetime(2026, 10, 29, 23, 40, tzinfo=timezone(timedelta(hours=8))).timestamp()
        clock = patch('face_service.time.time', side_effect=lambda: self.night + time.monotonic() - self.started)
        clock.start()
        self.addCleanup(clock.stop)
        self.face = FaceService()
        self.device = Device.__new__(Device)
        self.device.face = self.face
        self.pool = concurrent.futures.ThreadPoolExecutor(1)
        self.addCleanup(self.pool.shutdown)

    def begin(self):
        future = self.pool.submit(self.device.execute, 'camera_frame', {'mode': 'look'})
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not future.done():
            if self.face.snapshot().get('camera_look_until', 0) > time.time():
                return future
            time.sleep(.005)
        error = future.exception() if future.done() else None
        self.fail(f'look did not open the capture window: {error}')

    def upload(self, captured=None):
        return self.face.store_camera_frame({'jpeg_base64': JPEG, 'dhash': '0'*16,
            'captured_at': time.time() if captured is None else captured, 'reason': 'look'})

    def test_2340_idle_and_all_ai_call_states_get_frame_then_restore_schedule(self):
        periods = [dict(p) for p in self.face.watch_periods]
        for state in ('idle', 'connecting', 'connected', 'recovering'):
            with self.subTest(state=state):
                self.face.page_display = {'visible': True, 'call': {'state': state}}
                self.assertFalse(self.face.camera_allowed(time.time()))
                future = self.begin()
                snapshot = self.face.snapshot()
                self.assertTrue(snapshot['camera_allowed'])
                self.assertEqual(snapshot['camera_mode'], 'auto')
                self.assertLessEqual(snapshot['camera_look_until'] - time.time(), 60)
                self.assertFalse(future.done(), 'cached metadata cannot complete look')
                self.assertEqual(self.upload().get('selected'), 'look')
                result = future.result(1)
                self.assertEqual(result['jpeg_base64'], JPEG)
                self.assertLessEqual(time.time() - result['captured_at'], 8)
                self.assertFalse(self.face.camera_allowed(time.time()))
                self.assertEqual(self.face.camera_frame(), {})
                self.assertEqual(self.face.watch_periods, periods)
                self.assertEqual(self.face.camera_override_until, 0)
        self.face.page_display['call']['state'] = 'idle'
        self.assertFalse(self.face.snapshot()['camera_allowed'], 'hangup returns to the same timetable')

    def test_only_four_policy_blocks_are_reported_as_errors(self):
        for field, value, reason in (('camera_enabled', False, 'camera_off_by_user'),
                ('off_mode', True, 'off_mode'), ('thermal_hold', True, 'thermal'),
                ('phone_call_active', True, 'native_call')):
            with self.subTest(reason=reason):
                previous = getattr(self.face, field)
                setattr(self.face, field, value)
                with self.assertRaisesRegex(ActionError, '^'+reason+'$'):
                    self.device.execute('camera_frame', {'mode': 'look'})
                self.assertFalse(self.face.snapshot().get('camera_look_until'))
                setattr(self.face, field, previous)
        self.device.native_call_active = True
        with self.assertRaisesRegex(ActionError, '^native_call$'):
            self.device.execute('camera_frame', {'mode': 'look'})

    def test_closed_eyes_and_absence_do_not_block_explicit_look(self):
        self.face.closed_until = self.night + 3600
        future = self.begin()
        self.face.update_presence({'present': False, 'look': {'x': 0, 'y': 0}})
        self.upload()
        self.assertEqual(future.result(1)['jpeg_base64'], JPEG)
        self.assertEqual(self.face.closed_until, self.night + 3600)
        self.assertFalse(self.face.camera_allowed(time.time()))

    def test_old_frame_is_rejected_and_privacy_change_cancels_wait(self):
        future = self.begin()
        self.assertIn('ignored', self.upload(self.night-9))
        self.assertFalse(future.done())
        self.face.configure({'camera_mode': 'off'})
        with self.assertRaisesRegex(ActionError, '^camera_off_by_user$'):
            future.result(1)
        self.assertFalse(self.face.snapshot().get('camera_look_until'))

    def test_timeout_is_not_an_empty_success_and_drops_permission(self):
        # Speed up only the failure path; the public deadline remains <=8s.
        with patch('face_service.LOOK_WAIT_SECONDS', .03, create=True):
            with self.assertRaisesRegex(ActionError, '^camera_frame_timeout$'):
                self.device.execute('camera_frame', {'mode': 'look'})
        self.assertFalse(self.face.snapshot().get('camera_look_until'))
        self.assertFalse(self.face.camera_allowed(time.time()))

    def test_look_does_not_modify_existing_manual_override_or_schedule(self):
        self.face.camera_override_until = self.night + 300
        future = self.begin()
        self.upload()
        future.result(1)
        self.assertEqual(self.face.camera_override_until, self.night + 300)
        self.assertTrue(self.face.camera_allowed(time.time()))

    def test_guard_during_ai_calls_never_reloads(self):
        for state in ('connecting', 'connected', 'recovering'):
            with self.subTest(state=state):
                future = self.begin()
                self.face.page_display = {'visible': True, 'call': {'state': state}}
                self.device.brightness_status = {'face_foreground': False}
                self.device.shizuku_available = True
                self.device.check_shizuku = Mock()
                self.device.resume_face = Mock()
                self.device.camera_lost_since = None
                self.device.guard_camera(time.monotonic())
                self.device.resume_face.assert_called_once_with(reload=False)
                self.upload()
                future.result(1)


if __name__ == '__main__':
    unittest.main()
