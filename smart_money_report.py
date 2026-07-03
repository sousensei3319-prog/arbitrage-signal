"""
Smart Money Weekly Report v1 (④スマートマネー追跡 — 週次ダイジェスト)

「注目急上昇スクリーン」の定例化。追跡中の人間系ウォレットの直近14日の
約定を取り直し、以下を週1でDiscordに配信する:

  1. 注目急上昇銘柄: 直近7日 vs その前7日の売買代金の伸び (ローテーション初動検知)
  2. 今週の主戦場: 売買代金上位 + クリプト/株式perp(xyz:)のシェア
  3. 今週の稼ぎ頭銘柄: 実現PnL上位 (「代金上位」と「稼ぎ頭」は別物)
  4. チャート添付: 急上昇銘柄の棒グラフ + 日別代金推移

背景 (2026-07 分析): 人間系スマートマネーの資金は xyz: (HL上の株式/商品perp)
へ移行が進んでいる。この定点観測が「戦場の移り変わり」を検知する仕組み。

出力: data/smart_money/attention_screen.csv (コミットバック) + Discord embed

コスト: userFillsByTime weight20 x 追跡人数(最大200) ≈ 4000 weight ≈ 4分
依存: 標準ライブラリのみで動作 (matplotlibがあればチャート付き)
実行: python smart_money_report.py  (DISCORD_WEBHOOK_URL未設定ならDRY-RUN)
"""

import csv
import json
import os
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

JST = timezone(timedelta(hours=9))

# ============================================================
# Config (環境変数で調整可)
# ============================================================
DISCORD_WEBHOOK_URL = (os.environ.get("SMART_MONEY_WEBHOOK_URL")
                       or os.environ.get("DISCORD_WEBHOOK_URL", ""))
REPORT_N     = int(os.environ.get("REPORT_N") or "200")     # 対象人数上限
MIN_RISE_USD = float(os.environ.get("MIN_RISE_USD") or "1000000")  # 急上昇の下限(直近7日$)
TRACKED_FILE = os.environ.get("TRACKED_FILE") or "data/smart_money/tracked_addresses.csv"
VIP_FILE     = os.environ.get("VIP_FILE") or "data/smart_money/vip_addresses.csv"
OUT_CSV      = os.environ.get("OUT_CSV") or "data/smart_money/attention_screen.csv"

HL_INFO = "https://api.hyperliquid.xyz/info"
HL_WEIGHT_PER_MIN = 1000.0


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


def fetch_fills_14d(addrs):
    """直近14日の約定 [(addr, t_ms, coin, side, px, sz, closed_pnl)]"""
    start_ms = int((time.time() - 14 * 86400) * 1000)
    sleep_s = 20.0 / HL_WEIGHT_PER_MIN * 60.0
    rows, fail = [], 0
    for i, a in enumerate(addrs, 1):
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
        if i % 50 == 0:
            print(f"  [{i}/{len(addrs)}] fills={len(rows)}")
        time.sleep(sleep_s)
    print(f"fills: {len(rows)}件 (fail {fail}人)")
    return rows


def analyze(rows):
    """週次集計。返り値: dict"""
    now_ms = time.time() * 1000
    wk_ms = 7 * 86400 * 1000
    cur = {}   # coin -> {ntl, pnl, wallets:set}
    prev = {}  # coin -> ntl
    daily = {}  # date -> ntl (直近14日)
    for a, t, coin, side, px, sz, pnl in rows:
        ntl = px * sz
        d = datetime.fromtimestamp(t / 1000, JST).strftime("%m-%d")
        daily[d] = daily.get(d, 0) + ntl
        if t >= now_ms - wk_ms:
            c = cur.setdefault(coin, {"ntl": 0.0, "pnl": 0.0, "w": set()})
            c["ntl"] += ntl
            c["pnl"] += pnl
            c["w"].add(a)
        elif t >= now_ms - 2 * wk_ms:
            prev[coin] = prev.get(coin, 0) + ntl

    risers = []
    for coin, c in cur.items():
        if c["ntl"] < MIN_RISE_USD:
            continue
        ratio = (c["ntl"] + 1) / (prev.get(coin, 0) + 1)
        risers.append((coin, c["ntl"], prev.get(coin, 0), ratio, len(c["w"])))
    risers.sort(key=lambda r: -r[3])

    top_ntl = sorted(cur.items(), key=lambda kv: -kv[1]["ntl"])[:10]
    top_pnl = sorted(cur.items(), key=lambda kv: -kv[1]["pnl"])[:5]
    worst_pnl = sorted(cur.items(), key=lambda kv: kv[1]["pnl"])[:3]

    total = sum(c["ntl"] for c in cur.values()) or 1
    xyz = sum(c["ntl"] for coin, c in cur.items()
              if str(coin).startswith("xyz:"))
    return {"risers": risers, "top_ntl": top_ntl, "top_pnl": top_pnl,
            "worst_pnl": worst_pnl, "xyz_share": xyz / total * 100,
            "total_ntl": total, "daily": dict(sorted(daily.items()))}


