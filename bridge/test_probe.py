import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bridge.probe import (
    aggregate_mt5_ticks,
    classify_execution_history,
    initialize_mt5,
    initialization_failure_details,
    protection_price,
    position_ticket_from_deal_history,
    summarize_position_history,
    app,
)


class Mt5InitializationTests(unittest.TestCase):
    def test_initialization_uses_configured_terminal_login_and_server(self):
        with tempfile.NamedTemporaryFile() as terminal:
            captured = {}
            mt5 = SimpleNamespace(
                initialize=lambda **kwargs: captured.update(kwargs) or True
            )
            with patch.dict(
                os.environ,
                {
                    "MT5_TERMINAL_PATH": terminal.name,
                },
            ):
                self.assertTrue(
                    initialize_mt5(
                        mt5,
                        {
                            "accountId": "account-a",
                            "login": "1048288",
                            "password": "saved-password",
                            "server": "Capital.comBah-Demo",
                        },
                    )
                )

        self.assertEqual(captured["path"], terminal.name)
        self.assertEqual(captured["login"], 1048288)
        self.assertEqual(captured["server"], "Capital.comBah-Demo")
        self.assertEqual(captured["password"], "saved-password")

    def test_configured_password_is_passed_only_to_initialize(self):
        with tempfile.NamedTemporaryFile() as terminal:
            captured = {}
            mt5 = SimpleNamespace(
                initialize=lambda **kwargs: captured.update(kwargs) or True
            )
            with patch.dict(
                os.environ,
                {
                    "MT5_TERMINAL_PATH": terminal.name,
                },
            ):
                self.assertTrue(
                    initialize_mt5(
                        mt5,
                        {
                            "accountId": "account-a",
                            "login": "1048288",
                            "password": "local-test-secret",
                            "server": "Capital.comBah-Demo",
                        },
                    )
                )

        self.assertEqual(captured["password"], "local-test-secret")

    def test_missing_identity_config_fails_without_default_terminal_fallback(self):
        mt5 = SimpleNamespace(initialize=lambda **_kwargs: self.fail("must not initialize"))
        with patch.dict(
            os.environ,
            {"MT5_TERMINAL_PATH": ""},
        ):
            empty_account = {
                "accountId": "",
                "login": "",
                "password": "",
                "server": "",
            }
            self.assertFalse(initialize_mt5(mt5, empty_account))
            details = initialization_failure_details(mt5, empty_account)

        self.assertIn("X-MT5-Login", details)
        self.assertIn("X-MT5-Server", details)
        self.assertIn("MT5_TERMINAL_PATH", details)

    def test_initialize_failure_reports_mt5_error_without_credentials(self):
        with tempfile.NamedTemporaryFile() as terminal:
            mt5 = SimpleNamespace(
                initialize=lambda **_kwargs: False,
                last_error=lambda: (10004, "No connection"),
            )
            with patch.dict(
                os.environ,
                {
                    "MT5_TERMINAL_PATH": terminal.name,
                },
            ):
                account_config = {
                    "accountId": "account-a",
                    "login": "1048288",
                    "password": "do-not-report-this",
                    "server": "Capital.comBah-Demo",
                }
                self.assertFalse(initialize_mt5(mt5, account_config))
                details = initialization_failure_details(mt5, account_config)

        self.assertIn("code=10004", details)
        self.assertIn("No connection", details)
        self.assertNotIn("do-not-report-this", details)


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


class PositionHistorySummaryTests(unittest.TestCase):
    def setUp(self):
        self.deals = [
            {
                "position_id": 73,
                "entry": 0,
                "type": 0,
                "price": 1.1,
                "volume": 0.02,
                "profit": 0,
                "commission": -0.1,
                "swap": 0,
                "fee": 0,
                "time": 100,
            },
            {
                "position_id": 73,
                "entry": 1,
                "type": 1,
                "price": 1.12,
                "volume": 0.02,
                "profit": 40,
                "commission": -0.2,
                "swap": -0.1,
                "fee": -0.05,
                "time": 110,
                "reason": 4,
            },
        ]

    def test_returns_actual_close_price_and_net_realized_deal_profit(self):
        result = summarize_position_history(self.deals, 73, False)

        self.assertTrue(result["closed"])
        self.assertEqual(result["direction"], "BUY")
        self.assertAlmostEqual(result["buy_price"], 1.1)
        self.assertAlmostEqual(result["close_price"], 1.12)
        self.assertAlmostEqual(result["profit"], 39.55)
        self.assertEqual(result["close_time"], 110)

    def test_does_not_report_a_still_open_position_as_closed(self):
        result = summarize_position_history(self.deals, 73, True)

        self.assertTrue(result["is_open"])
        self.assertFalse(result["closed"])
        self.assertIsNone(result["profit"])

    def test_does_not_include_deals_from_another_position_ticket(self):
        result = summarize_position_history(
            self.deals
            + [
                {
                    "position_id": 99,
                    "entry": 1,
                    "price": 2.0,
                    "volume": 1.0,
                    "profit": 500,
                }
            ],
            73,
            False,
        )

        self.assertEqual(result["deal_count"], 2)
        self.assertAlmostEqual(result["profit"], 39.55)


class PositionTicketResolutionTests(unittest.TestCase):
    def test_resolves_position_identifier_from_execution_deal(self):
        mt5 = SimpleNamespace(
            history_deals_get=lambda **kwargs: [
                {"deal": kwargs["ticket"], "position_id": 92}
            ]
        )
        result = SimpleNamespace(deal=90)

        self.assertEqual(position_ticket_from_deal_history(mt5, result), 92)

    def test_missing_execution_deal_returns_no_position_identifier(self):
        mt5 = SimpleNamespace(history_deals_get=lambda **_kwargs: ())

        self.assertIsNone(
            position_ticket_from_deal_history(mt5, SimpleNamespace(deal=90))
        )


