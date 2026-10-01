"""v2 選市場：獎勵池、7 天 maker 毒性、即時簿子上有沒有「可以躲」的價位。
輸出 queue_markets.json (rewards_config 的 market 格式，queue_mode=true)。
條件：
- 現在的獎勵池 >= MIN_RATE/天、中價 0.15~0.85、離結束 2~120 天、min_size <= 50
- 7 天毒性：maker 在獎勵區被打的 6h 損失 / 池子 < 0.2 (交易太少的算 0)
- 排除股票、加密幣價格類 (毒性比 0.3~0.5)，排除政治/戰爭字眼 (跳價風險，09-22 教訓)
- 兩邊都找得到前面有 3 倍 min_size 別人掛單、且在獎勵範圍內的價位
- 一個事件只挑一個市場"""
import json, re, sys, requests
from datetime import datetime, timezone
sys.path.insert(0, ".."); sys.path.insert(0, ".")
from risk import queue_price
from bt_util import cat
S = requests.Session()
MIN_RATE = float(sys.argv[1]) if len(sys.argv) > 1 else 15
N = int(sys.argv[2]) if len(sys.argv) > 2 else 10
BUDGET = float(sys.argv[3]) if len(sys.argv) > 3 else 130
AX = 3.0
BAD = re.compile(r"iran|israel|gaza|ceasefire|\bwar\b|russia|ukrain|trump|putin|xi jinping|nuclear|strike|military|invade|"
                 r"election|president|prime minister|parliament|senate|house seats|governor|mayor|pope|resign|indict|arrest|"
                 r"blockade|tariff|sanction|mrbeast|video|views", re.I)
now = datetime.now(timezone.utc)

cur, rw = "", {}
while cur != "LTE=":
    j = S.get("https://clob.polymarket.com/rewards/markets/current", params={"next_cursor": cur}, timeout=30).json()
    for r in j["data"]:
        rw[r["condition_id"]] = r
    cur = j.get("next_cursor") or "LTE="
tox = {r["cid"]: r for r in json.load(open("markets_tox.json"))}

def tox_ratio(r, rate):
    z = [x for x in r["rows"] if -0.001 <= x["d"] <= r["v"]]
    have = [x for x in z if x.get("21600") is not None]
    if not have:
        return 0.0, len(z)
    loss = -sum(x["21600"] * x["sh"] for x in have) * len(z) / len(have) / 7
    return loss / rate, len(z)

cands = []
for cid, r in tox.items():
    w = rw.get(cid)
    if not w or w["total_daily_rate"] < MIN_RATE or w["rewards_min_size"] > 50:
        continue
    if BAD.search(r["q"]) or cat({"q": r["q"], "ev": r["ev"], "sports": None, "game": None}) in ("stocks", "crypto_price", "crypto_updown", "weather"):
        continue
    ratio, ntr = tox_ratio(r, w["total_daily_rate"])
    if ratio >= 0.2:
        continue
    cands.append((r, w, ratio, ntr))
print("after pool/category/toxicity filters:", len(cands), flush=True)

toks = [t for r, *_ in cands for t in (r["tok"], r["tok_no"])]
B = {}
for i in range(0, len(toks), 200):
    for b in S.post("https://clob.polymarket.com/books", json=[{"token_id": t} for t in toks[i:i + 200]], timeout=30).json():
        B[b["asset_id"]] = b

def lv(xs):
    return [(float(x["price"]), float(x["size"])) for x in xs]

