"""Resolve an independent plugin release without coupling catalog versions."""
import json
from pathlib import Path
import re

IDS = ('PTSignInKeeper', 'PTInviteKeeper')


def release_plugin(root: Path, tag: str) -> tuple[str, dict]:
    match = re.fullmatch(r'(PTSignInKeeper|PTInviteKeeper)_v(\d+\.\d+\.\d+)', tag)
    if not match:
        raise ValueError('Tag must be PTSignInKeeper_vX.Y.Z or PTInviteKeeper_vX.Y.Z')
    pid, version = match.groups()
    v3 = json.loads((root / 'package.v3.json').read_text())
    v2 = json.loads((root / 'package.v2.json').read_text())
    if v3[pid]['version'] != version or v2[pid]['version'] != version:
        raise ValueError(f'{tag}: selected plugin catalog versions differ from the tag')
    return pid, v3[pid]


def release_assets(root: Path, tag: str) -> list[Path]:
    pid, meta = release_plugin(root, tag)
    folder = root / 'output' / tag
    return [folder / f'{pid.lower()}_v{meta["version"]}.zip',
            folder / f'{pid.lower()}_mpv2_v{meta["version"]}.zip',
            folder / 'SHA256SUMS']
