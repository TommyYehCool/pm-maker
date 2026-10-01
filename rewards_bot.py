#!/usr/bin/env python3
"""流動性獎勵 bot：shadow (模擬) 與 live (真單) 兩種模式。

收入來源不是 spread，是 Polymarket 每分鐘取樣、每天 UTC 午夜發的流動性獎勵：
  S = ((v - s) / v)^2 * size     v = 該市場 max spread, s = 掛單離中價距離
  中價在 0.1~0.9 外要雙邊才算分；池子按所有 maker 的分數比例分。

兩種模式共用同一套報價：中價 ± offset_ticks，庫存越偏往減倉方向退 tick；跳價 (jump_move) 先撤單暫停。
  shadow：每分鐘抓訂單簿算「如果我們掛在那裡」能分到多少獎勵，用公開成交模擬會不會被吃。
  live  ：真的掛 BUY YES @ bid 和 BUY NO @ (1-ask)，post_only；成交從帳號成交紀錄記；配對自動 merge；
          每 10 分鐘抓 Polymarket 算的今日實際獎勵 (list_user_earnings_for_day) 跟自己估的比對。

  python rewards_bot.py select --capital 500 --n 5      # 掃市場、寫 rewards_config.json (shadow=true)
  python rewards_bot.py run                              # 依 config 的 shadow 旗標跑 (systemd: pm-rewards.service)
  python rewards_bot.py run --live [--yes]               # 強制 live
  python rewards_bot.py run --live --dry                 # live 流程但不真的下單 (印出來)
  python rewards_bot.py status                           # 餘額、持倉、掛單、今日實際獎勵
  python rewards_bot.py earnings [--date 2026-09-19]     # 某天 Polymarket 實際發放 (UTC 日)
  python rewards_bot.py cancel-all
  touch STOP                                             # 執行中：撤單並停止

  out/rewards_status.json  給 dashboard /rewards 看
  out/rewards_events.jsonl 成交 / 被打後 1 小時價格 / 每小時摘要 / 每日對帳
  out/rewards_state.json   累計數字，重啟接著算
"""
import argparse
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP

import requests

from mm_bot import days_until, get_positions, make_client, round_price
from ws_book import BookWatcher
from risk import (MARKOUT_WINDOWS, MarketLedger, cluster_loss_bound, liquidation_value, markout_terms,
                  queue_price, ref_reliable, replay, worst_loss)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
CONFIG_PATH = os.path.join(HERE, "rewards_config.json")
STATUS_PATH = os.path.join(OUT_DIR, "rewards_status.json")
EVENTS_PATH = os.path.join(OUT_DIR, "rewards_events.jsonl")
STATE_PATH = os.path.join(OUT_DIR, "rewards_state.json")
FILLS_PATH = os.path.join(OUT_DIR, "rewards_fills.jsonl")
LOG_PATH = os.path.join(OUT_DIR, "rewards.log")
LEDGER_PATH = os.path.join(OUT_DIR, "ledger.json")
PERF_PATH = os.path.join(OUT_DIR, "perf_daily.jsonl")
HALT_FILE = os.path.join(OUT_DIR, "HALT_INCREASE")  # 日損觸發後建立；人工刪除才恢復增倉
KILL_FILE = os.path.join(HERE, "STOP")
GAMMA = "https://gamma-api.polymarket.com"

# 問題文字黑名單：新聞/內線驅動、有「事件日」會跳價的不做
# (shadow 實測：MrBeast 觀看數、CMA 獎項這種一週內有消息的市場，一跳 8~26c 就把掛單順路吃掉)
EXCLUDE_WORDS = re.compile(r"tweet|mentions|bankrupt|resign|indict|arrest|"
                           r"award|grammy|\bcma\b|oscar|emmy|of the year|views|video|mrbeast|week 1|launch", re.I)

DEFAULTS = {"poll_seconds": 60, "reprice_ticks": 0, "reprice_move": 0.003, "skew_move": 0.02, "jump_move": 0.03, "jump_pause_min": 15,
            "min_days_left": 1.0, "auto_merge": True, "earnings_check_min": 10,
            "ws_enabled": True, "ws_move": 0.008,   # 簿子中價動超過這個就提早叫醒主迴圈重新報價
            # v1.3 減倉：不錨定歷史成本 (那是處分效應)。出場價相對「現價」讓步，讓步幅度隨持有時間變大；
            # 超過 exit_after_hours 還沒出場就接受目前可成交價 (排除自家單的對手最佳價)。
            "reduce_from_mid": 0.015,         # 初始：掛在現價往出場方向讓 1.5 分
            "reduce_widen_per_hour": 0.004,   # 每持有一小時多讓 0.4 分
            "reduce_max_give": 0.06,          # 讓步上限
            "exit_after_hours": 72,           # 超過這麼久就掛在對手最佳價 (可成交)
            "exit_stop_loss": 0.35,           # 部位浮虧超過成本的這個比例就進入強制出場
            "exit_max_cross": 0.02,           # 強制出場時最多比現價多付這麼多；簿子太寬就不追，改人工
            "wide_max": 0.15,                 # 排除自家單後 spread 超過這個就不開新倉 (09-22 Republican 0.12/0.91 錨在舊成交 0.30 立刻被倒 40 股)
            "fill_cooldown_min": 15,          # 某邊成交後，該邊 (增倉方向) 停掛這麼久；連續被打不自動補回原掛單 (Republican 3 分鐘吃了 70 股)
            # 成交即對沖 (hedge_on_fill 的市場)：一邊被打就立刻市價買另一邊配成一對、merge 回 1 美元
            "hedge_max_slip": 0.03,           # 配對總成本最多 1 + 這個 (每股最多虧 3 分 + 手續費)；超過就等下一輪
            "hedge_timeout_min": 15,          # 這麼久還對沖不掉就放棄，交給一般出場邏輯並記警告
            # v2 排隊掛價 (queue_mode 的市場)：09-27 研究發現我們 maker 成交 1h markout −3.1 分/股，是市場平均 3-4 倍，
            # 因為貼中價、前面沒人擋，知情單第一個吃我們。改成只加入「前面已有別人 queue_ahead_x 倍掛單」的價位，
            # 保護不夠就撤；被打之後不吃單出場，只掛被動單等配對 merge。
            "queue_ahead_x": 3.0,
            "min_round_gap_s": 10,            # ws 叫醒後兩輪之間至少隔這麼久 (活躍市場每秒都在動，避免打爆 API)
            # v1.1 風控/帳本 (初始設定，待驗證)
            "deposits_total": 320.0,          # 對帳基準
            "cluster_max_loss_usd": 30.0,     # 同事件叢集「納入掛單可能成交後」相對成本的最壞損失上限
            "account_max_loss_usd": 60.0,     # 全帳戶所有叢集加總的最壞結算損失上限
            "daily_loss_stop_usd": 15.0,      # 成本基礎當日 (UTC) 已實現 + 未實現變動，不含獎勵
            "cumulative_loss_stop_usd": 40.0, # 從實驗起點算的累積回撤 (不隨 UTC 午夜歸零)
            "stale_data_seconds": 300,        # 持倉/掛單資料超過這麼久沒更新成功就不准增倉
            "ledger_history_from": "2026-09-16", "ledger_tolerance_usd": 1.0}


def log(msg):
    line = f"{datetime.now().strftime('%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def event(rec):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(EVENTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_config():
    cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


def q_score(levels, mid, v, min_size, side):
    q = 0.0
    for p, sz in levels:
        if sz < min_size:
            continue
        s = (mid - p) if side == "bid" else (p - mid)
        if 0 <= s <= v:
            q += ((v - s) / v) ** 2 * sz
    return q


def order_score(s, v, size):
    return ((v - s) / v) ** 2 * size if 0 <= s <= v else 0.0


def combine(q1, q2, extreme):
    """兩邊分數合成一個：極端價 (mid 在 0.1~0.9 外) 要雙邊，取 min；否則 max(min, max/3)。"""
    return min(q1, q2) if extreme else max(min(q1, q2), max(q1, q2) / 3)


def book_levels(pc, yes_token, no_token, mine=None):
    """回 (bids, asks, tick)，全部換成 YES 價；NO 簿併進來 (NO bid @p = YES ask @1-p)。
    mine = {("bid"|"ask", price): size} 自己掛的量，要扣掉，不然會自我參照。"""
    mine = mine or {}
    yb = pc.get_order_book(token_id=yes_token)
    nb = pc.get_order_book(token_id=no_token)
    bids = [(float(x.price), float(x.size)) for x in yb.bids] + [(1 - float(x.price), float(x.size)) for x in nb.asks]
    asks = [(float(x.price), float(x.size)) for x in yb.asks] + [(1 - float(x.price), float(x.size)) for x in nb.bids]
    bids = [(p, s - mine.get(("bid", round(p, 4)), 0)) for p, s in bids]
    asks = [(p, s - mine.get(("ask", round(p, 4)), 0)) for p, s in asks]
    bids = [(p, s) for p, s in bids if s > 0.5]
    asks = [(p, s) for p, s in asks if s > 0.5]
    return bids, asks, float(yb.tick_size)


# ---------------------------------------------------------------- select

def cmd_select(a):
    from polymarket import PublicClient
    pc = PublicClient()
    rewards = [r for page in pc.list_current_rewards() for r in page.items if float(r.total_daily_rate or 0) >= a.min_pool]
    rewards.sort(key=lambda r: -float(r.total_daily_rate))
    now = datetime.now(timezone.utc)
    cands = []
    print(f"scanning {min(a.top, len(rewards))} markets with pool >= ${a.min_pool}/day", file=sys.stderr)
    for r in rewards[: a.top]:
        try:
            ms = requests.get(f"{GAMMA}/markets", params={"condition_ids": r.condition_id}, timeout=20).json()
            if not ms:
                continue
            m = ms[0]
            q = m["question"]
            if EXCLUDE_WORDS.search(q):
                continue
            end = m.get("gameStartTime") or m.get("endDate")
            if not end:
                continue
            end_dt = datetime.fromisoformat(end.replace(" ", "T").replace("+00", "+00:00").replace("Z", "+00:00"))
            days = (end_dt - now).total_seconds() / 86400
            if not (a.min_days <= days <= a.max_days):
                continue
            toks = json.loads(m["clobTokenIds"])
            bids, asks, tick = book_levels(pc, toks[0], toks[1])
            if not bids or not asks:
                continue
            mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2
            if mid < a.min_price or mid > a.max_price:
                continue
            v = float(r.rewards_max_spread) / 100
            mn = float(r.rewards_min_size)
            q_exist = combine(q_score(bids, mid, v, mn, "bid"), q_score(asks, mid, v, mn, "ask"), mid < 0.10 or mid > 0.90)
            # 我們：每邊 size 股，掛在中價 ± offset
            s = a.offset_ticks * tick
            size = max(mn, a.size)
            ours = order_score(s, v, size)  # 兩邊一樣 → min 就是這個
            share = ours / (q_exist + ours) if q_exist + ours > 0 else 0
            daily = share * float(r.total_daily_rate)
            capital = size * (mid - s) + size * (1 - mid - s)  # 買 YES + 買 NO 的錢
            cands.append({
                "name": q, "condition_id": r.condition_id, "yes_token": toks[0], "no_token": toks[1],
                "market_id": str(m["id"]), "end_date": end_dt.isoformat(), "days_left": round(days, 1),
                "pool": float(r.total_daily_rate), "max_spread": v, "min_size": mn, "tick": tick,
                "size": size, "offset_ticks": a.offset_ticks, "max_inventory": a.max_inventory,
                "est_daily": round(daily, 2), "est_share": round(share, 3), "q_existing": round(q_exist),
                "capital_needed": round(capital, 2), "mid": round(mid, 3),
                "vol24h": float(m.get("volume24hr") or 0),
                "event_slug": ((m.get("events") or [{}])[0]).get("slug"),   # 同事件的市場算同一個風險叢集
            })
            time.sleep(0.15)
        except Exception as e:  # noqa: BLE001
            print("  skip", r.condition_id[-8:], str(e)[:80], file=sys.stderr)
    cands.sort(key=lambda c: -c["est_daily"] / max(c["capital_needed"], 1))
    if a.show:
        print(f"\n--- top {a.show} candidates by est$/capital (before picking) ---")
        for c in cands[: a.show]:
            print(f"{c['est_daily']:6.2f} {c['est_share']*100:5.1f}% {c['pool']:5.0f} {c['mid']:5.3f} {c['max_spread']*100:4.1f} "
                  f"{c['min_size']:4.0f} {c['q_existing']:6.0f} {c['capital_needed']:6.2f} {c['days_left']:5.1f} {c['vol24h']:8.0f}  {c['name'][:60]}")
    picked, spent = [], 0.0
    for c in cands:
        if len(picked) >= a.n or spent + c["capital_needed"] > a.capital:
            continue
        picked.append(c)
        spent += c["capital_needed"]
    print(f"\n{'est$/d':>6} {'share':>6} {'pool':>5} {'mid':>5} {'±c':>4} {'min':>4} {'Qex':>6} {'cap$':>6} {'days':>5} {'vol24h':>8}  question")
    for c in picked:
        print(f"{c['est_daily']:6.2f} {c['est_share']*100:5.1f}% {c['pool']:5.0f} {c['mid']:5.3f} {c['max_spread']*100:4.1f} "
              f"{c['min_size']:4.0f} {c['q_existing']:6.0f} {c['capital_needed']:6.2f} {c['days_left']:5.1f} {c['vol24h']:8.0f}  {c['name'][:60]}")
    cfg = {"shadow": True, "capital": a.capital, **DEFAULTS, "markets": picked}
    if a.no_write:
        return
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"\n{len(picked)} markets, capital {spent:.2f}/{a.capital}, est {sum(c['est_daily'] for c in picked):.2f}/day (樂觀值)"
          f"\nwritten {CONFIG_PATH}")


