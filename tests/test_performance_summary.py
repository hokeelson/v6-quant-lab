from src.performance_summary import build_performance_summary


class FakeDB:
    def recent_trades(self, limit):
        return [
            {"account_id": "crypto", "horizon": "short", "realized_pnl": 100, "return_pct": 0.02, "exit_reason": "REALTIME_ATR_TARGET", "entry_order_id": "a"},
            {"account_id": "crypto", "horizon": "short", "realized_pnl": -50, "return_pct": -0.01, "exit_reason": "REALTIME_ATR_STOP", "entry_order_id": "b"},
        ]

    def equity(self, aid, limit):
        return [{"drawdown": -0.03}, {"drawdown": -0.01}]

    def diagnostics(self, limit):
        return [
            {"category": "RISK_SIZING", "payload_json": '{"order_id":"a","binance_context_multiplier":0.7}'},
            {"category": "RISK_SIZING", "payload_json": '{"order_id":"b","binance_context_multiplier":1.0}'},
        ]


def test_performance_summary_groups_binance_context():
    summary = build_performance_summary(FakeDB())
    assert summary["overall"]["trades"] == 2
    assert summary["overall"]["win_rate"] == 0.5
    assert summary["overall"]["profit_factor"] == 2.0
    assert summary["max_drawdown"] == -0.03
    assert summary["binance_context_comparison"]["reduced"]["trades"] == 1
    assert summary["binance_context_comparison"]["normal"]["trades"] == 1
