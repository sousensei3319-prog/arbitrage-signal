"""
Smart Money Report v2 (④スマートマネー追跡 — 週次/デイリー ダイジェスト)

追跡中の人間系ウォレットの約定を取り直し、**今回窓 vs 前回窓の比較**で
「何が伸び、何が減ったか」を定期配信する。

  週次: WINDOW_DAYS=7 (毎週月曜)  直近7日 vs その前7日
  日次: WINDOW_DAYS=1 (毎日)      直近24h vs その前24h

配信内容 (チャート3枚をembedで添付):
  [1] 比較4枚組: ①急上昇/減少(今回vs前回のペア棒) ②主戦場(ペア棒)
      ③稼ぎ頭/損失源(今回PnL、前回を灰色で併記) ④日別代金(判定窓の区切り線)
  [2] 時間帯×銘柄ヒートマップ (今回窓。いつ動いたか)
  [3] 現在ポジション (tracker stateから。銘柄別ネット$と保有者数)

自動解釈コメント (ユーザー要望):
  全体活動の増減% と 増加/減少銘柄数(breadth) を出し、
  「全体縮小の中で少数銘柄だけ急伸 = 資金集中/ファンダ発生の可能性」等を機械判定。

出力: data/smart_money/attention_screen.csv (コミットバック) + Discord embed
依存: 標準ライブラリのみで動作 (matplotlibがあればチャート付き)
実行: python smart_money_report.py  (Webhook未設定ならDRY-RUN)
"""

import csv
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# urlopenのtimeout引数が効かない経路 (DNS等) への保険。ジョブハング防止
socket.setdefaulttimeout(35)

JST = timezone(timedelta(hours=9))

# ============================================================
# Config (環境変数で調整可)
# ============================================================
DISCORD_WEBHOOK_URL = (os.environ.get("SMART_MONEY_WEBHOOK_URL")
                       or os.environ.get("DISCORD_WEBHOOK_URL", ""))
MENTION_EVERYONE = os.environ.get("MENTION_EVERYONE", "0") == "1"
WINDOW_DAYS  = float(os.environ.get("REPORT_WINDOW_DAYS") or "7")  # 比較窓(日)
REPORT_N     = int(os.environ.get("REPORT_N") or "200")            # 対象人数上限
MIN_RISE_USD = float(os.environ.get("MIN_RISE_USD")
                     or ("1000000" if WINDOW_DAYS >= 7 else "250000"))
DEADLINE_MIN = float(os.environ.get("REPORT_DEADLINE_MIN") or "12")
TRACKED_FILE = os.environ.get("TRACKED_FILE") or "data/smart_money/tracked_addresses.csv"
VIP_FILE     = os.environ.get("VIP_FILE") or "data/smart_money/vip_addresses.csv"
STATE_FILE   = os.environ.get("SM_STATE_FILE") or "smart_money_state.json"
OUT_CSV      = os.environ.get("OUT_CSV") or "data/smart_money/attention_screen.csv"

LABEL = "週次" if WINDOW_DAYS >= 7 else "デイリー"
CUR_L, PREV_L = ("今週", "前週") if WINDOW_DAYS >= 7 else ("今日", "前日")

HL_INFO = "https://api.hyperliquid.xyz/info"
HL_WEIGHT_PER_MIN = 1000.0
BLUE, RED, GRAY, GREEN = "#2a78d6", "#e34948", "#c3c2b7", "#0ca30c"


