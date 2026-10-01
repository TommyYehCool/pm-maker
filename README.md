# pm-maker

Polymarket 做市工具：掃描器找市場，bot 掛雙邊單。

## 掃描器 (scanner.py)

掃描全部活躍市場，找兩種東西：

- **A. negRisk 機率加總異常**：互斥多選項事件（例如 Fed 決議、選舉），買齊所有 YES 保證拿 $1。若 Σ bestAsk + taker fee < 1，或 Σ bestBid - fee > 1，就是無風險套利。
- **B. 做市候選**：spread 寬、24h 有成交、流動性薄（你的單會被吃到）、離結算還有幾天、且不是新聞/內線驅動的市場。Maker 免手續費還拿回饋，部分市場另有流動性獎勵（`rewards` 欄）。

只用公開 API，不需要錢包。

## 用法

```bash
pip install -r requirements.txt
python scanner.py                      # 全抓 + 分析（約 2 萬事件 / 17 萬市場，7 到 10 分鐘）
python scanner.py --from-cache         # 用上次抓的資料重跑，改參數用這個
python scanner.py --from-cache --books 15   # 對 A 表標記事件和前 15 個做市候選抓 CLOB 即時深度
python scanner.py --help
```

輸出在 `out/<timestamp>/`：`negrisk_all.csv`、`negrisk_flagged.csv`、`mm_candidates.csv`。

## 參數重點

| 參數 | 預設 | 意義 |
|---|---|---|
| `--arb-threshold` | 0.01 | 扣費後 edge 超過幾元才標記 |
| `--mm-min-spread` | 0.03 | 做市最小 spread（3 cents） |
| `--mm-min-vol24h` | 1000 | 24h 至少成交多少 USDC |
| `--mm-min-liq` / `--mm-max-liq` | 500 / 50000 | 流動性區間 |
| `--mm-min-days` / `--mm-max-days` | 2 / 30 | 離結算天數區間（太遠的話單邊成交會鎖資金） |
| `--mm-min-price` / `--mm-max-price` | 0.08 / 0.92 | 排除接近已決定的市場 |
| `--no-exclude` | | 關掉 scanner 的標籤黑名單（`EXCLUDE_TAG_SLUGS`：名人/推文、婚禮、諾貝爾、空投/發幣），只影響做市候選。`rewards_bot.py` 的問題文字黑名單是另一份，不受這個參數影響 |

## 看結果要注意

1. Gamma 的 `bestBid/bestAsk` 有延遲，A 表標記的東西一定要用 `--books` 確認；`live_sets` 是最薄那檔的掛單量，等於最多能買幾套，`live_profit_usd` 是吃完那些單的總利潤。
2. `incomplete_book=True` 表示有選項缺 bid 或 ask（通常是價格 <0.01 的選項），加總用中價補，可信度低。
3. `augmented=True` 的事件選項可能不完整（有 Other 桶或可新增選項），加總 < 1 不一定是套利。
4. 費用公式 `rate * (p*(1-p))^exponent`，只收 taker。做市當 maker 免費。
5. 做市評分只是排序用，不是期望值。真正的風險是 adverse selection，所以黑名單很重要。

## 第一次跑的結果 (2026-09-16)

- 8957 個 negRisk 事件，扣費後只剩 1 個真套利：Eurozone Annual Inflation 2026，Σ ask 0.971 + fee 0.015，edge 1.4 cents，但最薄那檔只掛 15 股，總利潤 $0.21，且資金要鎖到 2027 年 1 月結算。純套利確實已經沒肉。
- 做市候選 136 個，前段多為電競 O/U、小國選舉、YouTube 觀看數等長尾市場，spread 10 到 24 cents，24h 成交 $1.5k 到 $18k，且都在流動性獎勵範圍內。

## 做市 bot (mm_bot.py)

