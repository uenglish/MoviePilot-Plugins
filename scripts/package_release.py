"""Create deterministic independent plugin ZIPs for one repository release."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'output'
OUT.mkdir(exist_ok=True)
checksums = []
packages = [(platform, pid, meta) for platform in ('v3', 'v2')
            for pid, meta in json.loads((ROOT / f'package.{platform}.json').read_text()).items()]
for platform, pid, meta in packages:
    folder = pid.lower()
    src = ROOT / f'plugins.{platform}' / folder
    suffix = '_mpv2' if platform == 'v2' else ''
    target = OUT / f'{folder}{suffix}_v{meta["version"]}.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(src.rglob('*')):
            rel = path.relative_to(src)
            if not path.is_file() or any(p in ('node_modules', '__pycache__', '.git') or p.startswith('._') for p in rel.parts):
                continue
            if path.suffix == '.pyc' or path.name == '.DS_Store':
                continue
            entry = zipfile.ZipInfo(folder + '/' + rel.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, path.read_bytes(), compresslevel=9)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert all(folder + '/' + name in archive.namelist() for name in ('__init__.py', 'dist/assets/remoteEntry.js', 'LICENSE', 'NOTICE.md'))
    checksums.append(hashlib.sha256(target.read_bytes()).hexdigest() + '  ' + target.name)
(OUT / 'SHA256SUMS').write_text('\n'.join(checksums) + '\n')
print('\n'.join(checksums))
