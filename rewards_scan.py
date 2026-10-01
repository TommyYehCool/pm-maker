#!/usr/bin/env python3
"""流動性獎勵估算：對高獎勵市場抓訂單簿，算現有 maker 的 Q 分數，估我們投 C 元能分到多少/天。
公式 (docs): S = ((v - s)/v)^2 * size，v = max spread，s = 離中價距離；中價在 0.1~0.9 外要雙邊。
用法: python rewards_scan.py --capital 500 --top 40
"""
import argparse
import json
import time

import requests
from polymarket import PublicClient

GAMMA = "https://gamma-api.polymarket.com"


def q_score(levels, mid, v, min_size, side):
    """levels: [(price, size)]，side='bid' 或 'ask'。回傳 Q。"""
    q = 0.0
    for p, sz in levels:
        if sz < min_size:
            continue
        s = (mid - p) if side == "bid" else (p - mid)
        if s < 0 or s > v:
            continue
        q += ((v - s) / v) ** 2 * sz
    return q


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--capital", type=float, default=500)
    a.add_argument("--top", type=int, default=40, help="看獎勵最高的前 N 個市場")
    a.add_argument("--min-rate", type=float, default=50)
    args = a.parse_args()

    pc = PublicClient()
    rewards = [r for page in pc.list_current_rewards() for r in page.items if float(r.total_daily_rate or 0) >= args.min_rate]
    rewards.sort(key=lambda r: -float(r.total_daily_rate))
    print(f"markets with >= ${args.min_rate}/day: {len(rewards)}; scanning top {args.top}\n")
    out = []
    for r in rewards[: args.top]:
        try:
            m = requests.get(f"{GAMMA}/markets", params={"condition_ids": r.condition_id}, timeout=20).json()[0]
            toks = json.loads(m["clobTokenIds"])
            yb = pc.get_order_book(token_id=toks[0])
            nb = pc.get_order_book(token_id=toks[1])
            tick = float(yb.tick_size)
            bids = [(float(x.price), float(x.size)) for x in yb.bids]
            asks = [(float(x.price), float(x.size)) for x in yb.asks]
            if not bids or not asks:
                continue
            mid = (max(p for p, _ in bids) + min(p for p, _ in asks)) / 2
            v = float(r.rewards_max_spread) / 100
            ms = float(r.rewards_min_size)
            # NO 簿換算成 YES 價：NO bid @p = YES ask @1-p；NO ask @p = YES bid @1-p
            n_bids = [(1 - float(x.price), float(x.size)) for x in nb.bids]   # -> YES asks
            n_asks = [(1 - float(x.price), float(x.size)) for x in nb.asks]   # -> YES bids
            q1 = q_score(bids, mid, v, ms, "bid") + q_score(n_asks, mid, v, ms, "bid")
            q2 = q_score(asks, mid, v, ms, "ask") + q_score(n_bids, mid, v, ms, "ask")
            extreme = mid < 0.10 or mid > 0.90
            q_min = min(q1, q2) if extreme else max(min(q1, q2), max(q1, q2) / 3)
            # 我們：資金對半，各掛在中價 ± 1 tick，score ≈ ((v-tick)/v)^2 * shares
            half = args.capital / 2
            our_bid_sh = half / max(mid - tick, tick)
            our_ask_sh = half / max(1 - (mid + tick), tick)     # 賣 YES = 買 NO @ 1-p
            f = ((v - tick) / v) ** 2
            ours = min(our_bid_sh, our_ask_sh) * f
            share = ours / (q_min + ours) if (q_min + ours) > 0 else 0
            daily = share * float(r.total_daily_rate)
            out.append((daily, share, float(r.total_daily_rate), mid, v * 100, ms, q_min, float(m.get("volume24hr") or 0),
                        m["question"][:60], (m.get("endDate") or "")[:10]))
            time.sleep(0.15)
        except Exception as e:  # noqa: BLE001
            print("  skip", r.condition_id[-8:], str(e)[:60])
    out.sort(key=lambda x: -x[0])
    print(f"{'$/day':>6} {'share':>6} {'pool':>6} {'mid':>5} {'±c':>4} {'min':>4} {'Q existing':>10} {'vol24h':>9}  ends       question")
    for d, sh, pool, mid, v, ms, q, vol, qn, end in out[:25]:
        print(f"{d:6.2f} {sh*100:5.1f}% {pool:6.0f} {mid:5.3f} {v:4.1f} {ms:4.0f} {q:10.0f} {vol:9.0f}  {end}  {qn}")
    print(f"\ncapital ${args.capital:.0f}: best single market ${out[0][0]:.2f}/day = {out[0][0]/args.capital*100:.2f}%/day" if out else "")


if __name__ == "__main__":
    main()
