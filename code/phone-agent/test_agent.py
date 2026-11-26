import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path
from agent import Device, ActionError, ADB_IME

class DeviceTest(unittest.TestCase):
    def test_text_only_say_is_a_caption_without_audio_queue(self):
        from agent import valid_action_args
        from face_service import FaceService
        device = Device.__new__(Device)
        device.face = FaceService()
        from unittest.mock import Mock
        device.audio = Mock()
        self.assertTrue(valid_action_args('say', {'text': '静音字幕'}))
        result = device.execute('say', {'text': '静音字幕'})
        self.assertTrue(result['queued'])
        self.assertEqual(device.face.snapshot()['subtitle'], '静音字幕')
        self.assertEqual(device.audio.mock_calls, [])
        for args in ({'text': ''}, {'text': ' '}, {'text': 'x', 'extra': True}):
            with self.assertRaises(ActionError):device.execute('say', args)

    def test_android_stderr_is_filtered_before_rish_transport(self):
        import threading,os
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory)/'dumpsys';script.write_text('#!/bin/sh\nprintf "mCallState=0\\n" >&2\n');script.chmod(0o700)
            device=Device.__new__(Device);device.command_lock=threading.Lock();device.android_reads={};device.rish='/bin/sh';device.env={**os.environ,'PATH':directory+':/usr/bin:/bin'}
            self.assertIn(b'mCallState=0',device.run('dumpsys','telephony.registry'))
            script.write_text('#!/bin/sh\nprintf \"mCallState=0\\n\"\nexit 7\n')
            with self.assertRaises(ActionError):device.run('dumpsys','telephony.registry')

    def test_readonly_android_queries_keep_success_output_from_both_streams(self):
        import threading
        device=Device.__new__(Device);device.command_lock=threading.Lock();device.android_reads={};device.rish='rish';device.env={}
        completed=types.SimpleNamespace(returncode=0,stdout=b'header\n',stderr=b'mCallState=0\n')
        with patch('agent.subprocess.run',return_value=completed):
            self.assertIn(b'mCallState=0',device.run('dumpsys','telephony.registry'))
        completed.stdout=b'';completed.stderr=b'com.example.keyboard/.IME\n'
        with patch('agent.subprocess.run',return_value=completed):
            self.assertEqual(device.current_input_method(),'com.example.keyboard/.IME')
        self.assertNotIn('stderr',device.android_reads['dumpsys telephony.registry'])

    def test_update_status_keeps_release_evidence_when_android_query_fails(self):
        device = Device.__new__(Device)
        device.updater = types.SimpleNamespace(status=lambda: {'state':'installed','current_version':'test'})
        device.face = types.SimpleNamespace(snapshot=lambda: {'page_ready':'test'})
        device.run = lambda *args, **kwargs: b'cmd: service unavailable'
        result = device.execute('update_status', {})
        self.assertEqual(result['current_version'], 'test')
        self.assertEqual(result['face']['page_ready'], 'test')
        self.assertIsNone(result['default_input_method'])
        self.assertIn('service unavailable', result['input_method_error'])

    def test_input_restores_keyboard_on_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                device = Device.__new__(Device)
                device.ime_restore_path = Path(directory) / 'ime.json'
                current = ['com.example.keyboard/.IME']
                def run(*args, **kwargs):
                    if args[:2] == ('settings', 'get'):
                        return current[0].encode()
                    if args[:2] == ('ime', 'set'):
                        current[0] = args[2]
                    if args[:2] == ('am', 'broadcast'):
                        self.assertEqual(current[0], ADB_IME)
                        self.assertTrue(device.ime_restore_path.exists())
                        if fail:
                            raise ActionError('broadcast timed out')
                    return b''
                device.run = run
                if fail:
                    with self.assertRaises(ActionError):
                        device.execute('input_text', {'text': '中文测试'})
                else:
                    self.assertEqual(device.execute('input_text', {'text': '中文测试'})['length'], 4)
                self.assertEqual(current[0], 'com.example.keyboard/.IME')

    def test_startup_recovers_adb_keyboard_but_preserves_user_choice(self):
        for saved in (None, 'com.example.keyboard/.IME'):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as directory:
                device = Device.__new__(Device)
                device.ime_restore_path = Path(directory) / 'ime.json'
                if saved:
                    device.ime_restore_path.write_text(__import__('json').dumps({'input_method': saved}))
                current = [ADB_IME]
                def run(*args, **kwargs):
                    if args[:2] == ('settings', 'get'):
                        return current[0].encode()
                    if args[:2] == ('ime', 'set'):
                        current[0] = args[2]
                    return b''
                device.run = run
                device.recover_input_method()
                self.assertEqual(current[0], saved or 'com.sohu.inputmethod.sogou.xiaomi/.SogouIME')
                current[0] = 'com.user.new/.IME'
                device.recover_input_method()
                self.assertEqual(current[0], 'com.user.new/.IME')

    def test_screenshot_follows_folded_display(self):
        for state, display in [('CLOSED', '222'), ('TENT', '222'), ('OPENED', '111')]:
            with self.subTest(state=state):
                device=Device.__new__(Device)
                device.run=lambda *a,**kw: (
                    f"mCommittedState=Optional[DeviceState{{identifier=0, name='{state}'}}]".encode()
                    if a[1]=='device_state' else
                    b'Display 111 (HWC display 0)\nDisplay 222 (HWC display 1)'
                )
                commands=[]
                device._run_command=lambda command,timeout: commands.append(command)
                from unittest.mock import mock_open
                with patch('builtins.open',mock_open(read_data=b'\xff\xd8jpeg\xff\xd9')):
                    device.execute('screenshot',{})
                self.assertIn('screencap -d '+display+' -p',commands[0])

    def test_failed_dump_never_reads_old_screen(self):
        device=Device.__new__(Device)
        device.run=lambda *a,**kw: b'ERROR: could not get idle state.'
        device._run_command=lambda *a: self.fail('Read stale screen after failed dump')
        with self.assertRaises(ActionError):
            device.screen_nodes()

    def test_night_minimum_restart_caps_face_and_restores_after_leave(self):
        with tempfile.TemporaryDirectory() as directory:
            settings={'screen_brightness':'61','screen_brightness_mode':'1'}
            def run(*args,**kwargs):
                if args[:2]==('dumpsys','activity'):return b'mResumedActivity: com.android.chrome/org.chromium.chrome.browser.webapps.WebappActivity'
                if args[1]=='get':return settings[args[3]].encode()
                settings[args[3]]=args[4]
                return b''
            def device():
                d=Device.__new__(Device);d.run=run;d.night_applied=None
                d.face=types.SimpleNamespace(page_display={'visible':True},page_ready_at=__import__('time').time())
                d.brightness_restore_path=Path(directory)/'brightness.json'
                return d
            with patch('agent.time.localtime',return_value=types.SimpleNamespace(tm_hour=23,tm_wday=0)):
                device().update_night_brightness()
            self.assertEqual(settings,{'screen_brightness':'0','screen_brightness_mode':'0'})
            with patch('agent.time.localtime',return_value=types.SimpleNamespace(tm_hour=7,tm_wday=0)):
                resumed=device();resumed.update_night_brightness()
                self.assertEqual(settings,{'screen_brightness':'60','screen_brightness_mode':'0'})
                resumed.face.page_display={'visible':False}
                resumed.update_night_brightness()
            self.assertEqual(settings,{'screen_brightness':'61','screen_brightness_mode':'1'})

if __name__=='__main__':unittest.main()
