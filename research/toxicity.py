"""有獎勵的市場：maker 被成交後的 markout (毒性)。
對每筆吃單交易 (data-api trades，side = taker 方向)：換成 YES 價 y 與 taker 方向 dir (+1 買 YES)。
ref_t = 每 5 分鐘價格序列 (prices-history)。maker 賺 d = dir*(y-ref_t) (taker 付的溢價)，
之後賠 dir*(ref_{t+T} - ref_t)。maker 每股損益 = d - dir*(ref_{t+T}-ref_t)。
只看 d 在 [0, v] (有獎勵的掛價範圍) 的交易，並分 |d| 桶。
輸出 markets_tox.json 供後續合併獎勵估算。"""
import bisect, json, sys, time, requests
from concurrent.futures import ThreadPoolExecutor
S = requests.Session()
NOW = time.time(); DAYS = float(sys.argv[2]) if len(sys.argv) > 2 else 7
MINRATE = float(sys.argv[1]) if len(sys.argv) > 1 else 10

def rewards():
    out, cur = [], ""
    while cur != "LTE=":
        j = S.get("https://clob.polymarket.com/rewards/markets/current", params={"next_cursor": cur}, timeout=30).json()
        out += j["data"]; cur = j.get("next_cursor") or "LTE="
    return out

def trades(cid):
    out, off = [], 0
    while off < 10000:
        r = S.get("https://data-api.polymarket.com/trades", params={"market": cid, "limit": 500, "offset": off}, timeout=30)
        if r.status_code != 200: break
        d = r.json()
        if not d: break
        out += d; off += len(d)
        if d[-1]["timestamp"] < NOW - DAYS * 86400: break
    return [t for t in out if t["timestamp"] >= NOW - DAYS * 86400]

def series(tok):
    h = S.get("https://clob.polymarket.com/prices-history", params={"market": tok, "startTs": int(NOW - (DAYS + 2) * 86400), "fidelity": 5}, timeout=30).json().get("history") or []
    return [x["t"] for x in h], [x["p"] for x in h]

def at(ts_, ps, t):
    i = bisect.bisect_right(ts_, t) - 1
    return ps[i] if i >= 0 else None

def one(rw):
    cid = rw["condition_id"]
    try:
        m = S.get("https://gamma-api.polymarket.com/markets", params={"condition_ids": cid}, timeout=20).json()
        if not m: return None
        m = m[0]; tok = json.loads(m["clobTokenIds"])[0]
        tt, pp = series(tok)
        if len(tt) < 20: return None
        mid = pp[-1]
        tr = trades(cid)
        v = rw["rewards_max_spread"] / 100
        rec = {"cid": cid, "q": m["question"][:70], "ev": (m.get("events") or [{}])[0].get("slug"), "rate": rw["total_daily_rate"],
               "v": v, "min_size": rw["rewards_min_size"], "mid": mid, "end": (m.get("endDate") or "")[:10], "n_trades": len(tr),
               "tok": tok, "tok_no": json.loads(m["clobTokenIds"])[1], "neg": m.get("negRisk")}
        rows = []
        for t in tr:
            y = t["price"] if t["outcomeIndex"] == 0 else 1 - t["price"]
            dr = 1 if (t["side"] == "BUY") == (t["outcomeIndex"] == 0) else -1
            r0 = at(tt, pp, t["timestamp"] - 1)
            if r0 is None: continue
            d = dr * (y - r0)
            res = {"d": d, "sz": t["size"] * y if dr > 0 else t["size"] * (1 - y), "sh": t["size"]}
            for H in (3600, 6 * 3600, 86400):
                if t["timestamp"] + H > NOW: res[H] = None; continue
                r1 = at(tt, pp, t["timestamp"] + H)
                res[H] = d - dr * (r1 - r0)
            rows.append(res)
        rec["rows"] = rows
        return rec
    except Exception as e:
        print("err", cid[:10], e); return None

if __name__ == "__main__":
    rw = [r for r in rewards() if r["total_daily_rate"] >= MINRATE]
    print("reward markets >= $%s/day:" % MINRATE, len(rw), flush=True)
    with ThreadPoolExecutor(8) as ex:
        res = [r for r in ex.map(one, rw) if r]
    json.dump(res, open("markets_tox.json", "w"))
    print("done", len(res))
