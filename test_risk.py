import sys
sys.path.insert(0, __file__.rsplit('/',1)[0])
from risk import (MarketLedger, cluster_loss_bound, liquidation_value, markout_terms, ref_reliable, replay, worst_loss)


def approx(a, b, eps=1e-6):
    return abs(a - b) < eps


def test_worst_loss_example():
    # 持有 20 YES 成本 0.50；掛買 50 YES @0.48、買 50 NO @0.45
    assert approx(worst_loss(20, 10.0, 0, 0.0, []), 10.0)                       # 都不成交，結算 NO
    assert approx(worst_loss(20, 10.0, 0, 0.0, [("no", 50, 0.45)]), 12.5)      # 只 NO 成交：30 股超額 NO
    assert approx(worst_loss(20, 10.0, 0, 0.0, [("yes", 50, 0.48)]), 34.0)     # 只 YES 成交
    both = worst_loss(20, 10.0, 0, 0.0, [("yes", 50, 0.48), ("no", 50, 0.45)])
    assert approx(both, 34.0)                                                   # 枚舉後最壞仍是只 YES 成交


def test_reducing_order_up_to_net_does_not_add_risk():
    # 持有 20 YES；買 20 NO 完全配對，最壞損失不增加；超額的 NO 夠多 (50) 時最壞情境變成「只 NO 成交、結算 YES」
    base = worst_loss(20, 10.0, 0, 0.0, [])
    assert worst_loss(20, 10.0, 0, 0.0, [("no", 20, 0.45)]) <= base + 1e-9
    assert approx(worst_loss(20, 10.0, 0, 0.0, [("no", 30, 0.45)]), base)   # 超額 30 股：cost 23.5 − 20 = 3.5 < 10，最壞仍是原部位
    assert worst_loss(20, 10.0, 0, 0.0, [("no", 50, 0.45)]) > base           # 超額 50 股：12.5 > 10


def test_cluster_bound_is_sum():
    a = (20, 10.0, 0, 0.0, [("yes", 50, 0.48)])
    b = (0, 0.0, 40, 22.0, [])
    assert approx(cluster_loss_bound([a, b]), worst_loss(*a) + worst_loss(*b))


def test_ledger_time_ordered_cost():
    L = MarketLedger()
    L.buy("yes", 50, 0.53)
    L.buy("no", 50, 0.44)
    L.merge(50)                       # 回款 50，成本 26.5 + 22 = 48.5 → 已實現 +1.5
    assert approx(sum(L.realized.values()), 1.5)
    L.buy("no", 20, 0.60)             # 之後的買入不能改寫上面的已實現
    assert approx(sum(L.realized.values()), 1.5)
    assert approx(L.qty("no"), 20) and approx(L.cost("no"), 12.0)
    u = L.unrealized(0.4, 0.6)
    assert approx(u["new"], 20 * 0.6 - 12.0)


def test_ledger_legacy_first():
    L = MarketLedger()
    L.buy("no", 40, 0.55)
    L.mark_legacy()                   # 觀察期開始：40 NO 是舊部位
    L.buy("yes", 50, 0.38)            # 新買 50 YES
    L.merge(40)                       # 先消耗 legacy NO；新層 YES 也被消耗 40
    assert approx(L.no["legacy"].qty, 0) and approx(L.yes["new"].qty, 10)
    assert approx(L.merged_pairs, 40)
    assert approx(sum(L.realized.values()), 40 - 40 * 0.55 - 40 * 0.38)


def test_replay_inferred_merges():
    trades = [(1, "m", "yes", 30, 0.53), (2, "m", "no", 50, 0.466), (3, "m", "no", 20, 0.513)]
    led, inferred = replay(trades)
    L = led["m"]
    assert inferred == 1 and approx(L.merged_pairs, 30)
    assert approx(L.qty("yes"), 0) and approx(L.qty("no"), 40)
    assert approx(sum(L.realized.values()), 30 - 30 * 0.53 - 30 * 0.466)


def test_liquidation_walk():
    proceeds, left = liquidation_value(120, [(0.39, 50), (0.40, 30), (0.38, 20)])
    assert approx(proceeds, 30 * 0.40 + 50 * 0.39 + 20 * 0.38) and approx(left, 20)


def test_markout_terms_and_reliability():
    # 買 20 YES @0.48，ref0 0.50：ref 不變 S=+0.40 A=0；跌到 0.47 A=−0.60 (S+A = −0.20)
    S, A = markout_terms(20, 0.48, 0.50, 0.50)
    assert approx(S, 0.40) and approx(A, 0.0)
    S, A = markout_terms(20, 0.48, 0.50, 0.47)
    assert approx(S + A, -0.20)
    assert markout_terms(20, 0.48, None, 0.5) == (None, None)
    assert ref_reliable(0.50, 0.52, 20, 20, 0.045, 20)
    assert not ref_reliable(0.40, 0.60, 20, 20, 0.045, 20)   # 太寬
    assert not ref_reliable(0.50, 0.52, 5, 20, 0.045, 20)    # 深度不足


def test_ledger_sell_realizes():
    L = MarketLedger()
    L.buy("yes", 70, 0.30)
    L.sell("yes", 6.13, 0.14); L.sell("yes", 63.87, 0.13)
    assert approx(L.qty("yes"), 0)
    assert approx(sum(L.realized.values()), 6.13 * 0.14 + 63.87 * 0.13 - 21.0)


def test_exit_give_widens_and_stops():
    """v1.3 出場：讓步隨持有時間變大、達停損就讓到底；不看歷史成本錨定。"""
    import rewards_bot as rb
    from datetime import datetime, timezone, timedelta

    class Fake:
        cfg = {"reduce_from_mid": 0.015, "reduce_widen_per_hour": 0.004, "reduce_max_give": 0.06,
               "exit_after_hours": 72, "exit_stop_loss": 0.35, "exit_max_cross": 0.02}
    f = Fake()
    L = MarketLedger(); L.buy("no", 40, 0.50)          # 持 40 NO 成本 0.50
    st = {"net": -40.0, "pos_since": None}
    # 剛建倉、現價還在 0.50 (mid 0.50 → NO 現價 0.50)：只讓 1.5 分
    g, why = rb.Quoter.exit_price_give(f, {}, st, 0.50, L)
    assert approx(g, 0.015) and why == ""
    # 持有 5 小時：1.5 + 5×0.4 = 3.5 分
    st["pos_since"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    g, _ = rb.Quoter.exit_price_give(f, {}, st, 0.50, L)
    assert approx(g, 0.035, 1e-3)
    # 讓步有上限
    st["pos_since"] = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
    g, why = rb.Quoter.exit_price_give(f, {}, st, 0.50, L)
    assert approx(g, 0.06) and why == ""   # 上限 min(reduce_max_give, exit_max_cross*3)
    # NO 現價掉到 0.30 (mid 0.70) = 虧 40% > 停損 35% → 讓到底
    st["pos_since"] = datetime.now(timezone.utc).isoformat()
    g, why = rb.Quoter.exit_price_give(f, {}, st, 0.70, L)
    assert approx(g, 0.02) and "stop-loss" in why   # 強制出場也只讓 exit_max_cross，不吃整個 spread
    # 超過 72 小時也讓到底
    st["pos_since"] = (datetime.now(timezone.utc) - timedelta(hours=80)).isoformat()
    g, why = rb.Quoter.exit_price_give(f, {}, st, 0.50, L)
    assert approx(g, 0.02) and "held" in why


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("ok  ", name)
            except AssertionError as e:
                fails += 1; print("FAIL", name, e)
    sys.exit(1 if fails else 0)