def _post_hl(body, timeout=30, retries=3):
    data = json.dumps(body).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request(
                HL_INFO, data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "arb-signal/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(2 ** (i + 1))


def load_humans():
    """追跡リストから非Bot (人間系) を読む。VIPは必ず含める。"""
    addrs = []
    if os.path.exists(VIP_FILE):
        with open(VIP_FILE, newline="", encoding="utf-8") as f:
            addrs += [r["address"] for r in csv.DictReader(f)]
    if os.path.exists(TRACKED_FILE):
        with open(TRACKED_FILE, newline="", encoding="utf-8") as f:
            addrs += [r["address"] for r in csv.DictReader(f)
                      if r.get("is_bot") != "1"]
    addrs = list(dict.fromkeys(addrs))[:REPORT_N]
    if not addrs:
        print("追跡リストなし。先に smart-money.yml を実行すること。")
        sys.exit(0)
    return addrs


def fetch_fills(addrs):
    """直近 2*WINDOW_DAYS 日の約定。DEADLINE_MIN分超で打ち切り (ハング防止)。"""
    start_ms = int((time.time() - 2 * WINDOW_DAYS * 86400) * 1000)
    sleep_s = 20.0 / HL_WEIGHT_PER_MIN * 60.0
    deadline = time.time() + DEADLINE_MIN * 60
    rows, fail = [], 0
    for i, a in enumerate(addrs, 1):
        if time.time() > deadline:
            print(f"  ⚠️ デッドライン({DEADLINE_MIN:.0f}分)超過 — {i-1}人分で打ち切り")
            break
        try:
            fills = _post_hl({"type": "userFillsByTime", "user": a,
                              "startTime": start_ms, "aggregateByTime": True})
            for f in fills or []:
                rows.append((a, f.get("time", 0), f.get("coin", ""),
                             f.get("side", ""), float(f.get("px") or 0),
                             float(f.get("sz") or 0),
                             float(f.get("closedPnl") or 0)))
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(addrs)}] {a[:10]}.. fail: {type(e).__name__}")
        if i % 25 == 0:
            print(f"  [{i}/{len(addrs)}] fills={len(rows)}", flush=True)
        time.sleep(sleep_s)
    print(f"fills: {len(rows)}件 (fail {fail}人)")
    return rows


# ============================================================
# 集計 (今回窓 vs 前回窓)
# ============================================================

def analyze(rows):
    now_ms = time.time() * 1000
    W = WINDOW_DAYS * 86400 * 1000
    cur, prev = {}, {}
    daily = {}   # 日付 -> 代金
    hours = {}   # (coin, hour_jst) -> 代金 (今回窓)
    for a, t, coin, side, px, sz, pnl in rows:
        ntl = px * sz
        dt = datetime.fromtimestamp(t / 1000, JST)
        daily[dt.strftime("%m-%d")] = daily.get(dt.strftime("%m-%d"), 0) + ntl
        if t >= now_ms - W:
            c = cur.setdefault(coin, {"ntl": 0.0, "pnl": 0.0, "w": set()})
            c["ntl"] += ntl
            c["pnl"] += pnl
            c["w"].add(a)
            hours[(coin, dt.hour)] = hours.get((coin, dt.hour), 0) + ntl
        elif t >= now_ms - 2 * W:
            p = prev.setdefault(coin, {"ntl": 0.0, "pnl": 0.0, "w": set()})
            p["ntl"] += ntl
            p["pnl"] += pnl
            p["w"].add(a)

    def prev_ntl(c):
        return prev.get(c, {}).get("ntl", 0.0)

    # 急上昇: 今回MIN以上 かつ 前回比1.3x以上 (前回ゼロ=新規)
    risers = []
    for coin, c in cur.items():
        if c["ntl"] < MIN_RISE_USD:
            continue
        pv = prev_ntl(coin)
        ratio = (c["ntl"] + 1) / (pv + 1)
        if ratio >= 1.3:
            risers.append((coin, c["ntl"], pv, ratio, len(c["w"])))
    risers.sort(key=lambda r: -r[3])

    # 減少: 前回MIN以上 かつ 今回が前回の2/3未満 (手仕舞い検知)
    fallers = []
    for coin, p in prev.items():
        if p["ntl"] < MIN_RISE_USD:
            continue
        cv = cur.get(coin, {}).get("ntl", 0.0)
        if cv < p["ntl"] * 0.67:
            fallers.append((coin, cv, p["ntl"], (cv + 1) / (p["ntl"] + 1)))
    fallers.sort(key=lambda r: r[3])

    total_cur = sum(c["ntl"] for c in cur.values())
    total_prev = sum(p["ntl"] for p in prev.values())
    xyz_cur = sum(c["ntl"] for k, c in cur.items() if str(k).startswith("xyz:"))
    xyz_prev = sum(p["ntl"] for k, p in prev.items() if str(k).startswith("xyz:"))

    top_ntl = sorted(cur.items(), key=lambda kv: -kv[1]["ntl"])[:8]
    top_pnl = sorted(cur.items(), key=lambda kv: -abs(kv[1]["pnl"]))[:8]

    return {"cur": cur, "prev": prev, "risers": risers, "fallers": fallers,
            "top_ntl": top_ntl, "top_pnl": top_pnl,
            "total_cur": total_cur, "total_prev": total_prev,
            "xyz_share_cur": xyz_cur / total_cur * 100 if total_cur else 0,
            "xyz_share_prev": xyz_prev / total_prev * 100 if total_prev else 0,
            "daily": dict(sorted(daily.items())), "hours": hours}