# ---------------------------------------------------------------- shared quoting

class Quoter:
    """兩種模式共用：狀態持久化、報價計算、獎勵估算、被打後價格追蹤、每輪 status。"""
    mode = "?"

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = {}
        self.total_reward = 0.0
        self.start = datetime.now()
        self.est_by_day = {}  # UTC 日 → 估算獎勵，跟實際發放對帳用
        self.load_state()
        for m in cfg["markets"]:
            self.state.setdefault(m["condition_id"], {"net": 0.0, "cash": 0.0, "fills": 0, "reward": 0.0, "adverse": []})
            self.state[m["condition_id"]].update({"last_mid": None, "pause_until": None, "last_ts": datetime.now(timezone.utc).isoformat()})

    # ---- 狀態持久化：重啟不歸零
    def load_state(self):
        if not os.path.exists(STATE_PATH):
            return
        try:
            s = json.load(open(STATE_PATH, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if s.get("mode") != self.mode:
            log(f"state file is from mode={s.get('mode')}, starting fresh for {self.mode}")
            return
        self.state = s["markets"]
        self.total_reward = s["total_reward"]
        self.start = datetime.fromisoformat(s["started_at"])
        self.est_by_day = s.get("est_by_day", {})
        if s.get("actual") and hasattr(self, "actual"):
            self.actual = s["actual"]
        log(f"resumed state: reward {self.total_reward:.3f} since {s['started_at']}")

    def save_state(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"mode": self.mode, "started_at": self.start.isoformat(timespec="seconds"), "total_reward": self.total_reward,
                       "est_by_day": self.est_by_day, "actual": getattr(self, "actual", None), "markets": self.state}, f, ensure_ascii=False, default=str)
        os.replace(tmp, STATE_PATH)

    # ---- 報價
    def last_trade_price(self, m):
        """最近一筆公開成交，換成 YES 價；沒有回 None。"""
        try:
            pg = next(iter(self.pc.list_trades(condition_id=m["condition_id"], page_size=5)), None)
            for t in (pg.items if pg else []):
                p = float(t.price)
                return 1 - p if t.outcome == "No" else p
        except Exception as e:  # noqa: BLE001
            log(f"  last_trade failed: {e}")
        return None

    def quotes(self, m, st, bids, asks, tick):
        """回 (bb, ba, ref, yes_bid, yes_ask, want_bid, want_ask)。
        ref 是報價的錨：簿子夠緊就用中價；簿子比 max_spread 的兩倍還寬 (沒人掛、我們就是市場) 時中價沒意義，
        改錨在最近成交價 (夾在 bb/ba 之間)。庫存 skew 用絕對價差 (skew_move)，tick 數在 0.001 市場等於沒退。"""
        bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
        mid = (bb + ba) / 2
        ref = mid
        if ba - bb > 2 * m["max_spread"]:
            lt = self.last_trade_price(m)
            if lt is None:
                return bb, ba, None, None, None, False, False  # 寬簿又沒成交可錨 → 不掛
            ref = max(bb + tick, min(ba - tick, lt))
        off = m["offset_ticks"] * tick
        k = max(-1, min(1, -st["net"] / m["max_inventory"])) * self.cfg.get("skew_move", 0.02)
        yes_bid = round_price(ref - off + k, tick, ROUND_DOWN)
        yes_ask = round_price(ref + off + k, tick, ROUND_UP)
        yes_bid = max(tick, min(yes_bid, round_price(ba - tick, tick, ROUND_DOWN)))
        yes_ask = min(1 - tick, max(yes_ask, round_price(bb + tick, tick, ROUND_UP)))
        want_bid = st["net"] < m["max_inventory"]
        want_ask = st["net"] > -m["max_inventory"]
        if m.get("reduce_only"):  # 只掛減倉那邊，庫存歸零就不掛
            want_bid = want_bid and st["net"] < 0
            want_ask = want_ask and st["net"] > 0
        # side_only: 研究過結算來源後只站一邊 (yes = 只買 YES，no = 只買 NO)；被打就是拿到想要的部位
        if m.get("side_only") == "yes":
            want_ask = False
        elif m.get("side_only") == "no":
            want_bid = False
        return bb, ba, ref, yes_bid, yes_ask, want_bid, want_ask

    def exit_price_give(self, m, st, mid, L):
        """回 (願意讓步的價差, 原因)。相對現價讓步，持有越久讓越多；達停損或超時就讓到可成交。"""
        cfg = self.cfg
        held_h = 0.0
        if st.get("pos_since"):
            held_h = (datetime.now(timezone.utc) - datetime.fromisoformat(st["pos_since"])).total_seconds() / 3600
        side = "no" if st["net"] < 0 else "yes"
        qty, cost = L.qty(side), L.cost(side)
        avg = cost / qty if qty > 0 else 0.0
        cur = (1 - mid) if side == "no" else mid            # 這一邊現在的市價
        loss_frac = (avg - cur) / avg if avg > 0 else 0.0   # 浮虧佔成本比例
        # 強制出場也有上限：不能為了出場付掉整個 spread (09-23 Anthropic 簿子 0.58/0.71，
        # give=1.0 直接吃到 0.71，比標記價多虧 2.3)。寬簿出不掉就掛在上限等，不追。
        hard = cfg.get("exit_max_cross", 0.02)
        if loss_frac >= cfg.get("exit_stop_loss", 0.35):
            return hard, f"stop-loss {loss_frac:.0%}"
        if held_h >= cfg.get("exit_after_hours", 72):
            return hard, f"held {held_h:.0f}h"
        give = cfg.get("reduce_from_mid", 0.015) + cfg.get("reduce_widen_per_hour", 0.004) * held_h
        return min(give, cfg.get("reduce_max_give", 0.06), hard * 3), ""

    def size_for(self, m, q_exist):
        """沒人競爭時掛 min_size 就是 100% 佔比，多掛只是多被打；有競爭才加到 size (以 90% 佔比為目標)。"""
        per_share = order_score(m["offset_ticks"] * m["tick"], m["max_spread"], 1.0) or 1e-9
        need = 9 * q_exist / per_share
        return float(max(m["min_size"], min(m["size"], round(need))))

    def check_adverse(self, m, st, mid):
        """上一輪成交時記的 mid，一小時後再看往哪走 (正 = 對我們有利)。"""
        now = datetime.now(timezone.utc)
        for adv in st["adverse"]:
            at = adv["at"] if isinstance(adv["at"], datetime) else datetime.fromisoformat(adv["at"])
            if adv.get("mid_after") is None and (now - at).total_seconds() >= 3600:
                adv["mid_after"] = mid
                event({"type": "adverse", "market": m["name"], "side": adv["side"], "price": adv["price"],
                       "mid_at_fill": adv["mid"], "mid_1h_later": mid, "move_vs_us": round((mid - adv["mid"]) * (1 if adv["side"] == "bid" else -1), 4)})
        st["adverse"] = [a for a in st["adverse"] if a.get("mid_after") is None][-50:] + [a for a in st["adverse"] if a.get("mid_after") is not None][-20:]

    def note_fill(self, m, st, side, price, size, mid, extra=None):
        """side: bid (我們買 YES) / ask (我們賣 YES = 買 NO)。cash 用 YES 視角記，mtm = cash + net*mid。"""
        if side == "bid":
            st["net"] += size; st["cash"] -= size * price
        else:
            st["net"] -= size; st["cash"] += size * price
        st["fills"] += 1
        st["adverse"].append({"at": datetime.now(timezone.utc).isoformat(), "side": side, "price": price, "mid": mid, "mid_after": None})
        event({"type": "fill", "mode": self.mode, "market": m["name"], "side": "BUY YES" if side == "bid" else "SELL YES",
               "price": price, "size": round(size, 2), "mid": mid, **(extra or {})})

    def jump_check(self, m, st, mid, tick, spread=0.0):
        """中價一輪內跳超過 jump_move (絕對價差，tick 0.001 的市場用 tick 數會一直誤觸)：
        先撤單、暫停 jump_pause_min 分鐘 (事件消息來了，別站在路中間)。"""
        jm = self.cfg.get("jump_move", 0)
        now = datetime.now(timezone.utc)
        # 薄簿 spread 很寬 (London 10c、Miami 19c)，有人加減一張單 mid 就晃 3c，所以門檻還要 >= 上一輪的 spread
        thr = max(jm, st.get("last_spread") or 0)
        if jm and st["last_mid"] is not None and abs(mid - st["last_mid"]) >= thr - 1e-9:
            st["pause_until"] = (now + timedelta(minutes=self.cfg["jump_pause_min"])).isoformat()
            log(f"  [{m['name'][:40]}] JUMP {st['last_mid']:.3f} -> {mid:.3f}, pause {self.cfg['jump_pause_min']}m")
            event({"type": "jump", "market": m["name"], "from": st["last_mid"], "to": mid})
        st["last_mid"] = mid
        st["last_spread"] = spread
        pu = st.get("pause_until")
        return bool(pu) and datetime.fromisoformat(pu) > now

    def reward_estimate(self, m, st, bids, asks, mid, yes_bid, yes_ask, size_bid, size_ask):
        """mid 要用 Polymarket 看到的中價：含我們自己的單 (寬簿裡我們就是最佳買賣價)。"""
        v, mn = m["max_spread"], m["min_size"]
        bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
        mid = ((max(bb, yes_bid) if size_bid else bb) + (min(ba, yes_ask) if size_ask else ba)) / 2
        extreme = mid < 0.10 or mid > 0.90
        q_exist = combine(q_score(bids, mid, v, mn, "bid"), q_score(asks, mid, v, mn, "ask"), extreme)
        ob = order_score(mid - yes_bid, v, size_bid) if size_bid >= mn else 0.0
        oa = order_score(yes_ask - mid, v, size_ask) if size_ask >= mn else 0.0
        ours = combine(ob, oa, extreme)
        share = ours / (q_exist + ours) if q_exist + ours > 0 else 0.0
        minute_reward = share * m["pool"] / 1440 * self.cfg.get("poll_seconds", 60) / 60
        st["reward"] += minute_reward
        self.total_reward += minute_reward
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.est_by_day[day] = self.est_by_day.get(day, 0.0) + minute_reward
        return share, q_exist, mid

    def snapshot(self, m, st, mid, bb, ba, share, q_exist, yes_bid, yes_ask, size, extra=None):
        return {"name": m["name"], "state": "quoting", "mid": round(mid, 4), "best": [bb, ba], "yes_bid": yes_bid, "yes_ask": yes_ask,
                "size": size, "share": round(share, 4), "q_existing": round(q_exist), "pool": m["pool"],
                "reward_accum": round(st["reward"], 4), "reward_rate_day": round(share * m["pool"], 2),
                "net": round(st["net"], 2), "fills": st["fills"], "mtm_pnl": round(st["cash"] + st["net"] * mid, 4),
                "days_left": round(days_until(m.get("end_date")), 1), **(extra or {})}

    def write_status(self, snap, extra=None):
        hours = (datetime.now() - self.start).total_seconds() / 3600
        status = {"mode": self.mode, "updated_at": datetime.now().isoformat(timespec="seconds"), "started_at": self.start.isoformat(timespec="seconds"),
                  "hours": round(hours, 2), "capital": self.cfg["capital"], "reward_accum": round(self.total_reward, 4),
                  "reward_rate_day": round(sum(s.get("reward_rate_day", 0) for s in snap), 2),
                  "mtm_pnl": round(sum(s.get("mtm_pnl", 0) for s in snap), 4), "fills": sum(s.get("fills", 0) for s in snap),
                  "est_by_day": {k: round(v, 4) for k, v in self.est_by_day.items()}, **(extra or {}), "markets": snap}
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATUS_PATH)
        return status

    def start_watcher(self):
        if not self.cfg.get("ws_enabled", True):
            return None
        toks = [t for m in self.cfg["markets"] if m.get("enabled", True) for t in (m["yes_token"], m["no_token"])]
        w = BookWatcher(toks, move=self.cfg.get("ws_move", 0.008), log=log)
        w.start()
        return w

    def run(self):
        log(f"{self.mode} start: {len(self.cfg['markets'])} markets, capital {self.cfg['capital']}")
        self.ws = self.start_watcher()
        last_summary = datetime.now()
        while True:
            if os.path.exists(KILL_FILE):
                log("STOP file found, exiting")
                self.shutdown()
                os.remove(KILL_FILE)
                return
            snap = []
            round_t0 = time.time()
            for m in self.cfg["markets"]:
                if not m.get("enabled", True):
                    self.on_disabled(m)
                    continue
                try:
                    snap.append(self.step_market(m))
                except Exception as e:  # noqa: BLE001
                    log(f"[{m['name'][:40]}] error: {e}")
                    snap.append({"name": m["name"], "state": "error", "error": str(e)[:150]})
            self.after_round()
            status = self.write_status(snap, self.status_extra())
            self.save_state()
            if (datetime.now() - last_summary) >= timedelta(hours=1):
                last_summary = datetime.now()
                event({"type": "hourly", **{k: status[k] for k in ("updated_at", "hours", "reward_accum", "reward_rate_day", "mtm_pnl", "fills")}})
                log(f"hourly: reward {self.total_reward:.3f} (rate {status['reward_rate_day']}/day) mtm {status['mtm_pnl']:+.3f} fills {status['fills']}"
                    + (f" ws {self.ws.stats()}" if getattr(self, "ws", None) else ""))
            nap = self.cfg.get("poll_seconds", 60)
            if getattr(self, "ws", None):
                self.ws.set_tokens([t for m in self.cfg["markets"] if m.get("enabled", True) for t in (m["yes_token"], m["no_token"])])
                if self.ws.wait(nap):
                    gap = self.cfg.get("min_round_gap_s", 10) - (time.time() - round_t0)
                    if gap > 0:
                        time.sleep(gap)
                    log("  ws wake: book moved, re-quoting early")
            else:
                time.sleep(nap)

    def after_round(self):
        pass

    def on_disabled(self, m):
        pass

    def status_extra(self):
        return {}

    def shutdown(self):
        pass


