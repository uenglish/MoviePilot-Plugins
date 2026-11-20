"""从改名前的插件复制配置和数据，保持目标实例与旧实例相互独立。"""
from copy import deepcopy
import json
from pathlib import Path
import shutil

MARKER = 'independent_identity_v1'


def _entries(value):
    """规整宿主返回的键值映射或数据记录列表。"""
    if isinstance(value, dict):
        return list(value.items())
    return [(getattr(item, 'key', None), getattr(item, 'value', None)) for item in (value or [])]


def migrate_identity(host, config, legacy_id):
    """复制缺失数据后落标记；失败不落标记，重试不会覆盖目标内容。"""
    current = deepcopy(config if config is not None else (host.get_config() or {}))
    if host.get_data(key=MARKER):
        return current
    legacy = host.get_config(plugin_id=legacy_id) or {}
    inherited = {**deepcopy(legacy), **current}
    inherited['onlyonce'] = bool(current.get('onlyonce', False))
    inherited['clean'] = bool(current.get('clean', False))
    for key, value in _entries(host.get_data(plugin_id=legacy_id)):
        if key and key != MARKER and host.get_data(key=key) is None:
            host.save_data(key=key, value=deepcopy(value))
    target = Path(host.get_data_path())
    target.mkdir(parents=True, exist_ok=True)
    source = Path(host.get_data_path(plugin_id=legacy_id))
    if source.resolve() != target.resolve():
        for name in ('site_data.json', 'history.json', 'consolidation-v3.json'):
            original, destination = source / name, target / name
            if original.is_file() and not destination.exists():
                json.loads(original.read_text(encoding='utf-8'))
                temporary = destination.with_name(destination.name + '.identity-tmp')
                shutil.copy2(original, temporary)
                temporary.replace(destination)
    host.update_config(inherited)
    host.save_data(key=MARKER, value={'version': 1, 'source': legacy_id})
    return inherited
