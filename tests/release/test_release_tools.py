"""Verify independent artifact selection and immutable release publication."""
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from release_common import release_plugin


class IndependentReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        scripts = self.root / 'scripts'
        scripts.mkdir()
        for name in ('release_common.py', 'package_release.py', 'publish_release.py'):
            shutil.copyfile(ROOT / 'scripts' / name, scripts / name)
        self.tag = 'PTSignInKeeper_v1.4.1'
        metadata = {'PTSignInKeeper': {'name': 'PT 签到管家', 'version': '1.4.1'},
                    'PTInviteKeeper': {'name': 'PT 邀请管家', 'version': '1.4.0'}}
        for platform in ('v2', 'v3'):
            (self.root / f'package.{platform}.json').write_text(json.dumps(metadata))
            for pid in metadata:
                folder = self.root / f'plugins.{platform}' / pid.lower()
                for name in ('__init__.py', 'LICENSE', 'NOTICE.md', 'dist/assets/remoteEntry.js'):
                    path = folder / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f'{pid} {platform} fixture')
        notes = self.root / 'docs/releases'
        notes.mkdir(parents=True)
        (notes / (self.tag + '.md')).write_text('签到管家测试说明')
        self.invoke('git', 'init', '-q')
        self.commit('chore(test): 初始化发行测试仓库')
        self.invoke('git', 'tag', self.tag)

    def invoke(self, *args, check=True):
        return subprocess.run(args, cwd=self.root, text=True, capture_output=True, check=check)

    def commit(self, message):
        self.invoke('git', '-c', 'user.name=Release Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--allow-empty', '-qm', message)

    def package(self, tag=None):
        self.invoke(sys.executable, '-B', 'scripts/package_release.py', '--tag', tag or self.tag)
        return self.root / 'output' / (tag or self.tag)

    def test_each_plugin_packages_only_its_own_versions_and_checksum_file(self):
        sign = self.package()
        before = {p.name: p.read_bytes() for p in sign.iterdir()}
        invite = self.package('PTInviteKeeper_v1.4.0')
        self.assertEqual(before, {p.name: p.read_bytes() for p in sign.iterdir()})
        for directory, prefix, version in ((sign, 'ptsigninkeeper', '1.4.1'),
                                            (invite, 'ptinvitekeeper', '1.4.0')):
            expected = {f'{prefix}_v{version}.zip', f'{prefix}_mpv2_v{version}.zip'}
            checksums = dict(line.split('  ', 1)[::-1] for line in (directory / 'SHA256SUMS').read_text().splitlines())
            self.assertEqual(set(checksums), expected)
            self.assertEqual({p.name for p in directory.iterdir()}, expected | {'SHA256SUMS'})
            for filename in expected:
                self.assertEqual(checksums[filename], hashlib.sha256((directory / filename).read_bytes()).hexdigest())
                with zipfile.ZipFile(directory / filename) as archive:
                    self.assertTrue(all(name.startswith(prefix + '/') for name in archive.namelist()))

    def test_invalid_tags_and_selected_platform_version_mismatch_are_rejected(self):
        for tag in ('v1.4.1', 'PTUnknown_v1.4.1', '../PTSignInKeeper_v1.4.1', 'PTSignInKeeper_v1.4.2'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release_plugin(self.root, tag)
        path = self.root / 'package.v2.json'
        value = json.loads(path.read_text())
        value['PTSignInKeeper']['version'] = '1.4.0'
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            release_plugin(self.root, self.tag)

    def test_dry_run_uses_plugin_specific_notes_and_only_two_archives(self):
        self.package()
        result = self.invoke(sys.executable, '-B', 'scripts/publish_release.py', '--tag', self.tag, '--dry-run')
        plan = json.loads(result.stdout)
        self.assertEqual(plan['plugin'], 'PTSignInKeeper')
        self.assertEqual(set(plan['assets']), {'ptsigninkeeper_v1.4.1.zip',
                                              'ptsigninkeeper_mpv2_v1.4.1.zip', 'SHA256SUMS'})

    def test_tag_must_point_to_checked_out_commit(self):
        self.package()
        self.commit('chore(test): 创建与标签不一致的提交')
        result = self.invoke(sys.executable, '-B', 'scripts/publish_release.py', '--tag', self.tag,
                             '--dry-run', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Release tag must point to the checked-out commit', result.stderr)

    def publish(self, published=False, corrupt=False):
        directory = self.package()
        records = []
        release = {'id': 8, 'draft': not published, 'assets': [],
                   'html_url': 'https://github.com/uenglish/MoviePilot-Plugins/releases/tag/' + self.tag,
                   'upload_url': 'https://uploads.github.com/repos/uenglish/MoviePilot-Plugins/releases/8/assets{?name}'}
        if published:
            release['assets'] = [{'id': i, 'name': path.name, 'size': path.stat().st_size,
                                  'state': 'uploaded', 'digest': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()}
                                 for i, path in enumerate(directory.iterdir())]
            if corrupt:
                release['assets'][0]['digest'] = 'sha256:invalid'

        def urlopen(request, **kwargs):
            path = urlsplit(request.full_url).path
            method = request.get_method()
            records.append((method, path))
            if '/releases/tags/' in path:
                if not published:
                    raise HTTPError(request.full_url, 404, 'Not Found', {}, None)
                result = release
            elif method == 'POST' and path.endswith('/releases'):
                payload = json.loads(request.data)
                self.assertEqual(payload['tag_name'], self.tag)
                self.assertTrue(payload['draft'])
                self.assertIn('签到管家测试说明', payload['body'])
                result = release
            elif method == 'POST' and path.endswith('/assets'):
                name = parse_qs(urlsplit(request.full_url).query)['name'][0]
                result = {'id': len(release['assets']) + 1, 'name': name, 'size': len(request.data),
                          'state': 'uploaded', 'digest': 'sha256:' + hashlib.sha256(request.data).hexdigest()}
                release['assets'].append(result)
            elif method == 'GET' and path.endswith('/releases/8'):
                result = release
            elif method == 'PATCH' and path.endswith('/releases/8'):
                self.assertEqual(records[-2], ('GET', path))
                self.assertEqual(len(release['assets']), 3)
                release.update(json.loads(request.data))
                result = release
            else:
                self.fail(f'Unexpected release request: {method} {path}')
            return io.BytesIO(json.dumps(result).encode())

        with patch('sys.argv', ['publish_release.py', '--tag', self.tag]), \
             patch.dict(os.environ, {'GH_TOKEN': 'test-release-token'}), \
             patch('urllib.request.urlopen', side_effect=urlopen), patch('sys.stdout', new_callable=io.StringIO):
            try:
                runpy.run_path(str(self.root / 'scripts/publish_release.py'), run_name='__main__')
            except SystemExit as error:
                self.assertEqual(error.code, 0)
        return release, records

    def test_publish_verifies_assets_before_making_only_selected_release_public(self):
        release, records = self.publish()
        self.assertFalse(release['draft'])
        self.assertEqual({a['name'] for a in release['assets']},
                         {'ptsigninkeeper_v1.4.1.zip', 'ptsigninkeeper_mpv2_v1.4.1.zip', 'SHA256SUMS'})
        self.assertNotIn('DELETE', [method for method, _ in records])

    def test_published_release_is_verified_without_mutation_and_bad_digest_stops(self):
        _, records = self.publish(published=True)
        self.assertEqual([method for method, _ in records], ['GET'])
        with self.assertRaisesRegex(AssertionError, 'digest differs'):
            self.publish(published=True, corrupt=True)
