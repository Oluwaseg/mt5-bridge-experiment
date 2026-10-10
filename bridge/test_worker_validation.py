import unittest

from worker_identity import (
    validate_bound_account,
    classify_mt5_failure,
    normalize_order_response,
    pre_submission_failure,
    validate_demo_order_payload,
    validate_demo_position_payload,
    validate_position_history_ticket,
    validate_symbol_order_specs,
)


class DemoOrderPayloadTests(unittest.TestCase):
    def valid_payload(self, **updates):
        payload = {
            "confirm": "DEMO_ONLY",
            "symbol": "EURUSD",
            "side": "BUY",
            "volume": 0.01,
            "stopLoss": 0,
            "takeProfit": 0,
        }
        payload.update(updates)
        return payload

    def test_normalizes_allowed_demo_order(self):
        order = validate_demo_order_payload(self.valid_payload(symbol="eurusd"))
        self.assertEqual(order["symbol"], "EURUSD")
        self.assertEqual(order["side"], "BUY")
        self.assertEqual(order["volume"], 0.01)

    def test_rejects_bad_confirmation_side_symbol_and_nonfinite_volume(self):
        invalid = [
            (self.valid_payload(confirm="LIVE"), "DEMO_ONLY"),
            (self.valid_payload(side="HOLD"), "BUY or SELL"),
            (self.valid_payload(symbol="NOT-A-SYMBOL"), "approved"),
            (self.valid_payload(volume=0.02), "between 0 and 0.01"),
            (self.valid_payload(volume=float("nan")), "between 0 and 0.01"),
        ]
        for payload, message in invalid:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, message):
                    validate_demo_order_payload(payload)

    def test_preserves_valid_client_order_identity_in_mt5_comment(self):
        order = validate_demo_order_payload(
            self.valid_payload(clientOrderId="mt5-0123456789abcdef01234567")
        )
        self.assertEqual(order["comment"], "mt5-0123456789abcdef01234567")
        with self.assertRaisesRegex(ValueError, "clientOrderId is invalid"):
            validate_demo_order_payload(
                self.valid_payload(clientOrderId="not-stable")
            )


class Mt5FailureClassificationTests(unittest.TestCase):
    def test_validation_failure_is_definitive_unsent_and_correlated(self):
        result = pre_submission_failure(
            "Volume does not match symbol volume step", "request-validation"
        )
        self.assertEqual(result["executionState"], "FAILED")
        self.assertEqual(result["requestId"], "request-validation")
        self.assertFalse(result["requestSent"])
        self.assertTrue(result["safeToRetry"])
        self.assertEqual(result["errorCategory"], "VOLUME_ERROR")

    def test_maps_supported_retcode_categories_and_preserves_unknown_as_rejection(self):
        self.assertEqual(classify_mt5_failure("", 10014), "VOLUME_ERROR")
        self.assertEqual(classify_mt5_failure("", 10016), "INVALID_STOPS")
        self.assertEqual(classify_mt5_failure("", 10017), "TRADE_DISABLED")
        self.assertEqual(classify_mt5_failure("", 10018), "MARKET_CLOSED")
        self.assertEqual(classify_mt5_failure("", 10031), "CONNECTION_ERROR")
        self.assertEqual(classify_mt5_failure("", 10009), "TRADE_REJECTED")
        self.assertEqual(
            classify_mt5_failure("unknown terminal response", None),
            "TRADE_REJECTED",
        )

    def test_probe_server_failure_after_submission_is_not_retryable(self):
        result = normalize_order_response(
            {"http_status": 503, "error": "MT5 terminal interrupted"},
            "request-1",
        )
        self.assertEqual(result["executionState"], "UNKNOWN")
        self.assertFalse(result["safeToRetry"])
        self.assertEqual(result["errorCategory"], "UNKNOWN_EXECUTION")

    def test_definitive_rejection_preserves_original_mt5_diagnostics(self):
        result = normalize_order_response(
            {
                "sent": False,
                "result": {"retcode": 10014, "comment": "Invalid volume"},
            },
            "request-2",
        )
        self.assertEqual(result["executionState"], "FAILED")
        self.assertFalse(result["safeToRetry"])
        self.assertEqual(result["errorCategory"], "VOLUME_ERROR")
        self.assertEqual(result["retcode"], 10014)
        self.assertEqual(result["mt5Message"], "Invalid volume")


