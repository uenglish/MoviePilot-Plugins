"""Explicit site IDs permitted to send requests through Browserless."""
import json


def browserless_site_ids(values):
    """Empty/missing values deny all; never accept a dynamic all-sites marker."""
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (TypeError, ValueError):
            return []
    if not isinstance(values, (list, tuple)):
        return []
    result = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            continue
        key = str(value).strip()
        if not key or key.lower() in {'all', '*'}:
            continue
        if key.isdecimal():
            value = int(key)
            if value <= 0:
                continue
        elif isinstance(value, int):
            continue
        else:
            value = key
        if value not in result:
            result.append(value)
    return result
