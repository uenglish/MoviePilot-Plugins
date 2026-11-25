"""Publish one plugin's V2/V3 assets as an independent release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from release_common import release_assets, release_plugin

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--tag', required=True)
parser.add_argument('--dry-run', action='store_true')
args = parser.parse_args()
pid, meta = release_plugin(ROOT, args.tag)
assets = release_assets(ROOT, args.tag)
checksums = dict(line.split('  ', 1)[::-1] for line in assets[-1].read_text().splitlines())
assert set(checksums) == {p.name for p in assets[:-1]}, 'Checksum list contains unrelated assets'
for path in assets[:-1]:
    assert checksums[path.name] == hashlib.sha256(path.read_bytes()).hexdigest()
sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
tag_sha = subprocess.check_output(['git', 'rev-parse', args.tag + '^{commit}'], cwd=ROOT, text=True).strip()
assert tag_sha == sha, 'Release tag must point to the checked-out commit'
repo = os.environ.get('GITHUB_REPOSITORY', 'uenglish/MoviePilot-Plugins')
assert repo == 'uenglish/MoviePilot-Plugins', 'Refusing to publish to a different repository'
body = (ROOT / 'docs/releases' / (args.tag + '.md')).read_text() + '\n\n```text\n' + assets[-1].read_text() + '```\n'
if args.dry_run:
    print(json.dumps({'repository': repo, 'plugin': pid, 'version': meta['version'],
                      'tag': args.tag, 'commit': sha, 'assets': [p.name for p in assets]}, ensure_ascii=False))
    raise SystemExit(0)
token = os.environ['GH_TOKEN']
base = 'https://api.github.com/repos/' + repo


def request(path, method='GET', payload=None, binary=False, missing_ok=False):
    url = path if path.startswith('https://uploads.github.com/') else base + path
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'User-Agent': 'PT-Keepers-release'}
    data = payload if binary else json.dumps(payload).encode() if payload is not None else None
    if data is not None:
        headers['Content-Type'] = 'application/octet-stream' if binary else 'application/json'
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=120) as response:
            content = response.read()
            return json.loads(content) if content else None
    except HTTPError as error:
        if error.code == 404 and missing_ok:
            return None
        raise RuntimeError(f'GitHub {method} failed: HTTP {error.code}') from None


def verify_assets(release):
    present = {a['name']: a for a in release['assets']}
    assert set(present) == {p.name for p in assets}, 'Published asset list differs'
    for path in assets:
        expected = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
        assert present[path.name]['state'] == 'uploaded', 'Asset is not uploaded'
        assert present[path.name]['size'] == path.stat().st_size, 'Asset size differs'
        assert present[path.name].get('digest') == expected, 'Published asset digest differs'


current = request('/releases/tags/' + quote(args.tag, safe=''), missing_ok=True)
if current and not current['draft']:
    # A published plugin version is immutable; reruns only verify the existing assets.
    verify_assets(current)
    print('Already published:', current['html_url'])
    raise SystemExit(0)
if current is None:
    current = request('/releases', 'POST', {'tag_name': args.tag, 'target_commitish': sha,
        'name': meta['name'] + ' v' + meta['version'], 'body': body, 'draft': True, 'prerelease': False})
existing = {a['name']: a for a in current['assets']}
assert set(existing) <= {p.name for p in assets}, 'Draft contains unrelated assets'
upload = current['upload_url'].split('{', 1)[0]
for path in assets:
    if path.name in existing:
        request('/releases/assets/' + str(existing[path.name]['id']), 'DELETE')
    result = request(upload + '?name=' + quote(path.name), 'POST', path.read_bytes(), binary=True)
    assert result['state'] == 'uploaded' and result['size'] == path.stat().st_size
    assert result.get('digest') == 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
verify_assets(request('/releases/' + str(current['id'])))
result = request('/releases/' + str(current['id']), 'PATCH', {'body': body, 'draft': False, 'make_latest': 'true'})
print('Published:', result['html_url'])
