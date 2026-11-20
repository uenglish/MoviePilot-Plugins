"""Generate the V2 runtime packages from the maintained V3 sources."""
import argparse
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDS = ('PTSignInKeeper', 'PTInviteKeeper')
MIN_VERSION = '2.15.6'
MODULES = {
    'app.sdk.config': 'app.core.config',
    'app.sdk.logging': 'app.log',
    'app.sdk.plugin': 'app.plugins',
    'app.sdk.events': 'app.core.event',
    'app.db.oper.site': 'app.db.site_oper',
    'app.adapters.external.ocr': 'app.helper.ocr',
    'app.adapters.network.browser': 'app.helper.browser',
    'app.adapters.network.cloudflare': 'app.helper.cloudflare',
    'app.sdk.utilities': 'app.utils.string',
    'app.agent.llm.gateway': 'app.plugins.ptsigninkeeper.host_ai',
    'app.agent.llm.helper': 'app.plugins.ptsigninkeeper.host_ai',
    'app.schemas.message': 'app.plugins.ptsigninkeeper.host_compat',
}


def adapt_python(source, plugin=None):
    """Rewrite import statements only; preserve site logic and source comments."""
    lines = source.splitlines(keepends=True)
    changes = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        if node.module == 'app.plugins' and not (plugin and any(a.name == '_PluginBase' for a in node.names)):
            continue
        if node.module not in MODULES and node.module not in ('app.sdk.network', 'app.plugins'):
            continue
        imports = []
        for alias in node.names:
            if node.module == 'app.sdk.network':
                module = {'RequestUtils': 'app.utils.http', 'SiteUtils': 'app.utils.site'}[alias.name]
            else:
                module = MODULES.get(node.module, node.module)
            if alias.name == '_PluginBase' and plugin:
                module = f'app.plugins.{plugin}.host_compat'
            name = alias.name + (f' as {alias.asname}' if alias.asname else '')
            imports.append(f'from {module} import {name}')
        start = sum(len(x) for x in lines[:node.lineno - 1]) + node.col_offset
        end = sum(len(x) for x in lines[:node.end_lineno - 1]) + node.end_col_offset
        changes.append((start, end, ('\n' + ' ' * node.col_offset).join(imports)))
    for start, end, value in sorted(changes, reverse=True):
        source = source[:start] + value + source[end:]
    if plugin == 'ptsigninkeeper':
        # Capability response lets the shared frontend hide the unsupported
        # V3 rich-message setting on V2 without branching on host versions.
        tree = ast.parse(source)
        for method in ast.walk(tree):
            if isinstance(method, ast.FunctionDef) and method.name == 'ui_options':
                value = next(node.value for node in method.body if isinstance(node, ast.Return))
                assert isinstance(value, ast.Dict)
                lines = source.splitlines(keepends=True)
                start = sum(map(len, lines[:value.lineno - 1])) + value.col_offset + 1
                source = source[:start] + "'rich_notifications_supported': False, " + source[start:]
                break
    return source


def expected_files():
    index = json.loads((ROOT / 'package.v3.json').read_text())
    assert set(index) == set(IDS)
    files = {}
    for pid in IDS:
        index[pid]['system_version'] = '>=' + MIN_VERSION
        folder = pid.lower()
        src = ROOT / 'plugins.v3' / folder
        for path in sorted(src.rglob('*')):
            rel = path.relative_to(src)
            if not path.is_file() or any(part in ('frontend', '__pycache__', 'node_modules', '.git')
                                         or part.startswith('._') for part in rel.parts):
                continue
            if path.name == '.DS_Store' or path.suffix == '.pyc':
                continue
            content = path.read_bytes()
            if path.suffix == '.py':
                content = adapt_python(content.decode(), folder).encode()
            if rel == Path('README.md'):
                content = content.decode().replace('MoviePilot V3（>=3.2.6）', f'MoviePilot V2（>={MIN_VERSION}）')
                content = ('> 此目录由 `scripts/build_v2.py` 生成。修改功能请编辑对应的 `plugins.v3/` 源码。\n\n' + content).encode()
            files[Path('plugins.v2') / folder / rel] = content
        files[Path('plugins.v2') / folder / 'host_compat.py'] = (
            ROOT / 'scripts/compat_v2/host_compat.py').read_bytes()
    files[Path('plugins.v2/ptsigninkeeper/host_ai.py')] = (
        ROOT / 'scripts/compat_v2/host_ai.py').read_bytes()
    files[Path('package.v2.json')] = (json.dumps(index, ensure_ascii=False, indent=2) + '\n').encode()
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    expected = expected_files()
    actual = {p.relative_to(ROOT) for p in (ROOT / 'plugins.v2').rglob('*') if p.is_file()
              and '__pycache__' not in p.parts and p.name != '.DS_Store'}
    changed = [str(rel) for rel, value in expected.items()
               if not (ROOT / rel).is_file() or (ROOT / rel).read_bytes() != value]
    extra = actual - set(expected)
    if args.check:
        if changed or extra:
            raise SystemExit('V2 generated files are stale: ' + ', '.join(changed + sorted(map(str, extra))))
    else:
        for rel, value in expected.items():
            target = ROOT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.read_bytes() != value:
                target.write_bytes(value)
        for rel in extra:
            (ROOT / rel).unlink()
    print(f'V2 {MIN_VERSION}: two plugin packages verified ({len(expected)} files)')


if __name__ == '__main__':
    main()