class PositionHistoryEndpointTests(unittest.TestCase):
    def test_endpoint_queries_requested_ticket_and_returns_history_summary(self):
        deals = [
            {
                "position_id": 88,
                "entry": 1,
                "price": 1.15,
                "volume": 0.01,
                "profit": 3.0,
                "commission": -0.1,
                "swap": 0,
                "fee": 0,
                "time": 120,
            }
        ]
        position_queries = []
        history_queries = []
        mt5 = SimpleNamespace(
            initialize=lambda **_kwargs: True,
            shutdown=lambda: None,
            positions_get=lambda **kwargs: position_queries.append(kwargs) or (),
            history_deals_get=lambda **kwargs: history_queries.append(kwargs)
            or deals,
            DEAL_ENTRY_IN=0,
            DEAL_ENTRY_OUT=1,
            DEAL_ENTRY_INOUT=2,
            DEAL_ENTRY_OUT_BY=3,
            DEAL_TYPE_BUY=0,
        )
        with patch("bridge.probe.load_mt5", return_value=mt5), patch(
            "bridge.probe.initialize_mt5", return_value=True
        ) as initialize:
            response = app.test_client().get(
                "/position-history/88",
                headers={
                    "X-MT5-Bridge-Secret": os.getenv("MT5_AGENT_SECRET", "")
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(initialize.called)
        self.assertEqual(response.json["ticket"], 88)
        self.assertTrue(response.json["closed"])
        self.assertEqual(response.json["close_price"], 1.15)
        self.assertAlmostEqual(response.json["profit"], 2.9)
        self.assertEqual(position_queries, [{"ticket": 88}])
        self.assertEqual(history_queries, [{"position": 88}])


class AccountScopedProbeEndpointTests(unittest.TestCase):
    def test_account_endpoint_initializes_each_request_with_its_worker_config(self):
        with tempfile.NamedTemporaryFile() as terminal:
            calls = []
            active = {}

            class FakeMt5:
                def initialize(self, **kwargs):
                    calls.append(dict(kwargs))
                    active.update(kwargs)
                    return True

                def account_info(self):
                    return SimpleNamespace(
                        login=active["login"],
                        server=active["server"],
                        trade_mode=0,
                        _asdict=lambda: {
                            "login": active["login"],
                            "server": active["server"],
                            "trade_mode": 0,
                        },
                    )

                def terminal_info(self):
                    return SimpleNamespace(connected=True, trade_allowed=True)

                def shutdown(self):
                    return None

            client = app.test_client()
            account_configs = [
                {
                    "accountId": "account-a",
                    "login": "1048288",
                    "password": "password-a",
                    "server": "Capital.comBah-Demo",
                },
                {
                    "accountId": "account-b",
                    "login": "2000001",
                    "password": "password-b",
                    "server": "OtherBroker-Demo",
                },
            ]

            with patch.dict(
                os.environ,
                {
                    "MT5_TERMINAL_PATH": terminal.name,
                    "MT5_AGENT_SECRET": "bridge-test-secret",
                },
                clear=False,
            ), patch("bridge.probe.load_mt5", return_value=FakeMt5()):
                responses = []
                for config in account_configs:
                    responses.append(
                        client.get(
                            "/account",
                            headers={
                                "X-MT5-Bridge-Secret": "bridge-test-secret",
                                "X-MT5-Account-Id": config["accountId"],
                                "X-MT5-Login": config["login"],
                                "X-MT5-Password": config["password"],
                                "X-MT5-Server": config["server"],
                            },
                        )
                    )

        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(
            [(call["login"], call["server"]) for call in calls],
            [
                (1048288, "Capital.comBah-Demo"),
                (2000001, "OtherBroker-Demo"),
            ],
        )
        self.assertEqual([call["password"] for call in calls], ["password-a", "password-b"])


class ExecutionStatusTests(unittest.TestCase):
    def setUp(self):
        self.mt5 = SimpleNamespace(
            DEAL_ENTRY_IN=0,
            DEAL_ENTRY_OUT=1,
            DEAL_ENTRY_INOUT=2,
            DEAL_ENTRY_OUT_BY=3,
            DEAL_TYPE_BUY=0,
            ORDER_STATE_REJECTED=4,
            ORDER_STATE_CANCELED=5,
            ORDER_STATE_EXPIRED=6,
            history_deals_get=lambda **_kwargs: (),
        )

    def test_open_position_is_confirmed_by_stable_order_comment(self):
        position = SimpleNamespace(
            comment="mt5-0123456789abcdef01234567",
            ticket=91,
            symbol="EURUSD",
            type=0,
        )
        result = classify_execution_history(
            "mt5-0123456789abcdef01234567", [position], (), (), self.mt5
        )
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["ticket"], 91)

    def test_rejected_order_is_failed_but_missing_history_is_unknown(self):
        rejected = SimpleNamespace(
            comment="mt5-0123456789abcdef01234567",
            state=self.mt5.ORDER_STATE_REJECTED,
        )
        failed = classify_execution_history(
            "mt5-0123456789abcdef01234567", (), [rejected], (), self.mt5
        )
        unknown = classify_execution_history(
            "mt5-0123456789abcdef01234567", (), (), (), self.mt5
        )

        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(unknown["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()