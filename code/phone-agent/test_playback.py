import unittest
from unittest.mock import patch,Mock
from agent import AudioPlayer
from face_service import FaceService

class PlaybackTest(unittest.TestCase):
    def test_mouth_tracks_confirmed_playback_not_subtitle(self):
        face=FaceService();face.configure({'muted':False});face.set_subtitle('字幕留着时，声音可能已经停了')
        self.assertFalse(face.snapshot().get('speaking',False))
        player=object.__new__(AudioPlayer);player.face=face
        observed=[]
        def run(args,**kwargs):
            if args[1]=='play':return Mock(returncode=0)
            if args[1]=='info':
                observed.append(face.snapshot().get('speaking',False))
                return Mock(returncode=0,stdout=b'Status: Playing\nTrack: test.mp3\nCurrent Position: 00:00 / 00:07\n' if len(observed)==1 else b'No track currently!')
            return Mock(returncode=0)
        with patch('agent.subprocess.run',side_effect=run),patch('agent.time.sleep'):
            player._play('/test/audio.mp3')
        self.assertEqual(observed,[False,True])
        self.assertFalse(face.snapshot()['speaking'])
        self.assertTrue(face.snapshot()['subtitle'])
if __name__=='__main__':unittest.main()