out = []
for r, w, ratio, ntr in cands:
    yb, nb = B.get(r["tok"]), B.get(r["tok_no"])
    if not yb or not nb:
        continue
    g = S.get("https://gamma-api.polymarket.com/markets", params={"condition_ids": r["cid"]}, timeout=20).json()
    if not g:
        continue
    g = g[0]
    end = g.get("endDate")
    if not end or not g.get("acceptingOrders"):
        continue
    days = (datetime.fromisoformat(end.replace("Z", "+00:00")) - now).total_seconds() / 86400
    if not (2 <= days <= 120):
        continue
    tick = float(yb.get("tick_size") or 0.01)
    bids = lv(yb["bids"]) + [(round(1 - p, 6), s) for p, s in lv(nb["asks"])]
    asks = lv(yb["asks"]) + [(round(1 - p, 6), s) for p, s in lv(nb["bids"])]
    if not bids or not asks:
        continue
    bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
    mid = (bb + ba) / 2
    if not (0.15 <= mid <= 0.85) or ba - bb > 0.15:
        continue
    v = w["rewards_max_spread"] / 100; ms = w["rewards_min_size"]
    no_levels = [(round(1 - p, 6), s) for p, s in asks]
    ypx, ya = queue_price(bids, mid, v, tick, ms, AX, round(ba - tick, 6))
    npx, na = queue_price(no_levels, 1 - mid, v, tick, ms, AX, round(1 - bb - tick, 6))
    if ypx is None or npx is None:
        continue
    def q(levels, m):
        return sum(((v - (m - p)) / v) ** 2 * s for p, s in levels if s >= ms and 0 <= m - p <= v)
    q1, q2 = q(bids, mid), q(no_levels, 1 - mid)
    o1, o2 = ((v - (mid - ypx)) / v) ** 2 * ms, ((v - (1 - mid - npx)) / v) ** 2 * ms
    Qf = lambda a, b: max(min(a, b), max(a, b) / 3)
    share = 1 - Qf(q1, q2) / Qf(q1 + o1, q2 + o2)
    est = share * w["total_daily_rate"]
    capital = ms * ypx + ms * npx
    out.append({"name": r["q"], "condition_id": r["cid"], "yes_token": r["tok"], "no_token": r["tok_no"], "market_id": str(g["id"]),
                "end_date": end, "days_left": round(days, 1), "pool": w["total_daily_rate"], "max_spread": v, "min_size": ms, "tick": tick,
                "size": ms, "offset_ticks": 1, "max_inventory": 2 * ms, "event_slug": r["ev"], "queue_mode": True, "enabled": True,
                "est_daily": round(est, 3), "est_share": round(share, 4), "capital_needed": round(capital, 2), "mid": round(mid, 3),
                "tox_ratio": round(ratio, 3), "tox_trades_7d": ntr, "queue_now": {"yes": [ypx, round(ya)], "no": [npx, round(na)]},
                "s_cents": [round(100 * (mid - ypx), 1), round(100 * (1 - mid - npx), 1)]})
out.sort(key=lambda o: -o["est_daily"] / max(o["capital_needed"], 1))
print(f"protected on both sides now: {len(out)}")
picked, seen, spent = [], set(), 0.0
for o in out:
    if o["event_slug"] in seen or spent + o["capital_needed"] > BUDGET:
        continue
    picked.append(o); seen.add(o["event_slug"]); spent += o["capital_needed"]
    if len(picked) >= N:
        break
print(f"{'est/d':>6} {'share':>6} {'pool':>5} {'mid':>5} {'s(c)':>10} {'ahead':>11} {'tox':>5} {'tr7d':>5} {'cap$':>6} {'days':>5}  question")
for o in picked:
    print(f"{o['est_daily']:6.2f} {o['est_share']*100:5.1f}% {o['pool']:5.0f} {o['mid']:5.3f} {str(o['s_cents']):>10} "
          f"{str([o['queue_now']['yes'][1], o['queue_now']['no'][1]]):>11} {o['tox_ratio']:5.2f} {o['tox_trades_7d']:5d} {o['capital_needed']:6.2f} {o['days_left']:5.1f}  {o['name'][:55]}")
print(f"\n{len(picked)} markets, resting capital {spent:.2f}, est {sum(o['est_daily'] for o in picked):.2f}/day (模擬值，過去實盤高估 3-5 倍)")
json.dump(picked, open("queue_markets.json", "w"), ensure_ascii=False, indent=1)
