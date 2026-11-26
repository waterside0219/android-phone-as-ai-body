"""Camera recovery uses Shizuku availability and actual frame evidence."""
import base64
import unittest
from unittest.mock import Mock, patch

from agent import ActionError, Device
from face_service import FaceService


class CameraGuardTest(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.base = 1793008800.0
        clock = patch('agent.time.time', side_effect=lambda: self.base + self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.events = []
        self.face = FaceService(lambda *event: self.events.append(event))
        self.face.watch_periods = [{'days': 'daily', 'start': '00:00', 'end': '24:00'}]
        self.device = Device.__new__(Device)
        self.device.face = self.face
        self.device.brightness_status = {'face_foreground': False}
        self.available = True
        self.device.run = Mock(side_effect=self.probe)
        self.device.resume_face = Mock()

    def probe(self, *args, **kwargs):
        self.assertEqual(args, ('dumpsys', 'battery'))
        if not self.available:
            raise ActionError('Shizuku unavailable')
        return b'Current Battery Service state:'

    def tick(self, now):
        self.now = now
        self.device.check_shizuku(now)
        self.device.guard_camera(now)

    def frame(self):
        self.face.update_presence({'present': True, 'look': {'x': 0, 'y': 0}})
        self.face.store_camera_frame({
            'jpeg_base64': base64.b64encode(b'\xff\xd8test\xff\xd9').decode(),
            'dhash': '0' * 16, 'captured_at': self.base + self.now, 'reason': 'manual',
        })

    def event_names(self):
        return [name for name, _ in self.events]

    def test_ten_minutes_without_shizuku_has_one_event_and_no_retries(self):
        self.available = False
        for now in range(0, 601, 5):
            self.tick(now)
        self.device.resume_face.assert_not_called()
        self.assertEqual(self.event_names(), ['shizuku_lost'])
        self.assertEqual(self.device.run.call_count, 3, 'keep the 5-minute offline probe')

    def test_outage_between_periodic_probes_is_detected_before_restart(self):
        self.device.check_shizuku(0)
        self.available = False
        self.tick(5)
        self.device.resume_face.assert_not_called()
        self.assertEqual(self.event_names(), ['shizuku_lost'])

    def test_recovery_attempts_immediately_once_even_with_foreground_grace(self):
        self.available = False
        self.tick(0)
        self.device.resume_face.reset_mock()
        self.device.brightness_status['face_foreground'] = True
        self.available = True
        self.tick(300)
        self.tick(301)
        self.device.resume_face.assert_called_once_with(reload=True)
        self.assertEqual(self.event_names(), ['shizuku_lost', 'shizuku_back'])

    def test_recovery_attempts_once_even_if_old_page_claims_healthy(self):
        self.available = False
        self.tick(0)
        self.device.resume_face.reset_mock()
        self.available = True
        self.device.brightness_status['face_foreground'] = True
        self.face.page_display = {'visible': True, 'camera_stream_alive': True, 'detector_ready': True}
        for now in (300, 301, 305):
            self.face.page_ready_at = self.base + now
            self.tick(now)
        self.device.resume_face.assert_called_once_with(reload=True)

    def test_recovery_respects_camera_and_call_policy(self):
        for blocked in ('camera_enabled', 'off_mode', 'thermal_hold', 'native_call', 'closed', 'schedule'):
            with self.subTest(blocked=blocked):
                self.setUp()
                self.available = False
                self.tick(0)
                self.device.resume_face.reset_mock()
                if blocked == 'camera_enabled': self.face.camera_enabled = False
                elif blocked == 'native_call': self.device.native_call_active = True
                elif blocked == 'closed': self.face.closed_until = self.base + 1000
                elif blocked == 'schedule': self.face.watch_periods = []
                else: setattr(self.face, blocked, True)
                self.available = True
                self.tick(300)
                self.device.resume_face.assert_not_called()

    def test_new_frame_after_camera_reopens_cancels_loss_report(self):
        for now in (0, 20, 40): self.tick(now)
        self.assertEqual(self.device.resume_face.call_count, 3)
        generation = self.face.camera_generation
        self.now = 60
        self.face.clear_camera_frame()
        self.frame()
        self.assertNotEqual(generation, self.face.camera_generation)
        self.tick(60)
        self.assertNotIn('camera_lost', self.event_names())
        self.assertEqual(self.device.camera_retries, 0)

    def test_generation_change_without_a_frame_is_not_recovery(self):
        for now in (0, 20, 40): self.tick(now)
        self.now = 60
        self.face.clear_camera_frame()
        self.tick(60)
        self.assertEqual(self.event_names(), ['camera_lost'])

    def test_frame_from_before_loss_does_not_mask_stalled_camera(self):
        self.now = -1
        self.frame()
        for now in (0, 20, 40, 60):
            self.face.presence_at = self.base + now
            self.tick(now)
        self.assertEqual(self.event_names(), ['camera_lost'])

    def test_true_loss_keeps_three_attempts_and_ten_minute_backoff(self):
        for now in range(0, 801, 5): self.tick(now)
        self.assertEqual(self.device.resume_face.call_count, 6)
        self.assertEqual(self.event_names(), ['camera_lost', 'camera_lost'])

    def test_active_ai_call_is_resumed_without_reload(self):
        self.face.page_display = {'call': {'state': 'active'}}
        self.tick(0)
        self.device.resume_face.assert_called_once_with(reload=False)


if __name__ == '__main__':
    unittest.main()
