from __future__ import annotations
import os, yaml
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.data import AlpacaData, BinanceData, validate_ohlcv
from src.backtest import ExecutionCosts, RiskRules, run_backtest
from src.research import grid_search, walk_forward, stress_test, strategy_signal
from src.cross_asset import cross_asset_grid_search
from src.advanced_research import advanced_validation
from src.portfolio import aligned_strategy_returns, portfolio_backtest
from src.robustness import final_research_grade
from src.forward_db import ForwardDB
from src.forward import (
    ForwardManager, ForwardConfig, register_from_stage3, rank_forward,
    promotion_decision
)
from src.horizon_db import HorizonDB
from src.horizon import (
    HorizonManager, HorizonConfig, register_three_horizons, rank_horizons,
    best_horizon_by_symbol, HORIZON_LABELS, HORIZON_PROFILES
)
from src.paper import AlpacaPaperBroker
from src.paper_mirror import PaperMirrorDB, AlpacaPaperMirror
from src.simulation_db import SimulationDB
from src.market_cache import MarketCache
from src.simulation_engine import SimulationLab
from src.decision_engine import HORIZON_SPECS
from src.scanner import (
    discover_stock_universe, discover_crypto_universe,
    coarse_strategy_scan, select_finalists, deep_validate_finalists,
    save_scan_checkpoint, ScanThresholds, STRATEGIES
)

load_dotenv()
st.set_page_config(page_title="V6 Quant Lab", layout="wide")
st.title("V6 Quant Lab — 精密多市場模擬研究")
st.caption("Paper / Research only. 核心規則：訊號在 t 產生，最早於 t+1 開盤成交，避免 look-ahead。")