def interpret(res):
    """増減の自動解釈 (ユーザー要望: 全体縮小×少数急伸=資金集中の検知など)"""
    chg = (res["total_cur"] / res["total_prev"] - 1) * 100 if res["total_prev"] else 0
    n_up, n_down = len(res["risers"]), len(res["fallers"])
    lines = [f"全体活動: ${res['total_cur']/1e6:,.0f}M "
             f"({PREV_L}${res['total_prev']/1e6:,.0f}M, **{chg:+.0f}%**) / "
             f"増加{n_up}銘柄・減少{n_down}銘柄 / "
             f"株式perpシェア {res['xyz_share_prev']:.0f}%→{res['xyz_share_cur']:.0f}%"]
    if chg < -20 and 1 <= n_up <= 3:
        lines.append("🚨 **全体が縮小する中で少数銘柄だけ急伸 — 資金集中"
                     "(イベント/ファンダ発生)の可能性。急上昇銘柄を要調査**")
    elif chg < -20 and n_down > n_up * 2:
        lines.append("⚠️ 手仕舞い優勢 (リスクオフ)。無理に張らない局面")
    elif chg > 20 and n_up >= 3:
        lines.append("✅ 活動拡大 (リスクオン気味)。急上昇銘柄が分散している時は地合い、"
                     "集中している時は個別イベント")
    return lines


def write_csv(res):
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["coin", "ntl_cur", "ntl_prev", "rise_ratio", "wallets",
                    "kind"])
        for coin, ntl, pv, ratio, nw in res["risers"]:
            w.writerow([coin, round(ntl), round(pv), round(ratio, 2), nw, "riser"])
        for coin, cv, pv, ratio in res["fallers"]:
            w.writerow([coin, round(cv), round(pv), round(ratio, 2), "", "faller"])
    print(f"wrote {OUT_CSV}")


# ============================================================
# チャート3枚
# ============================================================

def _style(ax):
    ax.set_facecolor("#fcfcfb")
    ax.grid(color="#e1e0d9", lw=0.6)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def _paired_barh(ax, names, cur_vals, prev_vals):
    """今回(青) vs 前回(灰) のペア横棒。増減が一目で分かる"""
    y = list(range(len(names)))
    ax.barh([i + 0.2 for i in y], cur_vals, height=0.38, color=BLUE, label=CUR_L)
    ax.barh([i - 0.2 for i in y], prev_vals, height=0.38, color=GRAY, label=PREV_L)
    ax.set_yticks(y, labels=names)
    ax.legend(fontsize=8, frameon=False, loc="lower right")


def _mpl():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        try:
            import matplotlib_fontja  # noqa: F401
        except ImportError:
            pass
        return plt
    except ImportError:
        return None


