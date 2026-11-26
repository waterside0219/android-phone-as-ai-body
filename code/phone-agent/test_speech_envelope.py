import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch
from face_service import FaceService
from agent import AudioPlayer, ActionError

class SpeechEnvelopeTest(unittest.TestCase):
    def make(self):
        face=FaceService();face.configure({'muted':False});return face

    def test_track_clock_pause_resume_and_no_retained_audio(self):
        face=self.make();self.assertTrue(hasattr(face,'prepare_speech'),'native playback must publish its audio/clock')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.mp3';path.write_bytes(b'ID3-test')
            face.prepare_speech(str(path));sid=face.speech_snapshot()['id']
            self.assertEqual(face.speech_snapshot()['state'],'preparing')
            self.assertNotIn(directory,json.dumps(face.speech_snapshot()))
            self.assertEqual(face.speech_audio(sid),b'ID3-test')
            with patch('face_service.time.time',return_value=100):face.speech_started(100)
            with patch('face_service.time.time',return_value=100.4):face.speech_playing(False)
            self.assertAlmostEqual(face.speech_snapshot()['position'],.4)
            with patch('face_service.time.time',return_value=110):face.speech_playing(True)
            state=face.speech_snapshot();self.assertEqual(state['state'],'playing');self.assertEqual(state['clock_at'],110)
            face.finish_speech();self.assertIsNone(face.speech_snapshot());self.assertIsNone(face.speech_audio(sid))

    def test_native_playback_binds_begin_and_end_without_changing_commands(self):
        face=self.make();self.assertTrue(hasattr(face,'prepare_speech'))
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'say.mp3';path.write_bytes(b'ID3-test')
            player=object.__new__(AudioPlayer);player.face=face;calls=[];infos=[]
            def run(args,**kwargs):
                calls.append(args[:2])
                if args[1]=='play':
                    self.assertEqual(face.speech_snapshot()['state'],'preparing');return Mock(returncode=0)
                if args[1]=='info':
                    infos.append(face.speech_snapshot());return Mock(returncode=0,stdout=b'Status: Playing\n' if len(infos)==1 else b'No track currently!')
                return Mock(returncode=0)
            with patch('agent.subprocess.run',side_effect=run),patch('agent.time.sleep'):player._play(str(path))
            self.assertEqual(calls,[['termux-media-player','play'],['termux-media-player','info'],['termux-media-player','info']])
            self.assertEqual(infos[0]['state'],'playing');self.assertIsNone(face.speech_snapshot());self.assertFalse(face.snapshot()['speaking'])
            with patch('agent.subprocess.run',return_value=Mock(returncode=1)):
                with self.assertRaises(ActionError):player._play(str(path))
            self.assertIsNone(face.speech_snapshot())

    def test_http_stream_closes_mouth_and_scopes_audio_to_current_track(self):
        face=self.make();self.assertTrue(hasattr(face,'prepare_speech'));server=face.start(port=0);base='http://127.0.0.1:'+str(server.server_address[1])
        try:
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'audio.mp3';path.write_bytes(b'ID3-test');face.prepare_speech(str(path));sid=face.speech_snapshot()['id']
                with urllib.request.urlopen(base+'/api/speech-audio/'+sid) as response:self.assertEqual(response.read(),b'ID3-test')
                with urllib.request.urlopen(base+'/api/speech-events',timeout=2) as response:
                    first=json.loads(response.readline().decode().removeprefix('data: '));response.readline();self.assertEqual(first['id'],sid)
                    started=time.monotonic();face.finish_speech()
                    self.assertEqual(response.readline(),b'data: null\n');self.assertLess(time.monotonic()-started,.3)
                with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(base+'/api/speech-audio/'+sid)
                req=urllib.request.Request(base+'/api/speech-events',headers={'Origin':'https://unrelated.example'})
                with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(req)
                self.assertEqual(error.exception.code,403)
        finally:face.stop()

if __name__=='__main__':unittest.main()
