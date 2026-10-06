import unittest
from types import SimpleNamespace

from bridge.probe import aggregate_mt5_ticks, protection_price


class ProtectionPriceTests(unittest.TestCase):
    def setUp(self):
        self.info = SimpleNamespace(
            trade_tick_size=0.01,
            trade_tick_value=1.0,
            trade_tick_value_loss=1.0,
            trade_tick_value_profit=1.0,
            digits=2,
        )

    def test_buy_stop_and_target_convert_currency_risk_to_native_prices(self):
        stop = protection_price(self.info, 100.0, "BUY", 0.1, 5.0, False)
        target = protection_price(self.info, 100.0, "BUY", 0.1, 5.0, True)

        self.assertEqual(stop, 99.5)
        self.assertEqual(target, 100.5)

    def test_sell_stop_and_target_use_the_opposite_price_direction(self):
        stop = protection_price(self.info, 100.0, "SELL", 0.1, 5.0, False)
        target = protection_price(self.info, 100.0, "SELL", 0.1, 5.0, True)

        self.assertEqual(stop, 100.5)
        self.assertEqual(target, 99.5)

    def test_nonzero_protection_requires_valid_tick_values(self):
        self.info.trade_tick_size = 0
        with self.assertRaises(ValueError):
            protection_price(self.info, 100.0, "BUY", 0.1, 5.0, False)


class SecondCandleAggregationTests(unittest.TestCase):
    def test_mt5_ticks_aggregate_to_ohlc_and_fill_empty_intervals(self):
        candles = aggregate_mt5_ticks(
            [
                {"time": 100, "last": 1.0, "bid": 0.9, "volume": 1},
                {"time": 102, "last": 1.2, "bid": 1.1, "volume": 2},
                {"time": 130, "last": 1.5, "bid": 1.4, "volume": 1},
            ],
            10,
            5,
        )

        self.assertEqual(
            [(bar["epoch"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) for bar in candles],
            [
                (100, 1.0, 1.2, 1.0, 1.2, 3),
                (110, 1.2, 1.2, 1.2, 1.2, 0),
                (120, 1.2, 1.2, 1.2, 1.2, 0),
                (130, 1.5, 1.5, 1.5, 1.5, 1),
            ],
        )


if __name__ == "__main__":
    unittest.main()