def chart_main(res, out_path="sm_main.png"):
    plt = _mpl()
    if plt is None:
        return None
    fig, axes = plt.subplots(2, 2, figsize=(12.5, 8.2), facecolor="#fcfcfb")

    # ① 急上昇 + 減少 (ペア棒: 前回と比べてどれだけ伸びた/減ったか)
    ax = axes[0][0]
    rows = ([(c, n, p, f"{'新規' if p < 1e4 else f'{r:.1f}x'}・{w}人")
             for c, n, p, r, w in res["risers"][:6]] +
            [(c, cv, pv, f"▼{(1-r)*100:.0f}%減") for c, cv, pv, r in res["fallers"][:3]])
    if rows:
        rows = rows[::-1]
        _paired_barh(ax, [r[0] for r in rows],
                     [r[1] / 1e6 for r in rows], [r[2] / 1e6 for r in rows])
        for y, (_, n, p, tag) in enumerate(rows):
            ax.text(max(n, p) / 1e6, y + 0.2, f" {tag}", va="center",
                    fontsize=8, color="#52514e")
        ax.set_xlim(0, max(max(r[1], r[2]) for r in rows) / 1e6 * 1.35)
    ax.set_title(f"① 急上昇/減少 ({CUR_L}=青 vs {PREV_L}=灰)", loc="left", fontsize=10)
    ax.set_xlabel("売買代金 ($M)")

    # ② 主戦場 (ペア棒)
    ax = axes[0][1]
    tn = res["top_ntl"][::-1]
    if tn:
        _paired_barh(ax, [c for c, _ in tn],
                     [v["ntl"] / 1e6 for _, v in tn],
                     [res["prev"].get(c, {}).get("ntl", 0) / 1e6 for c, _ in tn])
    ax.set_title(f"② 主戦場 = 売買代金上位 ({CUR_L} vs {PREV_L})", loc="left", fontsize=10)
    ax.set_xlabel("売買代金 ($M)")

    # ③ 実現PnL (今回を青/赤、前回を灰の細棒で併記)
    ax = axes[1][0]
    pn = sorted(res["top_pnl"], key=lambda kv: kv[1]["pnl"])
    if pn:
        y = list(range(len(pn)))
        cur_v = [v["pnl"] / 1e6 for _, v in pn]
        prev_v = [res["prev"].get(c, {}).get("pnl", 0) / 1e6 for c, _ in pn]
        ax.barh([i + 0.2 for i in y], cur_v, height=0.38,
                color=[BLUE if v > 0 else RED for v in cur_v], label=CUR_L)
        ax.barh([i - 0.2 for i in y], prev_v, height=0.38, color=GRAY, label=PREV_L)
        ax.set_yticks(y, labels=[c for c, _ in pn])
        ax.axvline(0, color="#c3c2b7", lw=1)
        ax.legend(fontsize=8, frameon=False, loc="lower right")
    ax.set_title(f"③ 実現PnL ({CUR_L}: 青=利益/赤=損失, 灰={PREV_L})", loc="left", fontsize=10)
    ax.set_xlabel("実現PnL ($M)")

    # ④ 日別代金 (窓の区切り線)
    ax = axes[1][1]
    days = list(res["daily"].keys())
    ax.bar(days, [v / 1e6 for v in res["daily"].values()], color=BLUE, width=0.8)
    split = len(days) - WINDOW_DAYS - 0.5
    if 0 < split < len(days):
        ax.axvline(split, color="#898781", lw=1, ls="--")
        ax.text(split + 0.2, ax.get_ylim()[1] * 0.95, f"← ここから{CUR_L}",
                fontsize=8, color="#52514e")
    ax.set_title(f"④ 日別売買代金 (破線より右が{CUR_L}=判定対象)", loc="left", fontsize=10)
    ax.set_ylabel("$M/日")
    ax.tick_params(axis="x", rotation=60, labelsize=7)

    for row in axes:
        for a in row:
            _style(a)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path


