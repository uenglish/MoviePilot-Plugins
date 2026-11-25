"""Check the two-plugin catalog, metadata and shipped runtime assets."""
import ast
import json
from pathlib import Path
from build_v2 import expected_files

ROOT = Path(__file__).resolve().parents[1]
IDS = ('PTSignInKeeper', 'PTInviteKeeper')
index = json.loads((ROOT / 'package.v3.json').read_text())
assert set(index) == set(IDS), 'Unexpected catalog entries'
assert {p.name for p in (ROOT / 'plugins.v3').iterdir() if p.is_dir()} == {p.lower() for p in IDS}
for pid in IDS:
    meta = index[pid]
    root = ROOT / 'plugins.v3' / pid.lower()
    tree = ast.parse((root / '__init__.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == pid)
    attrs = {t.id: ast.literal_eval(n.value) for n in cls.body if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name) and t.id in ('plugin_version', 'plugin_author', 'plugin_icon')}
    assert attrs['plugin_version'] == meta['version'], pid
    assert attrs['plugin_author'] == meta['author'] == 'serendipity', pid
    assert meta['release'] is False, 'Catalog uses source installation for both platforms'
    for name in ('package.json', 'package-lock.json'):
        front = json.loads((root / 'frontend' / name).read_text())
        assert front['version'] == meta['version'], (pid, name)
        if name == 'package-lock.json':
            assert front['packages']['']['version'] == meta['version']
    for name in ('LICENSE', 'NOTICE.md', 'README.md', 'dist/assets/remoteEntry.js'):
        assert (root / name).is_file(), (pid, name)
    icon = attrs['plugin_icon'].split('/assets/', 1)[1]
    assert (root / 'assets' / icon).is_file(), (pid, icon)
    assert (ROOT / 'icons' / meta['icon'].rsplit('/', 1)[1]).is_file(), pid
    for p in root.rglob('*.py'):
        ast.parse(p.read_text(), filename=str(p))
v2 = json.loads((ROOT / 'package.v2.json').read_text())
assert set(v2) == set(IDS)
assert {p.name for p in (ROOT / 'plugins.v2').iterdir() if p.is_dir()} == {p.lower() for p in IDS}
for pid in IDS:
    assert v2[pid]['version'] == index[pid]['version']
    assert v2[pid]['author'] == 'serendipity'
    assert v2[pid]['system_version'] == '>=2.15.6'
    for path in (ROOT / 'plugins.v2' / pid.lower()).rglob('*.py'):
        ast.parse(path.read_text(), filename=str(path))
for rel, content in expected_files().items():
    assert (ROOT / rel).read_bytes() == content, f'Stale generated file: {rel}'
print('Two-plugin V2/V3 catalogs, versions, authors, syntax and runtime resources verified')
