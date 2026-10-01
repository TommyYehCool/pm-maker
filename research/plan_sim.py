"""每個有獎勵市場，用即時簿子模擬「躲在別人後面掛」：
- 兩邊各掛 cap/2 美金 (YES bid、NO bid)，價格選在「前面已有 >= AHEAD_X 倍我們股數的別人掛單」的最高價，且離中價 <= v。
- 獎勵：S = ((v-s)/v)^2 * size，Q = max(min(Q1,Q2), max/3) (中價 0.1~0.9)，我們分 ours/(existing+ours)。
- 被打成本：過去 7 天吃單中溢價 d >= s 的交易量 × 我們在該價位以上排隊量的占比 × 這些交易的 6h maker 每股損益。
結果是粗估：簿子會變、別人會撤單，排隊位置也不是固定的。"""
import json, sys, requests, math
sys.path.insert(0, ".")
from bt_util import cat
S = requests.Session()
CAP = float(sys.argv[1]) if len(sys.argv) > 1 else 50
AHEAD_X = float(sys.argv[2]) if len(sys.argv) > 2 else 2
import time
from datetime import datetime, timezone
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
res = [r for r in json.load(open("markets_tox.json")) if r["rate"] >= 15 and 0.12 <= r["mid"] <= 0.88 and r["end"] > TODAY]
toks = [t for r in res for t in (r["tok"], r["tok_no"])]
B = {}
for i in range(0, len(toks), 200):
    for b in S.post("https://clob.polymarket.com/books", json=[{"token_id": t} for t in toks[i:i + 200]], timeout=30).json():
        B[b["asset_id"]] = ([(float(x["price"]), float(x["size"])) for x in b["bids"]], [(float(x["price"]), float(x["size"])) for x in b["asks"]], float(b.get("tick_size") or 0.01))
def qside(levels, mid, v, ms):
    return sum(((v - (mid - p)) / v) ** 2 * s for p, s in levels if s >= ms and 0 <= mid - p <= v)
out = []
for r in res:
    if r["tok"] not in B or r["tok_no"] not in B: continue
    yb, ya, tick = B[r["tok"]]; nb, na, _ = B[r["tok_no"]]
    if not yb or not ya: continue
    bb, ba = max(p for p, _ in yb), min(p for p, _ in ya)
    mid = (bb + ba) / 2; v = r["v"]; ms = r["min_size"]
    if ba - bb > v: continue
    # YES 買方 = YES bids + (NO asks 換算)；NO 買方 = NO bids + (YES asks 換算)，這裡各自只看同一本 bids (合併簿其實是鏡像)
    sides = []
    # 買 YES 的分數 = YES bids + NO asks (換算成 YES bid 價 1-p)；買 NO 同理
    yes_buy = yb + [(round(1 - p, 4), s) for p, s in na]
    no_buy = nb + [(round(1 - p, 4), s) for p, s in ya]
    for levels, m in ((yes_buy, mid), (no_buy, 1 - mid)):
        sh = (CAP / 2) / max(m - 0.01, 0.02)
        sh = max(sh, ms)
        # 由高到低找第一個價位：比它好的別人量 >= AHEAD_X*sh
        ahead = 0.0; px = None
        for p in sorted(set(p for p, _ in levels), reverse=True):
            better = sum(s for q, s in levels if q > p + 1e-9)
            if better >= AHEAD_X * sh and m - p <= v - tick:
                px = p; ahead = better; break
        if px is None:
            # 前面擋的不夠：退到 v 邊緣前一格
            px = round(m - v + tick, 3); ahead = sum(s for q, s in levels if q > px + 1e-9)
        s_ = m - px
        if s_ < 0 or s_ > v: sides = None; break
        q_exist = qside(levels, m, v, ms)
        ours = ((v - s_) / v) ** 2 * sh
        at_or_better = sum(s for q, s in levels if q >= px - 1e-9)
        sides.append(dict(px=px, s=s_, sh=sh, ours=ours, q=q_exist, ahead=ahead, queue=at_or_better))
    if not sides: continue
    Q1e, Q2e = sides[0]["q"], sides[1]["q"]; o1, o2 = sides[0]["ours"], sides[1]["ours"]
    def Qf(a, b): return max(min(a, b), max(a, b) / 3)
    share = 1 - Qf(Q1e, Q2e) / max(Qf(Q1e + o1, Q2e + o2), 1e-9)
    reward = share * r["rate"]
    # 被打：YES 方向 taker 賣 YES (dir=-1) 打到我們的 YES bid；taker 買 YES 打到我們的 NO bid (賣 YES)
    cost = 0.0; fills = 0.0
    for x in r["rows"]:
        if x.get("21600") is None: continue
        dr = 1 if x["d"] >= 0 else -1
        # x["d"] 是 taker 付的溢價；taker 賣 YES → 打 YES bids；我們在 s 距離
        for sd in sides:
            if x["d"] >= sd["s"] - 1e-9:
                frac = sd["sh"] / (sd["queue"] + sd["sh"])
                f = min(x["sh"] * frac, sd["sh"])
                cost += -x["21600"] * f / 2; fills += f / 2   # 不知道打哪一邊，兩邊平均
    have = sum(1 for x in r["rows"] if x.get("21600") is not None) or 1
    scale = len(r["rows"]) / have / 7
    out.append(dict(q=r["q"], c=cat({"q": r["q"], "ev": r["ev"], "sports": None, "game": None}), rate=r["rate"], share=share, reward=reward,
                    cost=cost * scale, fills=fills * scale, net=reward - cost * scale, mid=mid, s=[round(sd["s"] * 100, 1) for sd in sides], end=r["end"], cid=r["cid"], ev=r["ev"]))
json.dump(out, open("plan_sim.json", "w"))
out.sort(key=lambda o: -o["net"])
print(f"cap ${CAP}/market, ahead x{AHEAD_X}: {len(out)} markets simulated")
print(f"{'net/d':>6} {'rew/d':>6} {'cost/d':>6} {'fill sh/d':>9} {'share':>6} {'pool':>5} {'s(c)':>10}  cat        end        question")
for o in out[:30]:
    print(f"{o['net']:6.2f} {o['reward']:6.2f} {o['cost']:6.2f} {o['fills']:9.1f} {o['share']*100:5.1f}% {o['rate']:5.0f} {str(o['s']):>10}  {o['c']:10s} {o['end']}  {o['q'][:55]}")
