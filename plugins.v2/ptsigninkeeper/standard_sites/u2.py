"""Compatibility entrypoint; answer selection is owned by the quiz engine."""
from . import _ISiteSigninHandler


class U2(_ISiteSigninHandler):
    site_url = 'u2.dmhy.org'

    @classmethod
    def match(cls, url):
        from ..ptengine.sites import domain_matches
        return domain_matches(url, cls.site_url)

    def signin(self, site_info):
        from ..ptengine.core import resolve_rule, http_sign_auto
        from ..ptengine.quiz import AssistantSolver
        from app.core.config import settings
        rule = resolve_rule(site_info, {})
        rule['_quiz_solver'] = AssistantSolver(False)
        result = http_sign_auto(site_info, rule, timeout=int(site_info.get('timeout') or 30),
                                proxies=settings.PROXY if site_info.get('proxy') else None)
        return result.status in ('success', 'already'), result.message
