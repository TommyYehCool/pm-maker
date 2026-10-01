"""N 選 1 (negRisk) 事件：買齊全部 YES 的總成本 (逐檔吃到 size 股) 是否 < 1；買齊全部 NO 是否 < N-1。"""
import json, requests
from concurrent.futures import ThreadPoolExecutor
G = "https://gamma-api.polymarket.com"
evs, cur = [], None
while True:
    p = {"closed": "false", "active": "true", "limit": 100}
    if cur: p["after_cursor"] = cur
    j = requests.get(G + "/events/keyset", params=p, timeout=30).json()
    evs += j.get("events") or []; cur = j.get("next_cursor")
    if not cur or not j.get("events"): break
nr = [e for e in evs if e.get("negRisk") and len(e.get("markets") or []) >= 2]
print("events", len(evs), "negRisk", len(nr))
S = requests.Session()
def book(tok):
    try:
        b = S.get("https://clob.polymarket.com/book", params={"token_id": tok}, timeout=15).json()
        return sorted([(float(a["price"]), float(a["size"])) for a in b.get("asks", [])]), sorted([(float(a["price"]), float(a["size"])) for a in b.get("bids", [])], reverse=True)
    except Exception:
        return None
def cost(levels, q):
    got = c = 0.0
    for p, s in levels:
        t = min(s, q - got); c += t * p; got += t
        if got >= q - 1e-9: return c
    return None
res = []
BOOKS = {}
def prefetch(toks):
    for i in range(0, len(toks), 200):
        chunk = toks[i:i + 200]
        for _ in range(3):
            try:
                r = S.post("https://clob.polymarket.com/books", json=[{"token_id": t} for t in chunk], timeout=30); r.raise_for_status()
                for b in r.json():
                    BOOKS[b["asset_id"]] = (sorted([(float(a["price"]), float(a["size"])) for a in b.get("asks", [])]),
                                            sorted([(float(a["price"]), float(a["size"])) for a in b.get("bids", [])], reverse=True))
                break
            except Exception as e:
                print("books retry", e)
def ev_check(e):
    ms = [m for m in e["markets"] if m.get("active") and not m.get("closed") and m.get("clobTokenIds")]
    if len(ms) != len(e["markets"]): return None   # 有已關閉的選項就跳過，避免漏掉結果
    toks = [json.loads(m["clobTokenIds"]) for m in ms]
    bs = [(BOOKS.get(t[0]), BOOKS.get(t[1])) for t in toks]
    if any(x[0] is None or x[1] is None for x in bs): return None
    out = {"ev": e["slug"], "n": len(ms), "title": e["title"][:60], "aug": e.get("enableNegRisk") and any("other" in (m.get("groupItemTitle") or "").lower() for m in ms)}
    for q in (5, 20, 50):
        cy = [cost(b[0][0], q) for b in bs]; cn = [cost(b[1][0], q) for b in bs]
        out[f"yes{q}"] = sum(cy) / q if None not in cy else None
        out[f"no{q}"] = (sum(cn) / q) - (len(ms) - 1) if None not in cn else None
    return out
alltoks = [t for e in nr for m in e["markets"] if m.get("clobTokenIds") and m.get("active") and not m.get("closed") for t in json.loads(m["clobTokenIds"])]
print("tokens", len(alltoks), flush=True)
with ThreadPoolExecutor(6) as ex:
    list(ex.map(prefetch, [alltoks[i:i + 2000] for i in range(0, len(alltoks), 2000)]))
print("books", len(BOOKS), flush=True)
for e in nr:
    r = ev_check(e)
    if r: res.append(r)
json.dump(res, open("negrisk_scan.json", "w"), indent=0)
print("checked", len(res))
print("buy-all-YES cheapest (per $1 payout, 5 / 20 / 50 shares):")
for r in sorted([r for r in res if r["yes5"]], key=lambda r: r["yes5"])[:12]:
    print(f"  {r['yes5']:.4f} {r['yes20'] or 0:.4f} {r['yes50'] or 0:.4f}  n={r['n']:2d}  {r['title']}")
print("buy-all-NO excess over N-1 (<1 means arb):")
for r in sorted([r for r in res if r["no5"] is not None], key=lambda r: r["no5"])[:12]:
    print(f"  {r['no5']:.4f} {r['no20'] or 0:.4f} {r['no50'] or 0:.4f}  n={r['n']:2d}  {r['title']}")