# ---------------------------------------------------------------- shadow

class Shadow(Quoter):
    mode = "shadow"

    def __init__(self, cfg):
        from polymarket import PublicClient
        self.pc = PublicClient()
        super().__init__(cfg)

    def step_market(self, m):
        st = self.state[m["condition_id"]]
        bids, asks, tick = book_levels(self.pc, m["yes_token"], m["no_token"])
        if not bids or not asks:
            return {"name": m["name"], "state": "empty"}
        bb, ba, mid, yes_bid, yes_ask, want_bid, want_ask = self.quotes(m, st, bids, asks, tick)
        if mid is None:
            return {"name": m["name"], "state": "wide", "best": [bb, ba]}
        if mid >= 0.97 or mid <= 0.03:
            return {"name": m["name"], "state": "resolved", "mid": mid}
        self.check_adverse(m, st, mid)
        if self.jump_check(m, st, mid, tick, ba - bb):
            want_bid = want_ask = False
        size = m["size"]
        share, q_exist, mid_pm = self.reward_estimate(m, st, bids, asks, mid, yes_bid, yes_ask, size if want_bid else 0, size if want_ask else 0)

        # 模擬成交：看上次到現在的公開成交
        # data-api 的 start 參數實測沒作用，會回整段歷史；只抓第一頁 (最新 100 筆) 再自己用時間過濾
        now = datetime.now(timezone.utc)
        last_ts = datetime.fromisoformat(st["last_ts"])
        first = next(iter(self.pc.list_trades(condition_id=m["condition_id"], page_size=100)), None)
        trades = [t for t in (first.items if first else []) if t.timestamp > last_ts]
        st["last_ts"] = now.isoformat()
        bid_left, ask_left = (size if want_bid else 0), (size if want_ask else 0)
        for t in sorted(trades, key=lambda t: t.timestamp):
            p, sz = float(t.price), float(t.size)
            if t.outcome == "No":  # 換成 YES 視角
                p, side = 1 - p, ("SELL" if t.side == "BUY" else "BUY")
            else:
                side = t.side
            # taker SELL YES 打到 <= 我們買價 → 我們買單成交；價格相等算一半 (排隊位置未知)
            if side == "SELL" and bid_left > 0 and p <= yes_bid + 1e-9:
                f = min(bid_left, sz if p < yes_bid else sz * 0.5)
                if f >= 1:
                    bid_left -= f
                    self.note_fill(m, st, "bid", yes_bid, f, mid, {"trade_price": float(t.price)})
                    log(f"  [shadow FILL] BUY YES {f:.1f} @ {yes_bid} (taker sold @ {p:.3f}) [{m['name'][:40]}]")
            elif side == "BUY" and ask_left > 0 and p >= yes_ask - 1e-9:
                f = min(ask_left, sz if p > yes_ask else sz * 0.5)
                if f >= 1:
                    ask_left -= f
                    self.note_fill(m, st, "ask", yes_ask, f, mid, {"trade_price": float(t.price)})
                    log(f"  [shadow FILL] SELL YES {f:.1f} @ {yes_ask} (taker bought @ {p:.3f}) [{m['name'][:40]}]")
        return self.snapshot(m, st, mid, bb, ba, share, q_exist, yes_bid if want_bid else None, yes_ask if want_ask else None, size,
                             {"paused": bool(st.get("pause_until")) and datetime.fromisoformat(st["pause_until"]) > now})


# ---------------------------------------------------------------- live

