from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from src.paths import data_dir

# The legacy dashboard still contains synchronous button handlers.  Intercept only
# those two heavy buttons at render time: the real Streamlit button is shown, but
# dashboard_v8 always receives False so it can never execute the heavy cycle inside
# the web process.  The background worker consumes the queued request instead.
_REQUEST_PATH = Path(data_dir()) / "worker_request.json"
_original_button = st.button


def _queue_worker_request(kind: str):
    payload = {
        "kind": kind,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "source": "dashboard",
    }
    tmp = _REQUEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_REQUEST_PATH)
    st.session_state["v6_worker_request_notice"] = payload


def _safe_button(label, *args, **kwargs):
    if label == "立即完整更新":
        clicked = _original_button(label, *args, **kwargs)
        if clicked:
            _queue_worker_request("full_cycle")
        # Critical invariant: never allow dashboard_v8 to enter its synchronous
        # engine.full_cycle() branch.
        return False
    if label == "強制重新校準全部模型":
        clicked = _original_button(label, *args, **kwargs)
        if clicked:
            _queue_worker_request("force_calibration")
        # Never allow the legacy synchronous calibrate_due(force=True) branch.
        return False
    return _original_button(label, *args, **kwargs)


st.button = _safe_button
try:
    from dashboard_v8 import *  # noqa: F401,F403,E402
finally:
    # Keep Streamlit normal for the realtime/professional panels below.
    st.button = _original_button

from src.realtime_dashboard import render_realtime_panel  # noqa: E402
from src.pro_risk_dashboard import render_professional_risk_panel  # noqa: E402

notice = st.session_state.pop("v6_worker_request_notice", None)
if notice:
    label = "強制分批校準" if notice.get("kind") == "force_calibration" else "立即完整更新"
    st.success(f"{label}已交給背景 Worker；頁面不會執行重運算，Realtime 與 Risk 區塊會保持顯示。")

render_realtime_panel()
render_professional_risk_panel()
