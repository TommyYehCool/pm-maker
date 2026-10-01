"""已結算市場的定價校準：在參考時間 R 之前 h 小時的價格 p，實際 YES 發生率是多少。
R：運動用開賽時間 (之後結果逐漸明朗)，其他用 min(closedTime, endDate)。
價格用 prices-history 的每小時成交價，不是可成交 ask，所以報酬另外扣 1 分的成本。
同一個事件的多個市場高度相關，另外算「事件數」看樣本到底有多少獨立。"""
import json, os, collections, math, sys
from datetime import datetime

def ts(s):
    if not s: return None
    s = s.replace(" ", "T").replace("Z", "+00:00")
    if s.endswith("+00"): s += ":00"
    try: return datetime.fromisoformat(s).timestamp()
    except Exception: return None

def cat(r):
    q, ev = r["q"].lower(), (r["ev"] or "").lower()
    if r["sports"] or r["game"]: return "sports"
    if "up or down" in q: return "crypto_updown"
    if any(k in ev for k in ("btc", "eth", "sol", "xrp", "bitcoin", "ethereum", "solana", "bnb", "doge", "hype")) or "price of" in q: return "crypto_price"
    if "temperature" in q or "highest-temp" in ev or "precipitation" in q: return "weather"
    if any(k in ev for k in ("tsla","nvda","aapl","googl","meta-","msft","amzn","spx","nflx","pltr","coin","hood","spcx","ndx")) or "close above" in q or "finish week" in q: return "stocks"
    if any(k in q for k in ("tweet", "post ", "posts")): return "tweets"
    if any(k in q for k in ("mention", "say ")): return "mentions"
    return "other"

rows = [json.loads(l) for l in open(sys.argv[1] if len(sys.argv) > 1 else "markets.jsonl")]
VMIN = float(sys.argv[2]) if len(sys.argv) > 2 else 0; VMAX = float(sys.argv[3]) if len(sys.argv) > 3 else 1e18
rows = [r for r in rows if VMIN <= (r.get("vol") or 0) < VMAX]
ONLY = sys.argv[4].split(",") if len(sys.argv) > 4 else None
H_SHOW = [int(x) for x in sys.argv[5].split(",")] if len(sys.argv) > 5 else (24,)
H = [1, 6, 24, 72, 168]
recs = []
for r in rows:
    f = f"prices/{r['cid']}.json"
    if not os.path.exists(f): continue
    h = json.load(open(f))
    if len(h) < 3: continue
    c = cat(r)
    R = ts(r["game"]) if c == "sports" and r.get("game") else min(x for x in (ts(r["closed"]), ts(r["end"])) if x)
    if not R: continue
    t0 = h[0][0]
    for hh in H:
        t = R - hh * 3600
        if t < t0: continue
        # 取 t 之前最後一個價格
        p = None
        for tt, pp in h:
            if tt <= t: p = pp
            else: break
        if p is None or p <= 0 or p >= 1: continue
        recs.append((c, hh, p, 1 if r["yes_won"] else 0, r["ev"] or r["cid"], r["cid"]))
print("markets with prices", len(set(x[5] for x in recs)), "records", len(recs))

B = [0, .02, .05, .10, .20, .35, .50, .65, .80, .90, .95, .98, 1.0]
def bucket(p):
    for i in range(len(B) - 1):
        if B[i] <= p < B[i + 1]: return i
    return len(B) - 2

def table(filt, title):
    g = collections.defaultdict(list)
    for x in recs:
        if filt(x): g[bucket(x[2])].append(x)
    print(f"\n## {title}")
    print("  price bucket     n   events  avg_p  hit%   edge(pts)  z     buyYES ret/$  buyNO ret/$  (after 1c cost)")
    for i in sorted(g):
        xs = g[i]; n = len(xs); ev = len(set(x[4] for x in xs))
        ap = sum(x[2] for x in xs) / n; hit = sum(x[3] for x in xs) / n
        se = math.sqrt(max(ap * (1 - ap), 1e-4) / max(ev, 1))   # 以事件數當有效樣本，保守
        z = (hit - ap) / se
        ry = sum(x[3] / min(x[2] + .01, .999) - 1 for x in xs) / n
        rn = sum((1 - x[3]) / min(1 - x[2] + .01, .999) - 1 for x in xs) / n
        flag = " <==" if abs(z) > 2.5 and ev >= 30 else ""
        print(f"  {B[i]:.2f}-{B[i+1]:.2f}  {n:6d} {ev:6d}  {ap:.3f}  {hit*100:5.1f}  {100*(hit-ap):+6.2f}   {z:+5.1f}   {ry:+7.3f}      {rn:+7.3f}{flag}")

for hh in (24, 168):
    table(lambda x, hh=hh: x[1] == hh, f"ALL, {hh}h before R")
cats = ONLY or sorted(set(x[0] for x in recs))
for c in cats:
    for hh in (H_SHOW if 'H_SHOW' in dir() else (24,)):
        table(lambda x, c=c, hh=hh: x[0] == c and x[1] == hh, f"{c}, {hh}h before R")
