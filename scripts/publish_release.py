"""Publish both keeper packages as one release; optional guarded initial consolidation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--tag', required=True)
parser.add_argument('--consolidate-initial', action='store_true')
parser.add_argument('--dry-run', action='store_true')
args = parser.parse_args()
assert re.fullmatch(r'v\d+\.\d+\.\d+', args.tag), 'Invalid version tag'
index = json.loads((ROOT / 'package.v3.json').read_text())
assert set(index) == {'PTSignInKeeper', 'PTInviteKeeper'}
assert {x['version'] for x in index.values()} == {args.tag[1:]}
assets = [ROOT / 'output' / f'{pid.lower()}_{args.tag}.zip' for pid in index]
assets.append(ROOT / 'output' / 'SHA256SUMS')
checksums = dict(line.split('  ', 1)[::-1] for line in assets[-1].read_text().splitlines())
for path in assets[:-1]:
    assert checksums[path.name] == hashlib.sha256(path.read_bytes()).hexdigest()
sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
if args.consolidate_initial:
    assert args.tag == 'v1.0.0'
    assert subprocess.check_output(['git', 'rev-list', '--count', 'HEAD'], cwd=ROOT, text=True).strip() == '1'
repo = os.environ.get('GITHUB_REPOSITORY', 'uenglish/MoviePilot-Plugins')
assert repo == 'uenglish/MoviePilot-Plugins', 'Refusing to publish to a different repository'
body = (ROOT / 'docs' / 'RELEASE.md').read_text() + '\n\n```text\n' + assets[-1].read_text() + '```\n'
if args.dry_run:
    print(json.dumps({'repository': repo, 'tag': args.tag, 'commit': sha, 'assets': [p.name for p in assets], 'consolidate_initial': args.consolidate_initial}, ensure_ascii=False))
    raise SystemExit(0)
token = os.environ['GH_TOKEN']
base = 'https://api.github.com/repos/' + repo

def request(path, method='GET', payload=None, binary=False):
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
        raise RuntimeError(f'GitHub {method} failed: HTTP {error.code}') from None

releases = []
for page in range(1, 101):
    batch = request(f'/releases?per_page=100&page={page}')
    releases.extend(batch)
    if len(batch) < 100:
        break
else:
    raise RuntimeError('Release listing exceeded the bounded page limit')
current = next((r for r in releases if r['tag_name'] == args.tag), None)
published = bool(current and not current['draft'])
if published:
    present = {a['name']: a for a in current['assets']}
    assert set(present) == {p.name for p in assets}, 'Published asset list differs'
    for path in assets:
        expected = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
        assert present[path.name].get('digest') == expected, 'Published asset digest differs'
if current is None:
    current = request('/releases', 'POST', {'tag_name': args.tag, 'target_commitish': sha, 'name': args.tag + ' · 首次发布' if args.tag == 'v1.0.0' else args.tag, 'body': body, 'draft': True, 'prerelease': False})
existing = {a['name']: a for a in current['assets']}
upload = current['upload_url'].split('{', 1)[0]
for path in ([] if published else assets):
    if path.name in existing:
        request('/releases/assets/' + str(existing[path.name]['id']), 'DELETE')
    result = request(upload + '?name=' + quote(path.name), 'POST', path.read_bytes(), binary=True)
    assert result['state'] == 'uploaded' and result['size'] == path.stat().st_size
    if result.get('digest'):
        assert result['digest'] == 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
if args.consolidate_initial:
    for release in releases:
        if release['id'] == current['id']:
            continue
        if not re.fullmatch(r'PT(?:SignIn|Invite)Keeper_v\d+(?:\.\d+)+', release['tag_name']):
            raise RuntimeError('Unexpected release found; refusing to delete it')
    for release in releases:
        if release['id'] != current['id']:
            request('/releases/' + str(release['id']), 'DELETE')
            print('Removed superseded release:', release['tag_name'])
result = request('/releases/' + str(current['id']), 'PATCH', {'body': body, 'draft': False, 'make_latest': 'true'})
print('Published:', result['html_url'])
