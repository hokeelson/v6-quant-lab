"""Read-only progress presentation shared by both dashboards."""
from .worker_progress import PHASE_LABELS, progress_caption, public_progress


def render_worker_progress(st, raw):
    p = public_progress(raw)
    if not p.get("progress_schema_version"):
        st.caption("目前版本尚未提供分段進度；心跳不等於分析已完成。")
        return
    st.caption(progress_caption(p))
    total = p.get("phase_total") or 0
    completed = p.get("phase_completed") or 0
    if total > 0:
        st.progress(max(0.0, min(1.0, completed / total)))
    current = p.get("cycle_elapsed_seconds")
    previous = p.get("last_cycle_duration_seconds")
    st.caption(
        f"本輪耗時：{float(current):.0f} 秒" if isinstance(current, (int, float)) else "本輪尚未開始"
    )
    if isinstance(previous, (int, float)):
        st.caption(f"上一輪耗時：{previous:.0f} 秒")
    if p.get("last_cycle_slow_units"):
        with st.expander("上一輪最慢標的（含處理 K 棒數）", expanded=False):
            st.dataframe(p["last_cycle_slow_units"], hide_index=True, use_container_width=True)
    if p.get("recent_cycles"):
        with st.expander("最近 20 輪耗時（程序重啟後重新累積）", expanded=False):
            st.dataframe(p["recent_cycles"], hide_index=True, use_container_width=True)
    durations = p.get("phase_durations_seconds") or {}
    previous_durations = p.get("last_cycle_phase_durations_seconds") or {}
    phases = [phase for phase in PHASE_LABELS if phase in durations or phase in previous_durations]
    if phases:
        with st.expander("各階段耗時（秒）", expanded=False):
            st.dataframe([
                {"階段": PHASE_LABELS[phase], "本輪": durations.get(phase),
                 "上一輪": previous_durations.get(phase)}
                for phase in phases
            ], hide_index=True, use_container_width=True)
