import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('start_face',Path(__file__).with_name('start-face.py'))
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)

class LauncherTest(unittest.TestCase):
    def test_reload_uses_signed_shortcut_and_chrome_force_navigation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'pwa-shortcut.json').write_text('{"org.chromium.chrome.browser.webapp_url":"http://127.0.0.1:8190/"}')
            commands=[]
            with patch.object(launcher,'ROOT',root),patch.object(launcher.subprocess,'run',side_effect=lambda args,**kw: (commands.append(args) or types.SimpleNamespace(returncode=0))),patch.object(launcher,'RELOAD',True,create=True):
                launcher.main()
            self.assertIn('--ez org.chromium.chrome.browser.webapk_force_navigation true',commands[-1][-1])

if __name__=='__main__':unittest.main()