def chart_heat(res, out_path="sm_heat.png"):
    """時間帯(JST)×銘柄ヒートマップ (今回窓)。いつ動いたか"""
    plt = _mpl()
    if plt is None or not res["hours"]:
        return None
    import numpy as _np
    top = [c for c, _ in res["top_ntl"][:10]]
    m = _np.zeros((len(top), 24))
    for (coin, h), v in res["hours"].items():
        if coin in top:
            m[top.index(coin)][h] += v
    mx = m.max(axis=1, keepdims=True)
    mx[mx == 0] = 1
    fig, ax = plt.subplots(figsize=(11, 4.2), facecolor="#fcfcfb")
    from matplotlib.colors import LinearSegmentedColormap
    seq = LinearSegmentedColormap.from_list("sb", ["#cde2fb", "#3987e5", "#0d366b"])
    im = ax.imshow(m / mx, aspect="auto", cmap=seq)
    ax.set_xticks(range(24), labels=range(24))
    ax.set_yticks(range(len(top)), labels=top)
    ax.set_title(f"{CUR_L}の売買時間帯 (JST, 銘柄別に正規化) — 濃い列=活動時間",
                 loc="left", fontsize=10)
    ax.set_xlabel("時間帯 (JST)")
    ax.grid(False)
    fig.colorbar(im, ax=ax, label="ピーク比", shrink=0.8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path


def chart_positions(out_path="sm_pos.png"):
    """現在ポジション (tracker state)。銘柄別ネット$と保有者数"""
    plt = _mpl()
    if plt is None or not os.path.exists(STATE_FILE):
        return None
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return None
    agg, holders = {}, {}
    for coins in state.get("positions", {}).values():
        for coin, p in coins.items():
            usd = float(p.get("usd") or 0)
            agg[coin] = agg.get(coin, 0.0) + (usd if p.get("side") == "L" else -usd)
            holders[coin] = holders.get(coin, 0) + 1
    if not agg:
        return None
    top = sorted(agg.items(), key=lambda kv: -abs(kv[1]))[:12][::-1]
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4), facecolor="#fcfcfb")
    axes[0].barh([c for c, _ in top], [v / 1e6 for _, v in top],
                 color=[BLUE if v > 0 else RED for _, v in top])
    axes[0].axvline(0, color="#c3c2b7", lw=1)
    axes[0].set_title("現在の合算ネットポジション (青=ロング/赤=ショート)",
                      loc="left", fontsize=10)
    axes[0].set_xlabel("ネットポジション ($M)")
    hh = sorted(holders.items(), key=lambda kv: -kv[1])[:12][::-1]
    axes[1].barh([c for c, _ in hh], [v for _, v in hh], color=BLUE)
    axes[1].set_title("保有ウォレット数 (何人が持っているか)", loc="left", fontsize=10)
    axes[1].set_xlabel("人数")
    for a in axes:
        _style(a)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path


# ============================================================
# Discord (複数画像対応)
# ============================================================

def discord_post(payload, image_paths=None):
    if not DISCORD_WEBHOOK_URL:
        print("  [DRY-RUN] Webhook未設定 — 通知内容:")
        print(json.dumps(payload, ensure_ascii=False, indent=1)[:2500])
        return
    files = [p for p in (image_paths or []) if p and os.path.exists(p)]
    if files:
        boundary = "----smreport" + str(int(time.time()))
        parts = [(f"--{boundary}\r\nContent-Disposition: form-data; "
                  f'name="payload_json"\r\nContent-Type: application/json\r\n\r\n'
                  f"{json.dumps(payload)}\r\n").encode()]
        for i, path in enumerate(files):
            with open(path, "rb") as f:
                img = f.read()
            parts.append(
                (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f'name="files[{i}]"; filename="{os.path.basename(path)}"\r\n'
                 f"Content-Type: image/png\r\n\r\n").encode() + img + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "User-Agent": "smart-money-report/2.0"}
    else:
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json",
                   "User-Agent": "smart-money-report/2.0"}
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=data, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")