with open("config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

market = st.sidebar.selectbox("市場", ["美股 / ETF", "Crypto"])
source = st.sidebar.selectbox("資料源", ["Alpaca", "Binance"] if market=="Crypto" else ["Alpaca"])
symbols = cfg["universe"]["stocks"] if market=="美股 / ETF" else cfg["universe"]["crypto"]
symbol = st.sidebar.selectbox("標的", symbols)
start = st.sidebar.date_input("開始日期", pd.Timestamp("2018-01-01") if market=="美股 / ETF" else pd.Timestamp("2020-01-01"))
end = st.sidebar.date_input("結束日期", pd.Timestamp.today())
strategy = st.sidebar.selectbox("策略", ["Trend MA","Momentum","Mean Reversion RSI","Breakout"])

mkt_key = "stock" if market=="美股 / ETF" else "crypto"
ec = cfg["execution"][mkt_key]
costs = ExecutionCosts(**ec)
risk = RiskRules(
    max_position_pct=cfg["risk"]["max_position_pct"],
    stop_loss_pct=cfg["risk"]["stop_loss_pct"],
    take_profit_pct=cfg["risk"]["take_profit_pct"],
)
bars_per_year = cfg["research"]["bars_per_year_stock_daily"] if mkt_key=="stock" else cfg["research"]["bars_per_year_crypto_daily"]

@st.cache_data(ttl=900, show_spinner=False)
def load_data(market, source, symbol, start, end):
    start_s, end_s = str(start), str(end)
    if source == "Alpaca":
        return AlpacaData().bars(symbol, start_s, end_s, "1Day", feed="iex")
    return BinanceData().bars(symbol, start_s, end_s, "1d")

if st.button("載入資料", type="primary"):
    try:
        data = load_data(market, source, symbol, start, end)
        st.session_state["data"] = data
        st.success(f"載入 {len(data):,} 根 K 線")
    except Exception as e:
        st.error(str(e))

data = st.session_state.get("data")
if data is not None and len(data):
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Bars", f"{len(data):,}")
    c2.metric("Start", str(data.index.min().date()))
    c3.metric("End", str(data.index.max().date()))
    validation = validate_ohlcv(data)
    c4.metric("Missing OHLCV", str(validation["missing"]))
    critical = sum(validation[k] for k in ["duplicates","missing","bad_high","bad_low","nonpositive_price","negative_volume","non_monotonic_time"])
    if critical:
        st.error(f"資料驗證發現 {critical} 個異常，建議先不要拿此資料做策略結論。")
        st.json(validation)
    else:
        st.success("OHLCV 結構驗證通過")
    st.line_chart(data["close"])

    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(["單次回測","參數大篩選","Walk-Forward","壓力測試","跨標的大數據","進階統計驗證","多資產Portfolio"])

    with tab1:
        default_params = {
            "Trend MA":{"fast":20,"slow":100},
            "Momentum":{"lookback":60,"threshold":0.03},
            "Mean Reversion RSI":{"rsi_n":14,"entry":30,"exit_":55},
            "Breakout":{"lookback":55,"exit_lookback":20},
        }[strategy]
        sig = strategy_signal(strategy, data, default_params)
        result = run_backtest(
            data, sig, cfg["research"]["initial_capital"], costs, risk,
            bars_per_year, cfg["research"]["annual_risk_free_rate"]
        )
        st.json(result["metrics"])
        st.line_chart(result["equity"])
        st.dataframe(result["trades"], width="stretch")

    with tab2:
        if st.button("開始完整參數篩選"):
            with st.spinner("Running..."):
                ranking = grid_search(
                    data, strategy, cfg["research"]["initial_capital"], costs, risk,
                    bars_per_year, cfg["research"]["annual_risk_free_rate"],
                    cfg["research"]["min_trades"]
                )
                st.session_state["ranking"] = ranking
        ranking = st.session_state.get("ranking")
        if ranking is not None:
            st.dataframe(ranking.head(cfg["research"]["top_n_results"]), width="stretch")

    with tab3:
        if st.button("開始 Walk-Forward"):
            with st.spinner("Running..."):
                wf = walk_forward(
                    data, strategy, cfg["research"]["initial_capital"], costs, risk,
                    bars_per_year, cfg["research"]["train_bars"], cfg["research"]["test_bars"],
                    cfg["research"]["step_bars"], cfg["research"]["annual_risk_free_rate"],
                    cfg["research"]["min_trades"]
                )
                st.session_state["wf"] = wf
        wf = st.session_state.get("wf")
        if wf is not None:
            st.dataframe(wf, width="stretch")
            if len(wf):
                st.metric("OOS 平均 Score", f"{wf['oos_score'].mean():.1f}")
                st.metric("OOS Sharpe 中位數", f"{wf['sharpe'].median():.2f}")

    with tab4:
        ranking = st.session_state.get("ranking")
        if ranking is None or ranking.empty:
            st.info("先到「參數大篩選」跑完，再做壓力測試。")
        elif st.button("壓力測試目前第一名"):
            params = ranking.iloc[0]["params"]
            stress = stress_test(
                data, strategy, params, cfg["research"]["initial_capital"], costs, risk,
                bars_per_year, cfg["research"]["annual_risk_free_rate"]
            )
            st.session_state["stress"] = stress
            st.dataframe(stress, width="stretch")


    with tab5:
        st.write("同一組參數直接拿去測多個標的的 OOS 尾端資料；只有跨標的仍穩定，Generalization Score 才會高。")
        max_assets = st.slider("本次跨標的數量", 3, min(20, len(symbols)), min(8, len(symbols)))
        selected_assets = st.multiselect("標的", symbols, default=symbols[:max_assets], max_selections=max_assets)
        if st.button("開始跨標的大數據篩選"):
            batch = {}
            progress = st.progress(0)
            for i, sym in enumerate(selected_assets):
                try:
                    batch[sym] = load_data(market, source, sym, start, end)
                except Exception as e:
                    st.warning(f"{sym}: {e}")
                progress.progress((i+1)/max(len(selected_assets),1))
            good = {k:v for k,v in batch.items() if len(v) >= 120 and sum(validate_ohlcv(v)[q] for q in ["duplicates","missing","bad_high","bad_low","nonpositive_price","negative_volume","non_monotonic_time"]) == 0}
            if len(good) < 3:
                st.error("有效標的少於 3 個，無法做有意義的跨標的比較。")
            else:
                cross = cross_asset_grid_search(
                    good, strategy, cfg["research"]["initial_capital"], costs, risk,
                    bars_per_year, cfg["research"]["annual_risk_free_rate"],
                    cfg["research"]["min_trades"], oos_fraction=0.30
                )
                st.session_state["cross"] = cross
        cross = st.session_state.get("cross")
        if cross is not None:
            st.dataframe(cross.head(cfg["research"]["top_n_results"]), width="stretch")
            if len(cross):
                top = cross.iloc[0]
                st.metric("最佳 Generalization Score", f"{top['generalization_score']:.1f}")
                st.metric("OOS 正報酬標的比例", f"{top['positive_asset_ratio']*100:.1f}%")
                st.metric("OOS Sharpe 中位數", f"{top['median_oos_sharpe']:.2f}")

    with tab6:
        st.subheader("PBO / Deflated Sharpe / Bootstrap / Regime / 參數穩定度")
        st.caption("這一頁的目的不是找更漂亮的回測，而是專門找出『看起來很好、其實可能過度擬合』的策略。")
        ranking = st.session_state.get("ranking")
        if ranking is None or ranking.empty:
            st.info("先到「參數大篩選」跑完。")
        else:
            pbo_parts = st.selectbox("CSCV partitions", [4, 6, 8, 10], index=2)
            bootstrap_paths = st.select_slider("Bootstrap paths", options=[500, 1000, 2000, 5000], value=2000)
            if st.button("執行進階統計驗證"):
                with st.spinner("Running advanced validation..."):
                    adv = advanced_validation(
                        data, strategy, ranking,
                        cfg["research"]["initial_capital"], costs, risk, bars_per_year,
                        cfg["research"]["annual_risk_free_rate"],
                        pbo_partitions=pbo_parts,
                        bootstrap_paths=bootstrap_paths
                    )
                    st.session_state["advanced"] = adv

            adv = st.session_state.get("advanced")
            if adv is not None:
                pbo = adv["pbo"]
                dsr = adv["dsr"]
                bs = adv["bootstrap_summary"]
                stab = adv["stability"]

                a,b,c,d = st.columns(4)
                a.metric("PBO", "N/A" if pd.isna(pbo.get("pbo")) else f"{pbo['pbo']*100:.1f}%")
                b.metric("Deflated Sharpe probability", "N/A" if pd.isna(dsr.get("deflated_sharpe_probability")) else f"{dsr['deflated_sharpe_probability']*100:.1f}%")
                c.metric("Parameter Stability", f"{stab.get('stability_score',0):.1f}")
                d.metric("Bootstrap loss probability", "N/A" if not bs else f"{bs.get('probability_loss', float('nan'))*100:.1f}%")

                st.write("**PBO / CSCV**")
                st.json({k:v for k,v in pbo.items() if k != "oos_rank_percentiles"})
                st.write("**Deflated Sharpe**")
                st.json(dsr)
                st.write("**Stationary Block Bootstrap**")
                st.json(bs)
                st.write("**Parameter Neighborhood Stability**")
                st.json(stab)
                st.write("**不同市場 Regime 的策略表現**")
                st.dataframe(adv["regime_table"], width="stretch")

                if adv["bootstrap"] is not None and len(adv["bootstrap"]):
                    st.write("Bootstrap 終值報酬分布")
                    st.bar_chart(
                        adv["bootstrap"]["terminal_return"].value_counts(
                            bins=30, sort=False
                        )
                    )

    with tab7:
        st.subheader("多資產 Portfolio 回測")
        st.caption("先各自產生策略 equity curve，再用前一期資訊決定 equal-weight 或 inverse-volatility 權重；權重不使用未來資料。")
        max_port_assets = st.slider("Portfolio 標的數", 3, min(12, len(symbols)), min(6, len(symbols)), key="portfolio_asset_count")
        port_assets = st.multiselect(
            "Portfolio 標的",
            symbols,
            default=symbols[:max_port_assets],
            max_selections=max_port_assets,
            key="portfolio_assets"
        )
        alloc_method = st.selectbox("配置方式", ["inverse_vol", "equal"])
        rebalance_cost_bps = st.number_input("換倉成本 (bps)", min_value=0.0, max_value=100.0, value=3.0, step=0.5)
        if st.button("執行 Portfolio 回測"):
            ranking = st.session_state.get("ranking")
            if ranking is None or ranking.empty:
                st.error("請先跑目前策略的參數大篩選。")
            else:
                params = ranking.iloc[0]["params"]
                curves = {}
                progress = st.progress(0)
                for i, sym in enumerate(port_assets):
                    try:
                        d = load_data(market, source, sym, start, end)
                        if len(d) >= 120:
                            s = strategy_signal(strategy, d, params)
                            bt = run_backtest(
                                d, s, cfg["research"]["initial_capital"], costs, risk,
                                bars_per_year, cfg["research"]["annual_risk_free_rate"]
                            )
                            curves[sym] = bt["equity"]
                    except Exception as e:
                        st.warning(f"{sym}: {e}")
                    progress.progress((i+1)/max(len(port_assets),1))

                pr = aligned_strategy_returns(curves)
                if pr.shape[1] < 2:
                    st.error("有效標的不足 2 個。")
                else:
                    port = portfolio_backtest(
                        pr, cfg["research"]["initial_capital"], bars_per_year,
                        method=alloc_method, rebalance_cost_bps=rebalance_cost_bps,
                        lookback=60, max_weight=0.35,
                        rf_annual=cfg["research"]["annual_risk_free_rate"]
                    )
                    st.session_state["portfolio"] = port

        port = st.session_state.get("portfolio")
        if port is not None:
            st.json(port["metrics"])
            st.line_chart(port["equity"])
            st.write("最近權重")
            st.dataframe(port["weights"].tail(30), width="stretch")

        st.divider()
        st.subheader("研究證據總評")
        wf = st.session_state.get("wf")
        cross = st.session_state.get("cross")
        adv = st.session_state.get("advanced")
        stress = st.session_state.get("stress")

        oos_score = float(wf["oos_score"].mean()) if wf is not None and len(wf) and "oos_score" in wf else None
        generalization = float(cross.iloc[0]["generalization_score"]) if cross is not None and len(cross) else None
        pbo_v = adv["pbo"].get("pbo") if adv is not None else None
        dsr_v = adv["dsr"].get("deflated_sharpe_probability") if adv is not None else None
        stability_v = adv["stability"].get("stability_score") if adv is not None else None
        boot_loss = adv["bootstrap_summary"].get("probability_loss") if adv is not None and adv.get("bootstrap_summary") else None

        stress_survival = None
        if stress is not None and len(stress) and "sharpe" in stress:
            base_sh = stress.iloc[0]["sharpe"]
            stressed = stress["sharpe"].iloc[1:].replace([float("inf"),-float("inf")], pd.NA).dropna()
            if pd.notna(base_sh) and len(stressed):
                if base_sh > 0:
                    stress_survival = float(max(0, min(100, stressed.median()/base_sh*100)))
                else:
                    stress_survival = 0.0

        grade = final_research_grade(
            oos_score, generalization, pbo_v, dsr_v,
            stability_v, stress_survival, boot_loss
        )
        st.metric("V6 Research Grade", f"{grade['grade']:.1f}/100")
        st.metric("Evidence Coverage", f"{grade['evidence_coverage']*100:.0f}%")
        st.json(grade["components"])

st.divider()
st.header("V6 Stage 3 — 自動大範圍掃描器")
st.caption(
    "三層漏斗：① 動態找流動性/可交易標的 → ② 固定少量參數做快速 OOS 粗篩 "
    "→ ③ 只有 Finalists 才跑完整 Grid、Walk-Forward、壓力測試、PBO/DSR/Bootstrap。"
)

with st.expander("Stage 3 設定", expanded=True):
    s31, s32, s33, s34 = st.columns(4)
    scan_market = s31.selectbox("掃描市場", ["美股 / ETF", "Crypto"], key="s3_market")
    scan_max_symbols = s32.number_input(
        "候選池上限", min_value=20, max_value=500, value=150 if scan_market=="美股 / ETF" else 120,
        step=10, key="s3_max_symbols"
    )
    scan_finalists = s33.number_input(
        "完整深度驗證 Finalists", min_value=3, max_value=30, value=10, step=1,
        key="s3_finalist_count"
    )
    advanced_top_n = s34.number_input(
        "跑 PBO/DSR 的最終組數", min_value=1, max_value=15, value=5, step=1,
        key="s3_advanced_top"
    )

    s35, s36, s37, s38 = st.columns(4)
    min_bars_scan = s35.number_input("最低歷史 Bars", min_value=120, max_value=2000, value=300, step=20)
    min_trades_scan = s36.number_input("粗篩最低平倉交易數", min_value=1, max_value=100, value=5, step=1)
    max_dd_scan = s37.slider("粗篩最大容許回撤", 0.10, 0.90, 0.60, 0.05)
    min_score_scan = s38.slider("粗篩最低分", 0.0, 100.0, 35.0, 1.0)

    strategies_scan = st.multiselect(
        "要掃的策略", STRATEGIES, default=STRATEGIES, key="s3_strategies"
    )

    if scan_market == "美股 / ETF":
        q1, q2, q3 = st.columns(3)
        stock_snapshot_cap = q1.number_input(
            "動態抽查 active assets 數", min_value=500, max_value=8000, value=3000, step=500,
            help="會先包含 Alpaca Most Active，再用 deterministic hash sample 補滿，避免只掃到字母前段。"
        )
        stock_min_price = q2.number_input("最低股價", min_value=0.1, max_value=100.0, value=1.0, step=0.5)
        stock_min_dv = q3.number_input(
            "最低當日 dollar-volume proxy", min_value=0.0, max_value=1_000_000_000.0,
            value=500_000.0, step=100_000.0,
            help="Basic Alpaca 使用 IEX feed 時，這是 IEX 流動性 proxy，不等於全市場 SIP 成交量。"
        )
    else:
        q1, q2 = st.columns(2)
        crypto_min_quote_volume = q1.number_input(
            "最低 24h Quote Volume (USDT)", min_value=0.0, max_value=10_000_000_000.0,
            value=2_000_000.0, step=500_000.0
        )
        crypto_quote = q2.selectbox("Quote Asset", ["USDT", "USDC", "FDUSD"], index=0)

thresholds = ScanThresholds(
    min_bars=int(min_bars_scan),
    min_closed_trades=int(min_trades_scan),
    max_drawdown_abs=float(max_dd_scan),
    min_coarse_score=float(min_score_scan),
    finalist_count=int(scan_finalists),
)

scan_mkt_key = "stock" if scan_market == "美股 / ETF" else "crypto"
scan_exec_cfg = cfg["execution"][scan_mkt_key]
scan_costs = ExecutionCosts(**scan_exec_cfg)
scan_bars_per_year = (
    cfg["research"]["bars_per_year_stock_daily"]
    if scan_mkt_key == "stock"
    else cfg["research"]["bars_per_year_crypto_daily"]
)

if st.button("① 建立動態候選池", type="primary", key="s3_discover"):
    try:
        if scan_market == "美股 / ETF":
            universe_s3 = discover_stock_universe(
                AlpacaData(),
                max_assets_to_snapshot=int(stock_snapshot_cap),
                max_symbols=int(scan_max_symbols),
                min_price=float(stock_min_price),
                min_dollar_volume_proxy=float(stock_min_dv),
                feed="iex",
            )
        else:
            universe_s3 = discover_crypto_universe(
                BinanceData(),
                max_symbols=int(scan_max_symbols),
                min_quote_volume=float(crypto_min_quote_volume),
                quote_asset=crypto_quote,
            )
        st.session_state["s3_universe"] = universe_s3
        save_scan_checkpoint("scan_checkpoint", universe=universe_s3)
    except Exception as e:
        st.error(f"候選池建立失敗：{e}")

universe_s3 = st.session_state.get("s3_universe")
if universe_s3 is not None:
    st.subheader("動態候選池")
    st.metric("候選數", len(universe_s3))
    st.dataframe(universe_s3.head(200), width="stretch")

    if st.button("② 下載候選歷史資料", key="s3_load_history"):
        scan_symbols = universe_s3["symbol"].astype(str).head(int(scan_max_symbols)).tolist()
        try:
            if scan_market == "美股 / ETF":
                datasets_s3 = AlpacaData().bars_many(
                    scan_symbols, str(start), str(end), timeframe="1Day",
                    adjustment="all", feed="iex", batch_size=40
                )
            else:
                datasets_s3 = {}
                pb = st.progress(0)
                for i, sym in enumerate(scan_symbols):
                    try:
                        datasets_s3[sym] = BinanceData().bars(sym, str(start), str(end), "1d")
                    except Exception as e:
                        st.warning(f"{sym}: {e}")
                    pb.progress((i+1)/max(len(scan_symbols),1))
            # Keep only structurally valid datasets with enough history.
            valid = {}
            invalid_rows = []
            for sym, d in datasets_s3.items():
                v = validate_ohlcv(d)
                critical = sum(v[k] for k in [
                    "duplicates","missing","bad_high","bad_low",
                    "nonpositive_price","negative_volume","non_monotonic_time"
                ])
                if critical == 0 and len(d) >= thresholds.min_bars:
                    valid[sym] = d
                else:
                    invalid_rows.append({"symbol":sym, "bars":len(d), "critical_errors":critical})
            st.session_state["s3_datasets"] = valid
            st.session_state["s3_invalid"] = pd.DataFrame(invalid_rows)
            st.success(f"有效歷史資料：{len(valid)} / {len(scan_symbols)}")
        except Exception as e:
            st.error(f"歷史資料下載失敗：{e}")

datasets_s3 = st.session_state.get("s3_datasets")
if datasets_s3 is not None:
    st.write(f"已載入有效資料：**{len(datasets_s3)}** 個標的")
    invalid_s3 = st.session_state.get("s3_invalid")
    if invalid_s3 is not None and len(invalid_s3):
        with st.expander("被資料品質/歷史長度淘汰的標的"):
            st.dataframe(invalid_s3, width="stretch")

    if st.button("③ 執行快速 OOS 粗篩", key="s3_coarse_button"):
        pg = st.progress(0)
        status = st.empty()
        def _p(done, total, label):
            pg.progress(done/max(total,1))
            status.caption(label)
        coarse_s3 = coarse_strategy_scan(
            datasets_s3,
            cfg["research"]["initial_capital"], scan_costs, risk, scan_bars_per_year,
            thresholds, cfg["research"]["annual_risk_free_rate"],
            strategies=strategies_scan, progress=_p
        )
        finalists_s3 = select_finalists(coarse_s3, thresholds)
        st.session_state["s3_coarse"] = coarse_s3
        st.session_state["s3_finalists"] = finalists_s3
        save_scan_checkpoint(
            "scan_checkpoint",
            universe=universe_s3, coarse=coarse_s3, finalists=finalists_s3
        )

coarse_s3 = st.session_state.get("s3_coarse")
if not isinstance(coarse_s3, pd.DataFrame):
    coarse_s3 = None
if coarse_s3 is not None:
    st.subheader("快速 OOS 粗篩")
    passed = int(coarse_s3["passes_coarse"].sum()) if not coarse_s3.empty else 0
    c1,c2,c3 = st.columns(3)
    c1.metric("策略×標的測試", len(coarse_s3))
    c2.metric("通過粗篩", passed)
    c3.metric("通過率", f"{passed/max(len(coarse_s3),1)*100:.1f}%")
    st.dataframe(coarse_s3.head(200), width="stretch")

finalists_s3 = st.session_state.get("s3_finalists")
if not isinstance(finalists_s3, pd.DataFrame):
    finalists_s3 = None
if finalists_s3 is not None and not finalists_s3.empty:
    st.subheader("Finalists")
    st.dataframe(finalists_s3, width="stretch")

    if st.button("④ 執行完整深度驗證", key="s3_deep_button"):
        pg2 = st.progress(0)
        status2 = st.empty()
        def _p2(done, total, label):
            pg2.progress(done/max(total,1))
            status2.caption(label)
        deep_s3 = deep_validate_finalists(
            datasets_s3, finalists_s3,
            cfg["research"]["initial_capital"], scan_costs, risk, scan_bars_per_year,
            cfg["research"]["train_bars"], cfg["research"]["test_bars"],
            cfg["research"]["step_bars"],
            cfg["research"]["annual_risk_free_rate"],
            cfg["research"]["min_trades"],
            advanced_top_n=int(advanced_top_n),
            bootstrap_paths=1000,
            progress=_p2
        )
        deep_s3["current_survivor_universe"] = (scan_market == "美股 / ETF")
        deep_s3["interpretation"] = (
            "CURRENT-UNIVERSE CANDIDATE RANKING; requires forward paper validation and survivorship-bias-free research data"
            if scan_market == "美股 / ETF"
            else "CURRENT-LIQUIDITY UNIVERSE CANDIDATE RANKING; requires forward paper validation"
        )
        st.session_state["s3_deep"] = deep_s3
        save_scan_checkpoint(
            "scan_checkpoint",
            universe=universe_s3, coarse=coarse_s3,
            finalists=finalists_s3, deep=deep_s3
        )

deep_s3 = st.session_state.get("s3_deep")
if not isinstance(deep_s3, pd.DataFrame):
    deep_s3 = None
if deep_s3 is not None:
    st.subheader("最終深度排名")
    st.dataframe(deep_s3, width="stretch")
    if not deep_s3.empty:
        winner = deep_s3.iloc[0]
        w1,w2,w3,w4 = st.columns(4)
        w1.metric("目前第一名", f"{winner['symbol']} / {winner['strategy']}")
        w2.metric("Research Grade", f"{winner['research_grade']:.1f}")
        w3.metric("OOS Score", "N/A" if pd.isna(winner['oos_score']) else f"{winner['oos_score']:.1f}")
        w4.metric("Evidence Coverage", f"{winner['evidence_coverage']*100:.0f}%")

        if bool(winner.get("current_survivor_universe", False)):
            st.warning(
                "美股候選池來自『目前 active/tradable 資產』，因此多年回測仍有 survivorship bias。"
                "這個排名適合挑『現在值得進 Paper forward validation 的候選』，不能當成無偏的歷史全市場績效證明。"
            )
        st.warning(
            "這是研究排名，不是『最佳獲利保證』。即使排名第一，也應先進 Paper Trading 做真正時間向前的 forward validation。"
        )

        csv_bytes = deep_s3.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "下載最終排名 CSV", csv_bytes, "V6_stage3_final_ranking.csv",
            "text/csv", key="s3_download"
        )

