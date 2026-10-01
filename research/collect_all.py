"""已結算市場清單：closedTime 由新到舊翻頁，存 markets.jsonl。"""
import json, requests, sys, time
G = "https://gamma-api.polymarket.com/markets/keyset"
stop = sys.argv[1] if len(sys.argv) > 1 else "2026-05-01"
vmin = float(sys.argv[2]) if len(sys.argv) > 2 else 20000
out = open("markets_all.jsonl", "w"); n = off = 0; cur = None
while True:
    for _ in range(5):
        try:
            p = {"closed": "true", "limit": 500, "order": "closedTime", "ascending": "false", "volume_num_min": vmin, "end_date_min": "2026-09-14", "end_date_max": "2026-09-17"}
            if cur: p["after_cursor"] = cur
            r = requests.get(G, params=p, timeout=30); r.raise_for_status(); j = r.json(); d = j.get("markets") or []; cur = j.get("next_cursor"); break
        except Exception as e:
            print("retry", e); time.sleep(3)
    else:
        raise SystemExit("gamma failed")
    if not d: break
    for m in d:
        try:
            op = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            toks = json.loads(m.get("clobTokenIds") or "[]")
            outs = json.loads(m.get("outcomes") or "[]")
        except Exception:
            continue
        if len(op) != 2 or len(toks) != 2 or sorted(op) != [0.0, 1.0]:
            continue
        ev = (m.get("events") or [{}])[0]
        out.write(json.dumps({"cid": m["conditionId"], "q": m["question"], "ev": ev.get("slug"), "ev_title": ev.get("title"),
            "closed": m["closedTime"], "end": m.get("endDate"), "start": m.get("startDate") or m.get("createdAt"),
            "yes_won": op[0] == 1.0, "tok": toks[0], "outcomes": outs, "vol": m.get("volumeNum"), "negRisk": m.get("negRisk"),
            "cat": m.get("category") or ev.get("category"), "sports": m.get("sportsMarketType"), "game": m.get("gameStartTime"),
            "rewards": m.get("clobRewards"), "fee": m.get("feesEnabled") or m.get("takerBaseFee")}) + "\n"); n += 1
    off += len(d)
    last = d[-1]["closedTime"]
    print(off, n, last, flush=True)
    if last < stop or not cur: break