def main():
    now = datetime.now(JST)
    print(f"Smart money {LABEL} report {now.strftime('%Y-%m-%d %H:%M JST')} "
          f"(窓{WINDOW_DAYS:g}日)")
    addrs = load_humans()
    print(f"対象: 人間系{len(addrs)}人 (VIP含む)")
    rows = fetch_fills(addrs)
    if not rows:
        print("約定ゼロ — 送信しない。")
        return
    res = analyze(rows)
    write_csv(res)

    rise_lines = [(f"**{c}** " + ("🆕新規" if p < 1e4 else f"▲{r:.1f}x")
                   + f" ({CUR_L}${n/1e6:.1f}M, {w}人)")
                  for c, n, p, r, w in res["risers"][:8]]
    fall_lines = [f"**{c}** ▼{(1-r)*100:.0f}%減 ({PREV_L}${pv/1e6:.1f}M→{CUR_L}${cv/1e6:.1f}M)"
                  for c, cv, pv, r in res["fallers"][:4]]
    ntl_lines = [f"{c}: ${v['ntl']/1e6:.1f}M "
                 f"({PREV_L}${res['prev'].get(c, {}).get('ntl', 0)/1e6:.1f}M, "
                 f"{len(v['w'])}人)"
                 for c, v in res["top_ntl"][:8]]
    pnl_lines = [f"{'🟢' if v['pnl'] > 0 else '🔴'} {c}: "
                 f"{'+' if v['pnl'] > 0 else '-'}${abs(v['pnl'])/1e3:,.0f}k"
                 for c, v in sorted(res["top_pnl"], key=lambda kv: -kv[1]["pnl"])[:8]
                 if abs(v["pnl"]) > 1000]

    embed = {
        "title": f"📈 スマートマネー{LABEL}レポート ({CUR_L} vs {PREV_L})",
        "description": ("\n".join(interpret(res))
                        + "\n\n__**🚀 急上昇 (▲) と手仕舞い (▼)**__\n"
                        + ("\n".join(rise_lines) or "急上昇なし")
                        + ("\n" + "\n".join(fall_lines) if fall_lines else ""))[:3900],
        "color": 0x1BAF7A,
        "fields": [
            {"name": f"🏟️ 主戦場 ({CUR_L} vs {PREV_L})",
             "value": "\n".join(ntl_lines)[:1000] or "-", "inline": True},
            {"name": f"💰 実現PnL ({CUR_L})",
             "value": "\n".join(pnl_lines)[:1000] or "-", "inline": True},
            {"name": "🔎 どこを見て判別しているか",
             "value": (f"人間系{len(addrs)}人の直近{2*WINDOW_DAYS:g}日の全約定を取得し、"
                       f"{CUR_L}窓と{PREV_L}窓で比較。\n"
                       f"急上昇={CUR_L}${MIN_RISE_USD/1e6:g}M+かつ前回比1.3x+ / "
                       f"減少=前回${MIN_RISE_USD/1e6:g}M+が2/3未満に。\n"
                       "添付: [1]比較4枚組 [2]時間帯ヒートマップ [3]現在ポジション"),
             "inline": False},
        ],
        "footer": {"text": f"Smart Money {LABEL} | {now.strftime('%Y-%m-%d %H:%M JST')}"
                           " | 月1回 Smart Money Collect でリスト更新"},
    }

    charts = []
    try:
        charts = [chart_main(res), chart_heat(res), chart_positions()]
    except Exception as e:
        print(f"  チャート描画失敗: {type(e).__name__} {str(e)[:80]}")
    charts = [c for c in charts if c]

    embeds = [embed]
    if charts:
        embed["image"] = {"url": f"attachment://{os.path.basename(charts[0])}"}
        for c in charts[1:]:
            embeds.append({"image": {"url": f"attachment://{os.path.basename(c)}"},
                           "color": 0x1BAF7A})
    mention = "@everyone" if MENTION_EVERYONE else ""
    allowed = {"parse": ["everyone"]} if MENTION_EVERYONE else {"parse": []}
    try:
        discord_post({"content": mention, "embeds": embeds,
                      "allowed_mentions": allowed}, image_paths=charts)
    except Exception as e:
        print(f"  Discord送信失敗: {type(e).__name__} {str(e)[:80]}")
    print(f"送信完了: 急上昇{len(res['risers'])}/減少{len(res['fallers'])}銘柄, "
          f"チャート{len(charts)}枚。")


if __name__ == "__main__":
    main()