st.divider()
st.header("V6 Stage 4 — Forward Validation Manager")
st.caption(
    "只計算候選註冊後、真正收完的日 K。Stage 3 的歷史成績和 Stage 4 的 Forward 成績完全分開。"
)

forward_db = ForwardDB("forward_validation.sqlite3")
forward_cfg = ForwardConfig(
    stock_costs=ExecutionCosts(**cfg["execution"]["stock"]),
    crypto_costs=ExecutionCosts(**cfg["execution"]["crypto"]),
    risk=RiskRules(
        max_position_pct=cfg["risk"]["max_position_pct"],
        stop_loss_pct=cfg["risk"]["stop_loss_pct"],
        take_profit_pct=cfg["risk"]["take_profit_pct"],
    ),
    stock_bars_per_year=cfg["research"]["bars_per_year_stock_daily"],
    crypto_bars_per_year=cfg["research"]["bars_per_year_crypto_daily"],
)
forward_manager = ForwardManager(forward_db, forward_cfg)

deep_for_forward = st.session_state.get("s3_deep")
if not isinstance(deep_for_forward, pd.DataFrame):
    deep_for_forward = None
if deep_for_forward is not None and not deep_for_forward.empty:
    f1, f2 = st.columns(2)
    fwd_top_n = f1.number_input(
        "註冊 Stage 3 前 N 名", min_value=1,
        max_value=min(20, len(deep_for_forward)), value=min(5, len(deep_for_forward)), step=1
    )
    fwd_min_grade = f2.slider("最低 Research Grade", 0.0, 100.0, 50.0, 1.0)
    if st.button("⑤ 凍結並註冊到 Forward", key="s4_register"):
        reg_market = "stock" if scan_market == "美股 / ETF" else "crypto"
        registered = register_from_stage3(
            forward_db, deep_for_forward, reg_market,
            cfg["research"]["initial_capital"],
            top_n=int(fwd_top_n), min_research_grade=float(fwd_min_grade)
        )
        if len(registered):
            st.success(f"已註冊 {len(registered)} 個候選。參數與註冊時間已凍結。")
            st.dataframe(registered, width="stretch")
        else:
            st.warning("沒有候選達到目前 Research Grade 門檻。")
