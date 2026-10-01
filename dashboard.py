#!/usr/bin/env python3
"""pm-maker 儀表板：只讀 out/status.json、out/fills.jsonl、out/bot.log，不打任何 API。

  python dashboard.py            # http://127.0.0.1:8787
  python dashboard.py --port 9000
  GET /            HTML，30 秒自動刷新
  GET /api/status  status.json + 最近成交 + log 尾巴
"""
import argparse
import html
import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
STATUS_PATH = os.path.join(OUT_DIR, "status.json")
FILLS_PATH = os.path.join(OUT_DIR, "fills.jsonl")
LOG_PATH = os.path.join(OUT_DIR, "bot.log")
REWARDS_STATUS_PATH = os.path.join(OUT_DIR, "rewards_status.json")
REWARDS_EVENTS_PATH = os.path.join(OUT_DIR, "rewards_events.jsonl")
REWARDS_EVENTS_OLD = os.path.join(OUT_DIR, "rewards_shadow.jsonl")
DEPOSITS = 320.0


def load_status():
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_fills(n=50):
    try:
        with open(FILLS_PATH, encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()][::-1]
    except OSError:
        return []


def tail_log(n=40):
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except OSError:
        return []


def age_seconds(iso):
    try:
        return int((datetime.now() - datetime.fromisoformat(iso)).total_seconds())
    except (TypeError, ValueError):
        return None


CSS = """
:root{--bg:#0f1115;--card:#181b22;--line:#262a33;--fg:#e6e6e6;--dim:#8b919c;--ok:#4cc38a;--warn:#f0b429;--bad:#f25f5c;--acc:#5b8def}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans TC",sans-serif;padding:16px}
h1{font-size:18px;margin:0 0 4px}h2{font-size:14px;color:var(--dim);margin:20px 0 8px;text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--dim);font-size:12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}.tile .k{color:var(--dim);font-size:11px}.tile .v{font-size:20px;font-weight:600;margin-top:2px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.mk{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:10px}.mk .t{font-weight:600;margin-bottom:6px}
.row{display:flex;flex-wrap:wrap;gap:14px;font-size:13px}.row span b{color:var(--dim);font-weight:normal;margin-right:4px}
.book{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;margin-top:6px}
.book .me{color:var(--acc)}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}th{color:var(--dim);font-weight:normal}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;font-size:12px;overflow-x:auto;max-height:360px;white-space:pre-wrap;word-break:break-all}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;background:#232733;color:var(--dim)}
@media(max-width:480px){body{padding:10px}.tile .v{font-size:17px}}
"""


def fmt(x, nd=2):
    return "-" if x is None else f"{x:.{nd}f}"


