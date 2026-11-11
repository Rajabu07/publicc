import unittest

try:
    import pyrogram  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("pyrogram is not installed")

import asyncio
from helpers.str_to_buttons import string_to_buttons


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class ButtonTests(unittest.TestCase):
    def test_parses_rows_and_columns(self):
        markup = run(string_to_buttons("A - https://a.com | B - https://b.com\nC - https://c.com", include_mandatory=False))
        self.assertEqual(len(markup.inline_keyboard), 2)
        self.assertEqual(len(markup.inline_keyboard[0]), 2)

    def test_skip_words_give_no_buttons(self):
        for word in ("/pass", "/skip", "none", None, ""):
            self.assertIsNone(run(string_to_buttons(word, include_mandatory=False)))

    def test_bare_domain_gets_https(self):
        markup = run(string_to_buttons("Site - example.com", include_mandatory=False))
        self.assertEqual(markup.inline_keyboard[0][0].url, "https://example.com")

    def test_mandatory_button_added(self):
        markup = run(string_to_buttons("A - https://a.com", include_mandatory=True))
        self.assertGreaterEqual(len(markup.inline_keyboard), 2)


if __name__ == "__main__":
    unittest.main()