else:
    st.info("Stage 3 跑出最終深度排名後，可直接把前幾名凍結註冊到 Forward。")

registered_candidates = pd.DataFrame(forward_db.candidates())
if len(registered_candidates):
    st.subheader("已註冊 Forward 候選")
    show_cols = [c for c in [
        "candidate_id","market","symbol","strategy","registered_at",
        "research_grade","evidence_coverage","status"
    ] if c in registered_candidates.columns]
    st.dataframe(registered_candidates[show_cols], width="stretch")

    if st.button("⑥ 現在執行一次 Forward 檢查", type="primary", key="s4_run_once"):
        with st.spinner("只處理註冊後且已收完的新日 K..."):
            result = forward_manager.run_once()
            st.session_state["s4_last_run"] = result

    last_run = st.session_state.get("s4_last_run")
    if last_run:
        if last_run.get("errors"):
            st.warning(last_run)
        else:
            st.success(
                f"檢查 {last_run['candidates_checked']} 個候選；"
                f"新增處理 {last_run['bars_processed']} 根 Forward bars。"
            )

    ranking_fwd = rank_forward(
        forward_db,
        cfg["research"]["bars_per_year_stock_daily"],
        cfg["research"]["bars_per_year_crypto_daily"]
    )
    if len(ranking_fwd):
        st.subheader("Forward 真實時間向前排名")
        st.dataframe(ranking_fwd, width="stretch")

        top_fwd = ranking_fwd.iloc[0].to_dict()
        a,b,c,d = st.columns(4)
        a.metric("目前第一名", f"{top_fwd['symbol']} / {top_fwd['strategy']}")
        b.metric("Forward Score", f"{top_fwd['forward_score']:.1f}")
        c.metric("Forward Evidence", f"{top_fwd['forward_evidence']*100:.0f}%")
        d.metric("Forward Days", int(top_fwd["forward_days"]))

        decision = promotion_decision(top_fwd)
        if decision["eligible_for_extended_paper"]:
            st.success(
                "目前已通過『延長 Paper 驗證』門檻；這不是實盤授權。"
            )
        else:
            st.info("尚未通過延長 Paper 門檻：" + ", ".join(decision["reasons"]))

        selected_cid = st.selectbox(
            "查看候選明細",
            ranking_fwd["candidate_id"].tolist(),
            format_func=lambda cid: (
                ranking_fwd.loc[ranking_fwd["candidate_id"]==cid, "symbol"].iloc[0]
                + " / "
                + ranking_fwd.loc[ranking_fwd["candidate_id"]==cid, "strategy"].iloc[0]
            )
        )
        eq_rows = forward_db.equity(selected_cid)
        tr_rows = forward_db.trades(selected_cid)
        if eq_rows:
            eq_show = pd.DataFrame(eq_rows)
            eq_show["bar_time"] = pd.to_datetime(eq_show["bar_time"], utc=True)
            st.line_chart(eq_show.set_index("bar_time")["equity"])
        if tr_rows:
            st.dataframe(pd.DataFrame(tr_rows), width="stretch")

        fcsv = ranking_fwd.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "下載 Forward Ranking CSV", fcsv,
            "V6_stage4_forward_ranking.csv", "text/csv"
        )

    st.warning(
        "Stage 4 預設是本機 Shadow Paper Ledger，不會送真錢訂單。"
        "而且目前使用日 K，為避免 partial-bar bias，美股/Crypto 都採保守 closed-bar 規則，"
        "可能比市場實際收盤晚一個檢查週期才入帳。"
    )
