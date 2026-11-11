import datetime
import unittest
from types import SimpleNamespace

from helpers.utils import (
    parse_timer_seconds, format_duration, format_uptime, normalize_channel_id,
    parse_channel_ids, format_welcome_text, extract_media, progress_bar, sparkline,
    fill_daily_stats, safe_pct, chunked, extract_join_request,
)


class TimerTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_timer_seconds("30s"), 30)
        self.assertEqual(parse_timer_seconds("10m"), 600)
        self.assertEqual(parse_timer_seconds("1h"), 3600)
        self.assertEqual(parse_timer_seconds("2 hours"), 7200)
        self.assertEqual(parse_timer_seconds("1d"), 86400)

    def test_plain_number_is_minutes(self):
        self.assertEqual(parse_timer_seconds("10"), 600)

    def test_pass_and_garbage(self):
        for word in ("/pass", "pass", "no", "none", "0", "", None, "abc", "10x"):
            self.assertIsNone(parse_timer_seconds(word), word)

    def test_minimum_five_seconds(self):
        self.assertEqual(parse_timer_seconds("1s"), 5)

    def test_format_duration(self):
        self.assertIn("Permanent", format_duration(None))
        self.assertIn("30 second", format_duration(30))
        self.assertIn("5 minute", format_duration(300))
        self.assertIn("2 hour", format_duration(7200))
        self.assertIn("1 day", format_duration(86400))

    def test_format_uptime(self):
        self.assertEqual(format_uptime(59), "0m 59s")
        self.assertEqual(format_uptime(3661), "1h 1m 1s")
        self.assertEqual(format_uptime(90000), "1d 1h 0m")


class ChannelIdTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_channel_id("-1001234567890"), -1001234567890)
        self.assertEqual(normalize_channel_id("1234567890"), -1001234567890)
        self.assertEqual(normalize_channel_id("1001234567890"), -1001234567890)
        self.assertEqual(normalize_channel_id("-123456"), -123456)

    def test_normalize_invalid(self):
        for bad in ("abc", "", None, "-", "12a", "0", "-0"):
            self.assertIsNone(normalize_channel_id(bad), bad)

    def test_multiple_ids_space_comma_newline(self):
        ids, invalid, wants_all = parse_channel_ids("-1001111111111 -1002222222222,-1003333333333\n-1004444444444")
        self.assertEqual(ids, [-1001111111111, -1002222222222, -1003333333333, -1004444444444])
        self.assertEqual(invalid, [])
        self.assertFalse(wants_all)

    def test_duplicates_removed_order_kept(self):
        ids, _, _ = parse_channel_ids(["-1001111111111", "-1002222222222", "-1001111111111"])
        self.assertEqual(ids, [-1001111111111, -1002222222222])

    def test_invalid_and_all(self):
        ids, invalid, wants_all = parse_channel_ids("-1001111111111 hello ALL")
        self.assertEqual(ids, [-1001111111111])
        self.assertEqual(invalid, ["hello"])
        self.assertTrue(wants_all)

    def test_empty(self):
        self.assertEqual(parse_channel_ids(""), ([], [], False))
        self.assertEqual(parse_channel_ids(None), ([], [], False))


class WelcomeFormatTests(unittest.TestCase):
    def test_placeholders(self):
        out = format_welcome_text("Hi {name} in {chat} #{id}", "Amit", "Deals", 42)
        self.assertEqual(out, "Hi Amit in Deals #42")

    def test_aliases(self):
        out = format_welcome_text("{first_name}|{title}|{chat_title}|{user_id}", "A", "C", 1)
        self.assertEqual(out, "A|C|C|1")

    def test_mention(self):
        self.assertEqual(format_welcome_text("{mention}", "Am[it]", "C", 7), "[Amit](tg://user?id=7)")

    def test_empty_values_do_not_crash(self):
        self.assertEqual(format_welcome_text("Hi {name} @ {chat}", None, "", 1), "Hi Friend @ our Channel")
        self.assertEqual(format_welcome_text(None, "A", "C", 1), "")


class MediaTests(unittest.TestCase):
    def test_photo(self):
        msg = SimpleNamespace(photo=SimpleNamespace(file_id="P1"), video=None, animation=None, document=None)
        self.assertEqual(extract_media(msg), ("photo", "P1"))

    def test_animation_before_document(self):
        msg = SimpleNamespace(photo=None, video=None, animation=SimpleNamespace(file_id="G1"),
                              document=SimpleNamespace(file_id="D1"))
        self.assertEqual(extract_media(msg), ("animation", "G1"))

    def test_no_media(self):
        msg = SimpleNamespace(photo=None, video=None, animation=None, document=None, text="hi")
        self.assertEqual(extract_media(msg), (None, None))


class DashboardHelperTests(unittest.TestCase):
    def test_progress_bar(self):
        self.assertTrue(progress_bar(0, 0).endswith("0%"))
        self.assertTrue(progress_bar(50, 100).endswith("50%"))
        self.assertTrue(progress_bar(200, 100).endswith("100%"))  # never above 100

    def test_sparkline(self):
        self.assertEqual(sparkline([]), "")
        self.assertEqual(sparkline([0, 0]), "▁▁")
        line = sparkline([0, 5, 10])
        self.assertEqual(len(line), 3)
        self.assertEqual(line[-1], "█")

    def test_fill_daily_stats_fills_gaps(self):
        today = datetime.date(2026, 11, 9)
        rows = [{"date": "2026-11-09", "approved": 4}, {"date": "2026-11-07", "approved": 2}]
        out = fill_daily_stats(rows, 3, today)
        self.assertEqual([r["date"] for r in out], ["2026-11-07", "2026-11-08", "2026-11-09"])
        self.assertEqual([r.get("approved", 0) for r in out], [2, 0, 4])

    def test_safe_pct(self):
        self.assertEqual(safe_pct(1, 2), "50%")
        self.assertEqual(safe_pct(1, 0), "—")

    def test_chunked(self):
        self.assertEqual(list(chunked([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]])
        self.assertEqual(list(chunked([], 3)), [])


class WebhookTests(unittest.TestCase):
    def test_valid_join_request(self):
        update = {"update_id": 1, "chat_join_request": {
            "chat": {"id": -1001, "title": "Deals"}, "from": {"id": 5, "first_name": "Sam"}}}
        self.assertEqual(extract_join_request(update),
                         {"chat_id": -1001, "user_id": 5, "name": "Sam", "title": "Deals"})

    def test_ignores_other_updates_and_garbage(self):
        self.assertIsNone(extract_join_request({"message": {}}))
        self.assertIsNone(extract_join_request(None))
        self.assertIsNone(extract_join_request([]))
        self.assertIsNone(extract_join_request({"chat_join_request": {"chat": {}, "from": {}}}))
        self.assertIsNone(extract_join_request({"chat_join_request": {"chat": {"id": "x"}, "from": {"id": 1}}}))


if __name__ == "__main__":
    unittest.main()
