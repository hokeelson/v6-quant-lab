from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from src.auto_orchestrator_v8 import AutoOrchestratorV8
from src.paths import data_dir

# Dashboard must never run the heavy market/calibration cycle synchronously. Doing so
# blocks Streamlit before it reaches the Realtime panel, making that panel disappear.
# Queue the request on the persistent volume and let the background worker execute it.
_REQUEST_PATH = Path(data_dir()) / "worker_request.json"
_original_calibrate_due = AutoOrchestratorV8.calibrate_due


def _queue_worker_request(kind: str):
    payload = {
        "kind": kind,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "source": "dashboard",
    }
    tmp = _REQUEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_REQUEST_PATH)
    st.session_state["v6_worker_request"] = payload


def _dashboard_full_cycle(self, now=None, force_recalibrate=False):
    _queue_worker_request("force_calibration" if force_recalibrate else "full_cycle")
    st.toast("已交給背景 Worker 執行；頁面與 Realtime 會保持在線。")
    st.rerun()


def _dashboard_calibrate_due(self, now=None, force=False):
    if force:
        _queue_worker_request("force_calibration")
        st.toast("已排入背景分批校準；不會阻塞 Realtime。")
        st.rerun()
    return _original_calibrate_due(self, now=now, force=force)


AutoOrchestratorV8.full_cycle = _dashboard_full_cycle
AutoOrchestratorV8.calibrate_due = _dashboard_calibrate_due

from dashboard_v8 import *  # noqa: F401,F403,E402

from src.realtime_dashboard import render_realtime_panel  # noqa: E402
from src.pro_risk_dashboard import render_professional_risk_panel  # noqa: E402

request = st.session_state.pop("v6_worker_request", None)
if request:
    label = "強制分批校準" if request.get("kind") == "force_calibration" else "立即完整更新"
    st.success(f"{label}已交給背景 Worker；你可以繼續看 Dashboard，不需要等待頁面運算。")

render_realtime_panel()
render_professional_risk_panel()