else:
    st.caption("目前還沒有 Forward 候選。")

st.divider()
st.header("V6 Stage 5 — 長／中／短線模擬交易")
st.caption(
    "三個獨立 Shadow 帳本都使用日 K，避免把尚未驗證的 intraday 引擎混進來。"
    "短線＝數天、中線＝數週、長線＝數月；每組本金、停損停利、最長持倉與績效完全分開。"
)

profile_rows = []
for hz in ["short","medium","long"]:
    p = HORIZON_PROFILES[hz]
    profile_rows.append({
        "週期": HORIZON_LABELS[hz],
        "單筆最大資金": f"{p['max_position_pct']*100:.0f}%",
        "停損": f"{p['stop_loss_pct']*100:.0f}%",
        "停利": f"{p['take_profit_pct']*100:.0f}%",
        "最長持倉（日K）": p["max_holding_bars"],
        "完整證據目標天數": p["evidence_days"],
        "完整證據目標交易數": p["evidence_trades"],
    })
st.dataframe(pd.DataFrame(profile_rows), width="stretch", hide_index=True)

horizon_db = HorizonDB("multi_horizon_validation.sqlite3")
horizon_cfg = HorizonConfig(
    stock_costs=ExecutionCosts(**cfg["execution"]["stock"]),
    crypto_costs=ExecutionCosts(**cfg["execution"]["crypto"]),
    stock_bars_per_year=cfg["research"]["bars_per_year_stock_daily"],
    crypto_bars_per_year=cfg["research"]["bars_per_year_crypto_daily"],
)
horizon_manager = HorizonManager(horizon_db, horizon_cfg)