def render():
    st = load_status()
    fills = load_fills()
    logs = tail_log()
    if not st:
        body = "<h1>pm-maker</h1><p class=bad>還沒有 out/status.json，bot 可能還沒跑完第一輪。</p>"
        return page(body)

    age = age_seconds(st["updated_at"])
    stale = age is None or age > 3 * (st.get("poll_seconds") or 20)
    alive = f'<span class="{"bad" if stale else "ok"}">{"STALE" if stale else "LIVE"}</span> {age}s 前更新'
    mode = '<span class="tag">DRY RUN</span>' if st.get("dry_run") else '<span class="tag" style="color:var(--ok)">LIVE</span>'
    total_pnl = sum((m.get("yes_pnl") or 0) + (m.get("no_pnl") or 0) for m in st["markets"])
    pnl_cls = "ok" if total_pnl > 0 else ("bad" if total_pnl < 0 else "")
    # 持倉市值：YES 用 mid、NO 用 1-mid 估
    pos_value = sum((m.get("yes_pos") or 0) * (m.get("mid") or 0) + (m.get("no_pos") or 0) * (1 - (m.get("mid") or 0))
                    for m in st["markets"] if m.get("state") == "quoting")
    equity = (st.get("balance") or 0) + pos_value

    tiles = f"""
<div class=grid>
 <div class=tile><div class=k>現金 (pUSD)</div><div class=v>{fmt(st.get('balance'))}</div></div>
 <div class=tile><div class=k>持倉市值</div><div class=v>{fmt(pos_value)}</div></div>
 <div class=tile><div class=k>總資產</div><div class=v>{fmt(equity)}</div></div>
 <div class=tile><div class=k>曝險 / 上限</div><div class=v>{fmt(st.get('exposure'))} <span class=sub>/ {st.get('exposure_cap')}</span></div></div>
 <div class=tile><div class=k>未實現損益</div><div class=v><span class="{pnl_cls}">{total_pnl:+.2f}</span></div></div>
 <div class=tile><div class=k>成交筆數 (bot 啟動後)</div><div class=v>{st.get('fills_total', 0)}</div></div>
 <div class=tile><div class=k>市場數</div><div class=v>{len(st['markets'])}</div></div>
</div>"""

    mks = []
    for m in st["markets"]:
        name = html.escape(m["name"])
        if m.get("state") != "quoting":
            mks.append(f'<div class=mk><div class=t>{name}</div><span class="warn">{m.get("state")}</span> {html.escape(str(m.get("error", "")))}</div>')
            continue
        orders = m.get("orders") or []
        order_lines = "".join(
            f'<span class=me>掛 {o["side"]} {html.escape(str(o["outcome"] or ""))} {o["price"]:.3f} × {o["remaining"]:.0f}'
            + (f' (已成交 {o["matched"]:.0f})' if o["matched"] else "") + "</span><br>"
            for o in orders) or '<span class=warn>沒有掛單</span>'
        net = m["net"]
        net_cls = "warn" if net else ""
        mks.append(f"""
<div class=mk>
 <div class=t>{name} <span class=sub>剩 {m['days_left']} 天</span></div>
 <div class=row>
  <span><b>市場</b>{m['best_bid']:.3f} / {m['best_ask']:.3f} <span class=sub>mid {m['mid']:.3f}</span></span>
  <span><b>我方報價</b>YES {m['quote_yes']} / NO {m['quote_no']} <span class=sub>(=YES ask {m['yes_ask_equiv']})</span></span>
  <span><b>持倉</b>YES {m['yes_pos']:.0f} / NO {m['no_pos']:.0f} <span class="{net_cls}">net {net:+.0f}</span> <span class=sub>skew {m['skew']:+.3f}</span></span>
  <span><b>損益</b>{(m.get('yes_pnl') or 0) + (m.get('no_pnl') or 0):+.2f}</span>
 </div>
 <div class=book>{order_lines}</div>
</div>""")

    if fills:
        rows = "".join(
            f"<tr><td>{html.escape(str(f.get('matched_at', ''))[:19])}</td><td>{f.get('my_side', f.get('side'))}</td><td>{html.escape(str(f.get('my_outcome') or f.get('outcome') or ''))}</td>"
            f"<td>{f.get('my_size', f.get('size'))}</td><td>{f.get('my_price', f.get('price'))}</td><td>{f.get('trader_side')}</td><td>{f.get('status')}</td></tr>"
            for f in fills)
        fills_html = f"<table><tr><th>時間</th><th>方向</th><th>outcome</th><th>股數</th><th>價格</th><th>角色</th><th>狀態</th></tr>{rows}</table>"
    else:
        fills_html = '<p class=sub>還沒有成交。</p>'

    body = f"""
<h1>pm-maker {mode} <a href="/rewards" class=tag style="text-decoration:none">rewards →</a></h1>
<div class=sub>{alive} · 錢包 {html.escape(str(st.get('wallet')))} · bot 啟動 {st.get('started_at')} · 每 {st.get('poll_seconds')}s 一輪</div>
{tiles}
<h2>市場</h2>{''.join(mks)}
<h2>成交</h2>{fills_html}
<h2>Log</h2><pre>{html.escape(chr(10).join(logs))}</pre>
"""
    return page(body)


