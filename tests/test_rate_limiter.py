from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from license_server.app import SlidingWindowLimiter


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class SlidingWindowLimiterTests(unittest.TestCase):
    def test_time_advance_reclaims_keys_and_global_window(self) -> None:
        clock = FakeClock()
        limiter = SlidingWindowLimiter(
            limit=2,
            window_seconds=10,
            global_limit=3,
            max_keys=3,
            clock=clock,
        )
        self.assertTrue(limiter.allow("one"))
        self.assertTrue(limiter.allow("two"))
        self.assertTrue(limiter.allow("three"))
        self.assertFalse(limiter.allow("four"))

        clock.advance(10)
        self.assertTrue(limiter.allow("four"))
        self.assertEqual(set(limiter.values), {"four"})
        self.assertEqual(len(limiter.global_values), 1)

    def test_unique_key_flood_is_bounded(self) -> None:
        limiter = SlidingWindowLimiter(
            limit=2,
            window_seconds=60,
            global_limit=10_000,
            max_keys=32,
            clock=lambda: 0.0,
        )
        accepted = sum(limiter.allow(f"address-{index}") for index in range(5_000))
        self.assertEqual(accepted, 32)
        self.assertEqual(len(limiter.values), 32)
        self.assertEqual(len(limiter.global_values), 32)

    def test_concurrent_burst_cannot_cross_atomic_limits(self) -> None:
        limiter = SlidingWindowLimiter(
            limit=40,
            window_seconds=60,
            global_limit=40,
            max_keys=8,
            clock=lambda: 0.0,
        )
        with ThreadPoolExecutor(max_workers=32) as pool:
            results = list(pool.map(lambda _index: limiter.allow("same-ip"), range(2000)))
        self.assertEqual(sum(results), 40)
        self.assertEqual(len(limiter.values["same-ip"]), 40)
        self.assertEqual(len(limiter.global_values), 40)

    def test_invalid_limits_fail_at_startup(self) -> None:
        for values in (
            {"limit": 0},
            {"window_seconds": 0},
            {"global_limit": 0},
            {"max_keys": 0},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                SlidingWindowLimiter(**values)


if __name__ == "__main__":
    unittest.main()