active_forward = pd.DataFrame(forward_db.candidates("ACTIVE"))
if len(active_forward):
    h1,h2 = st.columns(2)
    horizon_capital = h1.number_input(
        "每個週期／候選的獨立虛擬本金", min_value=1000.0, max_value=10_000_000.0,
        value=float(cfg["research"]["initial_capital"]), step=1000.0,
    )
    h2.metric("Stage 4 ACTIVE 候選", len(active_forward))
    if st.button("⑦ 為所有 ACTIVE 候選建立短／中／長三套帳本", key="s5_register_horizons"):
        created = register_three_horizons(
            forward_db, horizon_db, initial_capital_per_sleeve=float(horizon_capital)
        )
        st.success(
            f"已建立/確認 {len(created)} 個週期袖套（每個 Stage 4 候選 × 3）。"
            "重複按不會重置原本的註冊時間與績效。"
        )
else:
    st.info("先在 Stage 4 註冊候選，才能建立長／中／短線帳本。")

sleeves_df = pd.DataFrame(horizon_db.sleeves())
if len(sleeves_df):
    st.subheader("已建立的長／中／短線帳本")
    show = sleeves_df.copy()
    show["週期"] = show["horizon"].map(HORIZON_LABELS)
    cols = [c for c in ["market","symbol","strategy","週期","registered_at","initial_capital","research_grade","status"] if c in show.columns]
    st.dataframe(show[cols], width="stretch")

    if st.button("⑧ 現在執行一次長／中／短線 Forward 檢查", type="primary", key="s5_run_once"):
        with st.spinner("只處理註冊後且已收完的新日 K..."):
            s5_result = horizon_manager.run_once()
            st.session_state["s5_last_run"] = s5_result
    s5_last = st.session_state.get("s5_last_run")
    if s5_last:
        if s5_last.get("errors"):
            st.warning(s5_last)
        else:
            st.success(
                f"檢查 {s5_last['sleeves_checked']} 個週期帳本；"
                f"新增處理 {s5_last['bars_processed']} 根日 K。"
            )

    hz_rank = rank_horizons(
        horizon_db,
        cfg["research"]["bars_per_year_stock_daily"],
        cfg["research"]["bars_per_year_crypto_daily"],
    )
    if len(hz_rank):
        st.subheader("長／中／短線 Forward 排名")
        filter_label = st.selectbox("排名週期", ["全部","短線（數天）","中線（數週）","長線（數月）"], key="s5_horizon_filter")
        view = hz_rank if filter_label == "全部" else hz_rank[hz_rank["horizon_label"] == filter_label]
        st.dataframe(view, width="stretch")
        if len(view):
            top_h = view.iloc[0]
            x1,x2,x3,x4 = st.columns(4)
            x1.metric("目前第一名", f"{top_h['symbol']} / {top_h['horizon_label']}")
            x2.metric("週期分數", f"{top_h['horizon_score']:.1f}")
            x3.metric("證據成熟度", f"{top_h['evidence']*100:.0f}%")
            x4.metric("累積天數", int(top_h["forward_days"]))

        st.write("**每個標的目前最適合的週期（依 Forward 證據動態變化）**")
        st.dataframe(best_horizon_by_symbol(hz_rank), width="stretch")

        chosen_sleeve = st.selectbox(
            "查看週期帳本明細", hz_rank["sleeve_id"].tolist(), key="s5_detail_sleeve",
            format_func=lambda sid: (
                f"{hz_rank.loc[hz_rank['sleeve_id']==sid,'symbol'].iloc[0]} / "
                f"{hz_rank.loc[hz_rank['sleeve_id']==sid,'horizon_label'].iloc[0]} / "
                f"{hz_rank.loc[hz_rank['sleeve_id']==sid,'strategy'].iloc[0]}"
            ),
        )
        h_eq = horizon_db.equity(chosen_sleeve)
        h_tr = horizon_db.trades(chosen_sleeve)
        if h_eq:
            h_eq_df = pd.DataFrame(h_eq); h_eq_df["bar_time"] = pd.to_datetime(h_eq_df["bar_time"],utc=True)
            st.line_chart(h_eq_df.set_index("bar_time")["equity"])
        if h_tr:
            st.dataframe(pd.DataFrame(h_tr), width="stretch")

        hcsv = hz_rank.to_csv(index=False).encode("utf-8-sig")
        st.download_button("下載長中短線排名 CSV", hcsv, "V6_stage5_multi_horizon_ranking.csv", "text/csv")

    st.subheader("Alpaca Paper Execution Mirror")
    st.info("Stage 6 已改用本地虛擬券商。此版不再由主介面自動查詢或同步 Alpaca Paper 帳戶，避免不必要的交易 API 呼叫。")

    st.warning(
        "Stage 5 的『短線』目前是數天級，不是當沖/1小時線。這是刻意的：先把日 K 長中短三層 Forward 驗證穩定，"
        "之後再獨立加入 1H/15m intraday 引擎，避免時間尺度混用造成 look-ahead 或成交模型失真。"
    )
