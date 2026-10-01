"""風控與帳本的純函數 / 純資料結構，不打 API，方便測試。

- worst_loss：單一二元市場「納入掛單可能成交後」相對歷史成本的最壞損失。
- cluster_loss_bound：同事件叢集各市場最壞損失直接相加 (保守上界，不是有效情境精確值)。
- Ledger：依交易時序的加權平均成本帳；merge 依當時成本移除；legacy 層先消耗 (觀察期新舊部位分帳)。
- queue_price：v2 掛價，只加入前面已有足夠別人掛單的價位 (被打時別人先被吃)。
- liquidation_value：依簿子逐檔吃掉持倉的預估所得 (快照，不是保證)。
- Markout：成交後 1/5/30/60 分鐘的 S = q(ref0 - p)、A_T = q(ref_T - ref0)，ref 不可靠就 NA，不刪成交。
"""
from itertools import product


# ---------------------------------------------------------------- 曝險

def worst_loss(yes_qty, yes_cost, no_qty, no_cost, orders):
    """相對歷史成本的最壞損失 (正數 = 損失)。
    orders: [(side, qty, price)]，side 是 "yes"/"no"，都是買單。
    枚舉每張單成交/不成交 × 結算 YES/NO。掛單只有不利一側成交的情境自然包含在內。"""
    worst = 0.0
    for fills in product((0, 1), repeat=len(orders)):
        y, yc, n, nc = yes_qty, yes_cost, no_qty, no_cost
        for f, (side, q, p) in zip(fills, orders):
            if not f:
                continue
            if side == "yes":
                y += q; yc += q * p
            else:
                n += q; nc += q * p
        cost = yc + nc
        for payoff in (y, n):  # 結算 YES → YES 股各拿 1；結算 NO → NO 股各拿 1
            worst = max(worst, cost - payoff)
    return worst


def cluster_loss_bound(markets):
    """markets: [(yes_qty, yes_cost, no_qty, no_cost, orders)]。各市場最壞損失相加。
    命名為上界：互斥區間不會全部同時輸，這裡不做有效情境枚舉，寧可保守。"""
    return sum(worst_loss(*m) for m in markets)


# ---------------------------------------------------------------- 帳本

class Layer:
    __slots__ = ("qty", "cost")

    def __init__(self, qty=0.0, cost=0.0):
        self.qty, self.cost = qty, cost

    def avg(self):
        return self.cost / self.qty if self.qty > 1e-9 else 0.0

    def take(self, q):
        """移除 q 股，回傳其成本；不足就移除全部。"""
        q = min(q, self.qty)
        c = q * self.avg()
        self.qty -= q; self.cost -= c
        if self.qty < 1e-9:
            self.qty, self.cost = 0.0, 0.0
        return q, c


class MarketLedger:
    """一個市場的 YES/NO 兩邊，各有 legacy (觀察期開始前) 與 new 兩層。買入進 new；merge 先消耗 legacy。"""

    def __init__(self):
        self.yes = {"legacy": Layer(), "new": Layer()}
        self.no = {"legacy": Layer(), "new": Layer()}
        self.realized = {"legacy": 0.0, "new": 0.0}
        self.merged_pairs = 0.0

    def side(self, outcome):
        return self.yes if outcome == "yes" else self.no

    def qty(self, outcome):
        s = self.side(outcome)
        return s["legacy"].qty + s["new"].qty

    def cost(self, outcome):
        s = self.side(outcome)
        return s["legacy"].cost + s["new"].cost

    def buy(self, outcome, qty, price, layer="new"):
        L = self.side(outcome)[layer]
        L.qty += qty; L.cost += qty * price

    def sell(self, outcome, qty, price):
        """賣出 (taker 出場)：先消耗 legacy 層，已實現 = 所得 − 移除成本。"""
        left = qty
        for layer in ("legacy", "new"):
            q, c = self.side(outcome)[layer].take(left)
            self.realized[layer] += q * price - c
            left -= q
            if left <= 1e-9:
                break
        return qty - left

    def merge(self, pairs):
        """pairs 對 YES+NO 換回 pairs 美金。每邊先消耗 legacy 層；已實現 = 回款 − 移除成本，按移除來源分攤。"""
        pairs = min(pairs, self.qty("yes"), self.qty("no"))
        if pairs <= 1e-9:
            return 0.0
        removed = {"legacy": 0.0, "new": 0.0}
        for side in (self.yes, self.no):
            left = pairs
            for layer in ("legacy", "new"):
                q, c = side[layer].take(left)
                removed[layer] += c
                left -= q
        # 回款 1 美金/對，按各層被移除的股數比例分 (每層 YES+NO 股數 / 2 對)
        # 簡化：以成本比例分攤回款，避免一層被移除 YES 另一層被移除 NO 時無法對應
        tot_cost = removed["legacy"] + removed["new"]
        for layer in ("legacy", "new"):
            share = removed[layer] / tot_cost if tot_cost > 0 else 0.5
            self.realized[layer] += pairs * share - removed[layer]
        self.merged_pairs += pairs
        return pairs

    def mark_legacy(self):
        """觀察期開始：目前所有持倉標成 legacy 層，之前的已實現也歸 legacy。"""
        for side in (self.yes, self.no):
            side["legacy"].qty += side["new"].qty; side["legacy"].cost += side["new"].cost
            side["new"] = Layer()
        self.realized["legacy"] += self.realized["new"]; self.realized["new"] = 0.0

    def unrealized(self, yes_price, no_price):
        """{layer: 標記值 − 剩餘成本}"""
        out = {}
        for layer in ("legacy", "new"):
            val = self.yes[layer].qty * yes_price + self.no[layer].qty * no_price
            out[layer] = val - self.yes[layer].cost - self.no[layer].cost
        return out

    def to_dict(self):
        return {"yes": {k: [v.qty, v.cost] for k, v in self.yes.items()}, "no": {k: [v.qty, v.cost] for k, v in self.no.items()},
                "realized": dict(self.realized), "merged_pairs": self.merged_pairs}

    @classmethod
    def from_dict(cls, d):
        m = cls()
        for side_name in ("yes", "no"):
            for layer, (q, c) in d[side_name].items():
                getattr(m, side_name)[layer] = Layer(q, c)
        m.realized = dict(d["realized"]); m.merged_pairs = d.get("merged_pairs", 0.0)
        return m