class Live(Quoter):
    mode = "live"

    def __init__(self, cfg, dry=False):
        self.dry = dry
        from polymarket import PublicClient
        self.client, self.address = make_client()
        self.pc = PublicClient()  # SecureClient.list_trades 是帳號成交 (帶 condition_id 回空)，公開成交要用 PublicClient
        self.my_tokens = {t for m in cfg["markets"] for t in (m["yes_token"], m["no_token"])}
        self.start_ts = int(time.time())
        self.seen_trades = set()
        self.positions = {}
        self.balance = None
        self.actual = {"date": None, "today": 0.0, "by_market": {}, "yesterday": None, "checked_at": None}
        self.orders_view = {}   # condition_id -> [{"id","side","qty","price","pending_rounds"}]，本地視角 (API + 本輪送出/撤掉的)
        self.mismatch = {}      # condition_id -> 說明；帳本與 API 持倉對不上時只准不增加風險的單
        self.day_start = None   # {"date", "realized", "unrealized"} 日損計算基準
        self.earned_by_day = {}
        super().__init__(cfg)
        self.ledger = self.build_ledger()
        if os.path.exists(FILLS_PATH):
            for line in open(FILLS_PATH, encoding="utf-8"):
                try:
                    self.seen_trades.add(json.loads(line)["id"])
                except (ValueError, KeyError):
                    pass

    # ---- 帳本：依時序重建 (觀察期前 merge 用推算，之後用 bot 實際記錄)
    def build_ledger(self):
        saved = {}
        if os.path.exists(LEDGER_PATH):
            try:
                saved = json.load(open(LEDGER_PATH, encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                saved = {}
        obs = saved.get("observation_start") or self.cfg.get("observation_start")
        if not obs:
            obs = datetime.now(timezone.utc).isoformat(timespec="seconds")
            log(f"observation_start set to {obs} (現有持倉標為 legacy)")
        self.observation_start = obs
        self.merges_log = saved.get("merges_log", [])     # [(ts, cid, pairs, source)] source=bot 是實際下 merge 的紀錄
        self.earned_by_day = saved.get("earned_by_day", {})
        self.day_start = saved.get("day_start")
        self.exp_start = saved.get("exp_start")
        self.peak_cum = saved.get("peak_cum", 0.0)
        t0 = int(datetime.fromisoformat(self.cfg["ledger_history_from"]).replace(tzinfo=timezone.utc).timestamp())
        me = (self.address or "").lower()
        rows = []
        try:
            for page in self.client.list_account_trades(after=str(t0)):
                for t in page.items:
                    legs = []
                    if (t.trader_side or "").upper() == "TAKER":
                        # 我們當 taker (人工出場)：整筆是我們的，SELL 記負數量
                        sign = -1 if (t.side or "").upper() == "SELL" else 1
                        legs.append((t.outcome, sign * float(t.size), float(t.price)))
                    else:
                        for mo in (t.maker_orders or []):
                            if str(mo.maker_address).lower() == me:
                                legs.append((mo.outcome, float(mo.matched_amount), float(mo.price)))
                    for outc_raw, qty, price in legs:
                        outc = "no" if (outc_raw or "").lower() == "no" else "yes"   # 球隊名之類的當 YES 邊
                        ma = t.matched_at
                        if not isinstance(ma, datetime):
                            ma = datetime.fromisoformat(str(ma).replace(" ", "T").replace("Z", "+00:00"))
                        if ma.tzinfo is None:
                            ma = ma.replace(tzinfo=timezone.utc)
                        # 一律 isoformat：str(datetime) 會有空格，跟 "T" 格式字串比大小會全部排到前面 (09-22 踩過，觀察期分層全錯)
                        rows.append((ma.isoformat(), t.condition_id, outc, qty, price, t.id))
        except Exception as e:  # noqa: BLE001
            log(f"  ledger: list_account_trades failed: {e}")
            if saved.get("markets"):
                log("  ledger: using saved ledger")
                return {cid: MarketLedger.from_dict(d) for cid, d in saved["markets"].items()}
            raise
        manual = set(self.cfg.get("manual_cids", []))
        rows = [r for r in rows if r[1] not in manual]   # 使用者自己下的方向性部位分開記，不進 bot 帳本
        rows.sort()
        before = [(ts, cid, o, q, p) for ts, cid, o, q, p, _ in rows if ts < obs]
        after = [(ts, cid, o, q, p) for ts, cid, o, q, p, _ in rows if ts >= obs]
        led, inferred = replay(before)            # 觀察期前：merge 推算 (auto_merge 每輪都做，兩邊都有就 merge)
        for L in led.values():
            L.mark_legacy()
        events = sorted([(ts, 0, cid, o, q, p) for ts, cid, o, q, p in after] +
                        [(ts, 1, cid, None, k, None) for ts, cid, k, src in self.merges_log if ts >= obs])
        for ts, kind, cid, o, q, p in events:
            L = led.setdefault(cid, MarketLedger())
            if kind == 0:
                (L.sell(o, -q, p) if q < 0 else L.buy(o, q, p))
            else:
                L.merge(q)
        self.ledger_trade_ids = {r[5] for r in rows}
        # 過去每一天「賺得」的獎勵 (API)，殘差計算把昨天以前的視為推定已入帳
        d = datetime.fromisoformat(self.cfg["ledger_history_from"]).date()
        while d < datetime.now(timezone.utc).date():
            key = d.isoformat()
            if key not in self.earned_by_day:
                try:
                    self.earned_by_day[key] = sum(float(x.earnings) for x in self.client.get_total_earnings_for_user_for_day(date=key))
                except Exception as e:  # noqa: BLE001
                    log(f"  earnings backfill {key} failed: {e}"); break
            d += timedelta(days=1)
        log(f"ledger: {len(rows)} fills, {inferred} inferred merges before {obs[:16]}, {len(self.merges_log)} logged merges, "
            f"earned prior days {sum(v for k, v in self.earned_by_day.items() if k < datetime.now(timezone.utc).strftime('%Y-%m-%d')):.4f}")
        return led

    def save_ledger(self):
        os.makedirs(OUT_DIR, exist_ok=True)
        tmp = LEDGER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"observation_start": self.observation_start, "merges_log": self.merges_log, "earned_by_day": self.earned_by_day,
                       "day_start": self.day_start, "exp_start": getattr(self, "exp_start", None), "peak_cum": getattr(self, "peak_cum", 0.0),
                       "markets": {cid: L.to_dict() for cid, L in self.ledger.items()}}, f, ensure_ascii=False)
        os.replace(tmp, LEDGER_PATH)

    def first_fill_ts(self, cid):
        """這個市場最早一筆還沒被結清的成交時間 (近似：最早一筆成交)。"""
        if not hasattr(self, "_first_fill"):
            self._first_fill = {}
            if os.path.exists(FILLS_PATH):
                for line in open(FILLS_PATH, encoding="utf-8"):
                    try:
                        t = json.loads(line)
                        c, ts = t.get("condition_id"), t.get("matched_at")
                        if c and ts and (c not in self._first_fill or str(ts) < self._first_fill[c]):
                            self._first_fill[c] = str(ts).replace(" ", "T")
                    except (ValueError, KeyError):
                        pass
        return self._first_fill.get(cid)

    def ledger_apply_fill(self, cid, outcome, qty, price, trade_id):
        if trade_id in self.ledger_trade_ids:
            return
        self.ledger_trade_ids.add(trade_id)
        self.ledger.setdefault(cid, MarketLedger()).buy(outcome, qty, price)

    def check_mismatch(self, m):
        """帳本 vs API 持倉：差異的美金風險超過容忍就標記，該叢集只准不增加風險的單。"""
        L = self.ledger.get(m["condition_id"], MarketLedger())
        y = self.positions.get(m["yes_token"], {}).get("size", 0)
        n = self.positions.get(m["no_token"], {}).get("size", 0)
        diff = abs(L.qty("yes") - y) * max(self.positions.get(m["yes_token"], {}).get("cur", 0.5), 0.05) + \
               abs(L.qty("no") - n) * max(self.positions.get(m["no_token"], {}).get("cur", 0.5), 0.05)
        cid = m["condition_id"]
        if diff > self.cfg["ledger_tolerance_usd"]:
            if cid not in self.mismatch:
                log(f"  [{m['name'][:40]}] LEDGER MISMATCH ledger Y{L.qty('yes'):.2f}/N{L.qty('no'):.2f} vs api Y{y:.2f}/N{n:.2f} (${diff:.2f}) → 只准不增加風險的單")
                event({"type": "mismatch", "market": m["name"], "ledger": [L.qty("yes"), L.qty("no")], "api": [y, n], "usd": round(diff, 2)})
            self.mismatch[cid] = f"${diff:.2f}"
        elif cid in self.mismatch:
            log(f"  [{m['name'][:40]}] ledger mismatch cleared")
            del self.mismatch[cid]

    # ---- 曝險：事件叢集最壞損失 (含掛單可能成交)，送單前檢查
    def cluster_of(self, m):
        return m.get("cluster") or m.get("event_slug") or m["condition_id"]

    def cluster_bound(self, cluster, override=None):
        """override = (condition_id, [orders]) 用候選掛單取代該市場目前的掛單視角。"""
        rows = []
        for m in self.cfg["markets"]:
            if self.cluster_of(m) != cluster:
                continue
            L = self.ledger.get(m["condition_id"], MarketLedger())
            if override and override[0] == m["condition_id"]:
                orders = override[1]
            else:
                orders = [(o["side"], o["qty"], o["price"]) for o in self.orders_view.get(m["condition_id"], [])]
            rows.append((L.qty("yes"), L.cost("yes"), L.qty("no"), L.cost("no"), orders))
        return cluster_loss_bound(rows)

    def account_bound(self):
        """全帳戶：各叢集最壞結算損失相加 (保守上界)。"""
        return sum(self.cluster_bound(cl) for cl in {self.cluster_of(m) for m in self.cfg["markets"]})

    def data_age(self):
        """持倉/掛單資料距離上次成功更新幾秒。抓失敗會沿用舊資料，所以要有年齡上限。"""
        t = min(getattr(self, "positions_ts", 0), getattr(self, "orders_ts", 0))
        return time.time() - t if t else 1e9

    def risk_check(self, m, side, qty, price, other_orders):
        """回 (ok, reason)。候選單 = 該 token 這張新單 + 同市場另一邊現有的單。
        規則：叢集上界 ≤ cluster_max_loss_usd；若有未確認掛單 / 帳本不符 / 日損 HALT，則只准不增加上界的單。"""
        cid = m["condition_id"]; cl = self.cluster_of(m)
        # 基準要用「同一組 other_orders、不含候選單」算，不能用 orders_view 整包：view 裡若有剛撤掉但 API 還沒消失的同邊舊單，
        # 會讓基準虛高、候選單看起來「沒增加風險」而放行 (09-21 23:21 trim 後又重掛就是這樣漏的)
        cur = self.cluster_bound(cl, (cid, other_orders))
        new = self.cluster_bound(cl, (cid, other_orders + [(side, qty, price)]))
        increases = new > cur + 1e-9
        if not increases:
            return True, ""   # 不增加最壞損失的單 (配對減倉、或已超標但不再惡化) 放行；價格與現金另外檢查
        # 同一 token 已經有送出但 API 還沒確認的單 → 不再送，否則會重複下單
        pend = [o for o in self.orders_view.get(cid, []) if o["side"] == side and o.get("pending_rounds", 0) > 0]
        if pend:
            return False, f"pending {side} order {pend[0]['id'][:10]} unconfirmed ({pend[0]['pending_rounds']} rounds)"
        stale = self.data_age()
        if stale > self.cfg.get("stale_data_seconds", 300):
            return False, f"stale account data ({stale:.0f}s old)"
        if new > self.cfg["cluster_max_loss_usd"] + 1e-9:
            return False, f"cluster {cl[:12]} worst-loss {cur:.2f} → {new:.2f} > cap {self.cfg['cluster_max_loss_usd']:.2f}"
        acct_cur = self.account_bound()
        acct_new = acct_cur - cur + new
        if acct_new > self.cfg.get("account_max_loss_usd", 1e9) + 1e-9:
            return False, f"account worst-loss {acct_cur:.2f} → {acct_new:.2f} > cap {self.cfg['account_max_loss_usd']:.2f}"
        freeze = []
        if os.path.exists(HALT_FILE):
            freeze.append("daily-loss HALT")
        if cid in self.mismatch:
            freeze.append(f"ledger mismatch {self.mismatch[cid]}")
        unconfirmed = [o for o in self.orders_view.get(cid, []) if o.get("pending_rounds", 0) >= 2]
        if unconfirmed:
            freeze.append(f"{len(unconfirmed)} unconfirmed order(s)")
        if freeze:
            return False, f"frozen ({'; '.join(freeze)}): would raise worst-loss {cur:.2f} → {new:.2f}"
        return True, ""

    # ---- 成交後 markout (1/5/30/60 分鐘)；ref 不可靠就 NA，成交不刪
    def ref_snapshot(self, m, st, bids, asks):
        bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
        bd = sum(sz for p, sz in bids if abs(p - bb) < 1e-9); ad = sum(sz for p, sz in asks if abs(p - ba) < 1e-9)
        ok = ref_reliable(bb, ba, bd, ad, m["max_spread"], m["min_size"])
        st["ref"] = {"ts": datetime.now(timezone.utc).isoformat(), "mid": (bb + ba) / 2, "ok": ok}
        now = datetime.now(timezone.utc)
        done = []
        for mk in st.setdefault("markouts", []):
            t_fill = datetime.fromisoformat(mk["ts"])
            for w in MARKOUT_WINDOWS:
                key = f"ref_{w}"
                if key not in mk and (now - t_fill).total_seconds() >= w:
                    mk[key] = (bb + ba) / 2 if ok else None
            if all(f"ref_{w}" in mk for w in MARKOUT_WINDOWS):
                done.append(mk)
        for mk in done:
            q, p, r0 = mk["q"], mk["p"], mk["ref_0"]
            out = {"type": "markout", "market": m["name"], "cluster": self.cluster_of(m), "ts": mk["ts"], "q": q, "p": p, "ref_0": r0,
                   "S": None if r0 is None else round(q * (r0 - p), 4)}
            for w in MARKOUT_WINDOWS:
                S, A = markout_terms(q, p, r0, mk[f"ref_{w}"])
                out[f"A_{w}"] = None if A is None else round(A, 4)
            event(out)
            st["markouts"].remove(mk)

    # ---- 清算估值：持倉打進排除自家單的簿子
    def liquidation(self, m, bids, asks):
        L = self.ledger.get(m["condition_id"], MarketLedger())
        py, uy = liquidation_value(L.qty("yes"), bids)                      # YES 賣進 YES bids
        pn, un = liquidation_value(L.qty("no"), [(1 - p, sz) for p, sz in asks])   # NO 賣進 NO bids (= 1 − YES asks)
        return {"yes": [round(py, 2), round(uy, 2)], "no": [round(pn, 2), round(un, 2)], "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # ---- 績效 (成本基礎；獎勵按賺得日另列；殘差另列)
    def perf(self):
        realized = {"legacy": 0.0, "new": 0.0}; unreal = {"legacy": 0.0, "new": 0.0}
        mark_total = liq_total = liq_unsold = fees_total = 0.0
        for m in self.cfg["markets"] + [{"condition_id": cid} for cid in self.ledger if cid not in {x["condition_id"] for x in self.cfg["markets"]}]:
            cid = m["condition_id"]; L = self.ledger.get(cid)
            if not L:
                continue
            pc = getattr(self, "positions_by_cid", {}).get(cid, {})
            yp = self.positions.get(m.get("yes_token", ""), {}).get("cur", pc.get("yes"))
            npx = self.positions.get(m.get("no_token", ""), {}).get("cur", pc.get("no"))
            if yp is None:   # Data API 沒有這邊的持倉 → 沒有股數，價格用 1 − 另一邊，沒有就 0
                yp = (1 - npx) if npx is not None else 0.0
            if npx is None:
                npx = (1 - yp) if yp is not None else 0.0
            fees_total = fees_total + pc.get("fees", 0.0)
            u = L.unrealized(yp, npx)
            for k in realized:
                realized[k] += L.realized[k]; unreal[k] += u[k]
            mark_total += L.qty("yes") * yp + L.qty("no") * npx
            liq = self.state.get(cid, {}).get("liq")
            if liq:
                liq_total += liq["yes"][0] + liq["no"][0]; liq_unsold += liq["yes"][1] * yp + liq["no"][1] * npx
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        earned_prior = sum(v for d, v in self.earned_by_day.items() if d < today)
        man = getattr(self, "manual", None) or {"outlay": 0.0, "value": 0.0, "positions": []}
        # 人工部位花掉的現金視為從 bot 帳戶轉出：bot 淨值 = 現金 + bot 持倉 + 轉出金額
        equity = (self.balance or 0) + mark_total + man["outlay"]
        paid = getattr(self, "income_paid", None) or {"reward": 0.0, "rebate": 0.0}
        # 橋接用鏈上實際發放 (REWARD + MAKER_REBATE)，不用「賺得」估計；殘差 = 對不上的部分
        expected = self.cfg["deposits_total"] + sum(realized.values()) + sum(unreal.values()) + paid["reward"] + paid["rebate"]
        day = None
        if self.day_start and self.day_start.get("date") == today:
            day = round(sum(realized.values()) - self.day_start["realized"] + sum(unreal.values()) - self.day_start["unrealized"], 4)
        # 累積：從實驗起點算，UTC 午夜不歸零
        exp0 = getattr(self, "exp_start", None)
        cum = None
        if exp0:
            cum = round(sum(realized.values()) + sum(unreal.values()) - exp0["realized"] - exp0["unrealized"], 4)
            self.peak_cum = max(getattr(self, "peak_cum", 0.0), cum)
        return {"date": today, "cash": self.balance, "mark_value": round(mark_total, 2), "equity": round(equity, 2),
                "liq_value": round(liq_total, 2), "liq_unsold_marked": round(liq_unsold, 2),
                "realized": {k: round(v, 4) for k, v in realized.items()}, "unrealized": {k: round(v, 4) for k, v in unreal.items()},
                "rewards_earned_today": self.actual.get("today", 0.0), "rewards_earned_prior_days": round(earned_prior, 4),
                "rewards_paid_confirmed": round(paid["reward"], 4), "rebates_paid": round(paid["rebate"], 4),
                "rewards_earned_unpaid": round(earned_prior + (self.actual.get("today") or 0.0) - paid["reward"], 4),
                "fees_entry_data_api": round(fees_total, 4),   # Data API 的 entry_fees_usdc 合計 (maker 應為 0，列出來核對)
                "manual": {"outlay": round(man["outlay"], 2), "value": round(man["value"], 2),
                           "pnl": round(man["value"] - man["outlay"], 2), "positions": man["positions"]},
                "account_total": round((self.balance or 0) + mark_total + man["value"], 2),
                "residual": round(equity - expected, 4) if self.balance is not None else None,
                "day_pnl_cost_basis": day, "cum_pnl_cost_basis": cum,
                "cum_drawdown": round((getattr(self, "peak_cum", 0.0) - cum), 4) if cum is not None else None,
                "account_worst_loss": round(self.account_bound(), 2), "account_cap": self.cfg.get("account_max_loss_usd"),
                "data_age_s": round(self.data_age(), 1), "observation_start": self.observation_start,
                "halt": os.path.exists(HALT_FILE), "mismatch": dict(self.mismatch)}

    def roll_day(self, perf):
        """UTC 跨日：寫一行日績效，重設日損基準。"""
        today = perf["date"]
        if self.day_start and self.day_start.get("date") == today:
            return
        if self.day_start:
            with open(PERF_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps({**perf, "closed_date": self.day_start["date"]}, ensure_ascii=False) + "\n")
        self.day_start = {"date": today, "realized": sum(perf["realized"].values()), "unrealized": sum(perf["unrealized"].values())}

    # ---- 帳號資料
    def my_orders(self, condition_id):
        try:
            return [o for page in self.client.list_open_orders(market=condition_id) for o in page.items if o.side == "BUY"]
        except Exception as e:  # noqa: BLE001
            log(f"  list_open_orders failed: {e}")
            return []

    def open_notional(self):
        total = 0.0
        try:
            for page in self.client.list_open_orders():
                for o in page.items:
                    total += (float(o.original_size) - float(o.size_matched or 0)) * float(o.price)
        except Exception as e:  # noqa: BLE001
            log(f"  open_notional failed: {e}")
        return total

    def exposure(self):
        pos_cost = sum(p["size"] * p["avg"] for t, p in self.positions.items() if t in self.my_tokens)
        return self.open_notional() + pos_cost

    # ---- 下單
    def place_buy(self, token, price, size, label, m=None, side=None):
        """送出後立刻記進 orders_view (pending_rounds=1)，下一輪 API 看到才算確認。"""
        oid = None
        if self.dry:
            log(f"  [dry] BUY {label} {size} @ {price}")
            oid = f"dry-{int(time.time()*1000)}"
        else:
            try:
                r = self.client.place_limit_order(token_id=token, price=str(price), size=str(size), side="BUY", post_only=True)
                if r.ok:
                    log(f"  BUY {label} {size} @ {price} -> {r.status} {r.order_id[:12]}")
                    oid = r.order_id
                else:
                    log(f"  BUY {label} {size} @ {price} REJECTED {r.code}: {r.message}")
            except Exception as e:  # noqa: BLE001
                log(f"  BUY {label} {size} @ {price} FAILED: {e}")
                # 不知道有沒有送進去：當作 pending 計入曝險，等 API 確認
                oid = f"unknown-{int(time.time()*1000)}"
        if oid and m is not None:
            self.orders_view.setdefault(m["condition_id"], []).append({"id": oid, "side": side, "qty": float(size), "price": float(price), "pending_rounds": 1})
        return bool(oid) and not str(oid).startswith("unknown")

    def cancel(self, order_ids, why):
        """撤單成功才從本地視角移除；失敗就留著 (仍計入曝險)。"""
        if not order_ids:
            return
        ok = True
        if self.dry:
            log(f"  [dry] cancel {len(order_ids)} ({why})")
        else:
            try:
                self.client.cancel_orders(order_ids=order_ids)
                log(f"  cancelled {len(order_ids)} ({why})")
            except Exception as e:  # noqa: BLE001
                log(f"  cancel failed: {e}"); ok = False
        if ok:
            ids = set(order_ids)
            for cid, lst in self.orders_view.items():
                self.orders_view[cid] = [o for o in lst if o["id"] not in ids]

    def reconcile(self, orders, token, want_price, size, min_size, tick, label, budget, reducing=False, m=None, side=None, net=0.0, exact=False):
        """該 token 只留一張 BUY：價格在容忍範圍、剩餘量還夠算分就留 (獎勵不看排隊順序，但撤掛也沒好處)；
        否則撤掉重掛。回傳留在簿上的量 (算分用)。exact：排隊模式，價格差一格就換 (保護要跟著簿子走)。"""
        keep, drop, freed = None, [], 0.0
        tol = tick / 2 if exact else max(tick * self.cfg["reprice_ticks"], self.cfg.get("reprice_move", 0)) + tick / 2
        for o in orders:
            rem = float(o.original_size) - float(o.size_matched or 0)
            ok = want_price is not None and abs(float(o.price) - want_price) <= tol and rem >= max(min_size, size * 0.5)
            if ok and keep is None:
                keep = rem
            else:
                drop.append(o.id); freed += rem * float(o.price)
        self.cancel(drop, f"{label} reprice")
        if drop:
            budget["left"] += freed
        if want_price is None:
            return 0.0
        if keep is not None:
            return keep
        cost = want_price * size
        # 資金占用上限：配對部分 (數量 ≤ |淨部位|) 不計，超額部分照算；不管哪邊都要有現金、都要過叢集最壞損失檢查
        pair_qty = min(size, abs(net)) if reducing else 0.0
        counted = (size - pair_qty) * want_price
        if counted > budget["left"]:
            log(f"  skip {label}: capital cap (need {counted:.2f}, left {budget['left']:.2f})")
            return 0.0
        if self.balance is not None and cost > self.balance:
            log(f"  skip {label}: cash {self.balance:.2f} < cost {cost:.2f}")
            return 0.0
        other = [(o["side"], o["qty"], o["price"]) for o in self.orders_view.get(m["condition_id"], []) if o["side"] != side]
        ok, why = self.risk_check(m, side, size, want_price, other)
        if not ok:
            log(f"  skip {label}: RISK {why}")
            event({"type": "risk_block", "market": m["name"], "side": side, "qty": size, "price": want_price, "why": why})
            return 0.0
        budget["left"] -= counted
        return size if self.place_buy(token, want_price, size, label, m, side) else 0.0

    # ---- 每市場一輪
    def step_market(self, m):
        st = self.state[m["condition_id"]]
        orders = self.my_orders(m["condition_id"])
        mine = {}
        for o in orders:
            rem = float(o.original_size) - float(o.size_matched or 0)
            if str(o.asset_id) == m["yes_token"]:
                key = ("bid", round(float(o.price), 4))
            else:
                key = ("ask", round(1 - float(o.price), 4))
            mine[key] = mine.get(key, 0) + rem
        bids, asks, tick = book_levels(self.client, m["yes_token"], m["no_token"], mine)
        yes_orders = [o for o in orders if str(o.asset_id) == m["yes_token"]]
        no_orders = [o for o in orders if str(o.asset_id) == m["no_token"]]
        budget = {"left": self.cfg["capital"] - self.exposure_cached}

        def pull(why):
            self.cancel([o.id for o in orders], why)
            self.exposure_cached -= sum((float(o.original_size) - float(o.size_matched or 0)) * float(o.price) for o in orders)

        if not bids or not asks:
            pull("empty book")
            return {"name": m["name"], "state": "empty"}
        # 庫存用帳本 (成交一記到就更新)，不用 Data API 持倉 (會慢一輪，09-22 London 因此重複掛了第二張 42 股減倉單)；
        # API 持倉只拿來對帳，對不上就標記 (只准不增加風險的單)
        y = self.positions.get(m["yes_token"], {}).get("size", 0)
        n = self.positions.get(m["no_token"], {}).get("size", 0)
        Lm = self.ledger.get(m["condition_id"])
        st["net"] = (Lm.qty("yes") - Lm.qty("no")) if Lm else (y - n)
        if abs(st["net"]) < 1e-9:
            st["pos_since"] = None; st["exit_why"] = None
        elif not st.get("pos_since"):
            # 持有時間從實際成交時算，不是從 bot 重啟時算 (Anthropic 那批從 09-20 就在手上)
            st["pos_since"] = self.first_fill_ts(m["condition_id"]) or datetime.now(timezone.utc).isoformat()
        self.check_mismatch(m)
        self.ref_snapshot(m, st, bids, asks)
        st["liq"] = self.liquidation(m, bids, asks)
        bb, ba, mid, yes_bid, yes_ask, want_bid, want_ask = self.quotes(m, st, bids, asks, tick)
        if mid is None:
            pull("wide book, no trade anchor")
            return {"name": m["name"], "state": "wide", "best": [bb, ba]}
        if m.get("hedge_on_fill"):
            # 只在「被打之後對沖得起來」時報價：在中價外 off 成交、立刻吃對面最佳價，配對成本 ≈ 1 + spread/2 − off，
            # 要 ≤ 1 + hedge_max_slip；留一半空間給成交後的價格移動 → spread ≤ 2·off + slip
            off_px = m["offset_ticks"] * tick
            lim = 2 * off_px + self.cfg.get("hedge_max_slip", 0.03)
            if ba - bb > lim + 1e-9:
                pull(f"hedge: spread {ba - bb:.3f} > {lim:.3f}")
                return {"name": m["name"], "state": "wide_for_hedge", "best": [bb, ba]}
        if ba - bb > self.cfg.get("wide_max", 0.15) and not m.get("reduce_only"):
            # 簿子這麼寬代表沒有價格發現，錨在哪都是猜；減倉單另有兩平價上限所以不受此限
            pull(f"wide book {bb:.2f}/{ba:.2f}")
            return {"name": m["name"], "state": "wide", "best": [bb, ba]}
        if not st.get("cash_seeded"):
            # bot 之前就有的持倉 (例如 London 5 NO) 以現價入帳，損益從零起算
            st["cash"] = -st["net"] * mid if st["fills"] == 0 else st["cash"]
            st["cash_seeded"] = True
        days = days_until(m.get("end_date"))
        if mid >= 0.97 or mid <= 0.03 or days < self.cfg["min_days_left"]:
            pull("resolved/expiring")
            return {"name": m["name"], "state": "resolved" if days >= self.cfg["min_days_left"] else "expiring", "mid": mid}
        paused = self.jump_check(m, st, mid, tick, ba - bb)
        if paused:
            want_bid = want_ask = False
        # 對沖市場：還有待對沖的部位時，同方向不再掛單 (避免越對沖越多，也避免自成交)
        if m.get("hedge_on_fill"):
            hn, _ = self.hedge_net(m, st)
            if st.get("hedge_blocked"):
                want_bid = want_ask = False
            elif hn >= 1:
                want_bid = False
            elif hn <= -1:
                want_ask = False
        # 成交冷卻：剛被打的那一邊如果是增倉方向，先不補 (減倉方向不受影響)
        now_utc = datetime.now(timezone.utc)
        for side_key, is_increasing in (("bid", st["net"] >= 0), ("ask", st["net"] <= 0)):
            cd = st.get(f"cooldown_{side_key}")
            if cd and datetime.fromisoformat(cd) > now_utc and is_increasing:
                if side_key == "bid":
                    want_bid = False
                else:
                    want_ask = False
        mn = m["min_size"]
        q_now = combine(q_score(bids, mid, m["max_spread"], mn, "bid"), q_score(asks, mid, m["max_spread"], mn, "ask"), mid < 0.10 or mid > 0.90)
        size = self.size_for(m, q_now)
        # v1.3：減倉價相對「現價」，不看歷史成本。持有越久讓步越多；太久或虧太多就直接接受可成交價。
        # (v1.2 錨在損益兩平價是處分效應：市場走掉之後那張單永遠不會成交，部位被鎖到結算。)
        L = self.ledger.get(m["condition_id"])
        reduce_qty = size
        no_price_q = None
        queue = bool(m.get("queue_mode"))
        if queue:
            # v2：兩邊都只掛 min_size，價位由 queue_price 決定 (前面別人夠多才掛)；沒有出場讓價、沒有吃單，
            # 持倉靠另一邊被動成交配對 merge。庫存上限 (max_inventory) 與成交冷卻照舊只擋增倉那邊。
            size = reduce_qty = float(mn)
            ax = self.cfg.get("queue_ahead_x", 3.0)
            v = m["max_spread"]
            qlvl = st.setdefault("qlvl", {})
            yes_levels = list(bids)
            no_levels = [(round(1 - p, 6), sz) for p, sz in asks]
            # 已經掛著的單：同價位的別人只算我們掛上去當時就在的量 (之後加入的排在我們後面，不算保護)
            for key, levels, ords in (("yes", yes_levels, yes_orders), ("no", no_levels, no_orders)):
                if ords and key in qlvl:
                    p0 = float(ords[0].price)
                    levels[:] = [(q, min(sz, qlvl[key]) if abs(q - p0) < 1e-9 else sz) for q, sz in levels]
            ypx, y_ahead = queue_price(yes_levels, mid, v, tick, size, ax, round_price(ba - tick, tick, ROUND_DOWN))
            npx, n_ahead = queue_price(no_levels, 1 - mid, v, tick, size, ax, round_price(1 - bb - tick, tick, ROUND_DOWN))
            for key, px, levels, ords in (("yes", ypx, yes_levels, yes_orders), ("no", npx, no_levels, no_orders)):
                if px is not None and not (ords and abs(float(ords[0].price) - px) < tick / 2):
                    qlvl[key] = sum(sz for q, sz in levels if abs(q - px) < 1e-9)   # 換新價位：記下當下同價位的量
            if ypx is None:
                want_bid = False
            else:
                yes_bid = ypx
            if npx is None:
                want_ask = False
            else:
                no_price_q = npx
                yes_ask = round(1 - npx, 6)
            st["queue"] = {"yes": [ypx, round(y_ahead)], "no": [npx, round(n_ahead)]}
        # 零頭部位 (小於最低掛單量) 不掛減倉單：min_size 會把「不超過淨部位」蓋掉，反而開出新的方向部位
        # (09-23 London 只剩 0.88 股 NO，減倉單掛了 20 股 YES，多出 19 股新部位虧 1.5)
        elif L and 0 < abs(st["net"]) < mn and not m.get("hedge_on_fill"):   # 對沖市場的零頭交給對沖處理
            if st["net"] < 0:
                want_bid = False
            else:
                want_ask = False
            if st.get("exit_why") != "dust":
                log(f"  [{m['name'][:36]}] 零頭 {st['net']:+.2f} 股 < min {mn:.0f}，不掛減倉單")
                st["exit_why"] = "dust"
        elif L and abs(st["net"]) >= mn and not m.get("hedge_on_fill"):
            give, why = self.exit_price_give(m, st, mid, L)
            if st["net"] < 0:                                   # 持 NO：買 YES 出場，願意出的價 = 現價 + give
                px = round_price(min(ba - tick, mid + give), tick, ROUND_DOWN)
                if want_bid:
                    yes_bid = max(yes_bid, px) if why else yes_bid
                reduce_qty = float(min(size, max(mn, math.ceil(-st["net"]))))
            else:                                               # 持 YES：賣 YES 出場，願意收的價 = 現價 − give
                px = round_price(max(bb + tick, mid - give), tick, ROUND_UP)
                if want_ask:
                    yes_ask = min(yes_ask, px) if why else yes_ask
                reduce_qty = float(min(size, max(mn, math.ceil(st["net"]))))
            if why and why != st.get("exit_why"):
                log(f"  [{m['name'][:36]}] exit mode: {why} give {give:.3f} net {st['net']:+.1f}")
                st["exit_why"] = why
        if want_bid and yes_bid < tick:
            want_bid = False
        if want_ask and yes_ask > 1 - tick:
            want_ask = False
        no_price = no_price_q if no_price_q is not None else round_price(1 - yes_ask, tick, ROUND_DOWN)
        # 先做減倉那邊：多 YES 就先掛 NO
        order = [("no", no_orders, m["no_token"], no_price if want_ask else None, "NO", st["net"] > 0),
                 ("yes", yes_orders, m["yes_token"], yes_bid if want_bid else None, "YES", st["net"] < 0)]
        if st["net"] < 0:
            order.reverse()
        on_book = {}
        for key, ords, token, price, label, reducing in order:
            qty = reduce_qty if reducing else size
            on_book[key] = self.reconcile(ords, token, price, qty, mn, tick, f"{label} [{m['name'][:30]}]", budget, reducing, m, key, st["net"], exact=queue)
        self.exposure_cached = self.cfg["capital"] - budget["left"]
        share, q_exist, mid_pm = self.reward_estimate(m, st, bids, asks, mid, yes_bid, yes_ask, on_book["yes"], on_book["no"])
        if getattr(self, "ws", None):
            self.ws.note_quote(m["yes_token"], (bb + ba) / 2)
            self.ws.note_quote(m["no_token"], 1 - (bb + ba) / 2)
        return self.snapshot(m, st, mid, bb, ba, share, q_exist, yes_bid if on_book["yes"] else None, yes_ask if on_book["no"] else None, size,
                             {"paused": paused, "on_book": {"yes": on_book["yes"], "no": on_book["no"]}, "position": {"yes": y, "no": n}, "mid_pm": round(mid_pm, 4),
                              "actual_today": round(self.actual["by_market"].get(m["condition_id"], 0.0), 4),
                              "cluster": self.cluster_of(m), "cluster_worst_loss": round(self.cluster_bound(self.cluster_of(m)), 2),
                              "liq": st.get("liq"), "ref_ok": st.get("ref", {}).get("ok"), "mismatch": self.mismatch.get(m["condition_id"]),
                              "queue": st.get("queue") if queue else None})

    # ---- 每輪前後
    def run(self):
        log(f"LIVE dry={self.dry} address={self.address} capital cap {self.cfg['capital']} cluster cap {self.cfg['cluster_max_loss_usd']} daily stop {self.cfg['daily_loss_stop_usd']}")
        self.before_round()
        try:
            self.balance = int(self.client.get_balance_allowance(asset_type="COLLATERAL").balance) / 1e6
        except Exception as e:  # noqa: BLE001
            log(f"  balance fetch failed: {e}")
        try:
            self.income_paid = fetch_income_paid(self.address, self.cfg["ledger_history_from"])
            self.manual = fetch_manual(self.address, self.cfg.get("manual_cids", []))
            log(f"  income paid on-chain since {self.cfg['ledger_history_from']}: reward {self.income_paid['reward']:.4f} rebate {self.income_paid['rebate']:.4f}")
        except Exception as e:  # noqa: BLE001
            log(f"  income_paid fetch failed: {e}")
        self.perf_cached = self.perf()
        if not getattr(self, "exp_start", None):
            self.exp_start = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "realized": sum(self.perf_cached["realized"].values()),
                              "unrealized": sum(self.perf_cached["unrealized"].values())}
            log(f"experiment baseline set at {self.exp_start['at']}")
        self.perf_cached = self.perf()
        self.roll_day(self.perf_cached)
        log(f"  account worst-loss {self.account_bound():.2f} / cap {self.cfg.get('account_max_loss_usd')}")
        for cl in {self.cluster_of(m) for m in self.cfg["markets"]}:
            log(f"  cluster {cl[:30]}: worst-loss bound {self.cluster_bound(cl):.2f} / cap {self.cfg['cluster_max_loss_usd']}")
        super().run()

    def before_round(self):
        # 抓失敗 (data-api 偶爾 rate limit) 就沿用上一輪，不然 net 變 0 會忘記庫存、兩邊全掛
        try:
            pos, by_cid = {}, {}
            for page in self.client.list_positions(user=self.address):
                for p in page.items:
                    pos[str(p.asset_id)] = {"size": float(p.current_size or 0), "avg": float(p.avg_price or 0),
                                            "cur": float(p.current_price or 0), "pnl": float(p.total_pnl or 0)}
                    d = by_cid.setdefault(p.condition_id, {"fees": 0.0, "title": p.title})
                    d["yes" if (p.outcome_index or 0) == 0 else "no"] = float(p.current_price or 0)
                    d["fees"] += float(p.entry_fees_usdc or 0)
            self.positions = pos
            self.positions_by_cid = by_cid
            self.positions_ts = time.time()
        except Exception as e:  # noqa: BLE001
            log(f"  positions fetch failed, keeping last ({self.data_age():.0f}s old): {e}")
        self.exposure_cached = self.exposure()
        self.refresh_orders_view()
        self.enforce_cluster_caps()

    def enforce_cluster_caps(self):
        """叢集上界已超過上限 (例如上限調低、或舊掛單) 時，從貢獻最大的掛單開始撤，撤到 ≤ 上限或沒單可撤。
        只撤掛單，不動持倉 (平倉是另一個決策)。"""
        cap = self.cfg["cluster_max_loss_usd"]
        for cl in {self.cluster_of(m) for m in self.cfg["markets"]}:
            for _ in range(20):
                cur = self.cluster_bound(cl)
                if cur <= cap + 1e-9:
                    break
                best = None
                for m in self.cfg["markets"]:
                    if self.cluster_of(m) != cl:
                        continue
                    cid = m["condition_id"]
                    for o in self.orders_view.get(cid, []):
                        rest = [(x["side"], x["qty"], x["price"]) for x in self.orders_view[cid] if x["id"] != o["id"]]
                        without = self.cluster_bound(cl, (cid, rest))
                        if best is None or cur - without > best[0]:
                            best = (cur - without, m, o)
                if best is None or best[0] <= 1e-9:
                    log(f"  cluster {cl[:30]} bound {cur:.2f} > cap {cap} but no order reduces it (holdings alone)")
                    break
                _, m, o = best
                log(f"  cluster {cl[:30]} bound {cur:.2f} > cap {cap}: cancel {o['side']} {o['qty']:.0f}@{o['price']} [{m['name'][:30]} ..{m['condition_id'][-6:]}]")
                event({"type": "risk_trim", "cluster": cl, "market": m["name"], "side": o["side"], "qty": o["qty"], "price": o["price"], "bound": round(cur, 2)})
                self.cancel([o["id"]], "cluster cap")

    def refresh_orders_view(self):
        """用 API 全量掛單重建本地視角；本輪送出但 API 還沒看到的單保留為 pending (計入曝險)，超過 3 輪沒出現就丟掉並警告。"""
        try:
            api = {}
            for page in self.client.list_open_orders():
                for o in page.items:
                    if o.side != "BUY":
                        continue
                    m = next((mk for mk in self.cfg["markets"] if str(o.asset_id) in (mk["yes_token"], mk["no_token"])), None)
                    if not m:
                        continue
                    side = "yes" if str(o.asset_id) == m["yes_token"] else "no"
                    api.setdefault(m["condition_id"], []).append({"id": o.id, "side": side, "qty": float(o.original_size) - float(o.size_matched or 0),
                                                                    "price": float(o.price), "pending_rounds": 0})
        except Exception as e:  # noqa: BLE001
            log(f"  orders view refresh failed, keeping last ({self.data_age():.0f}s old): {e}")
            for lst in self.orders_view.values():
                for o in lst:
                    o["pending_rounds"] = o.get("pending_rounds", 0) + 1
            return
        self.orders_ts = time.time()
        for cid, lst in self.orders_view.items():
            seen = {o["id"] for o in api.get(cid, [])}
            for o in lst:
                if o.get("pending_rounds", 0) > 0 and o["id"] not in seen and not o["id"].startswith("dry"):
                    o["pending_rounds"] += 1
                    if o["pending_rounds"] <= 3:
                        api.setdefault(cid, []).append(o)
                    else:
                        log(f"  pending order {o['id'][:12]} never appeared in API after 3 rounds, dropping from view")
        self.orders_view = api

    def after_round(self):
        self.record_fills()
        for m in self.cfg["markets"]:
            st = self.state.get(m["condition_id"])
            if m.get("hedge_on_fill") and m.get("enabled", True) and st is not None:
                self.run_hedges(m, st)
        self.merge_pairs()
        self.check_earnings()
        if self.actual.get("date"):
            self.earned_by_day[self.actual["date"]] = float(self.actual.get("today") or 0.0)
        try:
            self.balance = int(self.client.get_balance_allowance(asset_type="COLLATERAL").balance) / 1e6
        except Exception as e:  # noqa: BLE001
            log(f"  balance fetch failed: {e}")
        self.before_round()
        self.perf_cached = self.perf()
        self.roll_day(self.perf_cached)
        self.perf_cached = self.perf()
        d = self.perf_cached.get("day_pnl_cost_basis")
        c = self.perf_cached.get("cum_pnl_cost_basis")
        why = None
        if d is not None and d < -self.cfg["daily_loss_stop_usd"]:
            why = f"day_pnl {d:+.2f} < -{self.cfg['daily_loss_stop_usd']}"
        elif c is not None and c < -self.cfg.get("cumulative_loss_stop_usd", 1e9):
            why = f"cumulative {c:+.2f} < -{self.cfg['cumulative_loss_stop_usd']}"
        if why and not os.path.exists(HALT_FILE):
            open(HALT_FILE, "w").write(f"{datetime.now(timezone.utc).isoformat()} {why}\n")
            log(f"LOSS STOP: {why} → HALT_INCREASE (人工刪除 {HALT_FILE} 才恢復增倉)")
            event({"type": "halt", "why": why, "day_pnl": d, "cum_pnl": c})
        self.save_ledger()

    def status_extra(self):
        return {"dry_run": self.dry, "wallet": self.address, "balance": self.balance, "exposure": round(self.exposure_cached, 2),
                "actual": self.actual, "perf": getattr(self, "perf_cached", None),
                "ws": self.ws.stats() if getattr(self, "ws", None) else None,
                "clusters": {cl: round(self.cluster_bound(cl), 2) for cl in {self.cluster_of(m) for m in self.cfg["markets"]}},
                "cluster_cap": self.cfg["cluster_max_loss_usd"],
                "hedge_report": getattr(self, "hedge_rep", None), "queue_report": getattr(self, "queue_rep", None)}

    def on_disabled(self, m):
        """config 停用的市場：掛單要撤掉 (持倉留著)；清算估值照算，不然 perf 會漏掉這些部位。"""
        ords = self.my_orders(m["condition_id"])
        if ords:
            self.cancel([o.id for o in ords], f"disabled [{m['name'][:30]}]")
        try:
            bids, asks, _ = book_levels(self.client, m["yes_token"], m["no_token"])
            if bids and asks:
                self.state.setdefault(m["condition_id"], {})["liq"] = self.liquidation(m, bids, asks)
        except Exception as e:  # noqa: BLE001
            log(f"  liq for disabled [{m['name'][:30]}] failed: {e}")

    def shutdown(self):
        if getattr(self, "ws", None):
            self.ws.stop()
        if not self.dry:
            r = self.client.cancel_all()
            log(f"cancel_all: {len(r.canceled)} cancelled")

    def record_fills(self):
        """帳號成交紀錄 (bot 啟動後、只看自己市場)，用 maker_orders 裡自己那份的量記 fill。"""
        try:
            trades = [t for page in self.client.list_account_trades(after=str(self.start_ts)) for t in page.items]
        except Exception as e:  # noqa: BLE001
            log(f"  list_account_trades failed: {e}")
            return
        me = (self.address or "").lower()
        os.makedirs(OUT_DIR, exist_ok=True)
        for t in sorted(trades, key=lambda t: str(t.matched_at or "")):
            if t.id in self.seen_trades or str(t.asset_id) not in self.my_tokens:
                continue
            self.seen_trades.add(t.id)
            mine = [mo for mo in (t.maker_orders or []) if str(mo.maker_address).lower() == me]
            if not mine and (t.trader_side or "").upper() == "TAKER":
                # 人工 taker 出場：只進帳本，不算 bot 的成交
                mk = next((x for x in self.cfg["markets"] if str(t.asset_id) in (x["yes_token"], x["no_token"])), None)
                if mk:
                    outc = "yes" if str(t.asset_id) == mk["yes_token"] else "no"
                    q = -float(t.size) if (t.side or "").upper() == "SELL" else float(t.size)
                    if t.id not in self.ledger_trade_ids:
                        self.ledger_trade_ids.add(t.id)
                        L = self.ledger.setdefault(mk["condition_id"], MarketLedger())
                        (L.sell(outc, -q, float(t.price)) if q < 0 else L.buy(outc, q, float(t.price)))
                        if mk.get("hedge_on_fill") and q > 0:
                            stx = self.state.setdefault(mk["condition_id"], {})
                            left = q
                            for f in stx.get("hedge_inflight") or []:
                                if f["comp"] == outc and left > 1e-9:
                                    use = min(f["qty"], left); f["qty"] -= use; left -= use
                            stx["hedge_inflight"] = [f for f in stx.get("hedge_inflight") or [] if f["qty"] > 1e-6]
                    log(f"  TAKER {t.side} {outc} {float(t.size):g} @ {t.price} [{mk['name'][:40]}] (manual exit, ledger only)")
                continue
            my_size = sum(float(mo.matched_amount) for mo in mine) if mine else float(t.size)
            my_price = float(mine[0].price) if mine else float(t.price)
            my_outcome = (mine[0].outcome if mine else t.outcome) or ""
            # t.asset_id 是 taker 那邊的 token；互補撮合 (taker 買 NO 打到我們的 YES 買單) 時跟我們的不同，要看自己 maker order 的
            my_asset = str(mine[0].asset_id) if mine else str(t.asset_id)
            m = next((mk for mk in self.cfg["markets"] if my_asset in (mk["yes_token"], mk["no_token"])), None)
            with open(FILLS_PATH, "a", encoding="utf-8") as f:
                rec = t.model_dump(mode="json")
                rec.update({"my_size": my_size, "my_price": my_price, "my_outcome": my_outcome})
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if m is None or my_size <= 0:
                continue
            st = self.state[m["condition_id"]]
            mid = st.get("last_mid") or my_price
            outc = "yes" if my_asset == m["yes_token"] else "no"
            self.ledger_apply_fill(m["condition_id"], outc, my_size, my_price, t.id)
            ref = st.get("ref") or {}
            p_yes = my_price if outc == "yes" else round(1 - my_price, 4)
            q = my_size if outc == "yes" else -my_size
            st.setdefault("markouts", []).append({"ts": str(t.matched_at or datetime.now(timezone.utc).isoformat()), "q": q, "p": p_yes,
                                                  "ref_0": ref.get("mid") if ref.get("ok") else None, "trade_id": t.id})
            cd_until = (datetime.now(timezone.utc) + timedelta(minutes=self.cfg.get("fill_cooldown_min", 15))).isoformat()
            if outc == "yes":  # 買到 YES
                st["cooldown_bid"] = cd_until
                self.note_fill(m, st, "bid", my_price, my_size, mid, {"trade_id": t.id, "ref_0_ok": bool(ref.get("ok"))})
            else:  # 買到 NO = 以 (1-p) 賣出 YES
                st["cooldown_ask"] = cd_until
                self.note_fill(m, st, "ask", p_yes, my_size, mid, {"trade_id": t.id, "ref_0_ok": bool(ref.get("ok"))})
            log(f"  FILL BUY {my_outcome} {my_size:g} @ {my_price} [{m['name'][:40]}]")
            if m.get("hedge_on_fill"):
                self.run_hedges(m, st)

    # ---- 成交即對沖 (淨部位制)
    def hedge_net(self, m, st=None):
        """帳本淨部位 + 已成交但還沒被 record_fills 記進帳本的對沖 (inflight)。
        FAK 的回傳沒有成交編號，沒辦法先記帳再去重，所以反過來：成交先記在 inflight，record_fills 記進帳本時消掉。"""
        L = self.ledger.get(m["condition_id"])
        net = (L.qty("yes") - L.qty("no")) if L else 0.0
        for f in (st or {}).get("hedge_inflight") or []:
            net += f["qty"] if f["comp"] == "yes" else -f["qty"]
        return net, L

    def run_hedges(self, m, st):
        """對沖市場的目標是淨部位 = 0。每次呼叫看帳本淨部位，不是零就用 FAK 買互補那邊。
        - FAK：吃得到的吃、吃不到的當場取消，對沖單永遠不會掛在簿上被人慢慢成交 (09-26 Zverev 就是這樣多出 19.5 股)
        - 成交當場記進帳本 (用回傳的 trade_ids 去重)，下一輪不會把對沖單自己的成交當成新成交
        - 價格上限：配對成本 ≤ 1 + hedge_max_slip；上限內吃不到就等，超過 timeout 就封鎖這個市場等人工處理"""
        # inflight 超過 10 分鐘還沒被 record_fills 消掉 → 丟掉 (帳本對帳會抓到差異)
        now0 = datetime.now(timezone.utc)
        infl = st.get("hedge_inflight") or []
        stale = [f for f in infl if (now0 - datetime.fromisoformat(f["ts"])).total_seconds() > 600]
        if stale:
            log(f"  HEDGE inflight never recorded [{m['name'][:36]}]: {stale}")
        st["hedge_inflight"] = [f for f in infl if f not in stale]
        net, L = self.hedge_net(m, st)
        if abs(net) < 1:
            st["hedge_since"] = None
            st["hedge_blocked"] = False
            return
        if st.get("hedge_blocked"):
            return
        now = datetime.now(timezone.utc)
        if not st.get("hedge_since"):
            st["hedge_since"] = now.isoformat()
        age = (now - datetime.fromisoformat(st["hedge_since"])).total_seconds() / 60
        held = "yes" if net > 0 else "no"
        qty = abs(net)
        avg = (L.cost(held) / L.qty(held)) if L and L.qty(held) > 0 else 0.5
        cap = round(1 - avg + self.cfg.get("hedge_max_slip", 0.03), 4)
        if age > self.cfg.get("hedge_timeout_min", 15):
            st["hedge_blocked"] = True
            log(f"  HEDGE BLOCKED [{m['name'][:36]}] {held} {qty:.2f} 股 {age:.0f} 分鐘對沖不掉 (上限 {cap})，停止這個市場報價等人工處理")
            event({"type": "hedge_blocked", "market": m["name"], "held": held, "qty": qty, "avg": avg, "cap": cap})
            return
        comp_token = m["no_token"] if held == "yes" else m["yes_token"]
        comp = "no" if held == "yes" else "yes"
        # 對面的賣單 (排除自家)：算出 cap 以內能買到多少股、要花多少
        try:
            ob = self.client.get_order_book(token_id=comp_token)
        except Exception as e:  # noqa: BLE001
            log(f"  HEDGE book failed: {e}")
            return
        asks = sorted((float(x.price), float(x.size)) for x in ob.asks)
        want, spend, worst = qty, 0.0, None
        for px, sz in asks:
            if px > cap + 1e-9 or want <= 1e-9:
                break
            take = min(want, sz)
            spend += take * px; want -= take; worst = px
        can = qty - want
        if can < 1 or worst is None:
            return                                               # cap 以內沒貨，等下一輪
        # 自己同側的買單會跟對沖自成交，先撤
        same = [o["id"] for o in self.orders_view.get(m["condition_id"], []) if o["side"] == held]
        if same:
            self.cancel(same, f"hedge: clear own {held} bids")
        if self.dry:
            log(f"  [dry] HEDGE buy {can:.2f} {comp} spend {spend:.2f} ≤{worst}")
            return
        # 可成交買單平台最低 $1。不到 $1 就多買一點湊到 $1：多出來的一點點變成反向小部位，下一輪被反向對沖回去
        # (反向那邊價格高，金額一定過 $1)，淨部位收斂到零，額外成本只有幾分錢。
        size = can
        if size * worst < 1.02:
            depth = sum(sz for px, sz in asks if px <= worst + 1e-9)
            size = min(depth, math.ceil(1.02 / worst * 100) / 100)
            if size * worst < 1.0:
                return                                           # cap 以內的深度連 $1 都不到，等
        try:
            # 可成交限價單，價格 = cap 以內實際有貨的最深那一檔 → 當下吃得到 (含鏡像流動性，MINT 撮合)；
            # FAK 市價單不會跟鏡像流動性撮合 (09-26 實測: OpenAI NO 賣單全是 YES 買單的鏡像，FAK 一直找不到對手)。
            # 沒吃完的剩餘立刻撤；萬一撤單前被零星成交，淨部位制也會正確處理，不會重複計算。
            r = self.client.place_limit_order(token_id=comp_token, price=str(worst), size=str(round(size, 2)),
                                              side="BUY", post_only=False)
            if getattr(r, "ok", False) and getattr(r, "status", "") == "live":
                try:
                    self.client.cancel_orders(order_ids=[r.order_id])
                except Exception as e:  # noqa: BLE001
                    log(f"  HEDGE cancel remainder failed: {e}")
        except Exception as e:  # noqa: BLE001
            log(f"  HEDGE failed [{m['name'][:36]}]: {e}")
            return
        got = paid = 0.0
        if getattr(r, "ok", False):
            try:
                paid = float(r.making_amount or 0); got = float(r.taking_amount or 0)
                if got > qty * 100:                              # 鏈上最小單位保險
                    got /= 1e6; paid /= 1e6
            except (TypeError, ValueError):
                got = paid = 0.0
            if got > 0:
                st.setdefault("hedge_inflight", []).append({"comp": comp, "qty": got, "paid": paid, "ts": now.isoformat()})
        event({"type": "hedge", "market": m["name"], "held": held, "net_before": round(net, 4), "got": round(got, 4),
               "paid": round(paid, 4), "avg_held": round(avg, 4), "cap": cap, "status": getattr(r, "status", None),
               "msg": getattr(r, "message", None), "ts": now.isoformat(timespec="seconds")})
        log(f"  HEDGE [{m['name'][:36]}] net {net:+.2f} → bought {got:.2f} {comp} for {paid:.2f} (≤{worst}, cap {cap}) "
            f"{getattr(r, 'status', getattr(r, 'message', ''))}")

    def merge_pairs(self):
        if self.dry or not self.cfg.get("auto_merge", True):
            return
        for m in self.cfg["markets"]:
            y = self.positions.get(m["yes_token"], {}).get("size", 0)
            n = self.positions.get(m["no_token"], {}).get("size", 0)
            if min(y, n) < 1:
                continue
            try:
                out = self.client.merge_positions(condition_id=m["condition_id"], amount="max").wait()
                k = min(y, n)
                ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                log(f"  MERGE {k:.1f} pairs -> pUSD [{m['name'][:40]}] {getattr(out, 'status', out)}")
                event({"type": "merge", "market": m["name"], "pairs": k, "ts": ts})
                self.merges_log.append([ts, m["condition_id"], k, "bot"])
                self.ledger.setdefault(m["condition_id"], MarketLedger()).merge(k)
            except Exception as e:  # noqa: BLE001
                log(f"  MERGE failed [{m['name'][:40]}]: {e}")

    def check_earnings(self):
        """每 earnings_check_min 分鐘抓 Polymarket 算的今日實際獎勵；跨 UTC 日時把昨天的估算 vs 實際寫進事件。"""
        now = datetime.now(timezone.utc)
        if self.actual["checked_at"] and (now - datetime.fromisoformat(self.actual["checked_at"])).total_seconds() < self.cfg["earnings_check_min"] * 60:
            return
        today = now.strftime("%Y-%m-%d")
        try:
            if self.actual["date"] and self.actual["date"] != today:
                y = self.actual["date"]
                actual_y = fetch_earnings(self.client, y, {m["condition_id"] for m in self.cfg["markets"]})
                est_y = self.est_by_day.get(y, 0.0)
                self.actual["yesterday"] = {"date": y, "actual": round(sum(actual_y.values()), 4), "est": round(est_y, 4)}
                event({"type": "daily", "date": y, "est": round(est_y, 4), "actual": round(sum(actual_y.values()), 4),
                       "ratio": round(sum(actual_y.values()) / est_y, 3) if est_y else None, "by_market": actual_y})
                log(f"DAILY {y}: est {est_y:.3f} actual {sum(actual_y.values()):.3f}")
            by = fetch_earnings(self.client, today, {m["condition_id"] for m in self.cfg["markets"]})
            self.actual.update({"date": today, "today": round(sum(by.values()), 4), "by_market": by, "checked_at": now.isoformat()})
            self.income_paid = fetch_income_paid(self.address, self.cfg["ledger_history_from"])
            self.manual = fetch_manual(self.address, self.cfg.get("manual_cids", []))
            hm = [m for m in self.cfg["markets"] if m.get("hedge_on_fill")]
            if hm and self.cfg.get("hedge_start"):
                self.hedge_rep = hedge_report(self.client, self.address, hm, self.cfg["hedge_start"])
            qm = [m for m in self.cfg["markets"] if m.get("queue_mode")]
            if qm and self.cfg.get("queue_start"):
                self.queue_rep = queue_report(self.client, self.address, qm, self.cfg["queue_start"])
        except Exception as e:  # noqa: BLE001
            log(f"  earnings check failed: {e}")
            self.actual["checked_at"] = now.isoformat()