else:
    st.caption("目前還沒有長／中／短線帳本。")



st.divider()
st.header("V6 Stage 6 — Local Quant Simulation Lab")
st.caption(
    "本地虛擬券商：不送 Alpaca/Binance 交易訂單。六個帳戶使用相同起始資金，"
    "美股/Crypto × 短/中/長獨立競爭；行情採 SQLite 快取，只補抓缺少的新 K 線。"
)

sim_db = SimulationDB("simulation_lab.sqlite3")
market_cache = MarketCache("market_cache.sqlite3")
sim_lab = SimulationLab(sim_db, market_cache, initial_equity=float(cfg["research"]["initial_capital"]))
sim_db.ensure_accounts(float(cfg["research"]["initial_capital"]))

spec_table=[]
for hz,label in [("short","短線"),("medium","中線"),("long","長線")]:
    sp=HORIZON_SPECS[hz]
    spec_table.append({
        "週期":label,
        "主要K線":"1H" if hz=="short" else "4H" if hz=="medium" else "1D",
        "每筆風險預算":f"{sp['risk_budget']*100:.2f}%",
        "單標的基礎資金上限":f"{sp['max_position']*100:.0f}%",
        "模擬槓桿上限":f"{sp['max_leverage']:.1f}x",
        "最低決策信心":sp["confidence"],
    })
st.dataframe(pd.DataFrame(spec_table),width="stretch",hide_index=True)

s61,s62,s63=st.columns(3)
s61.metric("本地交易 API", "0 筆")
s62.metric("行情更新", "只補新 K 線")
s63.metric("虛擬帳戶", "6 個")

