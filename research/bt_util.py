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

