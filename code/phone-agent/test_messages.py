import tempfile
import json
import urllib.request
import unittest
from pathlib import Path
from unittest.mock import patch
from face_service import FaceService


class MessagesTest(unittest.TestCase):
    def test_call_lines_update_without_duplicate_or_external_send(self):
        events=[]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'privacy.json'
            face=FaceService(lambda *a:events.append(a), privacy_path=path)
            face.record_call_line('call:one','AI','一句')
            face.record_call_line('call:one','AI','一句完整的话')
            self.assertEqual(len(face.messages),1)
            self.assertEqual(face.messages[0]['text'],'一句完整的话')
            face=FaceService(privacy_path=path)
            face.record_call_line('call:one','AI','一句完整的话')
            face.record_call_line('call:two','我','回应')
            self.assertEqual(len(face.messages),2)
            self.assertEqual(events,[])
            with self.assertRaises(ValueError):face.record_call_line('x','第三人','字')

    def test_untrusted_web_origin_cannot_read_or_send_messages(self):
        face=FaceService();server=face.start(port=0)
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for method in ('GET','POST','OPTIONS'):
                req=urllib.request.Request('http://127.0.0.1:%s/api/messages'%server.server_port,
                    method=method,headers={'Origin':'https://untrusted.example','Content-Type':'application/json'},
                    data=b'{"text":"no"}' if method=='POST' else None)
                with self.assertRaises(urllib.error.HTTPError) as error:opener.open(req)
                self.assertEqual(error.exception.code,403)
            req=urllib.request.Request('http://127.0.0.1:%s/api/messages'%server.server_port,
                headers={'Origin':'http://127.0.0.1:8190'})
            with opener.open(req) as response:
                self.assertEqual(response.headers['Access-Control-Allow-Origin'],'http://127.0.0.1:8190')
        finally:face.stop()

    def test_storage_failure_never_sends_and_does_not_break_subtitles(self):
        events=[];face=FaceService(lambda *args:events.append(args))
        with patch.object(face,'_save_messages',side_effect=OSError('disk full')):
            for _ in range(2):
                with self.assertRaises((OSError,ValueError)):face.submit_message('只发一次')
            self.assertEqual(events,[])
            self.assertEqual(face.messages,[])
            face.set_subtitle('存盘失败也应继续显示与播放')
            self.assertEqual(face.subtitle,'存盘失败也应继续显示与播放')
            self.assertTrue(face.messages_snapshot()['error'])
        face=FaceService(lambda *a:False)
        with self.assertRaises(ValueError):face.submit_message('未送出')
        self.assertEqual(face.messages,[])

    def test_ready_reports_actual_page_layout(self):
        face=FaceService();server=face.start(port=0)
        try:
            display={'mode':'eyes','viewport':{'width':312,'height':536},'layout':{'caption':{'top':330},'forbidden':{'bottom':310}}}
            req=urllib.request.Request('http://127.0.0.1:%s/api/ready'%server.server_port,
                data=json.dumps({'version':face.release,'display':display}).encode(),headers={'Content-Type':'application/json'})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req) as response:
                self.assertEqual(response.status,200)
            self.assertEqual(face.snapshot()['page_display'],display)
        finally:face.stop()

    def test_history_bound_read_receipt_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'privacy.json'
            face = FaceService(privacy_path=path)
            for index in range(33):
                face.set_subtitle('第%s句话' % index)
            history = face.messages_snapshot()
            self.assertEqual(len(history['messages']), 30)
            self.assertEqual(history['messages'][0]['text'], '第3句话')
            self.assertEqual(history['unread'], 30)
            through = history['messages'][-1]['id']
            face.mark_messages_read(through)
            face.set_subtitle('稍后到的新话')
            face.mark_messages_read(through)
            restored = FaceService(privacy_path=path).messages_snapshot()
            self.assertEqual(restored['unread'], 1, 'late new message must remain unread')
            self.assertEqual(restored['messages'][-1]['text'], '稍后到的新话')

    def test_reply_works_in_full_off_and_rejects_oversize_or_offline(self):
        events = []
        face = FaceService(lambda name,args: events.append((name,args)))
        face.configure({'off_mode': True})
        face.submit_message('我在这里')
        self.assertEqual(events[-1], ('message', {'text':'我在这里'}))
        self.assertEqual(face.messages_snapshot()['messages'][-1]['speaker'], '我')
        self.assertEqual(face.messages_snapshot()['unread'], 0)
        with self.assertRaises(ValueError):
            face.submit_message('字' * 501)
        with self.assertRaises(ValueError):
            FaceService(lambda *a: False).submit_message('不可假装送出')

    def test_subtitle_expires_after_five_seconds_history_remains(self):
        face = FaceService()
        with patch('face_service.time.time', return_value=1000):
            face.set_subtitle('好')
            self.assertEqual(face.snapshot()['subtitle_until'], 1005)
            face.set_subtitle('长' * 100)
            self.assertEqual(face.snapshot()['subtitle_until'], 1005)
        with patch('face_service.time.time', return_value=2000):
            self.assertEqual(face.snapshot()['subtitle'], '长' * 100)


if __name__ == '__main__':
    unittest.main()
