from pathlib import Path


def replace(path, old, new):
    p = Path(path)
    s = p.read_text(encoding="utf-8")
    if old not in s:
        raise SystemExit(f"patch target not found in {path}")
    p.write_text(s.replace(old, new), encoding="utf-8")


# Cloud persistent database paths.
p = Path("src/paths.py")
p.write_text('''from __future__ import annotations\n\nimport os\nfrom pathlib import Path\n\n\ndef data_dir() -> Path:\n    raw = os.getenv("V6_DATA_DIR", "").strip()\n    path = Path(raw).expanduser() if raw else Path(".")\n    path.mkdir(parents=True, exist_ok=True)\n    return path\n\n\ndef db_path(filename: str) -> str:\n    return str(data_dir() / filename)\n''', encoding="utf-8")

replace(
    "src/auto_orchestrator.py",
    "from .simulation_engine import SimulationLab\n",
    "from .simulation_engine import SimulationLab\nfrom .paths import db_path\n",
)
replace(
    "src/auto_orchestrator.py",
    '        self.forward=ForwardDB("forward_validation.sqlite3")\n        self.db=SimulationDB("simulation_lab.sqlite3")\n        self.cache=MarketCache("market_cache.sqlite3")\n',
    '        self.forward=ForwardDB(db_path("forward_validation.sqlite3"))\n        self.db=SimulationDB(db_path("simulation_lab.sqlite3"))\n        self.cache=MarketCache(db_path("market_cache.sqlite3"))\n',
)

# SQLite concurrency for dashboard + worker in one service.
p = Path("src/simulation_db.py")
s = p.read_text(encoding="utf-8")
s = s.replace(
    "    def _c(self):\n        c = sqlite3.connect(self.path); c.row_factory = sqlite3.Row; return c\n",
    '    def _c(self):\n        c = sqlite3.connect(self.path, timeout=30)\n        c.row_factory = sqlite3.Row\n        c.execute("PRAGMA journal_mode=WAL")\n        c.execute("PRAGMA busy_timeout=30000")\n        return c\n',
)
p.write_text(s, encoding="utf-8")

for filename in ("src/market_cache.py", "src/forward_db.py"):
    p = Path(filename)
    s = p.read_text(encoding="utf-8")
    s = s.replace("sqlite3.connect(self.path)", "sqlite3.connect(self.path, timeout=30)")
    s = s.replace(
        "c.row_factory = sqlite3.Row\n        return c",
        'c.row_factory = sqlite3.Row\n        c.execute("PRAGMA journal_mode=WAL")\n        c.execute("PRAGMA busy_timeout=30000")\n        return c',
    )
    s = s.replace(
        "conn.row_factory = sqlite3.Row\n        return conn",
        'conn.row_factory = sqlite3.Row\n        conn.execute("PRAGMA journal_mode=WAL")\n        conn.execute("PRAGMA busy_timeout=30000")\n        return conn',
    )
    p.write_text(s, encoding="utf-8")

# Optional password gate for the public cloud URL.
p = Path("dashboard.py")
s = p.read_text(encoding="utf-8")
if "import os\n" not in s:
    s = s.replace("import json, yaml\n", "import json, yaml\nimport os\n")
needle = 'load_dotenv(); st.set_page_config(page_title="V6 Live Dashboard",layout="wide",page_icon="📊")\n'
insert = '''load_dotenv(); st.set_page_config(page_title="V6 Live Dashboard",layout="wide",page_icon="📊")\n\n_required_password = os.getenv("V6_PASSWORD", "")\nif _required_password and not st.session_state.get("v6_authenticated", False):\n    st.title("V6 Web Quant Lab")\n    with st.form("v6_login"):\n        _pw = st.text_input("密碼", type="password")\n        _ok = st.form_submit_button("登入", type="primary")\n    if _ok:\n        if _pw == _required_password:\n            st.session_state["v6_authenticated"] = True\n            st.rerun()\n        else:\n            st.error("密碼錯誤")\n    st.stop()\n'''
if needle in s and "v6_authenticated" not in s:
    s = s.replace(needle, insert)
p.write_text(s, encoding="utf-8")

print("Cloud patch applied")