def render_rewards():
    try:
        st = json.load(open(REWARDS_STATUS_PATH, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return page("<h1>rewards shadow</h1><p class=bad>還沒有 rewards_status.json</p>")
    age = age_seconds(st["updated_at"])
    stale = age is None or age > 240
    alive = f'<span class="{"bad" if stale else "ok"}">{"STALE" if stale else "LIVE"}</span> {age}s 前更新'
    net_est = st["reward_accum"] + st["mtm_pnl"]
    live = st.get("mode") == "live"
    act = st.get("actual") or {}
    est_today = (st.get("est_by_day") or {}).get(act.get("date") or "", 0.0)
    live_tiles = ""
    if live:
        y = act.get("yesterday") or {}
        y_txt = f"{y['actual']:.3f} <span class=sub>/ 估 {y['est']:.3f}</span>" if y else "<span class=sub>還沒跨日</span>"
        live_tiles = f"""
 <div class=tile><div class=k>現金 pUSD</div><div class=v>{(st.get('balance') or 0):.2f}</div></div>
 <div class=tile><div class=k>曝險 / 上限</div><div class=v>{st.get('exposure', 0):.2f} <span class=sub>/ {st['capital']:.0f}</span></div></div>
 <div class=tile><div class=k>今日實際獎勵 (Polymarket 算)</div><div class=v class=ok>{act.get('today', 0):.4f} <span class=sub>/ 估 {est_today:.3f}</span></div></div>
 <div class=tile><div class=k>昨日實際 / 估</div><div class=v>{y_txt}</div></div>"""
    tiles = f"""<div class=grid>
 <div class=tile><div class=k>跑了</div><div class=v>{st['hours']:.1f} h</div></div>
 <div class=tile><div class=k>累計獎勵 (估)</div><div class=v class=ok>{st['reward_accum']:.3f}</div></div>
 <div class=tile><div class=k>目前獎勵速率 (估)</div><div class=v>{st['reward_rate_day']:.2f} <span class=sub>/天</span></div></div>
 <div class=tile><div class=k>成交 mark-to-mid</div><div class=v><span class="{'ok' if st['mtm_pnl']>0 else ('bad' if st['mtm_pnl']<0 else '')}">{st['mtm_pnl']:+.3f}</span></div></div>
 <div class=tile><div class=k>獎勵 + 損益</div><div class=v><span class="{'ok' if net_est>0 else ('bad' if net_est<0 else '')}">{net_est:+.3f}</span></div></div>
 <div class=tile><div class=k>成交筆數</div><div class=v>{st['fills']}</div></div>
 <div class=tile><div class=k>{'資金上限' if live else '假設本金'}</div><div class=v>{st['capital']:.0f}</div></div>{live_tiles}
</div>"""
    perf_html = ""
    pf = st.get("perf")
    if pf:
        cl = st.get("clusters") or {}
        cl_html = "".join(f"<tr><td>{html.escape(k[:60])}</td><td>{v:.2f}</td><td>{'<span class=bad>超標</span>' if v > st.get('cluster_cap', 1e9) else ''}</td></tr>" for k, v in cl.items())
        r, u = pf["realized"], pf["unrealized"]
        res = pf["residual"] if pf["residual"] is not None else 0.0
        dp = pf.get("day_pnl_cost_basis") or 0.0
        perf_html = f"""<h2>成本基礎帳 (v1.1) <span class=sub>入金基準 {DEPOSITS:.0f} · 觀察期起 {html.escape(str(pf.get('observation_start','')))[:16]} UTC</span></h2>
<div class=grid>
 <div class=tile><div class=k>淨資產 (標記)</div><div class=v>{pf['equity']:.2f}</div></div>
 <div class=tile><div class=k>清算估值 (快照)</div><div class=v>{(pf['cash'] or 0) + pf['liq_value']:.2f} <span class=sub>持倉 {pf['liq_value']:.2f}</span></div></div>
 <div class=tile><div class=k>已實現 舊/新</div><div class=v>{r['legacy']:+.2f} / {r['new']:+.2f}</div></div>
 <div class=tile><div class=k>未實現 舊/新</div><div class=v>{u['legacy']:+.2f} / {u['new']:+.2f}</div></div>
 <div class=tile><div class=k>獎勵 已入帳 / 待入帳</div><div class=v>{(pf.get('rewards_paid_confirmed') or 0):.2f} / {(pf.get('rewards_earned_unpaid') or 0):.2f}</div></div>
 <div class=tile><div class=k>Maker 回饋已入帳</div><div class=v>{(pf.get('rebates_paid') or 0):.2f}</div></div>
 <div class=tile><div class=k>人工部位 (不歸 bot)</div><div class=v>{(pf.get('manual') or {}).get('value', 0):.2f} <span class=sub>成本 {(pf.get('manual') or {}).get('outlay', 0):.2f} · {(pf.get('manual') or {}).get('pnl', 0):+.2f}</span></div></div>
 <div class=tile><div class=k>帳戶總值</div><div class=v>{(pf.get('account_total') or 0):.2f}</div></div>
 <div class=tile><div class=k>未解釋殘差</div><div class=v><span class="{'warn' if abs(res) > 0.5 else ''}">{res:+.2f}</span></div></div>
 <div class=tile><div class=k>當日成本基礎損益</div><div class=v><span class="{'bad' if dp < 0 else ''}">{dp:+.2f}</span> {'<span class=bad>HALT</span>' if pf.get('halt') else ''}</div></div>
</div>
<table><tr><th>事件叢集</th><th>最壞損失上界</th><th>上限 {st.get('cluster_cap', 0):.0f}</th></tr>{cl_html}</table>"""
    mks = []
    for m in st["markets"]:
        name = html.escape(m["name"])
        if m.get("state") != "quoting":
            mks.append(f'<div class=mk><div class=t>{name}</div><span class=warn>{m.get("state")}</span> {html.escape(str(m.get("error","")))}</div>')
            continue
        mks.append(f"""<div class=mk><div class=t>{name} <span class=sub>剩 {m['days_left']} 天 · 池 ${m['pool']:.0f}/天</span></div>
 <div class=row>
  <span><b>市場</b>{m['best'][0]:.3f} / {m['best'][1]:.3f} <span class=sub>mid {m['mid']:.3f}</span></span>
  <span><b>我方</b>YES bid {m['yes_bid']} / ask {m['yes_ask']} × {m['size']:.0f}{' <span class=warn>PAUSED</span>' if m.get('paused') else ''}</span>
  <span><b>分數佔比</b>{m['share']*100:.1f}% <span class=sub>(現有 Q {m['q_existing']})</span> → {m['reward_rate_day']:.2f}/天</span>
  <span><b>累計獎勵</b>{m['reward_accum']:.3f}</span>
  <span><b>庫存</b><span class="{'warn' if m['net'] else ''}">net {m['net']:+.0f}</span> 成交 {m['fills']} 損益 {m['mtm_pnl']:+.3f}{f" 實際獎勵 {m['actual_today']:.4f}" if 'actual_today' in m else ''}</span>
 </div></div>""")
    ev = []
    for path in (REWARDS_EVENTS_PATH, REWARDS_EVENTS_OLD):
        try:
            with open(path, encoding="utf-8") as f:
                ev = [json.loads(x) for x in f.readlines()[-30:] if x.strip()][::-1]
            break
        except OSError:
            continue
    ev_html = "".join(f"<tr><td>{html.escape(str(e.get('type')))}</td><td>{html.escape(json.dumps({k: v for k, v in e.items() if k != 'type'}, ensure_ascii=False))[:220]}</td></tr>" for e in ev)
    mode_tag = ("LIVE (dry)" if st.get("dry_run") else "LIVE") if live else "SHADOW"
    body = f"""<h1>pm-maker rewards <span class=tag>{mode_tag}</span></h1>
<div class=sub>{alive} · 開始 {st['started_at']} · <a href="/" style="color:var(--acc)">回做市 bot</a></div>
{tiles}{perf_html}<h2>市場</h2>{''.join(mks)}
<h2>事件 (成交 / 被打後 1 小時價格 / 跳價 / 每小時摘要 / 每日對帳)</h2><table><tr><th>type</th><th>detail</th></tr>{ev_html}</table>"""
    return page(body)


def page(body):
    return f"""<!doctype html><html lang=zh-Hant><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=30><title>pm-maker</title><style>{CSS}</style></head><body>{body}</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/status"):
            payload = {"status": load_status(), "fills": load_fills(), "log": tail_log()}
            data = json.dumps(payload, ensure_ascii=False).encode()
            ctype = "application/json; charset=utf-8"
        elif self.path in ("/", "/index.html"):
            data = render().encode()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/rewards"):
            data = render_rewards().encode()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):  # 安靜
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    a = p.parse_args()
    print(f"dashboard on http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