class BoundAccountPreflightTests(unittest.TestCase):
    def setUp(self):
        self.account_health = {
            "account": {
                "login": 123456,
                "server": "Broker-Demo",
                "trade_mode": 0,
            },
            "terminal": {"connected": True, "tradeAllowed": True},
        }
        self.order = {
            "symbol": "EURUSD",
            "side": "BUY",
            "volume": 0.01,
        }
        self.symbol_info = {
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
            "trade_mode": 4,
        }

    def test_accepts_matching_demo_account_and_live_terminal(self):
        result = validate_bound_account(
            self.account_health, '123456', 'broker-demo'
        )
        self.assertEqual(result["login"], 123456)

    def test_rejects_account_login_or_server_mismatch(self):
        with self.assertRaisesRegex(ValueError, "login does not match"):
            validate_bound_account(self.account_health, '654321', 'Broker-Demo')
        with self.assertRaisesRegex(ValueError, "server does not match"):
            validate_bound_account(self.account_health, '123456', 'Other-Demo')

    def test_rejects_disconnected_or_trade_disabled_terminal(self):
        disconnected = {
            **self.account_health,
            "terminal": {"connected": False, "tradeAllowed": True},
        }
        with self.assertRaisesRegex(ValueError, "not connected"):
            validate_bound_account(disconnected, '123456', 'Broker-Demo')

        trading_disabled = {
            **self.account_health,
            "terminal": {"connected": True, "tradeAllowed": False},
        }
        with self.assertRaisesRegex(ValueError, "trading is not allowed"):
            validate_bound_account(trading_disabled, '123456', 'Broker-Demo')
        self.assertEqual(
            validate_bound_account(
                trading_disabled,
                '123456',
                'Broker-Demo',
                require_trade_allowed=False,
            )["login"],
            123456,
        )

    def test_rejects_non_demo_account(self):
        live_account = {
            **self.account_health,
            "account": {**self.account_health["account"], "trade_mode": 2},
        }
        with self.assertRaisesRegex(ValueError, "not in demo trade mode"):
            validate_bound_account(live_account, '123456', 'Broker-Demo')

    def test_validates_volume_min_max_and_step_from_connected_symbol(self):
        validate_symbol_order_specs(self.order, self.symbol_info)
        with self.assertRaisesRegex(ValueError, "outside symbol limits"):
            validate_symbol_order_specs(
                {**self.order, "volume": 0.005}, self.symbol_info
            )
        with self.assertRaisesRegex(ValueError, "outside symbol limits"):
            validate_symbol_order_specs(
                {**self.order, "volume": 100.01}, self.symbol_info
            )
        with self.assertRaisesRegex(ValueError, "volume step"):
            validate_symbol_order_specs(
                {**self.order, "volume": 0.015}, self.symbol_info
            )

    def test_validates_symbol_trade_mode_for_order_side(self):
        validate_symbol_order_specs(self.order, {**self.symbol_info, "trade_mode": 1})
        with self.assertRaisesRegex(ValueError, "does not allow opening BUY"):
            validate_symbol_order_specs(self.order, {**self.symbol_info, "trade_mode": 2})
        with self.assertRaisesRegex(ValueError, "does not allow opening SELL"):
            validate_symbol_order_specs(
                {**self.order, "side": "SELL"},
                {**self.symbol_info, "trade_mode": 1},
            )
        with self.assertRaisesRegex(ValueError, "disabled or close-only"):
            validate_symbol_order_specs(self.order, {**self.symbol_info, "trade_mode": 0})


class DemoPositionPayloadTests(unittest.TestCase):
    def test_validates_demo_close_ticket(self):
        self.assertEqual(
            validate_demo_position_payload(
                {"ticket": 42, "confirm": "DEMO_ONLY"}
            ),
            {"ticket": 42, "confirm": "DEMO_ONLY"},
        )

    def test_requires_a_valid_demo_ticket_and_protection_level(self):
        for payload in (
            {"ticket": 0, "confirm": "DEMO_ONLY"},
            {"ticket": 42, "confirm": "LIVE"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    validate_demo_position_payload(payload)

        with self.assertRaisesRegex(ValueError, "stopLoss or takeProfit"):
            validate_demo_position_payload(
                {"ticket": 42, "confirm": "DEMO_ONLY"}, protection=True
            )
        with self.assertRaisesRegex(ValueError, "non-negative"):
            validate_demo_position_payload(
                {
                    "ticket": 42,
                    "confirm": "DEMO_ONLY",
                    "stopLoss": float("nan"),
                },
                protection=True,
            )


class PositionHistoryTicketTests(unittest.TestCase):
    def test_accepts_positive_position_ticket(self):
        self.assertEqual(validate_position_history_ticket(123), 123)

    def test_rejects_invalid_position_ticket(self):
        for ticket in (None, 0, -1, "not-a-ticket"):
            with self.subTest(ticket=ticket):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    validate_position_history_ticket(ticket)


if __name__ == "__main__":
    unittest.main()
