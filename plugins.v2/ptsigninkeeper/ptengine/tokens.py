"""Read an expandable Browserless token list, retaining old configuration support."""
import re

LEGACY_TOKEN_KEYS = ('browserless_token', 'browserless_token2', 'browserless_token3', 'browserless_token4')


def browserless_tokens(config):
    values = config.get('browserless_tokens')
    if values is None:
        values = [config.get(key) for key in LEGACY_TOKEN_KEYS]
    elif isinstance(values, str):
        values = re.split(r'[\r\n,，;；]+', values)
    if not isinstance(values, (list, tuple)):
        return []
    tokens = []
    for value in values:
        if not isinstance(value, str):
            continue
        token = value.strip()
        if token and token not in tokens:
            tokens.append(token)
    return tokens
