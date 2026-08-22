# V6 Quant Lab

這是一個 **Paper / Research only** 的多市場量化研究版本，優先做「計算正確、可驗證、避免過度擬合」，而不是追求回測圖看起來最好看。

## 已完成

- 美股 / ETF：Alpaca 官方 Market Data
- Crypto：Binance 官方 Spot Kline
- 4 類策略：Trend MA、Momentum、RSI Mean Reversion、Breakout
- 事件式回測：**t 時點訊號，t+1 開盤才成交**
- Commission / Slippage / Bid-Ask Spread
- Stop loss / Take profit
- 最終持倉強制平倉
- CAGR / Volatility / Sharpe / Sortino / Max Drawdown / Calmar
- Win Rate / Profit Factor / Expectancy / Trade Count
- 參數 Grid Search
- Walk-Forward：訓練窗與完全未看過的 OOS 測試窗分開
- Stress Test：成本 x2/x3、額外滑價
- OHLCV 嚴格資料驗證：重複、缺值、High/Low 邏輯、非正價格、負成交量、時間順序
- **跨標的大數據篩選**：同一參數在多檔資產的 OOS 尾端同時驗證，以 Generalization Score 排名
- Alpaca Paper Broker adapter（**預設禁止送單**）

## 為什麼這版不直接選「歷史報酬第一名」

大量嘗試參數後，最漂亮的回測很可能只是 selection bias / backtest overfitting。
因此 V6 把 OOS Walk-Forward、成本壓力測試與最低交易樣本數列為核心流程。

## 安裝

Windows 最簡單：

1. 解壓縮。
2. 雙擊 `start_v6.bat`。
3. 第一次會建立 `.venv` 並安裝套件。
4. 把 Alpaca Paper API key 填進 `.env`。
5. 瀏覽器會開啟 V6 Quant Lab。

`.env`：

```text
ALPACA_API_KEY=你的KEY
ALPACA_API_SECRET=你的SECRET
V6_ALLOW_PAPER_ORDERS=false
```

不要把 `.env` 傳給別人，也不要把金鑰寫死在 Python 程式內。

## 研究流程

1. 選市場與標的
2. 載入資料
3. 先做單次回測 sanity check
4. 跑「參數大篩選」
5. 跑 Walk-Forward
6. 只對 OOS 穩定者跑壓力測試
7. 通過後才考慮進 Paper Trading 觀察

## 資料與方法的依據

本版本設計參考：
- Alpaca 官方 Market Data / Historical Bars 文件
- Binance 官方 Spot REST / WebSocket / Testnet 文件
- Interactive Brokers 官方 Paper Trading limitations
- Bailey, Borwein, López de Prado & Zhu：Probability of Backtest Overfitting / CSCV

## 目前刻意還沒做

這是第一個「精密核心版」，下列功能沒有假裝已完成：
- 完整股票 survivorship-bias-free 歷史成分股資料庫
- Level 2 order book 的真實 queue-position fill simulator
- 選擇權 Greeks / assignment / exercise 引擎
- Futures roll / margin / expiry 引擎
- PBO/CSCV 完整矩陣實作
- Deflated Sharpe Ratio
- 多資產同時組合最佳化
- 真實資金交易

這些應該在核心驗證通過後逐步加入，而不是混在第一版造成「功能很多但回測不可信」。


## 跨標的大數據篩選

這頁不是把很多股票的結果混在一起看平均而已。每一組參數都會：
1. 對每個標的保留最後 30% 作為 OOS。
2. 分別計算 OOS Return / Sharpe / Max Drawdown / Score。
3. 再看跨標的「正報酬比例、正 Sharpe 比例、中位數、最差回撤、分數離散度」。
4. 只有同一組參數在多個標的都站得住腳，Generalization Score 才會高。

這是用來降低「只剛好適合 NVDA 或 BTC 某段歷史」的風險，不代表保證未來獲利。


## 第二階段：進階統計驗證

新增：
- PBO / CSCV：把每個參數組合的策略報酬組成 trial matrix，做 combinatorially symmetric cross-validation。
- Deflated Sharpe Ratio：以所有試驗 Sharpe 的分布估計 multiple-testing 下的 expected maximum Sharpe，再對最佳策略做 PSR-style 修正。
- Stationary block bootstrap：不是把每一天完全亂洗，而是以隨機區塊抽樣保留部分序列相依，用數千條重抽路徑估計虧損機率與回撤分布。
- Parameter Neighborhood Stability：最佳參數附近如果績效立刻崩掉，會被視為脆弱。
- Market Regime：Bull/Bear × High/Low Volatility + Sideways，逐環境檢查策略。
- Multi-Asset Portfolio：Equal Weight / Inverse Volatility；Inverse-vol 權重使用 shift(1)，避免使用當天尚未知資訊。
- V6 Research Grade：把 OOS、跨標的一致性、PBO、DSR、參數穩定、壓力測試、Bootstrap 合併；缺少測試不會被默認為及格，而會降低 evidence coverage。

