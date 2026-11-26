#!/data/data/com.termux/files/usr/bin/python
"""Open the existing installed face shortcut using its original signed intent."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
RELOAD = '--reload' in sys.argv

def main():
    fields = json.loads((ROOT / 'pwa-shortcut.json').read_text())
    if fields.get('org.chromium.chrome.browser.webapp_url') != 'http://127.0.0.1:8190/':
        raise ValueError('face shortcut URL mismatch')
    env = os.environ.copy()
    env.pop('LD_LIBRARY_PATH', None)
    env['RISH_APPLICATION_ID'] = 'com.termux'
    env['PATH'] = '/system/bin:/system/xbin:' + env.get('PATH', '')
    def shell(command):
        return subprocess.run([str(ROOT / 'rish'), '-c', command], env=env, capture_output=True, text=True, timeout=12)
    # Xiaomi can freeze cold-started Termux children until the app is opened.
    if not RELOAD and '--resume' not in sys.argv and shell('am start --display 0 -n com.termux/.app.TermuxActivity').returncode:
        return 1
    # Android swaps the active panel into logical display 0 when folded.
    # HWC 1 identifies the outer screenshot target, not an activity display.
    args = ['am', 'start', '--display', '0', '-a', 'com.google.android.apps.chrome.webapps.WebappManager.ACTION_START_WEBAPP', '-p', 'com.android.chrome']
    for key, value in fields.items():
        option = '--ez' if value in ('true', 'false') else '--el' if key.endswith(('theme_color', 'background_color')) else '--ei' if key.endswith(('webapp_source', 'webapp_shortcut_version', 'webapp_display_mode', 'orientation')) else '--es'
        args += [option, key, value]
    args += ['--ei', 'org.chromium.chrome.browser.webapp_display_mode', '4', '--ei', 'org.chromium.content_public.common.orientation', '7', '--ei', 'org.chromium.chrome.browser.webapp_source', '7']
    if RELOAD:
        args += ['--ez', 'org.chromium.chrome.browser.webapk_force_navigation', 'true']
    result = shell(shlex.join(args))
    # Keep the signed shortcut out of logs.
    return result.returncode

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise SystemExit(1)
