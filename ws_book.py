"""訂單簿 websocket 監看：在背景執行緒維護各 token 的最佳買賣價，價格一動就叫醒主迴圈。

主迴圈原本每 poll_seconds 才看一次簿子，中間市場動了我們就掛在錯價等著被打
(09-20 到 09-22 的虧損大多是這樣來的)。這裡不改報價邏輯，只把「等下一輪」換成
「等下一輪 or 價格動超過門檻就馬上醒」。

    w = BookWatcher(token_ids, move=0.01)
    w.start()
    ...
    w.wait(60)          # 最多睡 60 秒，簿子動超過 move 就提早回來
    w.note_quote(token, price)   # 記下我們這輪掛在哪，之後用它判斷「動多少」
"""
import asyncio
import logging
import threading
import time

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class BookWatcher:
    def __init__(self, token_ids, move=0.01, log=None):
        self.tokens = list(token_ids)
        self.move = move
        self.log = log or (lambda m: None)
        self.best = {}          # token -> (bid, ask, ts)
        self.ref = {}           # token -> 上次主迴圈看到的中價
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.events = 0
        self.wakes = 0
        self.connected = False
        self.last_event_ts = 0.0

    # ---- 主迴圈用
    def start(self):
        self._thread = threading.Thread(target=self._run, name="ws-book", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def set_tokens(self, token_ids):
        """市場增減後重新訂閱 (下一次重連生效)。"""
        new = list(token_ids)
        if set(new) != set(self.tokens):
            self.tokens = new
            self._resubscribe = True

    def snapshot(self, token):
        return self.best.get(token)

    def note_quote(self, token, mid):
        """主迴圈這輪看到的中價，之後拿來比對移動幅度。"""
        if mid is not None:
            self.ref[token] = mid

    def wait(self, seconds):
        """睡到時間到，或簿子動超過 move 就提早醒。回傳是否被叫醒。"""
        self._wake.clear()
        woke = self._wake.wait(seconds)
        if woke:
            self.wakes += 1
        return woke

    def stats(self):
        return {"connected": self.connected, "events": self.events, "wakes": self.wakes,
                "tokens": len(self.tokens), "last_event_age": round(time.time() - self.last_event_ts, 1) if self.last_event_ts else None}

    # ---- 背景執行緒
    def _run(self):
        logging.getLogger("polymarket").setLevel(logging.WARNING)
        while not self._stop.is_set():
            try:
                asyncio.run(self._loop())
            except Exception as e:  # noqa: BLE001
                self.connected = False
                self.log(f"  ws: {type(e).__name__}: {str(e)[:120]}; reconnect in 5s")
                self._stop.wait(5)

    async def _loop(self):
        from polymarket._internal.streams.clob.market import ClobMarketStreamManager
        if not self.tokens:                    # 沒有啟用的市場就不訂閱，等 set_tokens
            self._resubscribe = False
            while not self.tokens and not self._stop.is_set():
                await asyncio.sleep(5)
            if self._stop.is_set():
                return
        mgr = ClobMarketStreamManager(url=WS_URL)
        self._resubscribe = False
        h = await mgr.subscribe(token_ids=self.tokens)
        self.connected = True
        self.log(f"  ws connected, {len(self.tokens)} tokens")
        try:
            async for ev in h:
                if self._stop.is_set() or getattr(self, "_resubscribe", False):
                    break
                self.events += 1
                self.last_event_ts = time.time()
                self._apply(ev)
        finally:
            self.connected = False
            try:
                await h.close()
            finally:
                await mgr.close()

    def _apply(self, ev):
        """book / best_bid_ask 每個事件一個 token；price_change 一個事件含多個 token 的異動。"""
        p = getattr(ev, "payload", None)
        if p is None:
            return
        t = getattr(ev, "type", "")
        if t == "book":
            bids = [float(x.price) for x in (getattr(p, "bids", None) or [])]
            asks = [float(x.price) for x in (getattr(p, "asks", None) or [])]
            self._update(str(p.asset_id), max(bids, default=None), min(asks, default=None))
        elif t == "best_bid_ask":
            self._update(str(p.asset_id), _f(p.best_bid), _f(p.best_ask))
        elif t == "price_change":
            for ch in (getattr(p, "price_changes", None) or []):
                self._update(str(ch.asset_id), _f(ch.best_bid), _f(ch.best_ask))

    def _update(self, token, bb, ba):
        if not token:
            return
        old = self.best.get(token)
        if bb is None or ba is None:          # 只給單邊時沿用上一份快照的另一邊
            if not old:
                return
            bb = old[0] if bb is None else bb
            ba = old[1] if ba is None else ba
        if ba <= bb:                          # 交叉/壞資料不採用
            return
        self.best[token] = (bb, ba, time.time())
        ref = self.ref.get(token)
        if ref is not None and abs((bb + ba) / 2 - ref) >= self.move - 1e-9:
            self._wake.set()


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
