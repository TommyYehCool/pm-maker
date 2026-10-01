"""高機率掃尾盤：某一邊價格首次 >= thr (且 < cap) 時買入，持有到結算 (closedTime)。
每小時成交價；買價 = p + slip。同一事件只算第一個觸發，避免相關樣本灌水。"""
import json, os, sys, collections, math
from bt_util import ts, cat
f = sys.argv[1]; VMIN = float(sys.argv[2]) if len(sys.argv) > 2 else 0
rows = [json.loads(l) for l in open(f)]
rows = [r for r in rows if (r.get("vol") or 0) >= VMIN and os.path.exists(f"prices/{r['cid']}.json")]
def run(thr, cap, slip, catsel=None):
    out = []; seen = set()
    for r in rows:
        c = cat(r)
        if catsel and c not in catsel: continue
        ev = r["ev"] or r["cid"]
        if ev in seen: continue
        h = json.load(open(f"prices/{r['cid']}.json"))
        T = ts(r["closed"])
        if not T or len(h) < 2: continue
        for t, p in h:
            if t >= T - 1800: break
            for side, q in (("yes", p), ("no", 1 - p)):
                if thr <= q < cap:
                    won = r["yes_won"] if side == "yes" else not r["yes_won"]
                    buy = min(q + slip, 0.999)
                    out.append((c, (1 / buy - 1) if won else -1.0, (T - t) / 86400, won, q)); seen.add(ev)
                    break
            else:
                continue
            break
    return out
print(f"{'cat':14s} thr   n   loss%  avg_ret  med_hold_d  ret/day   (slip {SLIP if (SLIP:=0.005) else 0})")
for catsel in (None, ["sports"], ["other"], ["crypto_price"], ["stocks"], ["crypto_updown"]):
    for thr in (0.90, 0.95, 0.97):
        o = run(thr, 0.995, 0.005, catsel)
        if len(o) < 20: continue
        n = len(o); loss = sum(1 for x in o if not x[3]) / n; ar = sum(x[1] for x in o) / n
        holds = sorted(x[2] for x in o); md = holds[n // 2]
        rpd = sum(x[1] for x in o) / sum(max(x[2], 1/24) for x in o)
        se = (sum((x[1] - ar) ** 2 for x in o) / (n - 1)) ** .5 / n ** .5
        print(f"{str(catsel and catsel[0]):14s} {thr:.2f} {n:5d}  {loss*100:5.2f}  {ar*100:+6.2f}%±{se*100:.2f}  {md:7.2f}   {rpd*100:+.3f}%")
