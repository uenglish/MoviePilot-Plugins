import unittest

from app.plugins.ptsigninkeeper.ptengine.runtime import RuleEngine
from app.plugins.ptsigninkeeper.ptengine.tokens import browserless_tokens


class TokenListTests(unittest.TestCase):
    def test_old_slots_remain_usable_without_new_list(self):
        config = {'browserless_token': ' one ', 'browserless_token2': '',
                  'browserless_token3': 'two', 'browserless_token4': 'one'}
        self.assertEqual(browserless_tokens(config), ['one', 'two'])
        self.assertEqual(RuleEngine(config)._browserless_tokens(), ['one', 'two'])

    def test_more_than_four_keep_order_without_blanks_or_duplicates(self):
        tokens = [f'fixture-{i}' for i in range(9)]
        config = {'browserless_tokens': tokens + ['', ' fixture-2 ', None, False],
                  'browserless_token': 'old-removed'}
        engine = RuleEngine(config)
        self.assertEqual(engine._browserless_tokens(), tokens)
        engine._browserless_tokens().clear()
        self.assertEqual(engine._browserless_tokens(), tokens)

    def test_delete_all_never_resurrects_legacy_credentials(self):
        for values in ([], '', '\n , '):
            self.assertEqual(browserless_tokens({'browserless_tokens': values,
                                                 'browserless_token': 'old-removed'}), [])

    def test_legacy_form_multiline_values(self):
        self.assertEqual(browserless_tokens({'browserless_tokens': ' one\ntwo\r\none ' }), ['one', 'two'])
