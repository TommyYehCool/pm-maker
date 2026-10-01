"""每個市場：maker 在獎勵區被打的每日總損失 (24h markout，只算有 24h 資料的交易，按筆數比例放大到 7 天) vs 每日獎勵池。"""
import json, math, sys
sys.path.insert(0, ".")
from bt_util import cat
res = json.load(open("markets_tox.json")); DAYS = 7
out = []
for r in res:
    z = [x for x in r["rows"] if -0.001 <= x["d"] <= r["v"]]
    have = [x for x in z if x.get("86400") is not None]
    if have:
        pnl = sum(x["86400"] * x["sh"] for x in have) * len(z) / len(have)
        sh = sum(x["sh"] for x in z)
        per = [x["86400"] for x in have]
        m = sum(per) / len(per); sd = (sum((p - m) ** 2 for p in per) / max(len(per) - 1, 1)) ** .5
    else:
        pnl = sh = 0.0; sd = 0
    loss_day = -pnl / DAYS
    usd_day = sum(x["sz"] for x in z) / DAYS
    out.append(dict(q=r["q"], cid=r["cid"], rate=r["rate"], mid=r["mid"], v=r["v"], ms=r["min_size"], n=len(z), loss_day=loss_day,
                    net=r["rate"] - loss_day, usd_day=usd_day, c=cat({"q": r["q"], "ev": r["ev"], "sports": None, "game": None}), end=r["end"], neg=r["neg"], ev=r["ev"]))
json.dump(out, open("tox_rank.json", "w"))
tot_rate = sum(o["rate"] for o in out); tot_loss = sum(o["loss_day"] for o in out)
print(f"all reward markets: pools ${tot_rate:,.0f}/day, maker markout loss in reward zone ${tot_loss:,.0f}/day  => makers net ${tot_rate - tot_loss:,.0f}/day")
pos = [o for o in out if o["net"] > 0]
print(f"markets where pool > maker loss: {len(pos)}/{len(out)}; pool-weighted")
for lo, hi in ((0, 1), (1, 10), (10, 50), (50, 1e9)):
    g = [o for o in out if lo <= o["n"] < hi]
    if g: print(f"  trades/wk {lo:>3}-{hi:<4}: markets {len(g):5d}  pool ${sum(o['rate'] for o in g):8,.0f}/d  loss ${sum(o['loss_day'] for o in g):8,.0f}/d  net ${sum(o['net'] for o in g):8,.0f}/d")
print("\nby category:")
import collections
C = collections.defaultdict(lambda: [0, 0.0, 0.0])
for o in out:
    C[o["c"]][0] += 1; C[o["c"]][1] += o["rate"]; C[o["c"]][2] += o["loss_day"]
for k, (n, r, l) in sorted(C.items(), key=lambda kv: -kv[1][1]):
    print(f"  {k:14s} markets {n:5d} pool ${r:8,.0f}/d  loss ${l:8,.0f}/d  ratio loss/pool {l / r:5.2f}")
