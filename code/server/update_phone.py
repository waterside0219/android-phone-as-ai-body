#!/usr/bin/env python3
"""Publish B7 files locally, optionally upload over SSH forwarding, then request install."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'phone-agent'))
from remote_update import validate_version, validate_manifest, valid_path, safe_path, NoRedirect, MAX_MANIFEST

DEFAULT_RELAY = 'http://127.0.0.1:8832'
DEFAULT_TOKEN = ROOT / '.private/body-device-token'


def publish(version, paths):
    validate_version(version)
    store = ROOT / '.private/updates'
    store.mkdir(mode=0o700, exist_ok=True)
    destination = store / version
    if destination.exists():
        raise ValueError('version already published; use a new version')
    temporary = Path(tempfile.mkdtemp(prefix='.staging-', dir=store))
    manifest = {'version': version, 'files': []}
    try:
        for relative in paths:
            valid_path(relative)
            source = safe_path(ROOT, relative)
            data = source.read_bytes()
            if source.suffix == '.py':
                compile(data, relative, 'exec')
            if source.suffix in {'.js', '.mjs'}:
                subprocess.run(['node', '--check', str(source)], check=True, capture_output=True)
            path = temporary / 'files' / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            manifest['files'].append({'path': relative, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        validate_manifest(manifest)
        (temporary / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def relay_post(path, data, relay, token_file, content_type='application/json', length=None):
    parsed = urllib.parse.urlsplit(relay)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise ValueError('relay must be an http://127.0.0.1:PORT SSH forward or local relay')
    token = Path(token_file).read_text().strip()
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': content_type}
    if length is not None:
        headers['Content-Length'] = str(length)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(relay.rstrip('/') + path, data=data, headers=headers)
    with opener.open(request, timeout=130) as response:
        return json.load(response)


def request(action, args, relay=DEFAULT_RELAY, token_file=DEFAULT_TOKEN):
    return relay_post('/v1/do', json.dumps({'action': action, 'args': args}).encode(), relay, token_file)


def upload(version, relay, token_file=DEFAULT_TOKEN, store=None):
    validate_version(version)
    package = (Path(store) if store is not None else ROOT / '.private/updates') / version
    manifest_path = safe_path(package, 'manifest.json')
    if manifest_path.stat().st_size > MAX_MANIFEST:
        raise ValueError('manifest too large')
    manifest = json.loads(manifest_path.read_bytes())
    files = validate_manifest(manifest)
    if manifest['version'] != version:
        raise ValueError('version mismatch')
    # No compression or in-memory archive: VPS only needs the standard library.
    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode='w', format=tarfile.PAX_FORMAT) as archive:
            archive.add(manifest_path, arcname='manifest.json', recursive=False)
            for item in files:
                source = safe_path(package, 'files/' + item['path'])
                if source.stat().st_size != item['size']:
                    raise ValueError('local release file size changed')
                archive.add(source, arcname='files/' + item['path'], recursive=False)
        length = stream.tell()
        stream.seek(0)
        return relay_post('/v1/releases', stream, relay, token_file, 'application/x-tar', length)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('version', nargs='?')
    parser.add_argument('files', nargs='*', help='explicit project-relative shipped files')
    parser.add_argument('--publish-only', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--relay', default=DEFAULT_RELAY, help='loopback relay or SSH -L URL')
    parser.add_argument('--token-file', type=Path, default=DEFAULT_TOKEN)
    parser.add_argument('--upload', action='store_true', help='upload local package to selected relay before install')
    args = parser.parse_args()
    if args.status:
        result = request('update_status', {}, args.relay, args.token_file)
    else:
        validate_version(args.version)
        if args.files:
            manifest = publish(args.version, args.files)
            print(json.dumps({'published': args.version, 'files': len(manifest['files'])}))
        if args.upload:
            print(json.dumps(upload(args.version, args.relay, args.token_file)))
        if args.publish_only:
            return
        result = request('update', {'version': args.version}, args.relay, args.token_file)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
