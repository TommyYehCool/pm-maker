#!/usr/bin/env python3
"""氣溫市場 shadow：算出「今天最高溫落在各檔的機率」，跟 Polymarket 的價格比對，只記錄不下單。

為什麼可能有 edge：這些市場問的是「今天最高溫是不是 X°C」，結算靠 NOAA 實測。
真正的資訊是 (a) 已經觀測到的當日溫度 (b) 剩下幾小時的預報。速度不是重點 (METAR 每 30 分鐘一包、
還延遲 5 分鐘發布)，重點是「已知半天的溫度，今天的最高溫分布長什麼樣」。

做法：
  1. 從 Gamma 找出今天的氣溫市場 (同一個事件下有好幾檔)
  2. open-meteo 集合預報 (31 個成員) 給當日各小時溫度 → 每個成員取最高溫 → 機率分布
  3. 已經過去的小時用實測值取代預報 (aviationweather METAR)，只有未來小時才用預報
  4. 我們的機率 vs 市場價格，差距大的記下來
  5. 每天結算後對帳：我們的機率有沒有比市場準 (Brier score)

  python weather_shadow.py scan            # 掃今天的氣溫市場，印出我們 vs 市場
  python weather_shadow.py run             # 每 10 分鐘跑一次，寫 out/weather_shadow.jsonl
  python weather_shadow.py score           # 用已結算的紀錄算 Brier score (我們 vs 市場誰準)
"""
import argparse
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
LOG_PATH = os.path.join(OUT_DIR, "weather_shadow.jsonl")
GAMMA = "https://gamma-api.polymarket.com"

GEO_CACHE = os.path.join(OUT_DIR, "weather_geo.json")
# 多模型集合 (143 個成員)：只用 GFS 會輸給用 ECMWF 的人
MODELS = "ecmwf_ifs025,gfs025,icon_seamless,gem_global"
PAT = re.compile(r"Will the (highest|lowest) temperature in (.+?) be (?:between )?(-?[\d.]+)"
                 r"(?:-(-?[\d.]+))?°([CF])( or below| or above)? on (\w+ \d+)\?")
# 城市 → 最近的 METAR 站台 (有的才用實測校正；沒有的就純預報)
STATIONS = {"Shanghai": "ZSPD", "Seoul": "RKSI", "Incheon": "RKSI", "Beijing": "ZBAA", "Tokyo": "RJTT",
            "London": "EGLL", "Paris": "LFPG", "Moscow": "UUDD", "Milan": "LIMC", "Munich": "EDDM",
            "Amsterdam": "EHAM", "Helsinki": "EFHK", "Singapore": "WSSS", "Jeddah": "OEJN", "Busan": "RKPK",
            "New York City": "KNYC", "Miami": "KMIA", "Atlanta": "KATL", "Chicago": "KORD", "Wuhan": "ZHHH",
            "Istanbul": "LTFM", "Dubai": "OMDB", "Delhi": "VIDP", "Mumbai": "VABB", "Sydney": "YSSY",
            "Berlin": "EDDB", "Madrid": "LEMD", "Rome": "LIRF", "Toronto": "CYYZ", "Los Angeles": "KLAX",
            "Houston": "KIAH", "Dallas": "KDFW", "Denver": "KDEN", "Seattle": "KSEA", "Boston": "KBOS",
            "Philadelphia": "KPHL", "Washington": "KDCA", "San Francisco": "KSFO", "Phoenix": "KPHX"}


