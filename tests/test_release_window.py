from datetime import date
import unittest


def calendar_request(url, params):
    month = params["month"]
    year, number = map(int, month.split("-"))
    if number != 10 or year != 2026:
        raise AssertionError(f"unexpected calendar month: {month}")
    open_days = {19, 20, 21, 22, 23, 26, 27, 28, 29, 30}
    return {
        "data": [
            {
                "jyrq": f"{year:04d}-{number:02d}-{day:02d}",
                "jybz": "1" if day in open_days else "0",
            }
            for day in range(1, 32)
        ]
    }


class ReleaseWindowTests(unittest.TestCase):
    def test_weekend_after_friday_has_no_release_window(self):
        from market_data.release_window import next_release_window

        self.assertIsNone(
            next_release_window(
                "2026-10-23",
                "2026-10-25",
                request=calendar_request,
            )
        )

    def test_saturday_catches_up_delayed_friday(self):
        from market_data.release_window import next_release_window

        window = next_release_window(
            "2026-10-22",
            "2026-10-24",
            request=calendar_request,
        )
        self.assertIsNotNone(window)
        self.assertEqual(window.start, "2026-10-23")
        self.assertEqual(window.target, "2026-10-23")
        self.assertEqual(window.sessions, ("2026-10-23",))

    def test_multiple_missed_sessions_are_collected_in_one_batch(self):
        from market_data.release_window import next_release_window

        window = next_release_window(
            "2026-10-23",
            "2026-10-27",
            request=calendar_request,
        )
        self.assertIsNotNone(window)
        self.assertEqual(window.start, "2026-10-26")
        self.assertEqual(window.target, "2026-10-27")
        self.assertEqual(
            window.sessions,
            ("2026-10-26", "2026-10-27"),
        )

    def test_bootstrap_on_weekend_targets_latest_closed_session(self):
        from market_data.release_window import latest_session_on_or_before

        window = latest_session_on_or_before(
            "2026-10-25",
            lookback_days=3,
            request=calendar_request,
        )
        self.assertEqual(window.target, "2026-10-23")
        self.assertEqual(window.start, "2026-10-23")
        self.assertEqual(window.sessions, ("2026-10-23",))

    def test_future_or_invalid_dates_are_rejected(self):
        from market_data.release_window import next_release_window

        with self.assertRaisesRegex(ValueError, "previous release date"):
            next_release_window("not-a-date", "2026-10-25", request=calendar_request)
        with self.assertRaisesRegex(ValueError, "upper bound"):
            next_release_window("2026-10-23", "not-a-date", request=calendar_request)


if __name__ == "__main__":
    unittest.main()
