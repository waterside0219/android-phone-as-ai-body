#!/usr/bin/env python3
"""Authenticated, bounded file updates. One temporary worker survives agent restart."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 160 * 1024 * 1024
MAX_MANIFEST = 256 * 1024
RELAY_ROUTE = 'face/relay-route.json'
# 允许迁移到的中转地址（换中转时只接受这里列出的）。换成你自己的。
RELAY_URLS = {'wss://old-relay.example.com/agent', 'wss://relay.example.com/agent'}
# Only shipped code/resources; runtime configuration, credentials and state stay local.
AGENT_FILES = {'agent.py', 'face_service.py', 'remote_update.py', 'image_convert.py',
               'start-face.py', 'termux-boot.sh', 'presets.json', 'serve_face.py'}
CALL_FILES = {'server.py', 'app.js', 'index.html', 'start.sh'}
FACE_SUFFIXES = {'.html', '.js', '.mjs', '.css', '.json', '.png', '.jpg', '.webp',
                 '.ico', '.wasm', '.task', '.moc3', '.mp3'}


def validate_version(version):
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', version):
        raise ValueError('invalid update version')
    return version


def valid_path(value):
    if not isinstance(value, str) or len(value) > 240 or '\\' in value:
        raise ValueError('invalid update path')
    parts = value.split('/')
    if len(parts) < 2 or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', p) for p in parts):
        raise ValueError('invalid update path')
    group, rest = parts[0], '/'.join(parts[1:])
    allowed = ((group == 'face' and PurePosixPath(value).suffix in FACE_SUFFIXES and rest != 'release.json')
               or (group == 'phone-agent' and (rest in AGENT_FILES or re.fullmatch(r'presets/[0-9]+\.mp3', rest)))
               or (group == 'call-web' and rest in CALL_FILES))
    if not allowed:
        raise ValueError('update path is outside shipped files')
    return value


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) != {'version', 'files'}:
        raise ValueError('invalid update manifest')
    validate_version(manifest['version'])
    files = manifest['files']
    if not isinstance(files, list) or not 1 <= len(files) <= 512:
        raise ValueError('invalid update file count')
    seen, total = set(), 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'}:
            raise ValueError('invalid update file')
        path = valid_path(item['path'])
        if path in seen or any(path.startswith(p + '/') or p.startswith(path + '/') for p in seen):
            raise ValueError('duplicate or overlapping update path')
        seen.add(path)
        if type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE:
            raise ValueError('invalid update file size')
        if not isinstance(item['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', item['sha256']):
            raise ValueError('invalid update digest')
        total += item['size']
    if total > MAX_TOTAL:
        raise ValueError('update is too large')
    return files


def safe_path(root, relative):
    path = root
    for part in relative.split('/'):
        path = path / part
        if path.is_symlink():
            raise ValueError('symlinks are forbidden in update paths')
    if path.exists() and not path.is_file():
        raise ValueError('update target is not a regular file')
    return path


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), mode)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False).encode())


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def validate_relay_route(data):
    if len(data) > 4096:
        raise ValueError('relay route too large')
    route = json.loads(data)
    if (not isinstance(route, dict) or set(route) != {'url'}
            or not isinstance(route['url'], str) or route['url'] not in RELAY_URLS):
        raise ValueError('relay route must contain one approved TLS address')
    return route['url']


def connection_config(root, config=None):
    root = Path(root)
    if config is None:
        path = root / 'phone-agent/config.json'
        if path.stat().st_size > 64 * 1024:
            raise ValueError('config too large')
        config = read_json(path)
    if not isinstance(config, dict):
        raise ValueError('invalid config')
    route = safe_path(root, RELAY_ROUTE)
    if route.exists():
        if route.stat().st_size > 4096:
            raise ValueError('relay route too large')
        return {**config, 'url': validate_relay_route(route.read_bytes())}
    return config


def update_origin(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.path != '/agent' or parsed.query or parsed.fragment:
        raise ValueError('invalid update origin')
    if parsed.scheme == 'wss' and parsed.hostname:
        return 'https://' + parsed.netloc
    if parsed.scheme == 'ws' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}:
        return 'http://' + parsed.netloc
    raise ValueError('updates require the existing TLS agent origin')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Updater:
    def __init__(self, root, config=None):
        self.root = Path(root).resolve()
        self.work = self.root / '.body-update'
        self.work.mkdir(mode=0o700, exist_ok=True)
        self.txn = self.work / 'transaction'
        self.config = config

    def current(self):
        return read_json(self.work / 'current.json', {'version': 'bootstrap'})['version']

    def status(self):
        result = read_json(self.work / 'status.json', {'state': 'idle'})
        return {**result, 'current_version': self.current()}

    def record(self, state, version, error=None):
        record = {'state': state, 'version': version, 'at': time.time()}
        if error:
            record['error'] = error
        write_json(self.work / 'status.json', record)
        with (self.work / 'updates.jsonl').open('a') as log:
            log.write(json.dumps(record) + '\n')
            log.flush()
            os.fsync(log.fileno())

    def fetch(self, path, limit):
        origin = update_origin(self.config['url'])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(origin + path, headers={
            'Authorization': 'Bearer ' + self.config['token'], 'User-Agent': 'AndroidBody-Updater/1.0'})
        with opener.open(request, timeout=30) as response:
            content = response.read(limit + 1)
        if len(content) > limit:
            raise ValueError('download exceeds declared size')
        return content

    def stage(self, version, fetch=None):
        validate_version(version)
        if (self.txn / 'journal.json').exists():
            raise ValueError('an interrupted update needs recovery')
        self.record('downloading', version)
        fetch = fetch or self.fetch
        prefix = '/agent?update=' + version + '&resource='
        raw = fetch(prefix + 'manifest.json', MAX_MANIFEST)
        if len(raw) > MAX_MANIFEST:
            raise ValueError('manifest too large')
        manifest = json.loads(raw)
        files = validate_manifest(manifest)
        if manifest['version'] != version:
            raise ValueError('update version mismatch')
        needed = sum(f['size'] * 2 for f in files) + 16 * 1024 * 1024
        if shutil.disk_usage(self.root).free < needed:
            raise ValueError('not enough storage for safe update')
        shutil.rmtree(self.txn, ignore_errors=True)
        for item in files:
            safe_path(self.root, item['path'])
            data = fetch(prefix + 'files/' + item['path'], item['size'])
            if len(data) != item['size'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                raise ValueError('update SHA-256 mismatch: ' + item['path'])
            if item['path'] == RELAY_ROUTE:
                validate_relay_route(data)
            if item['path'].endswith('.py'):
                compile(data, item['path'], 'exec')
            atomic_write(self.txn / 'staged' / item['path'], data)
        write_json(self.txn / 'manifest.json', manifest)
        self.record('verified', version)
        return manifest

    def recover(self):
        journal = read_json(self.txn / 'journal.json')
        if journal is None:
            return False
        # Backups remain intact until the whole rollback is complete, including after power loss.
        for item in journal['files']:
            path = safe_path(self.root, item['path'])
            if item['existed']:
                atomic_write(path, (self.txn / 'backup' / item['path']).read_bytes(), item['mode'])
            else:
                path.unlink(missing_ok=True)
        write_json(self.work / 'current.json', {'version': journal['previous_version']})
        write_json(self.root / 'face/release.json', {'version': journal['previous_version']})
        self.record('rolled_back', journal['version'], 'interrupted or unhealthy update')
        (self.txn / 'journal.json').unlink()
        sync_directory(self.txn)
        return True

    def install(self, manifest, restart, healthy):
        files = validate_manifest(manifest)
        journal = {'version': manifest['version'], 'previous_version': self.current(), 'files': []}
        for item in files:
            path = safe_path(self.root, item['path'])
            mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
            if path.exists():
                atomic_write(self.txn / 'backup' / item['path'], path.read_bytes(), mode)
            journal['files'].append({'path': item['path'], 'existed': path.exists(), 'mode': mode})
        write_json(self.txn / 'journal.json', journal)
        self.record('installing', manifest['version'])
        try:
            for item in journal['files']:
                path = safe_path(self.root, item['path'])
                atomic_write(path, (self.txn / 'staged' / item['path']).read_bytes(), item['mode'])
                if path.suffix == '.py':
                    for cached in (path.parent / '__pycache__').glob(path.stem + '.*.pyc'):
                        cached.unlink()
            write_json(self.work / 'current.json', {'version': manifest['version']})
            write_json(self.root / 'face/release.json', {'version': manifest['version']})
            restart()
            if not healthy():
                raise ValueError('new services or page did not become healthy')
        except Exception:
            self.recover()
            try:
                restart()
                restored = healthy()
            except Exception:
                restored = False
            if not restored:
                self.record('recovery_needed', manifest['version'], 'old files restored; services need attention')
            return
        # Commit point: journal removal means the new version was checked healthy.
        (self.txn / 'journal.json').unlink()
        sync_directory(self.txn)
        self.record('installed', manifest['version'])
        shutil.rmtree(self.txn)

    def lock(self):
        handle = (self.work / 'lock').open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ValueError('an update is already running')
        return handle

    def begin(self, version):
        validate_version(version)
        lock = self.lock()
        try:
            if (self.txn / 'journal.json').exists():
                raise ValueError('an interrupted update needs recovery')
            # This known-good copy is outside all three updateable directories.
            atomic_write(self.work / 'runner.py', Path(__file__).read_bytes())
            env = os.environ.copy()
            env.pop('LD_LIBRARY_PATH', None)
            self.record('queued', version)
            with (self.work / 'worker.log').open('ab') as log:
                subprocess.Popen([sys.executable, str(self.work / 'runner.py'), '--root', str(self.root),
                                  '--run', version, '--lock-fd', str(lock.fileno())],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 env=env, start_new_session=True, pass_fds=(lock.fileno(),))
        finally:
            lock.close()
        return {'accepted': True, 'version': version}

    def restart_services(self):
        env = os.environ.copy()
        env.pop('LD_LIBRARY_PATH', None)
        env.update(TZ='Asia/Shanghai', PYTHONDONTWRITEBYTECODE='1')
        for relative, pidfile, logfile in [
            ('phone-agent/agent.py', self.work / 'agent.pid', self.root / 'phone-agent/agent.log'),
            ('phone-agent/serve_face.py', self.work / 'face.pid', self.root / 'face.log'),
            ('call-web/server.py', self.root / 'call-web/.call-web.pid', self.root / 'call-web/call-web.log'),
        ]:
            path = self.root / relative
            try:
                pid = int(pidfile.read_text())
                command = Path('/proc') / str(pid) / 'cmdline'
                if str(path).encode() in command.read_bytes().split(b'\0'):
                    os.kill(pid, signal.SIGTERM)
                    deadline = time.monotonic() + 5
                    while command.exists() and time.monotonic() < deadline:
                        time.sleep(.1)
                    if command.exists():
                        os.kill(pid, signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass
            with logfile.open('ab') as log:
                process = subprocess.Popen([sys.executable, str(path)], cwd=self.root,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           env=env, start_new_session=True)
            atomic_write(pidfile, str(process.pid).encode())
        # Recover a failed JS page without depending on that page's polling loop.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                with opener.open('http://127.0.0.1:8190/', timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(.25)
        subprocess.run([sys.executable, str(self.root / 'phone-agent/start-face.py'), '--reload'],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30, check=True)

    def healthy(self):
        deadline = time.monotonic() + 75
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        version = self.current()
        while time.monotonic() < deadline:
            try:
                with opener.open('http://127.0.0.1:8192/api/face', timeout=2) as response:
                    face = json.load(response)
                for port in (8190, 8191):
                    with opener.open('http://127.0.0.1:%s/' % port, timeout=2) as response:
                        if response.status != 200:
                            raise ValueError('HTTP health check failed')
                if (face.get('release') == version and face.get('agent_connected') is True
                        and face.get('page_ready') == version):
                    return True
            except (OSError, ValueError):
                pass
            time.sleep(1)
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--run')
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('--lock-fd', type=int)
    args = parser.parse_args()
    updater = Updater(args.root)
    lock = os.fdopen(args.lock_fd, 'a') if args.lock_fd is not None else updater.lock()
    with lock:
        if args.recover:
            updater.recover()
            return
        try:
            updater.config = connection_config(args.root)
            staged = updater.stage(args.run)
            # Let the old agent finish its accepted response before stopping it.
            time.sleep(1)
            updater.install(staged, updater.restart_services, updater.healthy)
        except Exception as error:
            # Never put authentication, URLs, local config or untrusted exception text in status.
            state = 'recovery_needed' if (updater.txn / 'journal.json').exists() else 'rejected'
            updater.record(state, args.run, type(error).__name__ + ': update could not complete')
            if not (updater.txn / 'journal.json').exists():
                shutil.rmtree(updater.txn, ignore_errors=True)


if __name__ == '__main__':
    main()