def geocode(city):
    """城市 → (lat, lon, tz)。查一次存檔。"""
    cache = {}
    if os.path.exists(GEO_CACHE):
        try:
            cache = json.load(open(GEO_CACHE, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    if city in cache:
        return tuple(cache[city])
    r = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                     params={"name": city, "count": 1, "language": "en", "format": "json"}, timeout=20).json()
    res = (r.get("results") or [None])[0]
    if not res:
        raise ValueError(f"geocode failed: {city}")
    out = (res["latitude"], res["longitude"], res.get("timezone") or "UTC")
    cache[city] = list(out)
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(cache, open(GEO_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return out


def log_event(rec):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 市場

def find_markets(day=None):
    """今天/明天的氣溫市場：用 Gamma 搜尋直接抓事件 (逐一掃獎勵清單太慢)，再標出有獎勵池的。"""
    from polymarket import PublicClient
    pools = {}
    try:
        pc = PublicClient()
        for page in pc.list_current_rewards():
            for r in page.items:
                pools[r.condition_id] = (float(r.total_daily_rate or 0), float(r.rewards_min_size or 0))
    except Exception as e:  # noqa: BLE001
        print(f"  rewards list failed: {e}", file=sys.stderr)
    today = datetime.now(timezone.utc)
    want = {today.strftime("%B %-d"), (today + timedelta(days=1)).strftime("%B %-d")}
    out = defaultdict(list)
    seen = set()
    for q in ("highest temperature", "lowest temperature"):
        try:
            r = requests.get(f"{GAMMA}/public-search",
                             params={"q": q, "limit_per_type": 60, "events_status": "active"}, timeout=25).json()
        except Exception as e:  # noqa: BLE001
            print(f"  search failed: {e}", file=sys.stderr)
            continue
        for ev in r.get("events", []):
            for m in ev.get("markets", []):
                cid = m.get("conditionId")
                if not cid or cid in seen or m.get("closed"):
                    continue
                mt = PAT.match(m.get("question", ""))
                if not mt:
                    continue
                kind, city, lo, hi, unit, tail, date = mt.groups()
                if date not in want:
                    continue
                seen.add(cid)
                p = m.get("outcomePrices")
                p = json.loads(p) if isinstance(p, str) else p
                pool, minsz = pools.get(cid, (0.0, 0.0))
                bp = m.get("bestAsk"); bb = m.get("bestBid")
                lo_v = float(lo); hi_v = float(hi) if hi else lo_v
                if tail and "below" in tail:
                    lo_v = -999.0                      # 「X 度或以下」是累積檔，下界無限
                elif tail and "above" in tail:
                    hi_v = 999.0
                out[(city.split("(")[0].strip(), date, kind, unit)].append({
                    "cid": cid, "q": m["question"], "lo": lo_v, "hi": hi_v,
                    "yes": float(p[0]) if p else 0.0, "pool": pool, "min_size": minsz,
                    "yes_ask": float(bp) if bp else None,
                    "no_ask": round(1 - float(bb), 4) if bb else None,   # 買 NO 的賣價 = 1 − YES 最佳買價
                })
    return out


# ---------------------------------------------------------------- 資料

def metar_today(station, tz_offset_hours):
    """今天 (當地) 已觀測到的各小時溫度 {hour: degC}。"""
    try:
        r = requests.get("https://aviationweather.gov/api/data/metar",
                         params={"ids": station, "format": "json", "hours": 24}, timeout=20).json()
    except Exception as e:  # noqa: BLE001
        print(f"  metar {station} failed: {e}", file=sys.stderr)
        return {}
    tz = timezone(timedelta(hours=tz_offset_hours))
    today = datetime.now(tz).strftime("%Y-%m-%d")
    out = {}
    for ob in r:
        t = ob.get("temp")
        ts = ob.get("obsTime")
        if t is None or ts is None:
            continue
        lt = datetime.fromtimestamp(ts, tz)
        if lt.strftime("%Y-%m-%d") != today:
            continue
        out[lt.hour] = max(out.get(lt.hour, -99), float(t))
    return out


def ensemble_day(lat, lon, tz, target_date):
    """集合預報：回 (target_date, times, {模型: [(idx, 逐小時溫度)]}, tz_off)。
    成員按「哪個模型」分組，之後用模型之間的分歧當機率區間，而不是把 143 個成員當成獨立試驗。"""
    by_model = {}
    tz_off = 0
    times = None
    for mdl in MODELS.split(","):
        try:
            r = requests.get("https://ensemble-api.open-meteo.com/v1/ensemble",
                             params={"latitude": lat, "longitude": lon, "hourly": "temperature_2m",
                                     "models": mdl, "past_days": 1, "forecast_days": 3, "timezone": tz}, timeout=40).json()
            h = r["hourly"]
        except Exception as e:  # noqa: BLE001
            print(f"  {mdl} failed: {e}", file=sys.stderr)
            continue
        tz_off = int(r.get("utc_offset_seconds", 0)) // 3600
        times = h["time"]
        idx = [i for i, t in enumerate(h["time"]) if t.startswith(target_date)]
        if not idx:
            continue
        mem = []
        for key in h:
            if not key.startswith("temperature_2m"):
                continue
            vals = [h[key][i] for i in idx if h[key][i] is not None]
            if len(vals) >= 20:
                mem.append((idx, vals))
        if mem:
            by_model[mdl] = mem
    if not by_model:
        raise ValueError(f"no forecast for {target_date}")
    return target_date, times, by_model, tz_off


def high_distribution(lat, lon, tz, station, target_date, kind="highest", obs_weight=True):
    """回 ({模型: 極值樣本 list}, 已觀測小時數, 已觀測極值)。
    只有 target_date 就是當地今天時才用實測取代已過去的小時，否則純預報。"""
    today, times, by_model, tz_off = ensemble_day(lat, lon, tz, target_date)
    local_now = datetime.now(timezone(timedelta(hours=tz_off)))
    is_today = local_now.strftime("%Y-%m-%d") == target_date
    obs = metar_today(station, tz_off) if (obs_weight and station and is_today) else {}
    now_h = local_now.hour if is_today else -1
    obs_extreme = (max(obs.values()) if kind == "highest" else min(obs.values())) if obs else None
    out = {}
    for mdl, members in by_model.items():
        samples = []
        for idx, vals in members:
            hourly = []
            for i, v in zip(idx, vals):
                hh = int(times[i][11:13])
                hourly.append(obs[hh] if (hh <= now_h and hh in obs) else v)
            samples.append(max(hourly) if kind == "highest" else min(hourly))
        out[mdl] = samples
    return out, len(obs), obs_extreme


def bucket_probs(samples, buckets, unit="C"):
    """每個檔位的機率。buckets: [(lo, hi)]，攝氏；unit='F' 時樣本先換算。"""
    n = len(samples) or 1
    vals = [v * 9 / 5 + 32 for v in samples] if unit == "F" else samples
    out = []
    for lo, hi in buckets:
        a, b = (lo - 0.5, hi + 0.5)     # 市場用整數度，X 表示四捨五入後等於 X
        out.append(sum(1 for v in vals if a <= v < b) / n)
    return out


def bucket_interval(by_model, buckets, unit="C"):
    """機率區間：每個模型各自算一次，取模型之間的 min/max。
    這是「模型分歧」區間，不是信賴區間；它不包含所有模型一起錯的情況。"""
    per = {m: bucket_probs(s, buckets, unit) for m, s in by_model.items()}
    n = len(buckets)
    lo = [min(per[m][i] for m in per) for i in range(n)]
    hi = [max(per[m][i] for m in per) for i in range(n)]
    mid = [sum(per[m][i] for m in per) / len(per) for i in range(n)]
    return lo, mid, hi, per


def max_buy_prices(q_lo, q_hi, buffer):
    """依規格：買 YES 最高價 = q_low − buffer；買 NO 最高價 = 1 − q_high − buffer。
    獎勵不得提高這個上限。"""
    return max(0.0, q_lo - buffer), max(0.0, 1 - q_hi - buffer)


# ---------------------------------------------------------------- 指令

def cmd_scan(a):
    groups = find_markets()
    if not groups:
        print("今天沒有符合的氣溫市場")
        return
    rows, tradable = [], []
    for (city, date, kind, unit), mkts in sorted(groups.items()):
        if a.min_pool and max(m["pool"] for m in mkts) < a.min_pool:
            continue
        try:
            lat, lon, tz = geocode(city)
            y = datetime.now(timezone.utc).year
            target = datetime.strptime(f"{date} {y}", "%B %d %Y").strftime("%Y-%m-%d")
            by_model, n_obs, obs_x = high_distribution(lat, lon, tz, STATIONS.get(city), target, kind)
        except Exception as e:  # noqa: BLE001
            print(f"  {city} {date} 預報失敗: {type(e).__name__} {e}", file=sys.stderr)
            continue
        mkts.sort(key=lambda m: m["lo"])
        buckets = [(m["lo"], m["hi"]) for m in mkts]
        q_lo, q_mid, q_hi, per = bucket_interval(by_model, buckets, unit)
        obs_txt = f'已觀測 {n_obs}h 極值 {obs_x}°C' if n_obs else '尚未開始'
        print(f'\n{city} {date} {kind} ({unit})  {obs_txt}  模型 {len(per)} 個  市場和 {sum(m["yes"] for m in mkts):.2f}')
        for m, lo, mid, hi in zip(mkts, q_lo, q_mid, q_hi):
            buy_yes, buy_no = max_buy_prices(lo, hi, a.buffer)
            label = ("≤%.0f" % m["hi"]) if m["lo"] < -900 else ("≥%.0f" % m["lo"]) if m["hi"] > 900 else \
                    ("%.0f" % m["lo"]) if m["hi"] == m["lo"] else ("%.0f-%.0f" % (m["lo"], m["hi"]))
            yes_ask, no_ask = m.get("yes_ask"), m.get("no_ask")
            act = ""
            # 只有「市場賣價低於我們的買價上限」才是候選；獎勵不參與這個判斷
            if yes_ask is not None and yes_ask <= buy_yes and buy_yes > 0:
                act = f"BUY YES @ ≤{buy_yes:.3f} (ask {yes_ask:.3f})"
            elif no_ask is not None and no_ask <= buy_no and buy_no > 0:
                act = f"BUY NO  @ ≤{buy_no:.3f} (ask {no_ask:.3f})"
            if act:
                worst = m["min_size"] * (yes_ask if "YES" in act else no_ask)
                if worst > a.event_budget:
                    act += f"  [跳過: 最低 {m['min_size']:.0f} 股最壞損失 {worst:.2f} > 預算 {a.event_budget:.0f}]"
                else:
                    tradable.append((city, date, label, act, worst, m))
            print(f'   {label:8} 市場 {m["yes"]:.3f}  我們 [{lo:.3f},{hi:.3f}] 中 {mid:.3f}  '
                  f'買YES≤{buy_yes:.3f} 買NO≤{buy_no:.3f}  池 {m["pool"]:.0f}  min {m["min_size"]:.0f}  {act}')
            rows.append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "city": city, "date": date,
                         "kind": kind, "unit": unit, "lo": m["lo"], "hi": m["hi"], "cid": m["cid"],
                         "market": m["yes"], "ours": round(mid, 4), "q_low": round(lo, 4), "q_high": round(hi, 4),
                         "buy_yes_max": round(buy_yes, 4), "buy_no_max": round(buy_no, 4),
                         "per_model": {k: round(v[buckets.index((m["lo"], m["hi"]))], 4) for k, v in per.items()},
                         "n_obs": n_obs, "obs_extreme": obs_x, "pool": m["pool"], "min_size": m["min_size"]})
    print(f'\n符合「賣價低於保守估值 − {a.buffer} 安全空間」的候選: {len(tradable)}')
    for city, date, label, act, worst, m in tradable:
        print(f'   {city} {date} {label}: {act}  最壞損失 {worst:.2f}')
    if rows and not a.no_write:
        for r in rows:
            log_event(r)
        print(f"{len(rows)} rows -> {LOG_PATH}")


def cmd_run(a):
    while True:
        try:
            cmd_scan(a)
        except Exception as e:  # noqa: BLE001
            print("scan failed:", e, file=sys.stderr)
        time.sleep(a.every * 60)


def cmd_score(_a):
    """已結算的市場：比我們和市場誰準 (Brier score，越低越好)。"""
    from polymarket import PublicClient
    pc = PublicClient()
    rows = [json.loads(l) for l in open(LOG_PATH, encoding="utf-8")] if os.path.exists(LOG_PATH) else []
    if not rows:
        print("還沒有紀錄")
        return
    # 每個 (cid, 小時) 只留最後一筆
    last = {}
    for r in rows:
        last[(r["cid"], r["ts"][:13])] = r
    resolved, ours, mkt = 0, 0.0, 0.0
    for r in last.values():
        try:
            m = requests.get(f"{GAMMA}/markets", params={"condition_ids": r["cid"]}, timeout=15).json()
        except Exception:  # noqa: BLE001
            continue
        if not m or not m[0].get("umaResolutionStatus") == "resolved":
            continue
        p = m[0].get("outcomePrices")
        p = json.loads(p) if isinstance(p, str) else p
        y = 1.0 if float(p[0]) > 0.5 else 0.0
        resolved += 1
        ours += (r["ours"] - y) ** 2
        mkt += (r["market"] - y) ** 2
    if not resolved:
        print("還沒有已結算的樣本")
        return
    print(f"已結算 {resolved} 筆\n  我們 Brier {ours/resolved:.4f}\n  市場 Brier {mkt/resolved:.4f}"
          f"\n  {'我們比較準' if ours < mkt else '市場比較準'}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--min-edge", type=float, default=0.10)
    s.add_argument("--min-pool", type=float, default=20, help="事件內最大獎勵池低於此就跳過")
    s.add_argument("--buffer", type=float, default=0.05, help="安全空間 (暫定，待歷史驗證)")
    s.add_argument("--event-budget", type=float, default=5.0, help="每事件最大結算損失")
    s.add_argument("--no-write", action="store_true")
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("run")
    s.add_argument("--every", type=int, default=10, help="幾分鐘掃一次")
    s.add_argument("--min-edge", type=float, default=0.10)
    s.add_argument("--min-pool", type=float, default=20)
    s.add_argument("--buffer", type=float, default=0.05)
    s.add_argument("--event-budget", type=float, default=5.0)
    s.add_argument("--no-write", action="store_true")
    s.set_defaults(fn=cmd_run)
    sub.add_parser("score").set_defaults(fn=cmd_score)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
