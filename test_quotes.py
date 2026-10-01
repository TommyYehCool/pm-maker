"""python -m pytest test_quotes.py  或  python test_quotes.py"""
from mm_bot import compute_quotes


def test_flat_inventory_symmetric():
    yb, ya, nb, skew = compute_quotes(0.30, 0.40, 0.01, net=0, max_inv=50, hs=0.04)
    assert skew == 0
    assert (yb, ya, nb) == (0.31, 0.39, 0.61)
    assert yb + nb < 1  # 兩邊都成交成本 < 1，差額是利潤


def test_long_yes_skews_down():
    yb, ya, nb, _ = compute_quotes(0.30, 0.40, 0.01, net=25, max_inv=50, hs=0.04)
    assert yb == 0.29 and ya == 0.37 and nb == 0.63


def test_short_yes_skews_up():
    yb, ya, nb, _ = compute_quotes(0.30, 0.40, 0.01, net=-50, max_inv=50, hs=0.04)
    assert yb == 0.35 and ya == 0.43 and nb == 0.57


def test_never_crosses_book():
    # 書很窄 (0.49/0.50)，想掛 ±4c 會穿價，要被夾回 bid<=ask-tick, ask>=bid+tick
    yb, ya, nb, _ = compute_quotes(0.49, 0.50, 0.01, net=0, max_inv=50, hs=0.04)
    assert yb <= 0.49 and ya >= 0.50
    # 極端 skew 也不能穿
    yb, ya, nb, _ = compute_quotes(0.30, 0.40, 0.01, net=-500, max_inv=50, hs=0.04)
    assert yb <= 0.39 and ya >= 0.31


def test_tick_001():
    yb, ya, nb, _ = compute_quotes(0.103, 0.105, 0.001, net=0, max_inv=50, hs=0.02)
    assert (yb, ya, nb) == (0.084, 0.124, 0.876)


def test_join_mode_sits_at_best():
    yb, ya, nb, _ = compute_quotes(0.30, 0.31, 0.01, net=0, max_inv=50, hs=0.04, mode="join")
    assert (yb, ya, nb) == (0.30, 0.31, 0.69)


def test_join_mode_skews_by_ticks_without_crossing():
    yb, ya, nb, _ = compute_quotes(0.30, 0.31, 0.01, net=50, max_inv=50, hs=0.04, mode="join")
    assert yb == 0.28 and ya == 0.31  # 多 YES：bid 退 2 tick；ask 想退但不能低於市場 bid+tick，夾在 0.31
    yb, ya, nb, _ = compute_quotes(0.30, 0.31, 0.01, net=-50, max_inv=50, hs=0.04, mode="join")
    assert yb == 0.30 and ya == 0.33  # 多 NO：ask 上 2 tick；bid 想上但不能高於市場 ask-tick，夾在 0.30


if __name__ == "__main__":
    import sys
    fns = [v for k, v in globals().items() if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok", fn.__name__)
    print(f"{len(fns)} passed")