if st.button("⑩ 匯入 Stage 4 ACTIVE 標的到 Simulation Lab", key="s6_import_assets"):
    rows=forward_db.candidates("ACTIVE")
    n=sim_lab.import_assets(rows)
    st.success(f"已匯入/確認 {n} 筆 ACTIVE 標的。每個標的會分別接受短、中、長三種打法研究。")

with st.expander("手動增加研究標的"):
    a1,a2=st.columns(2)
    manual_market=a1.selectbox("市場",["stock","crypto"],key="s6_manual_market")
    manual_symbol=a2.text_input("Symbol，例如 AAPL / BTCUSDT",key="s6_manual_symbol")
    if st.button("加入標的",key="s6_manual_add") and manual_symbol.strip():
        sim_db.add_asset(manual_market,manual_symbol.strip().upper())
        st.success("已加入。")

assets6=pd.DataFrame(sim_db.assets())
if len(assets6):
    st.subheader("Simulation Lab 研究標的")
    st.dataframe(assets6,width="stretch",hide_index=True)

    if st.button("⑪ 校準所有標的 × 短中長策略",type="primary",key="s6_calibrate_all"):
        with st.spinner("從本地快取補資料，並讓每個標的/週期獨立比較 Trend、Momentum、Mean Reversion、Breakout 的參數與 OOS 表現..."):
            cal=sim_lab.calibrate_all()
            st.session_state["s6_calibration"]=cal
    cal=st.session_state.get("s6_calibration")
    if cal:
        if cal.get("errors"):
            st.warning(f"完成 {cal.get('calibrated',0)} 組；另有 {len(cal['errors'])} 組需要處理。")
            st.dataframe(pd.DataFrame(cal["errors"]),width="stretch")
        else:
            st.success(f"完成 {cal.get('calibrated',0)} 組標的×週期模型校準。")

    models6=pd.DataFrame(sim_db.models())
    if len(models6):
        st.subheader("每個標的目前找到的適配打法")
        showm=models6.copy()
        showm["週期"]=showm["horizon"].map({"short":"短線","medium":"中線","long":"長線"})
        keep=[c for c in ["market","symbol","週期","strategy","calibration_score","oos_score","train_score","regime_fit","calibrated_through"] if c in showm.columns]
        st.dataframe(showm[keep].sort_values(["market","symbol","週期"]),width="stretch",hide_index=True)

    if st.button("⑫ 現在執行一次 Local Forward 模擬",type="primary",key="s6_run_once"):
        with st.spinner("只處理新的完整 K 線；上一根收盤決策最早在下一根開盤成交..."):
            rr=sim_lab.run_once()
            st.session_state["s6_run"]=rr
    rr=st.session_state.get("s6_run")
    if rr:
        if rr.get("errors"):
            st.warning(rr)
        else:
            st.success(
                f"檢查 {rr['assets_checked']} 個 標的×週期；處理 {rr['bars_processed']} 根新 K；"
                f"本次行情來源回傳 {rr['api_rows_fetched']} 列。交易訂單 API = 0。"
            )

    st.subheader("六個等本金模擬帳戶")
    acct6=pd.DataFrame(sim_lab.account_summary())
    if len(acct6):
        acct6["報酬率"]=acct6["return_pct"].map(lambda x:f"{x*100:.2f}%")
        acct6["回撤"]=acct6["drawdown"].map(lambda x:f"{x*100:.2f}%")
        st.dataframe(acct6[["account_id","initial_equity","equity","報酬率","cash","gross_exposure","leverage","回撤","positions"]],width="stretch",hide_index=True)
        selected_account=st.selectbox("查看帳戶 Equity",acct6["account_id"].tolist(),key="s6_account_chart")
        eq6=sim_db.equity(selected_account)
        if eq6:
            eqdf=pd.DataFrame(eq6); eqdf["bar_time"]=pd.to_datetime(eqdf["bar_time"],utc=True)
            st.line_chart(eqdf.set_index("bar_time")["equity"])

    d6=pd.DataFrame(sim_db.recent_decisions(200))
    if len(d6):
        st.subheader("最近決策 — ENTER / EXIT / NO_TRADE")
        cols=[c for c in ["bar_time","account_id","symbol","action","confidence","strategy","regime","atr_pct","requested_notional","leverage","reason"] if c in d6.columns]
        st.dataframe(d6[cols],width="stretch",hide_index=True)

    t6=pd.DataFrame(sim_db.recent_trades(200))
    if len(t6):
        st.subheader("本地虛擬成交與已平倉結果")
        st.dataframe(t6,width="stretch",hide_index=True)

    diag6=pd.DataFrame(sim_db.diagnostics(200))
    if len(diag6):
        st.subheader("問題診斷紀錄")
        st.dataframe(diag6,width="stretch",hide_index=True)

    with st.expander("重置 Simulation Lab（不影響 Stage 3/4/5）"):
        confirm_reset=st.checkbox("我確認要清除 Stage 6 的模型、虛擬交易與績效，研究標的名單保留",key="s6_reset_confirm")
        new_cap=st.number_input("重置後每個帳戶起始資金",min_value=1000.0,max_value=10000000.0,value=float(cfg["research"]["initial_capital"]),step=1000.0,key="s6_reset_cap")
        if st.button("重置 Stage 6",disabled=not confirm_reset,key="s6_reset_button"):
            sim_db.reset_lab(float(new_cap))
            st.success("Stage 6 已重置；Stage 4/5 SQLite 不受影響。")
else:
    st.info("先按『⑩ 匯入 Stage 4 ACTIVE 標的』，或手動增加研究標的。")

st.warning(
    "Stage 6 的槓桿只存在本地模擬帳本，不會向交易所借款或送單。"
    "槓桿會受風險預算、ATR 波動、OOS 信心與市場 Regime 限制；借貸成本亦會在負現金時逐 bar 扣除。"
)