### 重要限制

PBO、DSR、Bootstrap 都是「降低錯誤自信」的工具，不是獲利保證。
如果基礎資料存在 survivorship bias、錯誤 corporate-action 調整、成交模型錯誤，再漂亮的統計檢驗也不能修復資料本身。


## 第三階段：自動大範圍掃描

Stage 3 新增一個真正的「自動漏斗」，不是把幾百個標的全部直接做全參數暴力搜尋。

### Funnel

1. **Dynamic Universe**
   - 美股：Alpaca `/v2/assets` 取 active/tradable，再用 snapshot 的當日流動性 proxy 排名；候選 cap 若小於全部資產時，固定包含 Most Active，剩餘用 deterministic hash sample，避免按字母或 API 回傳順序偏掉。
   - Crypto：Binance `/api/v3/exchangeInfo` + `/api/v3/ticker/24hr`，保留 TRADING、指定 quote asset、達到 24h quote volume 門檻的 Spot pairs。

2. **Data Quality Gate**
   - OHLCV 缺值、重複時間、High/Low 邏輯錯、非正價格、負成交量、歷史長度不足直接淘汰。

3. **Coarse OOS Scan**
   - 每種策略只跑 3 組事先固定的代表性參數。
   - 只在最後 30% 資料做快速篩選。
   - 這一步不做 parameter optimization，目的是降低計算量與 data-mining。

4. **Finalists**
   - 每個 symbol 先只留下粗篩最佳策略。
   - 再依 coarse score 選前 N 個進完整深度驗證。

5. **Deep Validation**
   - 完整 parameter grid
   - Walk-Forward OOS
   - Parameter Neighborhood Stability
   - Stress Test
   - 前幾名才跑昂貴的 PBO / DSR / Stationary Block Bootstrap
   - 最後產生 Research Grade + Evidence Coverage

### 免費 Alpaca Basic 的重要限制

Basic equities data 的即時 coverage 是 IEX；因此 Stage 3 用 snapshot 算出的 `dollar_volume_proxy` 是 IEX feed 的流動性 proxy，不是全美股 consolidated SIP 成交量。這個欄位只拿來做候選排序/粗篩，不能當成完整市場成交量研究的替代品。

### Checkpoint

每次完成 Universe / Coarse / Finalists / Deep 時會寫入：

`scan_checkpoint/`

即使關掉 Streamlit，CSV 研究結果仍會留在本機資料夾；API keys 不會寫進 checkpoint。


### Survivorship Bias 標記

Stage 3 的美股 Dynamic Universe 來自「現在 active/tradable」的 Alpaca Assets API。
這非常適合建立 **現在要送進 Paper Trading 的候選池**，但如果拿這批現存公司往回測很多年，仍會有 survivorship bias，因為過去已下市/被併購/失敗的公司不在目前 active universe。

因此 Stage 3 最終表會寫入：

- `current_survivor_universe = True`
- `interpretation = CURRENT-UNIVERSE CANDIDATE RANKING...`

要把策略升級成「歷史全市場無 survivorship bias 的研究」，仍需要 point-in-time universe / delisted securities / historical constituents 資料源。


## 第四階段：Forward Validation Manager

Stage 4 的目的，是把 Stage 3 找到的候選送進「真正時間向前」的驗證。

### 核心規則

- Stage 3 候選一旦註冊，`strategy + params + registered_at` 會一起建立 candidate hash。
- **註冊日以前的任何 K 線都不能計入 Forward 成績。**
- 只處理已完成日 K：
  - Crypto：只接受 UTC 今天以前的 daily bar。
  - 美股：只接受紐約當地今天以前的 daily bar。
- 訊號在 bar `t` 完成後才產生；最早只能在之後的新 bar open 執行。
- 每個 candidate 有獨立虛擬本金與 ledger，避免不同策略互相污染。
- SQLite 使用 WAL，並用 `last_processed_bar` + unique trade key 做冪等控制；重複按「執行一次」不會重複算同一根 K。
- Forward Score 有 evidence discount：早期幾天突然暴賺不會直接拿到滿分。
- Promotion gate 至少要求：
  - 60 forward days
  - 20 closed trades
  - forward return > 0
  - forward Sharpe >= 0.5
  - max drawdown 不低於 -25%
- 通過 gate 只代表「可以延長 Paper 驗證」，**不是實盤授權**。

### 如何讓它持續跑

有兩種方式：

1. 開網頁，按「⑥ 現在執行一次 Forward 檢查」。
2. 雙擊 `start_forward_worker.bat`。
   - 會每小時檢查一次 API。
   - 因為使用日 K，同一根 bar 只會處理一次。
   - 黑色視窗關掉後 worker 就停止，不會在背景偷偷執行。

