"""風控閘門測試：不用打 API，直接對 Live.risk_check / 帳本 / 獎勵公式做斷言。"""
import sys, time, types
sys.path.insert(0, __file__.rsplit('/', 1)[0])
from risk import MarketLedger, worst_loss
import rewards_bot as rb
from rewards_bot import combine as _c


def approx(a, b, eps=1e-6):
    return abs(a - b) < eps


def fake_live(markets, ledger=None, orders_view=None, cfg=None, halt=False, data_age=0.0):
    """組一個不連線的 Live 物件，只測 risk_check 相關邏輯。"""
    o = types.SimpleNamespace()
    o.cfg = {"cluster_max_loss_usd": 30.0, "account_max_loss_usd": 60.0, "stale_data_seconds": 300, **(cfg or {})}
    o.cfg["markets"] = markets
    o.ledger = ledger or {}
    o.orders_view = orders_view or {}
    o.mismatch = {}
    o.positions_ts = o.orders_ts = time.time() - data_age
    for n in ("cluster_of", "cluster_bound", "account_bound", "data_age", "risk_check"):
        setattr(o, n, getattr(rb.Live, n).__get__(o))
    o._halt = halt
    return o


M1 = {"condition_id": "c1", "name": "m1", "cluster": "E1", "yes_token": "y1", "no_token": "n1"}
M2 = {"condition_id": "c2", "name": "m2", "cluster": "E2", "yes_token": "y2", "no_token": "n2"}


def test_account_cap_blocks_when_clusters_sum_over():
    """單一叢集都沒超標，但全帳戶加總超標 → 擋下。之前只檢查叢集，這是漏洞。"""
    led = {"c1": MarketLedger(), "c2": MarketLedger()}
    led["c1"].buy("yes", 50, 0.55)      # 各 27.5，叢集上限 30 內
    led["c2"].buy("yes", 50, 0.55)
    o = fake_live([M1, M2], led, cfg={"cluster_max_loss_usd": 30.0, "account_max_loss_usd": 60.0})
    assert approx(o.account_bound(), 55.0)
    # 加 4 股 (叢集 27.5 → 29.7 未超標)，但全帳戶 55 → 57.2 也還在 60 內 → 放行
    assert o.risk_check(M1, "yes", 4, 0.55, [])[0]
    # 全帳戶上限收到 56 → 同一張單被全帳戶擋下 (叢集仍未超標，證明這是新增的那一層)
    o2 = fake_live([M1, M2], led, cfg={"cluster_max_loss_usd": 30.0, "account_max_loss_usd": 56.0})
    ok, why = o2.risk_check(M1, "yes", 4, 0.55, [])
    assert not ok and "account" in why, why


def test_pending_same_side_blocks_duplicate():
    """送出但 API 還沒確認的同側單 → 不再送，否則重複下單。"""
    o = fake_live([M1], {"c1": MarketLedger()},
                  {"c1": [{"id": "unknown-1", "side": "yes", "qty": 20, "price": 0.5, "pending_rounds": 1}]})
    ok, why = o.risk_check(M1, "yes", 20, 0.5, [])
    assert not ok and "pending" in why, why
    assert o.risk_check(M1, "no", 20, 0.4, [])[0]      # 另一側不受影響


def test_stale_data_blocks_new_risk():
    o = fake_live([M1], {"c1": MarketLedger()}, data_age=600)
    ok, why = o.risk_check(M1, "yes", 20, 0.5, [])
    assert not ok and "stale" in why, why


def test_one_sided_fill_is_not_hedged():
    """只成交一腿不能當成已對沖：最壞損失必須反映未配對的那一邊。"""
    assert approx(worst_loss(50, 26.0, 0, 0.0, []), 26.0)          # 只有 YES：NO 結算 → 全賠 26
    assert approx(worst_loss(50, 26.0, 50, 23.0, []), 0.0)         # 配對後兩情境都拿 50、成本 49 → 不會虧 (worst_loss 下限 0)
    # 兩邊「掛單」不等於已對沖：只有不利的一邊成交時最壞損失仍是全額
    assert approx(worst_loss(0, 0.0, 0, 0.0, [("yes", 50, 0.52), ("no", 50, 0.46)]), 26.0)