def write_csv(res):
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["coin", "ntl_7d", "ntl_prev7d", "rise_ratio", "wallets"])
        for coin, ntl, pv, ratio, nw in res["risers"]:
            w.writerow([coin, round(ntl), round(pv), round(ratio, 2), nw])
    print(f"wrote {OUT_CSV} ({len(res['risers'])} rows)")


def render_chart(res, out_path="sm_report.png"):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        try:
            import matplotlib_fontja  # noqa: F401
        except ImportError:
            pass
    except ImportError:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(11, 4), facecolor="#fcfcfb")

    top = res["risers"][:8][::-1]
    if top:
        names = [r[0] for r in top]
        axes[0].barh(names, [r[3] for r in top], color="#2a78d6")
        axes[0].set_title("注目急上昇銘柄 (今週/前週の代金比)", loc="left", fontsize=10)
        axes[0].set_xlabel("伸び率 (x)")
    days = list(res["daily"].keys())
    axes[1].bar(days, [v / 1e6 for v in res["daily"].values()],
                color="#2a78d6", width=0.8)
    axes[1].set_title("人間系スマートマネーの日別売買代金", loc="left", fontsize=10)
    axes[1].set_ylabel("$M/日")
    axes[1].tick_params(axis="x", rotation=60, labelsize=7)
    for ax in axes:
        ax.set_facecolor("#fcfcfb")
        ax.grid(color="#e1e0d9", lw=0.6)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path


def discord_post(payload, image_path=None):
    if not DISCORD_WEBHOOK_URL:
        print("  [DRY-RUN] Webhook未設定 — 通知内容:")
        print(json.dumps(payload, ensure_ascii=False, indent=1)[:3000])
        return
    if image_path and os.path.exists(image_path):
        boundary = "----smreport" + str(int(time.time()))
        with open(image_path, "rb") as f:
            img = f.read()
        name = os.path.basename(image_path)
        data = b"".join([
            (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="payload_json"\r\nContent-Type: application/json\r\n\r\n'
             f"{json.dumps(payload)}\r\n").encode(),
            (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="files[0]"; filename="{name}"\r\n'
             f"Content-Type: image/png\r\n\r\n").encode() + img + b"\r\n",
            f"--{boundary}--\r\n".encode()])
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "User-Agent": "smart-money-report/1.0"}
    else:
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json",
                   "User-Agent": "smart-money-report/1.0"}
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=data, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")


def main():
    now = datetime.now(JST)
    print(f"Smart money weekly report {now.strftime('%Y-%m-%d %H:%M JST')}")
    addrs = load_humans()
    print(f"対象: 人間系{len(addrs)}人 (VIP含む)")
    rows = fetch_fills_14d(addrs)
    if not rows:
        print("約定ゼロ — 送信しない。")
        return
    res = analyze(rows)
    write_csv(res)

    rise_lines = [f"**{c}** {r:.1f}x (今週${n/1e6:.1f}M, {w}人)"
                  for c, n, p, r, w in res["risers"][:8]]
    ntl_lines = [f"{c}: ${v['ntl']/1e6:.1f}M ({len(v['w'])}人)"
                 for c, v in res["top_ntl"]]
    pnl_lines = ([f"🟢 {c}: +${v['pnl']/1e3:,.0f}k" for c, v in res["top_pnl"]
                  if v["pnl"] > 0] +
                 [f"🔴 {c}: -${abs(v['pnl'])/1e3:,.0f}k" for c, v in res["worst_pnl"]
                  if v["pnl"] < 0])

    embed = {
        "title": "📈 スマートマネー週次レポート",
        "description": (f"人間系{len(addrs)}人の直近7日: "
                        f"売買代金 **${res['total_ntl']/1e6:,.0f}M** / "
                        f"株式perp(xyz:)シェア **{res['xyz_share']:.0f}%**\n\n"
                        "__**🚀 注目急上昇 (今週/前週比)**__\n"
                        + ("\n".join(rise_lines) or "該当なし"))[:3900],
        "color": 0x1BAF7A,
        "fields": [
            {"name": "🏟️ 今週の主戦場 (売買代金上位)",
             "value": "\n".join(ntl_lines)[:1000] or "-", "inline": True},
            {"name": "💰 今週の稼ぎ頭/損失源 (実現PnL)",
             "value": "\n".join(pnl_lines)[:1000] or "-", "inline": True},
            {"name": "使い方",
             "value": ("急上昇銘柄 = ローテーションの初動候補 → 監視リストへ。\n"
                       "稼ぎ頭 = いま効いている銘柄 (代金上位とは別物)。\n"
                       "詳細は notebooks/smart_money_analysis.ipynb を再実行"),
             "inline": False},
        ],
        "footer": {"text": f"Smart Money Weekly | {now.strftime('%Y-%m-%d JST')} | "
                           "月1回 Smart Money Collect でリスト更新を忘れずに"},
    }
    chart = render_chart(res)
    if chart:
        embed["image"] = {"url": f"attachment://{os.path.basename(chart)}"}
    discord_post({"embeds": [embed], "allowed_mentions": {"parse": []}},
                 image_path=chart)
    print(f"送信完了: 急上昇{len(res['risers'])}銘柄"
          f"{' (チャート付き)' if chart else ''}。")


if __name__ == "__main__":
    main()