也可以用 Windows 工作排程器每天執行：

`python run_forward_once.py`

### 儲存

Forward 資料保存在：

`forward_validation.sqlite3`

包含：
- candidates
- forward_state
- forward_trades
- forward_equity
- forward_runs

不要刪除這個 SQLite 檔，否則 Forward 累積證據會遺失。

### Alpaca Paper 與 Shadow Ledger

Alpaca 官方 Paper Trading 是即時模擬環境，能透過 Trading API 提交與查詢訂單。
Stage 4 目前預設先使用 V6 自己的 **Shadow Ledger**，原因是研究上必須保證所有候選使用完全一致的成交成本與規則，方便公平比較。

等 Shadow Forward 累積出穩定候選後，再把少數候選接到 Alpaca Paper Account 做第二層 execution validation 會比較乾淨。

## Stage 5：長／中／短線模擬交易

Stage 5 會把 Stage 4 的每一個 ACTIVE 候選拆成三個完全獨立的 Shadow 帳本：

| 週期 | 定義 | 單筆最大資金 | 停損 | 停利 | 最長持倉 |
|---|---|---:|---:|---:|---:|
| 短線 | 數天級 | 10% | 4% | 8% | 10 根日 K |
| 中線 | 數週級 | 15% | 8% | 20% | 40 根日 K |
| 長線 | 數月級 | 20% | 15% | 40% | 180 根日 K |

設計原則：

- 目前三層都只使用**已完成的日 K**，延續 Stage 4 的 anti-lookahead 規則。
- 短線使用較快參數；中線沿用 Stage 3 已凍結的最佳參數；長線使用較慢參數。
- 每個 sleeve 有自己的 cash / position / trades / equity，不會互相污染。
- 會記錄 `TIME_EXIT`，防止短線策略因訊號一直維持而意外持有數月。
- 每個週期有不同的 evidence maturity：短線重交易樣本、中線兼顧天數與交易數、長線需要更長時間。
- `best_horizon_by_symbol` 會隨 Forward 證據動態顯示同一個標的目前較適合哪一種週期。

### Stage 5 資料庫

`multi_horizon_validation.sqlite3`

不要刪除此檔，否則長／中／短線的 Forward 累積資料會遺失。

## Alpaca Paper Execution Mirror

Stage 5 另外提供**選配**的 Alpaca Paper Mirror，只限美股：

- 預設 `V6_ALLOW_PAPER_ORDERS=false`，完全不送單。
- 要啟用時才把 `.env` 改成 `V6_ALLOW_PAPER_ORDERS=true` 並重啟。
- 程式硬性拒絕任何不是 `paper-api.alpaca.markets` 的 Alpaca endpoint。
- BUY 使用固定美元 notional 的 market/day Paper order。
- SELL 會關閉該 symbol 的 Paper position。
- 同一股票最多一個 mirror slot，避免短／中／長三策略在 Alpaca 的單一淨持倉中互相打架。
- Shadow ledger 才是主要研究帳本；Paper Mirror 是第二層 execution validation。
- Binance Crypto 目前仍只做 V6 Shadow 模擬，不會送 Alpaca Paper 單。

### Worker

`start_forward_worker.bat` 現在每小時依序執行：

1. Stage 4 Forward
2. Stage 5 長／中／短線 Forward
3. 已啟用的 Alpaca Paper Mirror

同一根日 K 與同一筆 Shadow trade 都有冪等控制，不會因每小時檢查而重複計算。

## Stage 6 — Local Quant Simulation Lab
Stage 6 replaces broker paper-order dependency with a local six-account simulation broker. It uses 1H/4H/1D horizons, per-asset horizon calibration, train/OOS scoring, regime-aware NO_TRADE gating, ATR-based risk sizing, volatility/confidence-limited simulated leverage, local order/fill/position/trade/equity ledgers, and an incremental SQLite market-data cache. It never sends broker orders.

## Stage 7 — Auto Live Dashboard
- `start_v6_auto.bat`: one-click normal-use launcher. Starts the local auto worker and the concise dashboard.
- Dashboard auto-refreshes from SQLite every 30 seconds; dashboard refresh itself makes zero API calls.
- Worker imports Stage 4 ACTIVE assets, recalibrates only stale/missing models, refreshes throttled market-data cache, computes short/medium/long decisions, and updates the local virtual broker.
- Broker/order API calls remain zero.
- Market data defaults: Crypto 60s/5m/30m for short/medium/long; Stock 5m/15m/60m. Decisions still only act on completed 1H/4H/1D bars.
- Trade confidence is separated into model confidence, signal strength, regime score and volatility quality.
- Dashboard includes cached latest prices, six-account performance, open positions, latest per-asset/horizon decisions, closed trades and strategy-problem ranking.