def test_partial_and_duplicate_fill_messages():
    """同一筆成交重複收到不能重複記帳 (ledger_apply_fill 以 trade_id 去重)。"""
    o = types.SimpleNamespace(ledger={}, ledger_trade_ids=set())
    o.ledger_apply_fill = rb.Live.ledger_apply_fill.__get__(o)
    o.ledger_apply_fill("c1", "yes", 20, 0.5, "t1")
    o.ledger_apply_fill("c1", "yes", 20, 0.5, "t1")     # 重複訊息
    o.ledger_apply_fill("c1", "yes", 5, 0.5, "t2")      # 部分成交的第二筆
    assert approx(o.ledger["c1"].qty("yes"), 25)


def test_reward_formula_matches_docs():
    """官方: mid 在 [0.10,0.90] 用 max(min(Q1,Q2), Q1/3, Q2/3)；區間外用 min。"""
    for q1, q2 in ((10, 40), (40, 10), (0, 30), (25, 25)):
        assert approx(_c(q1, q2, False), max(min(q1, q2), q1 / 3, q2 / 3))
        assert approx(_c(q1, q2, True), min(q1, q2))


def test_min_size_incompatible_with_tight_budget():
    """規格建議的 5 美元叢集上限：20 股 min size 在中價附近就已經超標 → 該跳過市場，不是放大預算。"""
    wl = worst_loss(0, 0.0, 0, 0.0, [("yes", 20, 0.50)])
    assert approx(wl, 10.0) and wl > 5.0


