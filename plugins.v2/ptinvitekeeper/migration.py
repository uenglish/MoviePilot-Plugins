"""普通版与增强版的配置、邀请缓存及历史合并。"""
from copy import deepcopy
import json
from pathlib import Path
import shutil

MIGRATION_VERSION = 1


def _site_ids(values):
    """兼容旧版站点 ID；空选择沿用原插件的全部站点语义。"""
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            values = values.replace("，", ",").split(",")
    if not values:
        return ["all"]
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    result = []
    for value in values:
        if str(value).strip().lower() == "all":
            return ["all"]
        if isinstance(value, bool):
            continue
        if str(value).strip().isdigit():
            value = int(value)
            if value not in result:
                result.append(value)
    return result


def merge_config(current, legacy):
    """优先保留增强版设置，只合并启用普通版的站点且不重放一次性操作。"""
    current, legacy = deepcopy(current or {}), deepcopy(legacy or {})
    if current.get("consolidation_version") == MIGRATION_VERSION:
        return current
    result = current if current else {**legacy, "onlyonce": False}
    if legacy.get("enabled"):
        selected = _site_ids(result.get("site_ids"))
        extra = _site_ids(legacy.get("site_ids"))
        result["site_ids"] = ["all"] if "all" in selected + extra else list(dict.fromkeys(selected + extra))
    result["consolidation_version"] = MIGRATION_VERSION
    return result


def _read(path, expected):
    if not path.exists():
        return expected()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, expected):
        raise ValueError(f"{path.name} 数据格式错误，保留原文件")
    return value


def _write(path, value):
    """同目录原子替换，避免写入中断损坏原缓存。"""
    temporary = path.with_name(path.name + ".merge-tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _time(record, field):
    try:
        return float(record.get(field) or 0) if isinstance(record, dict) else 0
    except (TypeError, ValueError):
        return 0


def merge_data(target_dir, legacy_dir):
    """一次性合并两个插件的缓存，保留原始归档；同时间优先增强版。"""
    target_dir, legacy_dir = Path(target_dir), Path(legacy_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    marker = target_dir / "consolidation-v3.json"
    if marker.exists():
        return
    current = _read(target_dir / "site_data.json", dict)
    legacy = _read(legacy_dir / "site_data.json", dict)
    current_history = _read(target_dir / "history.json", list)
    legacy_history = _read(legacy_dir / "history.json", list)
    archive = target_dir / "legacy-nexusinvitee-v3"
    archive.mkdir(exist_ok=True)
    for prefix, directory in (("enhanced", target_dir), ("ordinary", legacy_dir)):
        for filename in ("site_data.json", "history.json"):
            source, backup = directory / filename, archive / f"{prefix}-{filename}"
            if source.exists() and not backup.exists():
                shutil.copy2(source, backup)
    merged = deepcopy(current)
    for name, record in legacy.items():
        if name not in merged or _time(record, "last_update") > _time(merged[name], "last_update"):
            merged[name] = deepcopy(record)
    history, seen = [], set()
    for row in current_history + legacy_history:
        key = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            history.append(row)
    history.sort(key=lambda row: _time(row, "time"))
    if current or legacy or (target_dir / "site_data.json").exists():
        _write(target_dir / "site_data.json", merged)
    if current_history or legacy_history or (target_dir / "history.json").exists():
        _write(target_dir / "history.json", history)
    _write(marker, {"version": MIGRATION_VERSION, "source": "nexusinvitee", "sites": len(merged), "history": len(history)})