def hedge_report(client, address, markets, since_iso):
    """成交即對沖實驗的帳：每個市場、每個 UTC 日的「實領獎勵 − 交易成本」。
    交易成本用鏈上活動算 (usdcSize 已含手續費)：−買進 + 賣出 + merge + 兌付，再加上目前還沒配對的持倉市值。"""
    cids = {m["condition_id"]: m["name"] for m in markets}
    if not cids:
        return {}
    since = datetime.fromisoformat(since_iso).replace(tzinfo=timezone.utc).timestamp() if "+" not in since_iso \
        else datetime.fromisoformat(since_iso).timestamp()
    act = requests.get("https://data-api.polymarket.com/activity", params={"user": address, "limit": 500}, timeout=30).json()
    rows = defaultdict(lambda: {"cash": 0.0, "trades": 0, "reward": 0.0})
    for a in act:
        c = a.get("conditionId")
        if c not in cids or float(a.get("timestamp", 0)) < since:
            continue
        d = datetime.fromtimestamp(a["timestamp"], timezone.utc).strftime("%Y-%m-%d")
        v = float(a.get("usdcSize") or 0)
        t = a.get("type")
        if t == "TRADE":
            rows[(c, d)]["cash"] += -v if a.get("side") == "BUY" else v
            rows[(c, d)]["trades"] += 1
        elif t in ("MERGE", "REDEEM"):
            rows[(c, d)]["cash"] += v
    start_day = datetime.fromtimestamp(since, timezone.utc).date()
    day = start_day
    today = datetime.now(timezone.utc).date()
    while day <= today:
        ds = day.isoformat()
        try:
            earn = fetch_earnings(client, ds, set(cids))
        except Exception:  # noqa: BLE001
            earn = {}
        for c in cids:
            rows[(c, ds)]["reward"] += earn.get(c, 0.0)
        day += timedelta(days=1)
    pos = requests.get("https://data-api.polymarket.com/positions", params={"user": address, "sizeThreshold": 0.01}, timeout=30).json()
    open_val = defaultdict(float)
    for p in pos:
        if p.get("conditionId") in cids:
            open_val[p["conditionId"]] += float(p.get("currentValue") or 0)
    out = {"since": since_iso, "markets": {}, "total": {"reward": 0.0, "trading": 0.0, "net": 0.0}}
    for c, name in cids.items():
        days = []
        for (cc, d), r in sorted(rows.items()):
            if cc != c:
                continue
            days.append({"date": d, "reward": round(r["reward"], 4), "trading_cash": round(r["cash"], 4), "trades": r["trades"]})
        reward = sum(x["reward"] for x in days)
        trading = sum(x["trading_cash"] for x in days) + open_val[c]      # 現金流 + 還沒配對的持倉市值
        out["markets"][c] = {"name": name[:60], "reward": round(reward, 4), "trading": round(trading, 4),
                             "open_value": round(open_val[c], 4), "net": round(reward + trading, 4), "days": days}
        for k, v in (("reward", reward), ("trading", trading), ("net", reward + trading)):
            out["total"][k] += v
    out["total"] = {k: round(v, 4) for k, v in out["total"].items()}
    return out


