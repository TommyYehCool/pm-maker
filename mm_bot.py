#!/usr/bin/env python3
"""Polymarket 雙邊掛單做市 bot。

策略：在每個市場同時「買 YES @ bid」和「買 NO @ (1 - ask)」，只當 maker (post_only)。
兩邊都成交時手上是 1 YES + 1 NO = 結算必拿 $1，成本 = 1 - spread，spread 就是利潤。
單邊成交會累積庫存，用 skew 把報價往減倉方向推；超過 max_inventory 就停掉那一邊。

用法:
  python mm_bot.py add --market-id 2252242 [--half-spread 0.04 --size 20]   # 從 Gamma 抓資料加進 bot_config.json
  python mm_bot.py status                # 看餘額、持倉、掛單
  python mm_bot.py run                   # 依 bot_config.json 跑 (預設 dry_run=true 只印不下單)
  python mm_bot.py cancel-all            # 撤掉所有掛單
  touch STOP                             # 執行中建立 STOP 檔案 = 撤單並停止

環境變數 (.env):
  PM_PRIVATE_KEY      錢包私鑰 (Polymarket 設定頁匯出)
  PM_FUNDER           Polymarket 設定頁顯示的錢包地址 (資金所在的 proxy)；留空 = 用私鑰對應的 EOA
  PM_BUILDER_KEY / PM_BUILDER_SECRET / PM_BUILDER_PASSPHRASE
                      builder API key (client.create_builder_api_key() 產生)，有它才能 gasless merge / 授權
使用官方 polymarket-client SDK (舊的 py-clob-client 已被伺服器拒絕)。
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "bot_config.json")
OUT_DIR = os.path.join(HERE, "out")
FILLS_PATH = os.path.join(OUT_DIR, "fills.jsonl")
LOG_PATH = os.path.join(OUT_DIR, "bot.log")
SEEN_TRADES_PATH = os.path.join(OUT_DIR, "seen_trades.json")
MERGES_PATH = os.path.join(OUT_DIR, "merges.jsonl")
STATUS_PATH = os.path.join(OUT_DIR, "status.json")
KILL_FILE = os.path.join(HERE, "STOP")

CLOB_HOST = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CHAIN_ID = 137

DEFAULT_CONFIG = {
    "dry_run": True,
    "poll_seconds": 20,
    "max_total_exposure_usdc": 100,
    "auto_merge": True,
    "reprice_ticks": 1,
    "markets": [],
}


# ---------------------------------------------------------------- util

def log(msg):
    line = f"{datetime.now().strftime('%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env():
    """讀 .env (不依賴 python-dotenv)。"""
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    return {**DEFAULT_CONFIG, **cfg}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def round_price(p, tick, mode):
    t = Decimal(str(tick))
    # 先砍掉浮點尾差 (0.104-0.02 = 0.08399999...)，不然 ROUND_DOWN 會少一個 tick
    q = (Decimal(str(round(p, 8))) / t).quantize(Decimal("1"), rounding=mode) * t
    return float(q)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def days_until(iso):
    if not iso:
        return 999
    end = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return (end - datetime.now(timezone.utc)).total_seconds() / 86400


def compute_quotes(bb, ba, tick, net, max_inv, hs, mode="mid"):
    """回傳 (yes_bid, yes_ask, no_bid, skew)。bb/ba 是「不含自己掛單」的最佳買賣價。
    mode="mid"：mid ± half_spread，薄市場用，靠寬 spread 賺。
    mode="join"：直接掛在最佳買價/賣價排隊，厚市場用，賺的是市場本身的 spread，靠成交量。
    多 YES 時 skew<0：bid 壓低 (少買 YES)、ask 壓低 (= NO 買價提高，多買 NO)；反之亦然。
    兩邊都不穿現有最佳價，穿了就變 taker (post_only 也會被退)。"""
    mid = (bb + ba) / 2
    skew = clamp(-net / max_inv, -1, 1) * hs
    if mode == "join":
        # skew 以 tick 為單位：庫存越偏，往減倉方向退越多格
        k = round(clamp(-net / max_inv, -1, 1) * 2)
        yes_bid = round_price(bb + k * tick, tick, ROUND_DOWN)
        yes_ask = round_price(ba + k * tick, tick, ROUND_UP)
    else:
        yes_bid = round_price(mid - hs + skew, tick, ROUND_DOWN)
        yes_ask = round_price(mid + hs + skew, tick, ROUND_UP)
    yes_bid = clamp(yes_bid, tick, round_price(ba - tick, tick, ROUND_DOWN))
    yes_ask = clamp(yes_ask, round_price(bb + tick, tick, ROUND_UP), 1 - tick)
    no_bid = round_price(1 - yes_ask, tick, ROUND_DOWN)
    return yes_bid, yes_ask, no_bid, skew


# ---------------------------------------------------------------- client

def make_client(need_auth=True):
    """回 (client, wallet_address)。dry run 沒私鑰時回 PublicClient 只讀公開資料。"""
    load_env()
    key = os.environ.get("PM_PRIVATE_KEY")
    if not key:
        if need_auth:
            sys.exit("缺 PM_PRIVATE_KEY，請在 .env 設定 (參考 .env.example)")
        from polymarket import PublicClient
        return PublicClient(), None
    from polymarket import SecureClient
    api_key = None
    if os.environ.get("PM_BUILDER_KEY"):
        # 有 builder key 才能做 gasless 交易 (merge / redeem / 授權)；沒有只能掛單
        from polymarket.auth import BuilderApiKey
        api_key = BuilderApiKey(os.environ["PM_BUILDER_KEY"], os.environ["PM_BUILDER_SECRET"],
                                os.environ["PM_BUILDER_PASSPHRASE"])
    client = SecureClient.create(private_key=key, wallet=os.environ.get("PM_FUNDER") or None, api_key=api_key)
    return client, client.wallet


def get_positions(client, address):
    """持倉：{asset_id: {size, avg, cur, pnl}}。"""
    out = {}
    try:
        for page in client.list_positions(user=address):
            for p in page.items:
                out[str(p.asset_id)] = {
                    "size": float(p.current_size or 0),
                    "avg": float(p.avg_price or 0),
                    "cur": float(p.current_price or 0),
                    "pnl": float(p.total_pnl or 0),
                }
    except Exception as e:  # noqa: BLE001
        log(f"  positions fetch failed: {e}")
    return out


# ---------------------------------------------------------------- add market

def latest_candidates():
    import csv
    import glob
    files = sorted(glob.glob(os.path.join(OUT_DIR, "2*", "mm_candidates.csv")))
    if not files:
        sys.exit("找不到 out/*/mm_candidates.csv，先跑 scanner.py")
    with open(files[-1], encoding="utf-8") as f:
        return files[-1], list(csv.DictReader(f))


def cmd_add(a):
    if a.rank:
        path, rows = latest_candidates()
        print(f"from {path}")
        for rk in a.rank.split(","):
            row = rows[int(rk) - 1]
            label = f"{row['event']} | {row['outcome'] or row['question']}"
            add_market(a, row["market_id"], label)
    elif a.market_id:
        add_market(a, a.market_id, None)
    else:
        sys.exit("要給 --market-id 或 --rank")


def add_market(a, market_id, label):
    r = requests.get(f"{GAMMA}/markets/{market_id}", timeout=20)
    r.raise_for_status()
    m = r.json()
    tokens = json.loads(m["clobTokenIds"])
    entry = {
        "name": label or m["question"],
        "market_id": str(m["id"]),
        "condition_id": m["conditionId"],
        "yes_token": tokens[0],
        "no_token": tokens[1],
        "end_date": m.get("gameStartTime") or m.get("endDate"),  # 體育市場開賽就等於結束 (in-play 全是知情流量)
        "tick": float(m.get("orderPriceMinTickSize") or 0.01),
        "rewards_max_spread": float(m.get("rewardsMaxSpread") or 0),
        "rewards_min_size": float(m.get("rewardsMinSize") or 0),
        "half_spread": a.half_spread,
        "quote_mode": a.quote_mode,
        "size": a.size,
        "max_inventory": a.max_inventory,
        "min_days_left": a.min_days_left,
        "enabled": True,
    }
    cfg = load_config()
    cfg["markets"] = [x for x in cfg["markets"] if x["market_id"] != entry["market_id"]] + [entry]
    save_config(cfg)
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    print(f"\n已寫入 {CONFIG_PATH}（共 {len(cfg['markets'])} 個市場）")
    if entry["rewards_max_spread"] and a.half_spread * 100 > entry["rewards_max_spread"]:
        print(f"提醒：half_spread {a.half_spread*100:.1f}c 超過獎勵範圍 ±{entry['rewards_max_spread']}c，領不到流動性獎勵")


# ---------------------------------------------------------------- status

def cmd_status(_a):
    client, address = make_client()
    print(f"wallet: {address}  type: {getattr(client, 'wallet_type', '?')}")
    bal = client.get_balance_allowance(asset_type="COLLATERAL")
    print(f"USDC (pUSD) balance: {int(bal.balance) / 1e6:.2f}")
    appr = client.get_trading_approvals_state()
    print(f"trading approvals fully set: {appr.is_fully_approved}"
          + ("" if appr.is_fully_approved else "   (跑 `mm_bot.py approve` 補授權)"))
    pos = get_positions(client, address)
    cfg = load_config()
    print(f"\n持倉 ({len(pos)}):")
    for mk in cfg["markets"]:
        y = pos.get(mk["yes_token"], {}).get("size", 0)
        n = pos.get(mk["no_token"], {}).get("size", 0)
        pnl = pos.get(mk["yes_token"], {}).get("pnl", 0) + pos.get(mk["no_token"], {}).get("pnl", 0)
        print(f"  YES {y:7.1f}  NO {n:7.1f}  net {y-n:+7.1f}  pnl {pnl:+7.2f}  {mk['name'][:60]}")
    orders = [o for page in client.list_open_orders() for o in page.items]
    print(f"\n掛單 ({len(orders)}):")
    for o in orders:
        rem = float(o.original_size) - float(o.size_matched or 0)
        print(f"  {o.side} {o.outcome or ''} {o.price} x {rem:.1f}  asset ..{str(o.asset_id)[-8:]}  id {o.id[:12]}")


def cmd_approve(_a):
    client, _ = make_client()
    appr = client.get_trading_approvals_state()
    if appr.is_fully_approved:
        print("已經全部授權，不用做")
        return
    print("補齊交易授權 (proxy 錢包走 gasless relay，不用 gas)...")
    client.setup_trading_approvals().wait()
    print("done:", client.get_trading_approvals_state().is_fully_approved)


def cmd_cancel_all(_a):
    client, _ = make_client()
    r = client.cancel_all()
    print(f"cancelled {len(r.canceled)}  not cancelled {r.not_canceled}")


# ---------------------------------------------------------------- run

class Bot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.start_ts = int(time.time())
        self.my_tokens = {t for mk in cfg["markets"] for t in (mk["yes_token"], mk["no_token"])}
        self.dry = bool(cfg.get("dry_run", True))
        # dry_run 沒設私鑰也能跑：只讀公開訂單簿，看報價邏輯
        self.client, self.address = make_client(need_auth=not self.dry)
        self.authed = self.address is not None
        self.seen_trades = set()
        if os.path.exists(SEEN_TRADES_PATH):
            self.seen_trades = set(json.load(open(SEEN_TRADES_PATH)))

    # ---- data
    def book(self, yes_token, no_token):
        """YES 的最佳買賣價，扣掉自己掛的量 (自己的單不能當市場訊號，不然會自我參照)。
        NO 的買單等於 YES 的賣單 (價格 1-p)，所以兩邊的自家單都要扣。"""
        b = self.client.get_order_book(token_id=yes_token)
        mine = {}
        for o in self.my_orders(yes_token):
            mine[("bid", round(float(o.price), 4))] = mine.get(("bid", round(float(o.price), 4)), 0) + \
                float(o.original_size) - float(o.size_matched or 0)
        for o in self.my_orders(no_token):
            p = round(1 - float(o.price), 4)
            mine[("ask", p)] = mine.get(("ask", p), 0) + float(o.original_size) - float(o.size_matched or 0)
        bids = [(float(x.price), float(x.size) - mine.get(("bid", round(float(x.price), 4)), 0)) for x in b.bids]
        asks = [(float(x.price), float(x.size) - mine.get(("ask", round(float(x.price), 4)), 0)) for x in b.asks]
        bids = sorted(p for p, s in bids if s > 0.5)
        asks = sorted(p for p, s in asks if s > 0.5)
        best_bid = bids[-1] if bids else None
        best_ask = asks[0] if asks else None
        return best_bid, best_ask, float(b.tick_size), float(b.min_order_size or 5)

    def my_orders(self, token):
        if not self.authed:
            return []
        try:
            return [o for page in self.client.list_open_orders(token_id=token) for o in page.items]
        except Exception as e:  # noqa: BLE001
            log(f"  list_open_orders failed: {e}")
            return []

    def open_notional(self):
        total = 0.0
        if not self.authed:
            return total
        try:
            for page in self.client.list_open_orders():
                for o in page.items:
                    rem = float(o.original_size) - float(o.size_matched or 0)
                    total += rem * float(o.price)
        except Exception as e:  # noqa: BLE001
            log(f"  open_notional failed: {e}")
        return total

    # ---- orders
    def place_buy(self, token, price, size, label):
        if self.dry:
            log(f"  [dry] BUY {label} {size} @ {price}")
            return
        try:
            r = self.client.place_limit_order(token_id=token, price=str(price), size=str(size),
                                              side="BUY", post_only=True)
            if r.ok:
                log(f"  BUY {label} {size} @ {price} -> {r.status} {r.order_id[:12]}")
            else:
                log(f"  BUY {label} {size} @ {price} REJECTED {r.code}: {r.message}")
        except Exception as e:  # noqa: BLE001
            log(f"  BUY {label} {size} @ {price} FAILED: {e}")

    def cancel(self, order_ids, why):
        if not order_ids:
            return
        if self.dry:
            log(f"  [dry] cancel {len(order_ids)} ({why})")
            return
        try:
            self.client.cancel_orders(order_ids=order_ids)
            log(f"  cancelled {len(order_ids)} ({why})")
        except Exception as e:  # noqa: BLE001
            log(f"  cancel failed: {e}")

    def reconcile_side(self, token, want_price, size, tick, label, budget):
        """該邊只保留一張單：價格對就留著 (保住排隊位置)，不對就撤掉重掛。want_price=None 表示這邊不掛。
        budget 是還能新增的曝險 (dict 以便跨兩邊遞減)；留著的舊單已經算在曝險裡，不再扣。"""
        orders = [o for o in self.my_orders(token) if o.side == "BUY"]
        keep = []
        drop = []
        freed = 0.0
        for o in orders:
            rem = float(o.original_size) - float(o.size_matched or 0)
            tol = tick * float(self.cfg.get("reprice_ticks", 1)) + tick / 2
            same = want_price is not None and abs(float(o.price) - want_price) <= tol
            if same and rem >= size * 0.5 and not keep:
                keep.append(o)
            else:
                drop.append(o.id)
                freed += rem * float(o.price)
        self.cancel(drop, f"{label} reprice/dup")
        if drop and not self.dry:
            budget["left"] += freed  # 撤掉的單已算在曝險裡，額度要還回來，不然重掛會被自己擋住
        if want_price is None:
            return
        if keep:
            log(f"  keep {label} @ {want_price}")
            return
        cost = want_price * size
        if cost > budget["left"]:
            log(f"  skip {label}: exposure cap (need {cost:.2f}, left {budget['left']:.2f})")
            return
        budget["left"] -= cost
        self.place_buy(token, want_price, size, label)

    # ---- one market
    def quote_market(self, mk, positions):
        name = mk["name"][:50]
        dl = days_until(mk.get("end_date"))
        if dl < mk.get("min_days_left", 2):
            log(f"[{name}] {dl:.1f}d left < min, cancel & skip")
            self.cancel([o.id for t in (mk["yes_token"], mk["no_token"]) for o in self.my_orders(t)], "near end")
            return {"name": mk["name"], "state": "near_end", "days_left": round(dl, 1)}

        bb, ba, tick, min_size = self.book(mk["yes_token"], mk["no_token"])
        if bb is None and ba is None:
            log(f"[{name}] empty book, skip")
            return {"name": mk["name"], "state": "empty_book", "days_left": round(dl, 1)}
        if bb is None:
            bb = max(tick, ba - 0.10)
        if ba is None:
            ba = min(1 - tick, bb + 0.10)
        mid = (bb + ba) / 2
        if mid >= 0.97 or mid <= 0.03:
            # 實質上已經定案的市場：spread 沒得賺，剩下的只有被結算風險，撤單不做
            log(f"[{name}] mid {mid:.3f} effectively resolved, cancel & skip")
            self.cancel([o.id for t in (mk["yes_token"], mk["no_token"]) for o in self.my_orders(t)], "resolved")
            return {"name": mk["name"], "state": "resolved", "days_left": round(dl, 1), "mid": round(mid, 3)}

        y = positions.get(mk["yes_token"], {}).get("size", 0)
        n = positions.get(mk["no_token"], {}).get("size", 0)
        net = y - n
        max_inv = float(mk.get("max_inventory", 50))
        yes_bid, yes_ask, no_bid, skew = compute_quotes(bb, ba, tick, net, max_inv, float(mk["half_spread"]),
                                                        mk.get("quote_mode", "mid"))

        size = max(float(mk["size"]), min_size)
        want_yes = yes_bid if net < max_inv else None
        want_no = no_bid if net > -max_inv else None

        exposure = self.open_notional() + sum(
            p["size"] * p["avg"] for p in positions.values())
        budget = {"left": float(self.cfg.get("max_total_exposure_usdc", 100)) - exposure}

        log(f"[{name}] {mk.get('quote_mode', 'mid')} book {bb:.3f}/{ba:.3f} mid {mid:.3f} | inv Y{y:.0f} N{n:.0f} net {net:+.0f} skew {skew:+.3f} "
            f"| quote YES {want_yes} / NO {want_no} (=YES ask {yes_ask}) | exposure {exposure:.1f}")
        sides = [(mk["yes_token"], want_yes, "YES"), (mk["no_token"], want_no, "NO")]
        if net > 0:
            sides.reverse()  # 手上多 YES：先掛 NO 把對湊齊，錢不夠時犧牲 YES 那邊
        for token, want, label in sides:
            self.reconcile_side(token, want, size, tick, label, budget)
        return {
            "name": mk["name"], "state": "quoting", "days_left": round(dl, 1),
            "best_bid": bb, "best_ask": ba, "mid": round(mid, 4),
            "yes_pos": y, "no_pos": n, "net": net, "skew": round(skew, 4),
            "quote_yes": want_yes, "quote_no": want_no, "yes_ask_equiv": yes_ask, "size": size,
            "yes_pnl": positions.get(mk["yes_token"], {}).get("pnl", 0),
            "no_pnl": positions.get(mk["no_token"], {}).get("pnl", 0),
            "orders": [
                {"side": o.side, "outcome": o.outcome, "price": float(o.price),
                 "remaining": float(o.original_size) - float(o.size_matched or 0),
                 "matched": float(o.size_matched or 0), "id": o.id}
                for t in (mk["yes_token"], mk["no_token"]) for o in self.my_orders(t)
            ],
        }

    # ---- fills
    def record_fills(self):
        """只記 bot 啟動後、自己市場的成交 (帳號歷史成交會很多，不要全倒進來)。"""
        try:
            trades = [t for page in self.client.list_account_trades(after=str(self.start_ts)) for t in page.items]
        except Exception as e:  # noqa: BLE001
            log(f"  list_account_trades failed: {e}")
            return
        new = 0
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(FILLS_PATH, "a", encoding="utf-8") as f:
            me = (self.address or "").lower()
            for t in trades:
                if t.id in self.seen_trades or str(t.asset_id) not in self.my_tokens:
                    continue
                self.seen_trades.add(t.id)
                mine = [mo for mo in (t.maker_orders or []) if str(mo.maker_address).lower() == me]
                my_size = sum(float(mo.matched_amount) for mo in mine) if mine else float(t.size)
                my_price = float(mine[0].price) if mine else float(t.price)
                my_side = mine[0].side if mine else t.side
                my_outcome = (mine[0].outcome if mine else t.outcome) or ""
                rec = t.model_dump(mode="json")
                rec.update({"my_size": my_size, "my_price": my_price, "my_side": my_side, "my_outcome": my_outcome})
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                new += 1
                log(f"  FILL {my_side} {my_outcome} {my_size:g} @ {my_price} ({t.trader_side}, taker swept {float(t.size):g} @ {t.price}) {t.status}")
        if new:
            json.dump(sorted(self.seen_trades), open(SEEN_TRADES_PATH, "w"))

    def merge_pairs(self, positions):
        """YES 和 NO 都有持倉的市場，把配成對的部分 merge 回 pUSD (gasless)，不用等結算。"""
        if self.dry or not self.cfg.get("auto_merge", True):
            return
        for mk in self.cfg["markets"]:
            y = positions.get(mk["yes_token"], {}).get("size", 0)
            n = positions.get(mk["no_token"], {}).get("size", 0)
            pairs = min(y, n)
            if pairs < 1:
                continue
            try:
                h = self.client.merge_positions(condition_id=mk["condition_id"], amount="max")
                out = h.wait()
                log(f"  MERGE {pairs:.1f} pairs -> pUSD [{mk['name'][:40]}] {getattr(out, 'status', out)}")
                with open(MERGES_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "market": mk["name"],
                                        "pairs": pairs, "result": str(out)[:300]}, ensure_ascii=False) + "\n")
            except Exception as e:  # noqa: BLE001
                log(f"  MERGE failed [{mk['name'][:40]}]: {e}")

    def write_status(self, snapshot, positions):
        """給 dashboard.py 讀的快照，每輪覆蓋。"""
        balance = None
        if self.authed:
            try:
                balance = int(self.client.get_balance_allowance(asset_type="COLLATERAL").balance) / 1e6
            except Exception as e:  # noqa: BLE001
                log(f"  balance fetch failed: {e}")
        exposure = self.open_notional() + sum(p["size"] * p["avg"] for p in positions.values())
        status = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "started_at": datetime.fromtimestamp(self.start_ts).isoformat(timespec="seconds"),
            "dry_run": self.dry,
            "wallet": self.address,
            "balance": balance,
            "exposure": round(exposure, 2),
            "exposure_cap": self.cfg.get("max_total_exposure_usdc"),
            "poll_seconds": self.cfg.get("poll_seconds"),
            "fills_total": len(self.seen_trades),
            "markets": snapshot,
        }
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATUS_PATH)

    # ---- loop
    def run(self):
        log(f"start dry_run={self.dry} address={self.address} markets={len(self.cfg['markets'])} "
            f"cap={self.cfg.get('max_total_exposure_usdc')} USDC")
        while True:
            if os.path.exists(KILL_FILE):
                log("STOP file found, cancelling all and exiting")
                if not self.dry:
                    self.client.cancel_all()
                os.remove(KILL_FILE)
                return
            positions = get_positions(self.client, self.address) if self.authed else {}
            snapshot = []
            for mk in self.cfg["markets"]:
                if not mk.get("enabled", True):
                    continue
                try:
                    snapshot.append(self.quote_market(mk, positions))
                except Exception as e:  # noqa: BLE001
                    if "No orderbook exists" in str(e):
                        # 市場已結算/關閉：停用並寫回 config，下次重啟也不會再碰
                        mk["enabled"] = False
                        save_config(self.cfg)
                        log(f"[{mk['name'][:50]}] market closed, disabled in config")
                        snapshot.append({"name": mk["name"], "state": "closed"})
                        continue
                    log(f"[{mk['name'][:50]}] error: {e}")
                    snapshot.append({"name": mk["name"], "state": "error", "error": str(e)[:200]})
            if not self.dry:
                self.record_fills()
                self.merge_pairs(positions)
            self.write_status(snapshot, positions)
            time.sleep(float(self.cfg.get("poll_seconds", 20)))


def cmd_run(a):
    cfg = load_config()
    if a.live:
        cfg["dry_run"] = False
    if not cfg["markets"]:
        sys.exit("bot_config.json 沒有市場，先用 `mm_bot.py add --market-id ...`")
    if not cfg["dry_run"]:
        print(f"*** LIVE 模式：會用真錢下單，曝險上限 {cfg.get('max_total_exposure_usdc')} USDC ***")
        if not a.yes and input("輸入 yes 繼續: ").strip() != "yes":
            sys.exit("aborted")
    try:
        Bot(cfg).run()
    except KeyboardInterrupt:
        print("\nCtrl-C：掛單不會自動撤，要撤請跑 `mm_bot.py cancel-all`")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("add", help="從 Gamma 抓市場資料加進 bot_config.json")
    s.add_argument("--market-id", help="Gamma market id")
    s.add_argument("--rank", help="從最新 scanner 結果加第幾名，例如 1,2,5")
    s.add_argument("--half-spread", type=float, default=0.04, help="中價兩側各掛多遠 (0.04 = 4 cents)，mid 模式用")
    s.add_argument("--quote-mode", choices=["mid", "join"], default="mid",
                   help="mid=中價±half_spread (薄市場)；join=掛在最佳買賣價排隊 (厚市場)")
    s.add_argument("--size", type=float, default=20, help="每邊掛幾股")
    s.add_argument("--max-inventory", type=float, default=60, help="淨庫存 (YES-NO) 超過就停掉那一邊")
    s.add_argument("--min-days-left", type=float, default=2, help="離結算少於幾天就撤單不做")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("approve", help="補齊交易授權 (status 顯示沒授權時用)"); s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("cancel-all"); s.set_defaults(fn=cmd_cancel_all)
    s = sub.add_parser("run")
    s.add_argument("--live", action="store_true", help="覆蓋 config 的 dry_run，真的下單")
    s.add_argument("--yes", action="store_true", help="不要問確認 (systemd 用)")
    s.set_defaults(fn=cmd_run)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
