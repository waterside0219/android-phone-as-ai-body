import tempfile
import time
import unittest
from pathlib import Path
from face_service import FaceService

class FacePrivacyTest(unittest.TestCase):
    def test_closed_eyes_reject_late_presence_and_gesture(self):
        events=[]
        face=FaceService(lambda *event: events.append(event))
        face.close_eyes(1)
        face.update_presence({'present': True, 'look': {'x': .3, 'y': .2}, 'gesture': 'wave'})
        self.assertFalse(face.snapshot()['presence'])
        self.assertEqual(events, [])

    def test_closure_survives_restart_and_explicit_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'privacy.json'
            first=FaceService(privacy_path=path)
            until=first.close_eyes(4)['closed_until']
            restored=FaceService(privacy_path=path)
            self.assertEqual(restored.snapshot()['closed_until'],until)
            self.assertGreater(until,time.time())
            restored.close_eyes(0)
            self.assertEqual(FaceService(privacy_path=path).snapshot()['closed_until'],0)

if __name__=='__main__': unittest.main()