def queue_report(client, address, markets, since_iso):
    """v2 排隊實驗的帳：每市場「實領獎勵 + 交易 (現金流 + 未配對持倉市值)」，加上我們 maker 成交的 1 小時 markout。
    成功標準 (09-27 定)：markout 每股 ≥ −1 分 (之前 −3.1)，且 獎勵 ≥ 2 × 交易損失。"""
    rep = hedge_report(client, address, markets, since_iso)
    names = {m["name"] for m in markets}
    since = datetime.fromisoformat(since_iso)
    mk = {"n": 0, "shares": 0.0, "pnl_1h": 0.0, "na": 0}
    if os.path.exists(EVENTS_PATH):
        for line in open(EVENTS_PATH, encoding="utf-8"):
            if '"markout"' not in line:
                continue
            e = json.loads(line)
            if e.get("type") != "markout" or e.get("market") not in names:
                continue
            ts = datetime.fromisoformat(str(e["ts"]).replace(" ", "T").replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < since:
                continue
            if e.get("S") is None or e.get("A_3600") is None:
                mk["na"] += 1
                continue
            mk["n"] += 1; mk["shares"] += abs(e["q"]); mk["pnl_1h"] += e["S"] + e["A_3600"]
    mk["cents_per_share_1h"] = round(100 * mk["pnl_1h"] / mk["shares"], 2) if mk["shares"] else None
    mk["pnl_1h"] = round(mk["pnl_1h"], 4); mk["shares"] = round(mk["shares"], 2)
    rep["markout"] = mk
    t = rep["total"]
    loss = -min(t["trading"], 0.0)
    rep["verdict"] = {"markout_ok": mk["cents_per_share_1h"] is None or mk["cents_per_share_1h"] >= -1.0,
                      "reward_vs_loss": round(t["reward"] / loss, 2) if loss > 1e-9 else None}
    return rep


def fetch_manual(address, cids):
    """使用者自己下的方向性部位 (不歸 bot 管)：實付現金 (含手續費) 與目前市值。"""
    out = {"outlay": 0.0, "value": 0.0, "positions": []}
    if not cids:
        return out
    cids = set(cids)
    act = requests.get("https://data-api.polymarket.com/activity", params={"user": address, "limit": 500}, timeout=30).json()
    for a in act:
        if a.get("conditionId") in cids and a.get("type") == "TRADE":
            v = float(a.get("usdcSize") or 0)
            out["outlay"] += v if a.get("side") == "BUY" else -v
        elif a.get("conditionId") in cids and a.get("type") in ("REDEEM", "MERGE"):
            out["outlay"] -= float(a.get("usdcSize") or 0)
    pos = requests.get("https://data-api.polymarket.com/positions", params={"user": address, "sizeThreshold": 0.01}, timeout=30).json()
    for p in pos:
        if p.get("conditionId") in cids:
            out["value"] += float(p.get("currentValue") or 0)
            out["positions"].append({"title": p["title"][:60], "outcome": p["outcome"], "size": round(p["size"], 2),
                                     "avg": round(p["avgPrice"], 4), "cur": p["curPrice"], "value": round(p["currentValue"], 2)})
    return out


def fetch_income_paid(address, since_iso):
    """Data API activity 裡實際發到錢包的 REWARD / MAKER_REBATE (鏈上事件，不是「賺得」估計)。回 {"reward": x, "rebate": y, "items": [...]}。"""
    since = datetime.fromisoformat(since_iso).replace(tzinfo=timezone.utc).timestamp()
    out = {"reward": 0.0, "rebate": 0.0, "items": []}
    r = requests.get("https://data-api.polymarket.com/activity", params={"user": address, "type": "REWARD,MAKER_REBATE", "limit": 200}, timeout=30).json()
    for a in r:
        if float(a.get("timestamp", 0)) < since:
            continue
        v = float(a.get("usdcSize") or 0)
        out["reward" if a["type"] == "REWARD" else "rebate"] += v
        out["items"].append({"ts": datetime.fromtimestamp(a["timestamp"], timezone.utc).isoformat(timespec="minutes"), "type": a["type"], "usdc": v})
    return out


def fetch_earnings(client, date, condition_ids=None):
    """某 UTC 日 Polymarket 記到我們頭上的獎勵 {condition_id: earnings}。"""
    out = {}
    for page in client.list_user_earnings_for_day(date=date):
        for e in page.items:
            if condition_ids and e.condition_id not in condition_ids:
                continue
            out[e.condition_id] = out.get(e.condition_id, 0.0) + float(e.earnings or 0)
    return out


# ---------------------------------------------------------------- commands

def cmd_run(a):
    cfg = load_config()
    shadow = cfg.get("shadow", True) and not a.live
    if shadow:
        Shadow(cfg).run()
        return
    print(f"*** LIVE{' (dry)' if a.dry else ''}：真錢掛單，資金上限 {cfg['capital']} pUSD，{len(cfg['markets'])} 個市場 ***")
    if not a.dry and not a.yes and sys.stdin.isatty() and input("輸入 yes 繼續: ").strip() != "yes":
        sys.exit("aborted")
    try:
        Live(cfg, dry=a.dry).run()
    except KeyboardInterrupt:
        print("\nCtrl-C：掛單不會自動撤，要撤請跑 `rewards_bot.py cancel-all`")


def cmd_status(_a):
    cfg = load_config()
    client, address = make_client()
    bal = client.get_balance_allowance(asset_type="COLLATERAL")
    print(f"wallet {address}  pUSD {int(bal.balance) / 1e6:.2f}  mode={'shadow' if cfg.get('shadow', True) else 'LIVE'}  cap {cfg['capital']}")
    pos = get_positions(client, address)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    earn = fetch_earnings(client, today)
    print(f"\n市場 ({len(cfg['markets'])}):   今日 UTC {today} 實際獎勵合計 {sum(earn.values()):.4f}")
    for m in cfg["markets"]:
        y = pos.get(m["yes_token"], {}).get("size", 0)
        n = pos.get(m["no_token"], {}).get("size", 0)
        orders = [o for page in client.list_open_orders(market=m["condition_id"]) for o in page.items]
        od = " ".join(f"{o.outcome or '?'}@{o.price}x{float(o.original_size) - float(o.size_matched or 0):.0f}" for o in orders)
        print(f"  YES {y:6.1f} NO {n:6.1f}  earn {earn.get(m['condition_id'], 0):.4f}  orders[{od}]  {m['name'][:50]}")


def cmd_earnings(a):
    client, _ = make_client()
    date = a.date or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    by = fetch_earnings(client, date)
    tot = client.get_total_earnings_for_user_for_day(date=date)
    print(f"{date} (UTC)  total: {[float(t.earnings) for t in tot]}  markets: {len(by)}")
    for cid, e in sorted(by.items(), key=lambda kv: -kv[1]):
        print(f"  {e:8.4f}  {cid}")


def cmd_hedge_report(_a):
    cfg = load_config()
    client, address = make_client()
    hm = [m for m in cfg["markets"] if m.get("hedge_on_fill")]
    if not hm or not cfg.get("hedge_start"):
        print("沒有 hedge_on_fill 的市場或沒設 hedge_start")
        return
    r = hedge_report(client, address, hm, cfg["hedge_start"])
    print(f"成交即對沖實驗 since {r['since']}")
    for c, m in r["markets"].items():
        print(f"\n{m['name']}\n  獎勵 {m['reward']:+.4f}  交易 {m['trading']:+.4f} (未配對市值 {m['open_value']:.2f})  淨 {m['net']:+.4f}")
        for d in m["days"]:
            print(f"    {d['date']}  獎勵 {d['reward']:+.4f}  交易現金 {d['trading_cash']:+.4f}  成交 {d['trades']}")
    t = r["total"]
    print(f"\n合計  獎勵 {t['reward']:+.4f}  交易 {t['trading']:+.4f}  淨 {t['net']:+.4f}")


def cmd_queue_report(_a):
    cfg = load_config()
    client, address = make_client()
    qm = [m for m in cfg["markets"] if m.get("queue_mode")]
    if not qm or not cfg.get("queue_start"):
        print("沒有 queue_mode 的市場或沒設 queue_start")
        return
    r = queue_report(client, address, qm, cfg["queue_start"])
    print(f"v2 排隊實驗 since {r['since']}")
    for c, m in sorted(r["markets"].items(), key=lambda kv: -kv[1]["net"]):
        print(f"  獎勵 {m['reward']:+7.4f}  交易 {m['trading']:+7.4f} (未配對市值 {m['open_value']:6.2f})  淨 {m['net']:+7.4f}  {m['name'][:50]}")
    t, mk, v = r["total"], r["markout"], r["verdict"]
    print(f"\n合計  獎勵 {t['reward']:+.4f}  交易 {t['trading']:+.4f}  淨 {t['net']:+.4f}")
    print(f"maker 成交 {mk['n']} 筆 {mk['shares']} 股 (ref 不可靠 {mk['na']})  1h markout {mk['pnl_1h']:+.4f} = {mk['cents_per_share_1h']} 分/股 (目標 ≥ −1，之前 −3.1)")
    print(f"獎勵 / 交易損失 = {v['reward_vs_loss']} (目標 ≥ 2)")


def cmd_cancel_all(_a):
    client, _ = make_client()
    r = client.cancel_all()
    print(f"cancelled {len(r.canceled)}  not cancelled {r.not_canceled}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--capital", type=float, default=500)
    s.add_argument("--n", type=int, default=5)
    s.add_argument("--top", type=int, default=120, help="從獎勵最高的前 N 個市場裡挑")
    s.add_argument("--min-pool", type=float, default=50)
    s.add_argument("--min-days", type=float, default=3)
    s.add_argument("--max-days", type=float, default=120)
    s.add_argument("--min-price", type=float, default=0.06)
    s.add_argument("--max-price", type=float, default=0.94)
    s.add_argument("--size", type=float, default=50, help="每邊股數 (會自動拉到該市場 min size)")
    s.add_argument("--offset-ticks", type=int, default=2, help="離中價幾個 tick")
    s.add_argument("--max-inventory", type=float, default=150)
    s.add_argument("--show", type=int, default=0, help="先印前 N 名候選 (挑選前)")
    s.add_argument("--no-write", action="store_true", help="只看不寫 config")
    s.set_defaults(fn=cmd_select)
    s = sub.add_parser("run")
    s.add_argument("--live", action="store_true", help="無視 config 的 shadow 旗標，直接 live")
    s.add_argument("--dry", action="store_true", help="live 流程但不下單")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("earnings")
    s.add_argument("--date", help="UTC 日期 YYYY-MM-DD，預設昨天")
    s.set_defaults(fn=cmd_earnings)
    sub.add_parser("cancel-all").set_defaults(fn=cmd_cancel_all)
    sub.add_parser("hedge-report").set_defaults(fn=cmd_hedge_report)
    sub.add_parser("queue-report").set_defaults(fn=cmd_queue_report)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
