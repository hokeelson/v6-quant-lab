"""Display exported evidence only; no database writes or order actions."""
from datetime import datetime, timezone


def render_execution_audit(st, research):
    audit = research.get("execution_audit") if isinstance(research, dict) else None
    with st.expander("成交流程驗證與虧損分解", expanded=False):
        if not isinstance(audit, dict):
            st.caption("尚無逐筆驗證資料；不能用成交總數代替完整流程驗收。")
            return
        stamp = audit.get("generated_at")
        st.caption(f"驗證資料時間（UTC）：{stamp}｜狀態：{audit.get('status', 'UNKNOWN')}")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
            if age > 300:
                st.warning(f"驗證資料落後 {age / 60:.0f} 分鐘，不能視為即時狀態。")
        except (ValueError, TypeError, AttributeError):
            st.warning("驗證資料時間不明，不能視為即時狀態。")
        trace = audit.get("lifecycle") or {}
        labels = {"VALIDATED_CLOSED": "已串接完成平倉", "VALIDATED_OPEN": "已串接進場／尚持倉",
                  "VALIDATED_BLOCKED": "禁單且已取消", "UNRESOLVED": "缺少關聯證據",
                  "NOT_FILLED": "尚未成交", "ERROR": "關聯或禁單異常"}
        st.dataframe([{"驗證結果": labels.get(k, k), "筆數": v} for k, v in (trace.get("summary") or {}).items()],
                     hide_index=True, use_container_width=True)
        if trace.get("entries"):
            st.dataframe(trace["entries"], hide_index=True, use_container_width=True)
        coverage = trace.get("coverage") or {}
        st.caption(f"僅最近 {coverage.get('sampled_buy_orders', 0)} 筆進場訂單；舊資料不推測配對。持倉中不等於已完成平倉。")
        pnl = audit.get("pnl_attribution") or {}
        for key, label in (("by_account", "按帳戶"), ("by_strategy", "按策略"),
                           ("by_symbol", "按標的"), ("by_exit_reason", "按出場原因")):
            rows = (pnl.get("groups") or {}).get(key)
            if rows:
                st.caption(f"累計已平倉損益：{label}（損益最低前 50 組）")
                st.dataframe(rows, hide_index=True, use_container_width=True)
        st.caption("以上是帳本記錄損益分組，不是單日報酬或虧損因果。成交價已含成本調整，不重複扣費；未包含融資費用與未實現損益，各帳戶分開。")
