"""Check the two-plugin catalog, metadata and shipped runtime assets."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDS = ('PTSignInKeeper', 'PTInviteKeeper')
index = json.loads((ROOT / 'package.v3.json').read_text())
assert set(index) == set(IDS), 'Unexpected catalog entries'
assert {p.name for p in (ROOT / 'plugins.v3').iterdir() if p.is_dir()} == {p.lower() for p in IDS}
assert len({v['version'] for v in index.values()}) == 1, 'Unified release versions differ'
for pid in IDS:
    meta = index[pid]
    root = ROOT / 'plugins.v3' / pid.lower()
    tree = ast.parse((root / '__init__.py').read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == pid)
    attrs = {t.id: ast.literal_eval(n.value) for n in cls.body if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name) and t.id in ('plugin_version', 'plugin_author', 'plugin_icon')}
    assert attrs['plugin_version'] == meta['version'], pid
    assert attrs['plugin_author'] == meta['author'] == 'serendipity', pid
    assert meta['release'] is False, 'Unified release uses source installation'
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
print('Two-plugin catalog, versions, authors, syntax and runtime resources verified')