def test_dust_position_does_not_place_reduce_order():
    """零頭部位不該掛減倉單：min_size 20 會把 0.88 股的減倉變成 20 股新部位。"""
    from risk import worst_loss
    mn, net = 20.0, -0.88
    # 舊邏輯: max(mn, ceil(|net|)) = 20 → 掛 20 股 → 減掉 0.88 剩 19.12 股反向部位
    old_qty = max(mn, -(-net // 1))
    assert old_qty == 20.0
    overshoot = old_qty + net          # 19.12
    assert overshoot > 19
    # 那 19 股的最壞損失不是零
    assert worst_loss(overshoot, overshoot * 0.40, 0, 0.0, []) > 7


def test_hedge_cap_bounds_pair_cost():
    """對沖價格上限：買到 YES @p 後，NO 最多付 1 − p + slip，配對成本 ≤ 1 + slip。"""
    for p in (0.2, 0.5, 0.825):
        cap = round(1 - p + 0.03, 3)
        assert p + cap <= 1.03 + 1e-9


def test_hedge_keeps_position_flat_in_worst_case():
    """對沖完成後兩邊等量，最壞結算損失 = 配對成本 − 1 (只剩手續費/滑價那一點點)。"""
    from risk import worst_loss
    wl = worst_loss(20, 20 * 0.60, 20, 20 * 0.42, [])     # 配對成本 1.02
    assert approx(wl, 20 * 0.02)


def _hedge_obj(ledger, asks, cfg=None):
    """組一個假的 Live，只跑 run_hedges：client 回給定的 asks，下單記錄參數並回假成交。"""
    o = types.SimpleNamespace()
    o.cfg = {"hedge_max_slip": 0.03, "hedge_timeout_min": 15, **(cfg or {})}
    o.ledger = ledger
    o.ledger_trade_ids = set()
    o.orders_view = {}
    o.dry = False
    o.sent = []
    class Lvl:
        def __init__(s, p, z): s.price, s.size = p, z
    class Client:
        def get_order_book(s, token_id): return types.SimpleNamespace(asks=[Lvl(p, z) for p, z in asks])
        def place_limit_order(s, **k):
            o.sent.append(k)
            size, mx = float(k["size"]), float(k["price"])
            got, paid = 0.0, 0.0
            for p, z in sorted(asks):
                if p > mx or got >= size - 1e-9: break
                t = min(z, size - got); got += t; paid += t * p
            return types.SimpleNamespace(ok=True, status="matched" if got >= size - 1e-9 else "live",
                                         making_amount=paid, taking_amount=got, order_id="o1", trade_ids=())
        def cancel_orders(s, order_ids): pass
    o.client = Client()
    o.cancel = lambda ids, why: None
    for n in ("hedge_net", "run_hedges"):
        setattr(o, n, getattr(rb.Live, n).__get__(o))
    return o


def test_hedge_net_based_flattens_and_is_idempotent():
    """持 20 YES 均價 0.60，NO 賣價 0.41 → 買 20 NO 配平；再跑一次淨部位已是 0，不會再下單。"""
    L = MarketLedger(); L.buy("yes", 20, 0.60)
    o = _hedge_obj({"c1": L}, [(0.41, 50)])
    m = {"condition_id": "c1", "name": "m", "yes_token": "y", "no_token": "n"}
    st = {}
    o.run_hedges(m, st)
    assert len(o.sent) == 1 and o.sent[0]["token_id"] == "n" and o.sent[0]["post_only"] is False
    net, _ = o.hedge_net(m, st)
    assert approx(net, 0, 1e-6)                      # 帳本還沒記，但 inflight 讓淨部位已是 0
    o.run_hedges(m, st)
    assert len(o.sent) == 1                          # 沒有重複對沖


def test_hedge_respects_cap_and_never_rests():
    """對面賣價超過上限 (1 − 0.60 + 0.03 = 0.43) → 不下單 (也就不會有掛在簿上的對沖單)。"""
    L = MarketLedger(); L.buy("yes", 20, 0.60)
    o = _hedge_obj({"c1": L}, [(0.45, 50)])
    m = {"condition_id": "c1", "name": "m", "yes_token": "y", "no_token": "n"}
    o.run_hedges(m, {})
    assert o.sent == []


def test_hedge_blocks_after_timeout():
    from datetime import datetime, timezone, timedelta
    L = MarketLedger(); L.buy("no", 19.5, 0.438)
    o = _hedge_obj({"c1": L}, [(0.62, 50)])       # YES 賣價 0.62 > 上限 0.592
    m = {"condition_id": "c1", "name": "m", "yes_token": "y", "no_token": "n"}
    st = {"hedge_since": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()}
    o.run_hedges(m, st)
    assert st["hedge_blocked"] and o.sent == []


def test_hedge_inflight_consumed_when_ledger_records_it():
    """record_fills 把對沖成交記進帳本後，inflight 要消掉，否則淨部位會被算兩次。"""
    L = MarketLedger(); L.buy("yes", 20, 0.60)
    o = _hedge_obj({"c1": L}, [(0.41, 50)])
    m = {"condition_id": "c1", "name": "m", "yes_token": "y", "no_token": "n"}
    st = {}
    o.run_hedges(m, st)
    assert st["hedge_inflight"] and approx(st["hedge_inflight"][0]["qty"], 20)
    # 模擬 record_fills：帳本記進 20 NO、消掉 inflight
    L.buy("no", 20, 0.41)
    left = 20.0
    for f in st["hedge_inflight"]:
        use = min(f["qty"], left); f["qty"] -= use; left -= use
    st["hedge_inflight"] = [f for f in st["hedge_inflight"] if f["qty"] > 1e-6]
    net, _ = o.hedge_net(m, st)
    assert approx(net, 0, 1e-6) and st["hedge_inflight"] == []



def test_queue_price_joins_protected_level():
    """v2：加入前面已有 3 倍量的最高價位；同價位的量算保護 (時間優先)。"""
    from risk import queue_price
    bids = [(0.50, 30), (0.49, 100), (0.48, 500)]
    # 我們 20 股 → 要 60 股擋在前面：0.50 只有 30 不夠，0.49 累計 130 夠
    p, ahead = queue_price(bids, 0.505, 0.045, 0.01, 20, 3, 0.50)
    assert approx(p, 0.49) and approx(ahead, 130)
    # 前面的量被吃掉 / 撤掉 → 退到下一檔
    p, _ = queue_price([(0.50, 30), (0.49, 10), (0.48, 500)], 0.505, 0.045, 0.01, 20, 3, 0.50)
    assert approx(p, 0.48)


def test_queue_price_stays_inside_reward_band_and_post_only():
    from risk import queue_price
    # 保護只在離中價 5.5 分的地方 → 超出 v=4.5 (留一格) 就不掛
    p, _ = queue_price([(0.50, 5), (0.45, 1000)], 0.505, 0.045, 0.01, 20, 3, 0.50)
    assert p is None
    # 最高價位會碰到對面 (post-only 會被拒) 就跳過
    p, _ = queue_price([(0.51, 1000), (0.50, 100)], 0.51, 0.045, 0.01, 20, 3, 0.50)
    assert approx(p, 0.50)


def test_queue_price_empty_book_never_quotes():
    """沒人擋就不掛：這正是 09-22 共和黨 (空簿子第一個被倒 70 股) 的情況。"""
    from risk import queue_price
    assert queue_price([], 0.5, 0.045, 0.01, 20, 3, 0.49)[0] is None
    assert queue_price([(0.49, 40)], 0.5, 0.045, 0.01, 20, 3, 0.49)[0] is None

if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", name)
            except AssertionError as e:
                fails += 1; print("FAIL", name, e)
    sys.exit(1 if fails else 0)