def replay(trades, merges=None):
    """從時序成交重建帳本。trades: [(ts, condition_id, outcome "yes"/"no", qty, price)] 已排序。
    merges: [(ts, condition_id, pairs)] 有實際紀錄就用；沒有 (None) 就推算：每筆成交後兩邊都有就 merge (auto_merge 每輪都做)。
    回傳 {condition_id: MarketLedger}, inferred_merges 數。"""
    led = {}
    inferred = 0

    def apply(L, outc, q, p):
        # q < 0 表示賣出 (taker 出場)
        if q < 0:
            L.sell(outc, -q, p)
        else:
            L.buy(outc, q, p)

    if merges is None:
        for ts, cid, outc, q, p in trades:
            L = led.setdefault(cid, MarketLedger())
            apply(L, outc, q, p)
            k = min(L.qty("yes"), L.qty("no"))
            if k >= 1:
                L.merge(k); inferred += 1
        return led, inferred
    events = sorted([(ts, 0, cid, outc, q, p) for ts, cid, outc, q, p in trades] + [(ts, 1, cid, None, k, None) for ts, cid, k in merges])
    for ts, kind, cid, outc, q, p in events:
        L = led.setdefault(cid, MarketLedger())
        if kind == 0:
            apply(L, outc, q, p)
        else:
            L.merge(q)
    return led, 0


# ---------------------------------------------------------------- 排隊掛價 (v2)

def queue_price(levels, ref, v, tick, size, ahead_x, max_px):
    """躲在別人後面：回 (價格, 前面別人的量) 或 (None, 0)。
    levels: 別人 (已排除自家) 在這個 token 的買單 [(price, size)]；ref: 這個 token 的中價。
    由高到低找第一個價位 p：價格 >= p 的別人掛單總量 >= ahead_x * size (同價位的單先來，時間優先排在我們前面)，
    且 p <= max_px (post-only 不能碰到對面)、離中價 <= v - tick (一定在獎勵範圍內)。
    同價位加入比退到更低一格好：分數較高，前面的量一樣。"""
    need = ahead_x * size
    for p in sorted({round(p, 6) for p, _ in levels}, reverse=True):
        if p > max_px + 1e-9:
            continue
        if ref - p > v - tick + 1e-9:
            break                         # 再往下都超出獎勵範圍
        ahead = sum(s for q, s in levels if q >= p - 1e-9)
        if ahead >= need - 1e-9:
            return p, ahead
    return None, 0.0


# ---------------------------------------------------------------- 清算估值

def liquidation_value(qty, bids):
    """把 qty 股打進 bids [(price, size)] (已排除自家單) 逐檔賣掉：回 (預估所得, 賣不掉的股數)。快照估計，不是保證。"""
    left, proceeds = qty, 0.0
    for p, s in sorted(bids, key=lambda x: -x[0]):
        if left <= 1e-9:
            break
        take = min(left, s)
        proceeds += take * p; left -= take
    return proceeds, max(left, 0.0)


# ---------------------------------------------------------------- markout

MARKOUT_WINDOWS = (60, 300, 1800, 3600)


def ref_reliable(bb, ba, bid_depth, ask_depth, max_spread, min_size):
    """參考價可靠的條件：spread 不超過 max_spread 兩倍，且兩側最佳檔 (排除自家) 都有 min_size 以上。"""
    return (ba - bb) <= 2 * max_spread + 1e-9 and bid_depth >= min_size and ask_depth >= min_size


def markout_signed_qty(outcome, qty):
    """YES 視角的簽名數量：買 YES → +qty；買 NO → −qty。"""
    return qty if outcome == "yes" else -qty


def yes_price(outcome, price):
    return price if outcome == "yes" else 1 - price


def markout_terms(q, p, ref0, ref_t):
    """S = q(ref0 − p) 成交當下帳面優勢；A = q(ref_t − ref0) 成交後移動。任一 ref 為 None 就 None。"""
    if ref0 is None:
        return None, None
    S = q * (ref0 - p)
    A = None if ref_t is None else q * (ref_t - ref0)
    return S, A
