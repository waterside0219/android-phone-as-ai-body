import unittest
import types
from unittest.mock import Mock
from agent import Device,ActionError

class PhoneCallsTest(unittest.TestCase):
    def device(self):
        device=Device.__new__(Device)
        device.face=types.SimpleNamespace(phone_call_active=False)
        device.brightness_status={'face_foreground':True,'top':'com.android.chrome/org.chromium.chrome.browser.webapps.WebappActivity'}
        device.resume_face=Mock()
        return device

    def test_hangup_returns_only_from_idle_launcher_not_wechat(self):
        device=self.device()
        device.observe_call_state('mCallState=0',100)
        device.brightness_status={'face_foreground':False,'top':'com.android.incallui/.InCallActivity'}
        device.observe_call_state('mCallState=1',105)
        self.assertTrue(device.face.phone_call_active)
        device.brightness_status['top']='com.tencent.mm/.ui.LauncherUI'
        device.observe_call_state('mCallState=0',110)
        device.observe_call_state('mCallState=0',130)
        device.resume_face.assert_not_called()
        device.brightness_status['top']='com.miui.home/.launcher.Launcher'
        device.observe_call_state('mCallState=0',135,15)
        device.observe_call_state('mCallState=0',146,26)
        device.resume_face.assert_called_once()
        self.assertFalse(device.face.phone_call_active)

    def test_unknown_state_and_unrelated_calls_never_open_face(self):
        device=self.device();device.brightness_status={'face_foreground':False,'top':'com.miui.home/.launcher.Launcher'}
        for value,at in [('mCallState=1',100),('unavailable',110),('mCallState=0',120),('mCallState=0',140)]:device.observe_call_state(value,at)
        device.resume_face.assert_not_called()

    def test_active_input_or_unavailable_idle_never_returns(self):
        for idle in (None,0,9):
            device=self.device();device.observe_call_state('mCallState=0',100)
            device.observe_call_state('mCallState=1',105)
            device.brightness_status={'top':'com.miui.home/.launcher.Launcher','face_foreground':False}
            device.observe_call_state('mCallState=0',110,idle)
            device.observe_call_state('mCallState=0',140,idle)
            device.resume_face.assert_not_called()
        device=self.device();device.observe_call_state('mCallState=0',100)
        device.observe_call_state('mCallState=1',105)
        device.brightness_status={'top':'com.android.contacts/.DialtactsActivity','face_foreground':False}
        device.observe_call_state('mCallState=0',110,20)
        device.observe_call_state('mCallState=0',140,50)
        device.resume_face.assert_not_called()

    def test_idle_time_failure_does_not_hide_actual_hangup(self):
        device=self.device();device.native_call_active=True;device.face.phone_call_active=True
        device.face.temperature=lambda:None
        device.update_night_brightness=lambda:None
        def run(*args,**kw):
            if args==('dumpsys','telephony.registry'):return b'mCallState=0'
            raise ActionError('idle time unavailable')
        device.run=run
        class Stop:
            count=0
            def is_set(self):self.count+=1;return self.count>1
            def wait(self,seconds):pass
        device.maintain_device(Stop())
        self.assertFalse(device.face.phone_call_active)
        device.resume_face.assert_not_called()
