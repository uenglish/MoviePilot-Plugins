"""Keep structured notification composition internal on MoviePilot V2."""
from dataclasses import dataclass, field

from app.plugins import _PluginBase as HostPluginBase


@dataclass
class NotificationBlock:
    title: str = ''
    text: str = ''


@dataclass
class NotificationContent:
    title: str = ''
    blocks: list[NotificationBlock] = field(default_factory=list)


class _PluginBase(HostPluginBase):
    def post_message(self, *args, **kwargs):
        content = kwargs.pop('rich_content', None)
        if content is not None and not kwargs.get('text'):
            kwargs['text'] = '\n\n'.join(
                '\n'.join(part for part in (block.title, block.text) if part)
                for block in content.blocks)
            kwargs.setdefault('title', content.title)
        return super().post_message(*args, **kwargs)
