"""每個已結算市場 YES token 的歷史價格 (CLOB prices-history, 每小時)。存 prices/<cid>.json，可續跑。"""
import json, os, requests, sys, time
from concurrent.futures import ThreadPoolExecutor
os.makedirs("prices", exist_ok=True)
rows = [json.loads(l) for l in open(sys.argv[1] if len(sys.argv) > 1 else "markets.jsonl")]
rows = [r for r in rows if (r.get("vol") or 0) >= (float(sys.argv[2]) if len(sys.argv) > 2 else 0)]
todo = [r for r in rows if not os.path.exists(f"prices/{r['cid']}.json")]
print(len(rows), "todo", len(todo), flush=True)
S = requests.Session()
def get(r):
    for i in range(4):
        try:
            x = S.get("https://clob.polymarket.com/prices-history", params={"market": r["tok"], "interval": "max", "fidelity": 60}, timeout=20)
            if x.status_code == 429: time.sleep(2 + i * 3); continue
            x.raise_for_status()
            h = x.json().get("history") or []
            json.dump([[p["t"], p["p"]] for p in h], open(f"prices/{r['cid']}.json", "w"))
            return 1
        except Exception:
            time.sleep(1 + i)
    return 0
done = 0
with ThreadPoolExecutor(12) as ex:
    for ok in ex.map(get, todo):
        done += ok
        if done % 1000 == 0: print(done, flush=True)
print("done", done)
