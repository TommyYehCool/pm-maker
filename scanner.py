#!/usr/bin/env python3
"""Polymarket 掃描器：找定價異常 (negRisk 機率加總) 與做市候選市場。

只用公開 API，不需要錢包或金鑰：
  Gamma  https://gamma-api.polymarket.com  (市場列表、最佳買賣價、費率)
  CLOB   https://clob.polymarket.com       (訂單簿深度，只在 --books 時抓)

用法:
  python scanner.py                 # 抓全部活躍市場並分析
  python scanner.py --from-cache    # 用上次抓的 out/raw_events.json 重新分析
  python scanner.py --books 10      # 對前 10 個做市候選再抓 CLOB 深度
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
RAW_CACHE = os.path.join(OUT_DIR, "raw_events.json")

# 標籤黑名單：內線密集、新聞驅動的市場，做市會被單邊打穿
EXCLUDE_TAG_SLUGS = {
    "elon-musk", "mentions", "tweet", "tweets", "celebrity",
    "engagement", "wedding", "nobel-prize", "airdrops", "token-launch",
}

session = requests.Session()
session.headers["User-Agent"] = "pm-scanner/0.1"


# ---------------------------------------------------------------- fetch

OFFSET_CAP = 2000  # Gamma 的 offset 超過 2000 會回 422，所以要用 endDate 切窗口


def _get_events(params):
    for attempt in range(3):
        try:
            r = session.get(f"{GAMMA}/events", params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise
            print(f"  retry {params.get('offset')}: {e}", file=sys.stderr)
            time.sleep(2)


def _fetch_window(lo, hi, page_size, seen, out):
    """抓 endDate 在 [lo, hi) 的 events；若撞到 offset 上限就把窗口對半切。"""
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    got = []
    offset = 0
    while True:
        batch = _get_events({"closed": "false", "active": "true", "limit": page_size,
                             "offset": offset, "order": "id", "ascending": "true",
                             "end_date_min": fmt(lo), "end_date_max": fmt(hi)})
        got.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
        if offset >= OFFSET_CAP:
            if (hi - lo).total_seconds() < 3600:
                print(f"  window {fmt(lo)}..{fmt(hi)} still over cap, truncated", file=sys.stderr)
                break
            mid = lo + (hi - lo) / 2
            print(f"  split window {fmt(lo)}..{fmt(hi)}", file=sys.stderr)
            _fetch_window(lo, mid, page_size, seen, out)
            _fetch_window(mid, hi, page_size, seen, out)
            return
        time.sleep(0.15)
    new = 0
    for ev in got:
        if ev["id"] not in seen:
            seen.add(ev["id"])
            out.append(ev)
            new += 1
    print(f"  {fmt(lo)[:10]}..{fmt(hi)[:10]}: +{new} (total {len(out)})", file=sys.stderr)


def fetch_all_events(max_pages=None, page_size=100):
    """抓所有 active & not closed 的 events (含 markets)。
    先用粗窗口 (最近 1 天 / 1 週 / 1 月 / 3 月 / 1 年 / 更遠)，撞上限自動細切。"""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    edges = [now - timedelta(days=3650), now - timedelta(days=1), now + timedelta(days=1),
             now + timedelta(days=7), now + timedelta(days=30), now + timedelta(days=90),
             now + timedelta(days=365), now + timedelta(days=3650)]
    seen, out = set(), []
    for lo, hi in zip(edges, edges[1:]):
        _fetch_window(lo, hi, page_size, seen, out)
    return out


def fetch_book(token_id):
    r = session.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=20)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- parse

def _jlist(s):
    if isinstance(s, list):
        return s
    try:
        return json.loads(s or "[]")
    except json.JSONDecodeError:
        return []


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def days_left(iso):
    if not iso:
        return None
    try:
        end = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (end - datetime.now(timezone.utc)).total_seconds() / 86400


def taker_fee_per_share(market, price):
    """Polymarket 費用公式: rate * (p * (1-p)) ^ exponent，只收 taker。"""
    if not market.get("feesEnabled"):
        return 0.0
    sched = market.get("feeSchedule") or {}
    rate = _f(sched.get("rate"), 0.0)
    exp = _f(sched.get("exponent"), 1.0)
    if price is None:
        return 0.0
    return rate * (price * (1 - price)) ** exp


def flatten_markets(events):
    """把 event -> markets 攤平成一列一個 market 的 dict。"""
    rows = []
    for ev in events:
        tags = {t.get("slug", "") for t in (ev.get("tags") or [])}
        unresolved = [m for m in (ev.get("markets") or []) if not m.get("closed")]
        untradeable = sum(1 for m in unresolved
                          if not m.get("active") or not m.get("acceptingOrders"))
        for m in unresolved:
            if not m.get("active") or not m.get("acceptingOrders"):
                continue
            prices = [_f(p) for p in _jlist(m.get("outcomePrices"))]
            tokens = _jlist(m.get("clobTokenIds"))
            yes_mid = prices[0] if prices else None
            bid = _f(m.get("bestBid"))
            ask = _f(m.get("bestAsk"))
            spread = _f(m.get("spread"))
            if spread is None and bid is not None and ask is not None:
                spread = ask - bid
            rows.append({
                "event_id": ev.get("id"),
                "event_title": ev.get("title"),
                "event_slug": ev.get("slug"),
                "neg_risk": bool(ev.get("negRisk")),
                "neg_risk_augmented": bool(ev.get("negRiskAugmented")),
                "event_untradeable": untradeable,
                "tags": sorted(tags),
                "market_id": m.get("id"),
                "condition_id": m.get("conditionId"),
                "question": m.get("question"),
                "outcome": m.get("groupItemTitle") or "",
                "yes_mid": yes_mid,
                "bid": bid,
                "ask": ask,
                "spread": spread,
                "liquidity": _f(m.get("liquidityNum"), 0.0),
                "vol24h": _f(m.get("volume24hr"), 0.0),
                "volume": _f(m.get("volumeNum"), 0.0),
                # 體育市場開賽後就是 in-play，做市會被看比賽的人打；以 gameStartTime 當截止
                "days_left": days_left((m.get("gameStartTime") or "").replace(" ", "T").replace("+00", "Z") or m.get("endDate")),
                "end_date": (m.get("gameStartTime") or m.get("endDate") or "")[:16],
                "fee_type": m.get("feeType") or "",
                "fee_rate": _f((m.get("feeSchedule") or {}).get("rate"), 0.0)
                            if m.get("feesEnabled") else 0.0,
                "rewards_min_size": _f(m.get("rewardsMinSize"), 0.0),
                "rewards_max_spread": _f(m.get("rewardsMaxSpread"), 0.0),
                "tick": _f(m.get("orderPriceMinTickSize"), 0.01),
                "yes_token": tokens[0] if tokens else None,
                "no_token": tokens[1] if len(tokens) > 1 else None,
                "url": f"https://polymarket.com/event/{ev.get('slug')}",
                "_m": m,
            })
    return rows


# ---------------------------------------------------------------- scan A: negRisk 加總

def scan_neg_risk(rows, threshold):
    """互斥多選項事件：所有 YES 買齊必得 $1。
    buy_cost  = Σ bestAsk (+taker fee)  < 1  → 買齊所有 YES 套利
    sell_proc = Σ bestBid (-taker fee)  > 1  → 賣齊所有 YES (=買齊所有 NO) 套利
    """
    by_event = {}
    for r in rows:
        if r["neg_risk"]:
            by_event.setdefault(r["event_id"], []).append(r)

    results = []
    for eid, ms in by_event.items():
        if len(ms) < 2:
            continue
        buy_cost = sell_proc = mid_sum = 0.0
        buy_fee = sell_fee = 0.0
        # 有選項沒開放交易 (例如已被提前結算或暫停)，剩下的加總一定偏低，不是套利
        incomplete = ms[0]["event_untradeable"] > 0
        if all(r["bid"] is None and r["ask"] is None and r["volume"] == 0 for r in ms):
            continue  # 整個事件沒人報價過
        for r in ms:
            mid = r["yes_mid"] if r["yes_mid"] is not None else 0.0
            ask = r["ask"] if r["ask"] is not None else mid
            bid = r["bid"] if r["bid"] is not None else mid
            if r["ask"] is None or r["bid"] is None:
                incomplete = True
            buy_cost += ask
            sell_proc += bid
            mid_sum += mid
            buy_fee += taker_fee_per_share(r["_m"], ask)
            sell_fee += taker_fee_per_share(r["_m"], bid)
        buy_edge = 1.0 - (buy_cost + buy_fee)
        sell_edge = (sell_proc - sell_fee) - 1.0
        best = max(buy_edge, sell_edge)
        ev = ms[0]
        results.append({
            "event_id": eid,
            "event_title": ev["event_title"],
            "n_outcomes": len(ms),
            "augmented": ev["neg_risk_augmented"],
            "untradeable_outcomes": ev["event_untradeable"],
            "incomplete_book": incomplete,
            "mid_sum": round(mid_sum, 4),
            "buy_all_yes_cost": round(buy_cost, 4),
            "buy_fee": round(buy_fee, 4),
            "buy_edge": round(buy_edge, 4),
            "sell_all_yes_proceeds": round(sell_proc, 4),
            "sell_fee": round(sell_fee, 4),
            "sell_edge": round(sell_edge, 4),
            "best_edge": round(best, 4),
            "min_liquidity": round(min(r["liquidity"] for r in ms), 0),
            "vol24h": round(sum(r["vol24h"] for r in ms), 0),
            "days_left": round(min((r["days_left"] or 0) for r in ms), 1),
            "url": ev["url"],
        })
    results.sort(key=lambda x: -x["best_edge"])
    # 沒有完整雙邊報價的加總是用預設 0.5 中價湊的，全是假訊號；augmented 選項可能不完整
    flagged = [x for x in results
               if x["best_edge"] >= threshold and not x["incomplete_book"]
               and not x["augmented"] and x["days_left"] > 0]
    return results, flagged


# ---------------------------------------------------------------- scan B: 做市候選

def scan_market_making(rows, a):
    """--mode thin (預設)：寬 spread、薄流動性，靠 spread 賺。
    --mode volume：高成交量、有散戶噪音的市場，掛在最佳價排隊靠量賺 (bot 用 quote_mode=join)。"""
    volume_mode = a.mode == "volume"
    cands = []
    for r in rows:
        if r["bid"] is None or r["ask"] is None or r["spread"] is None:
            continue
        mid = (r["bid"] + r["ask"]) / 2
        if not (a.mm_min_price <= mid <= a.mm_max_price):
            continue
        if volume_mode:
            if r["vol24h"] < a.vol_min_vol24h or r["spread"] < r["tick"]:
                continue
        else:
            if r["spread"] < a.mm_min_spread:
                continue
            if r["vol24h"] < a.mm_min_vol24h:
                continue
            if not (a.mm_min_liq <= r["liquidity"] <= a.mm_max_liq):
                continue
        if r["days_left"] is None or not (a.mm_min_days <= r["days_left"] <= a.mm_max_days):
            continue
        if not a.no_exclude and (set(r["tags"]) & EXCLUDE_TAG_SLUGS):
            continue
        # 流動性獎勵：掛單在 mid ± rewards_max_spread (cents) 內且 size >= min 可領獎勵
        rewards = r["rewards_max_spread"] > 0
        if volume_mode:
            # 量 × spread = 每天在最佳價排隊理論上能分到的 spread 收入上限
            score = r["vol24h"] * r["spread"] / 100
        else:
            # 粗略評分：spread 越寬、成交越多、越薄越好
            score = r["spread"] * math.sqrt(r["vol24h"]) / math.log10(r["liquidity"] + 10)
        cands.append({
            "score": round(score, 3),
            "spread_c": round(r["spread"] * 100, 1),
            "mid": round(mid, 3),
            "bid": r["bid"],
            "ask": r["ask"],
            "vol24h": round(r["vol24h"], 0),
            "liquidity": round(r["liquidity"], 0),
            "days_left": round(r["days_left"], 1),
            "fee_type": r["fee_type"].replace("_fees", ""),
            "fee_rate": r["fee_rate"],
            "rewards": f"±{r['rewards_max_spread']}c/min{int(r['rewards_min_size'])}" if rewards else "",
            "neg_risk": r["neg_risk"],
            "event": r["event_title"],
            "question": r["question"],
            "outcome": r["outcome"],
            "tags": ",".join(r["tags"][:4]),
            "url": r["url"],
            "market_id": r["market_id"],
            "condition_id": r["condition_id"],
            "end_date": r["end_date"],
            "yes_token": r["yes_token"],
            "no_token": r["no_token"],
        })
    cands.sort(key=lambda x: -x["score"])
    return cands


def verify_neg_risk_with_books(flagged, rows_by_event):
    """對標記的 negRisk 事件抓即時 CLOB，重算 Σ ask，並算最小掛單量 = 能買幾套。"""
    for x in flagged:
        ms = rows_by_event[x["event_id"]]
        tot = fee = 0.0
        cap = float("inf")
        try:
            for r in ms:
                b = fetch_book(r["yes_token"])
                asks = sorted(b.get("asks") or [], key=lambda a: float(a["price"]))
                if not asks:
                    cap = 0
                    tot = 9
                    break
                p, sz = float(asks[0]["price"]), float(asks[0]["size"])
                tot += p
                fee += taker_fee_per_share(r["_m"], p)
                cap = min(cap, sz)
                time.sleep(0.1)
            edge = 1 - tot - fee
            x["live_buy_edge"] = round(edge, 4)
            x["live_sets"] = int(cap) if cap != float("inf") else 0
            x["live_profit_usd"] = round(max(edge, 0) * x["live_sets"], 2)
        except Exception as e:  # noqa: BLE001
            x["live_buy_edge"] = f"err {str(e)[:20]}"
    return flagged


def enrich_with_books(cands, n):
    """對前 n 個候選抓 CLOB 訂單簿，看最佳價位的深度。"""
    for c in cands[:n]:
        try:
            yb = fetch_book(c["yes_token"])
            bids = sorted(yb.get("bids") or [], key=lambda x: -float(x["price"]))
            asks = sorted(yb.get("asks") or [], key=lambda x: float(x["price"]))
            c["book_bid"] = f"{bids[0]['price']}x{float(bids[0]['size']):.0f}" if bids else "-"
            c["book_ask"] = f"{asks[0]['price']}x{float(asks[0]['size']):.0f}" if asks else "-"
            c["book_levels"] = f"{len(bids)}/{len(asks)}"
            time.sleep(0.15)
        except Exception as e:  # noqa: BLE001
            c["book_bid"] = c["book_ask"] = "err"
            c["book_levels"] = str(e)[:30]
    return cands


# ---------------------------------------------------------------- output

def print_table(rows, cols, title, limit):
    print(f"\n=== {title} ({len(rows)}) ===")
    if not rows:
        print("  (none)")
        return
    rows = rows[:limit]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    widths = {c: min(w, 60) for c, w in widths.items()}
    line = "  ".join(c.ljust(widths[c]) for c in cols)
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(r.get(c, ""))[:widths[c]].ljust(widths[c]) for c in cols))


def write_csv(path, rows):
    if not rows:
        return
    keys = [k for k in rows[0].keys() if not k.startswith("_")]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from-cache", action="store_true", help="用 out/raw_events.json 不重抓")
    p.add_argument("--max-pages", type=int, default=200)
    p.add_argument("--books", type=int, default=0, help="對前 N 個做市候選抓 CLOB 深度")
    p.add_argument("--top", type=int, default=30, help="每張表顯示幾列")
    p.add_argument("--arb-threshold", type=float, default=0.01,
                   help="negRisk 加總偏離多少才標記 (扣費後)，預設 1 cent")
    p.add_argument("--mode", choices=["thin", "volume"], default="thin", help="thin=薄市場寬 spread；volume=厚市場排隊")
    p.add_argument("--vol-min-vol24h", type=float, default=50000, help="volume 模式：24h 最少成交")
    p.add_argument("--mm-min-spread", type=float, default=0.03, help="做市最小 spread，預設 3 cents")
    p.add_argument("--mm-min-vol24h", type=float, default=1000)
    p.add_argument("--mm-min-liq", type=float, default=500)
    p.add_argument("--mm-max-liq", type=float, default=50000)
    p.add_argument("--mm-min-days", type=float, default=2)
    p.add_argument("--mm-max-days", type=float, default=30, help="離結算超過幾天就不做 (單邊成交會鎖資金)")
    p.add_argument("--mm-min-price", type=float, default=0.08)
    p.add_argument("--mm-max-price", type=float, default=0.92)
    p.add_argument("--no-exclude", action="store_true", help="不套用標籤黑名單")
    a = p.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    if a.from_cache and os.path.exists(RAW_CACHE):
        with open(RAW_CACHE, encoding="utf-8") as f:
            events = json.load(f)
        print(f"loaded {len(events)} events from cache", file=sys.stderr)
    else:
        print("fetching events...", file=sys.stderr)
        events = fetch_all_events(a.max_pages)
        with open(RAW_CACHE, "w", encoding="utf-8") as f:
            json.dump(events, f)

    rows = flatten_markets(events)
    print(f"{len(events)} events, {len(rows)} active markets", file=sys.stderr)

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    run_dir = os.path.join(OUT_DIR, stamp)
    os.makedirs(run_dir, exist_ok=True)

    # A. negRisk 加總
    nr_all, nr_flagged = scan_neg_risk(rows, a.arb_threshold)
    nr_cols = ["best_edge", "buy_edge", "sell_edge", "mid_sum", "n_outcomes",
               "min_liquidity", "vol24h", "days_left"]
    if a.books and nr_flagged:
        print(f"verifying {len(nr_flagged)} negRisk events on CLOB...", file=sys.stderr)
        by_ev = {}
        for r in rows:
            by_ev.setdefault(r["event_id"], []).append(r)
        verify_neg_risk_with_books(nr_flagged[:a.books], by_ev)
        nr_cols += ["live_buy_edge", "live_sets", "live_profit_usd"]
    write_csv(os.path.join(run_dir, "negrisk_all.csv"), nr_all)
    write_csv(os.path.join(run_dir, "negrisk_flagged.csv"), nr_flagged)
    print_table(nr_flagged, nr_cols + ["event_title"],
                f"A. negRisk 機率加總異常 (扣 taker fee 後 edge >= {a.arb_threshold})", a.top)

    # B. 做市候選
    mm = scan_market_making(rows, a)
    if a.books:
        print(f"fetching {min(a.books, len(mm))} order books...", file=sys.stderr)
        enrich_with_books(mm, a.books)
    write_csv(os.path.join(run_dir, "mm_candidates.csv"), mm)
    cols = ["score", "spread_c", "mid", "vol24h", "liquidity", "days_left", "fee_type",
            "rewards"]
    if a.books:
        cols += ["book_bid", "book_ask", "book_levels"]
    cols += ["event", "outcome"]
    print_table(mm, cols, "B. 做市候選 " + ("(高成交量 / 排隊模式)" if a.mode == "volume" else "(寬 spread / 有成交 / 薄流動性 / 非新聞驅動)"), a.top)

    # 摘要
    print(f"\n輸出目錄: {run_dir}")
    print(f"negRisk 事件 {len(nr_all)} 個，標記 {len(nr_flagged)} 個；做市候選 {len(mm)} 個")
    print("提醒：Gamma 的 bestBid/bestAsk 有延遲，標記的套利要用 --books 或 CLOB 即時確認再說。")


if __name__ == "__main__":
    main()
