from dashboard_v8 import *  # noqa: F401,F403

from src.realtime_dashboard import render_realtime_panel
from src.pro_risk_dashboard import render_professional_risk_panel

render_realtime_panel()
render_professional_risk_panel()