策略：每個市場同時掛「買 YES @ bid」和「買 NO @ (1 - ask)」，只當 maker（`post_only`，穿價會被退單而不是變 taker）。兩邊都成交後手上是 1 YES + 1 NO，結算必拿 $1，成本 = 1 - spread，差額就是利潤，不需要賣任何東西。單邊成交會累積淨庫存，報價會往減倉方向 skew，超過 `max_inventory` 就停掉那一邊。

### 準備

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # PM_PRIVATE_KEY = 設定頁匯出的私鑰；PM_FUNDER = 設定頁「地址」
```

用官方 `polymarket-client` SDK（舊的 `py-clob-client` 已封存，伺服器會回 `invalid order version`）。SDK 會自動判斷錢包型態（email 登入 = POLY_PROXY），不用再設 signature type。

入金：`bridge.polymarket.com/deposit` 用你的錢包地址產生入金地址，Base / Arbitrum / Polygon / Solana 的 USDC 都收，最低 $2，進來會自動包成 pUSD。

地區限制（[官方表](https://docs.polymarket.com/api-reference/geoblock)）：台灣、新加坡、美國等前端和 API 都只能平倉；日本只擋前端、API 不擋。bot 要放在沒被擋的 IP（例如 Vultr Tokyo）。

### 流程

```bash
.venv/bin/python mm_bot.py add --rank 1,3,6 --half-spread 0.04 --size 10   # 從最新 scanner 結果挑第 1、3、6 名
.venv/bin/python mm_bot.py add --market-id 4586972 --half-spread 0.03      # 或直接用 Gamma market id
.venv/bin/python mm_bot.py run            # dry run：只印會掛什麼價，不需要私鑰
.venv/bin/python mm_bot.py status         # 餘額 / 持倉 / 掛單 / 授權狀態
.venv/bin/python mm_bot.py approve        # status 說授權不全時用（需要 Relayer API key，實測掛單不需要）
.venv/bin/python mm_bot.py run --live     # 真的下單，會要你打 yes
touch STOP                                # 執行中：撤全部單並停止
.venv/bin/python mm_bot.py cancel-all     # 手動撤單（Ctrl-C 不會自動撤）
.venv/bin/python test_quotes.py           # 報價數學的單元測試
```

`bot_config.json` 可手動改：`auto_merge`（YES+NO 配成對就 gasless merge 回 pUSD，不等結算；需要 .env 有 builder key）、`half_spread`（中價兩側距離）、`size`（每邊股數，最少 5）、`max_inventory`（淨庫存上限）、`min_days_left`（快結算就撤）、`enabled`、全域 `max_total_exposure_usdc`（掛單 + 持倉成本上限）、`poll_seconds`。

### 紀錄

- `out/bot.log`：每輪的報價、庫存、動作
- `out/fills.jsonl`：每筆成交（從 CLOB trades API 拉，去重）。滿 30 筆再對帳。

### 風險提醒

- 這是 adverse selection 生意：單邊被吃通常代表對方知道什麼。skew 和 max_inventory 是唯一的煞車，不要調太鬆。
- bot 掛掉時掛單不會自動撤，用 `cancel-all` 或 Polymarket 網頁「取消所有」。
- 第一次 live 建議 `max_total_exposure_usdc` 設 30 到 50、`size` 5 到 10、只開 2 到 3 個市場。

### 部署 (Vultr Tokyo 專用機)

```bash
./deploy/deploy.sh pm-maker        # ssh alias；首次建 pmmaker user + venv + systemd，之後只同步程式碼並重啟
ssh pm-maker journalctl -u pm-maker -f          # 看 log
ssh pm-maker touch /opt/pm-maker/STOP           # 撤單並停止 (systemd 會在 15 秒後重啟，要真的停用 systemctl stop pm-maker)
ssh pm-maker systemctl stop pm-maker            # 停 bot，掛單留著
```

主機 `pm-maker`（vc2-1c-1gb，$5/mo，ufw 只開 22，密碼登入關閉）。遠端的 `.env` / `bot_config.json` 不會被 deploy 覆蓋，要改設定直接 ssh 上去改再 `systemctl restart pm-maker`。bot 重啟不會撤單，會接手現有掛單繼續管。

### 實盤結論 (2026-09-16 到 09-18，已停)

20 pUSD、35 筆成交、4 個有成交的市場全部虧：薄市場 (Fable、London) 來吃單的只有知情者；厚市場排隊 (WTI) 平時排不到、排到時就是價格單向跑的時候 (25 筆 -1.13)；體育 in-play (Tigers，掃描器沒看 gameStartTime) -1.50。總結 -3.24 (-16%)。**做市假設「多數成交是隨機流量」在 5 股、20 秒輪詢的規模下一次都沒遇到。** bot 已 `systemctl disable`，持倉抱到結算。

### 首次實盤 (2026-09-16)

10 pUSD、LoL CBLOL O/U 3.5 一個市場、size 5、half_spread 4c。兩張 post_only 單都 `live`，掛上去後我們就是全場最佳買賣價（book 從 0.03/0.22 變 0.08/0.17）。`status` 的 `trading approvals fully set: False` 不影響 CLOB 掛單。

## 流動性獎勵 bot (rewards_bot.py)

做市停掉後的換賽道。Polymarket 每天發約 $15 萬給掛單的人，不用成交：`S = ((v-s)/v)^2 * size`（v = 該市場 max spread，s = 離中價距離），每分鐘取樣、UTC 午夜按分數比例分池子。策略反過來：挑池子大但沒人掛的市場、掛在中價兩檔外領獎勵，**成交越少越好**。

```bash
python rewards_bot.py select --capital 500 --n 5   # 掃全站獎勵市場，寫 rewards_config.json (shadow=true)
python rewards_bot.py run                           # 依 config 跑；systemd: pm-rewards.service
python rewards_bot.py run --live --dry              # live 流程但只印不下單
python rewards_bot.py status                        # 餘額、持倉、掛單、今日 Polymarket 算的實際獎勵
python rewards_bot.py earnings --date 2026-09-19    # 某 UTC 日實際發放
python rewards_bot.py cancel-all
```

- **shadow**：每分鐘抓訂單簿算「如果掛在那裡」能分多少，用公開成交模擬會不會被吃，記被打後 1 小時價格移動。
- **live**：真掛 BUY YES @ bid + BUY NO @ (1-ask)，post_only；庫存用真實持倉；配對自動 merge；每 10 分鐘抓 `list_user_earnings_for_day` 跟自己估的比，跨 UTC 日寫 `daily` 事件（est vs actual）。這是唯一能驗證估算公式的方法。
- 共用的防護：`jump_move`（一輪內中價跳超過 0.03 就撤單暫停 `jump_pause_min` 分鐘）、`min_days_left`（快結算不掛）、mid ≤0.03/≥0.97 不掛、資金上限 `capital`、`STOP` 檔撤單退出。
- 狀態存 `out/rewards_state.json`，重啟接著算；事件在 `out/rewards_events.jsonl`。
- 切 live：`rewards_config.json` 把 `"shadow": false`，`capital` 設成真的上限，砍掉事件驅動的市場，`systemctl restart pm-rewards`。

### Shadow 結果 (2026-09-18 19:00 起，$500 假設本金，6 市場)

27 小時：估算獎勵 $271（平均 $241/天，每小時在 $137 到 $390 之間跳），模擬成交 12 筆 -5.4。競爭者幾小時內就會出現但也會走，佔比一天內從 100% 掉到 6% 又回到 86%。虧損全來自 MrBeast（一週內結算、砸盤前先吃掉買單，-7 到 -9c）和 Brooks & Dunn（CMA 消息一跳 26c）這種**有事件日的市場**，其他成交接近打平。黑名單已加 award/views/video 等字。估算獎勵未對過帳，$241 不能當真；上真錢一天拿實際發放比對才算數。
