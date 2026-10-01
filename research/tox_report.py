import json, math, collections, sys
sys.path.insert(0, ".")
from bt_util import cat
res = json.load(open("markets_tox.json"))
def c(r): return cat({"q": r["q"], "ev": r["ev"], "sports": None, "game": None})
print("markets", len(res), "with trades", sum(1 for r in res if r["rows"]))
# 整體：maker 每股損益 (分)，按 d 桶，只取 d 在 [0, v] 的交易
for H in ("3600", "21600", "86400"):
    B = collections.defaultdict(lambda: [0.0, 0.0, 0])
    for r in res:
        for x in r["rows"]:
            if x.get(H) is None or not (-0.001 <= x["d"] <= r["v"]): continue
            k = min(int(x["d"] * 100), 5)
            B[k][0] += x[H] * x["sh"]; B[k][1] += x["sh"]; B[k][2] += 1
    print(f"\nhorizon {int(H)//3600}h  maker P&L per share (cents) by taker premium d")
    for k in sorted(B):
        s, w, n = B[k]
        print(f"  d {k}-{k+1}c   trades {n:7d}  shares {w:12.0f}   maker {100*s/w:+.2f}c/share")
# 分類別
print("\nby category, 24h, d in [0,v]:")
C = collections.defaultdict(lambda: [0.0, 0.0, 0, 0])
for r in res:
    k = c(r)
    C[k][3] += 1
    for x in r["rows"]:
        if x.get("86400") is None or not (-0.001 <= x["d"] <= r["v"]): continue
        C[k][0] += x["86400"] * x["sh"]; C[k][1] += x["sh"]; C[k][2] += 1
for k, (s, w, n, m) in sorted(C.items(), key=lambda kv: -kv[1][1]):
    if w: print(f"  {k:14s} markets {m:5d} trades {n:7d}  maker {100*s/w:+.2f}c/share")
