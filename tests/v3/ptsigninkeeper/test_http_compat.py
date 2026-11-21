"""MoviePilot RequestUtils compatibility adapter tests."""
import unittest
from types import SimpleNamespace

from app.plugins.ptsigninkeeper.ptengine.http import (
    _LegacySdkSession,
    _SdkSession,
    _request_utils_session,
)


class HttpCompatibilityTests(unittest.TestCase):
    def test_legacy_request_utils_uses_v2_adapter(self):
        response = SimpleNamespace(cookies=SimpleNamespace(get_dict=lambda: {'sid': 'new'}))
        instances = []

        class LegacyRequestUtils:
            def __init__(self):
                self.calls = []
                instances.append(self)

            def get_res(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return response

            def get_cookies(self):
                return {'sid': 'old'}

        session = _request_utils_session(LegacyRequestUtils)
        self.assertIsInstance(session, _LegacySdkSession)
        self.assertEqual(session.cookies.get_dict(), {'sid': 'old'})
        self.assertIs(session.get('https://example.invalid/'), response)
        self.assertEqual(instances[0].calls[0][1]['cookies'], {'sid': 'old'})
        self.assertEqual(session.cookies.get_dict(), {'sid': 'new'})

    def test_request_utils_with_session_support_uses_sdk_adapter(self):
        class CurrentRequestUtils:
            def __init__(self, use_session=False):
                self.use_session = use_session

        session = _request_utils_session(CurrentRequestUtils)
        self.assertIsInstance(session, _SdkSession)
        self.assertTrue(session._client.use_session)

    def test_internal_constructor_type_error_is_not_misclassified_as_v2(self):
        class BrokenRequestUtils:
            def __init__(self, use_session=False):
                raise TypeError('constructor initialization failed')

        with self.assertRaisesRegex(TypeError, 'constructor initialization failed'):
            _request_utils_session(BrokenRequestUtils)


if __name__ == '__main__':
    unittest.main(verbosity=2)
