"""
Unified Signal Scanner v1 — 全部入り統合シグナル

3層フィルタで「全部重なった銘柄だけ通知」する本命ツール。
既存の4スクリプト (pump-screener / ①スプレッド / ②FR / ③ダッシュボード) を1本に統合。

=== 3層フィルタ ===
  Layer 1 マクロ環境 : FGI + BTC方向 → ショート向き/中立/ロング向きを判定
  Layer 2 ポンプ検知 : 1h/24h急騰 + 再現性分析 → ダンプ率が高い銘柄を絞る
  Layer 3 歪みスコア : FR5取引所+ベーシス+OI+L/S → 過熱の確度を数値化

全レイヤーが重なった銘柄のみ通知 → ノイズ激減・精度UP
チャート画像も添付 (matplotlib)

=== 引退スクリプト ===
  arbitrage-signal/screener.py  (①価格スプレッド — 実行不可ノイズが多い)
  arbitrage-signal/fr_scanner.py (②FR単体通知 — Layer3に統合)
  arbitrage-signal/dashboard.py  (③ダッシュボード — Layer3として統合)
  crypto-pump-screener/screener.py (ポンプ検知 — Layer2として統合)

CLAUDE.md: 集計期間(スキャン時刻)を必ず明記する。
"""

import io
import json
import math
import os
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    try:
        import japanize_matplotlib  # noqa
    except Exception:
        pass
    HAS_MPL = True
except Exception:
    HAS_MPL = False

JST = timezone(timedelta(hours=9))

# ============================================================
# Config
# ============================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MENTION_EVERYONE    = os.environ.get("MENTION_EVERYONE", "0") == "1"

# Layer2: ポンプ検知閾値
PUMP_1H         = float(os.environ.get("PUMP_1H", "8.0"))
PUMP_24H        = float(os.environ.get("PUMP_24H", "15.0"))
MIN_VOLUME_24H  = float(os.environ.get("MIN_VOLUME_24H", "2000000"))
HIST_DAYS       = int(os.environ.get("HIST_DAYS", "300"))
PUMP_EVENT_PCT  = float(os.environ.get("PUMP_EVENT_PCT", "15.0"))
FORWARD_WINDOW  = int(os.environ.get("FORWARD_WINDOW", "3"))
DUMP_RETRACE_MIN= float(os.environ.get("DUMP_RETRACE_MIN", "0.5"))

# Layer3: 歪みスコア閾値
MIN_DISTORTION  = int(os.environ.get("MIN_DISTORTION", "2"))  # 歪みスコア下限
MIN_TOTAL_SCORE = int(os.environ.get("MIN_TOTAL_SCORE", "4")) # 再現性+歪みの合計下限

MAX_SIGNALS     = int(os.environ.get("MAX_SIGNALS", "8"))

EXCLUDE = {"USDC","DAI","TUSD","FDUSD","USDD","USDE","BUSD","PYUSD",
           "BTC","ETH","SOL","BNB","XRP"}
STOCK_TOKENS = {
    "AAPL","MSFT","GOOGL","GOOG","AMZN","META","NVDA","TSLA","AMD","INTC",
    "IBM","ORCL","DELL","MU","SNDK","ARM","CRM","ADBE","NFLX","QCOM","TXN",
    "AVGO","AMAT","LRCX","KLAC","MRVL","SMCI","HPE","HPQ","WDC","STX",
    "COIN","HOOD","MSTR","MARA","RIOT","HUT","CLSK","CIFR","BTBT",
    "COST","WMT","TGT","HD","LOW","NKE","SBUX","MCD",
    "JPM","BAC","GS","MS","WFC","C","BLK","V","MA","PYPL",
    "JNJ","PFE","MRNA","ABBV","LLY","UNH","CVS",
    "XOM","CVX","COP","SLB","BP","SHEL",
    "GE","CAT","BA","LMT","RTX","NOC","GD",
    "QQQ","SPY","IWM","DIA","GLD","SLV","USO","TLT","HYG",
    "SOXS","SOXX","FNGU","TQQQ","SQQQ",
    "DRAM","CRCL","H","BIDU","BABA","JD","PDD","NIO","XPEV","LI",
}

OKX_BASE    = "https://www.okx.com/api/v5"
BYBIT_BASE  = "https://api.bybit.com/v5"
MEXC_BASE   = "https://contract.mexc.com/api/v1"
BITGET_BASE = "https://api.bitget.com/api/v2"


# ============================================================
# HTTP helpers
# ============================================================
def fetch(url, timeout=20):
    req = urllib.request.Request(
        url, headers={"User-Agent": "unified-signal/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def okx(path):
    d = fetch(f"{OKX_BASE}{path}")
    if d.get("code") != "0":
        raise RuntimeError(f"OKX {d.get('code')}: {d.get('msg')}")
    return d["data"]

def safe_float(x, default=None):
    try:
        v = float(x)
        return v if v == v else default
    except (TypeError, ValueError):
        return default


# ============================================================
# Layer 1: マクロ環境
# ============================================================
def get_fgi():
    """FGI (恐怖と欲望指数) を alternative.me の無料APIから取得。"""
    try:
        d = fetch("https://api.alternative.me/fng/?limit=1", timeout=10)
        item = d["data"][0]
        return int(item["value"]), item["value_classification"]
    except Exception:
        return None, None

def get_btc_direction():
    """BTC 24h変化率 (OKX)。"""
    try:
        d = okx("/market/ticker?instId=BTC-USDT-SWAP")
        last = safe_float(d[0].get("last"))
        open24 = safe_float(d[0].get("open24h"))
        if last and open24 and open24 > 0:
            return (last - open24) / open24 * 100, last
    except Exception:
        pass
    return None, None

def macro_verdict(fgi, btc_chg):
    """マクロ環境の判定。戻り値: (verdict_str, is_short_friendly)"""
    if fgi is None:
        return "不明", True  # データなければ中立
    if fgi <= 25:
        mood = "極度の恐怖"
    elif fgi <= 45:
        mood = "恐怖"
    elif fgi <= 55:
        mood = "中立"
    elif fgi <= 75:
        mood = "強欲"
    else:
        mood = "極度の強欲"

    btc_rising = btc_chg is not None and btc_chg > 3.0
    # FGI 70超 + BTC急騰中 = ショート環境ではない (BTCラリー中のアルトポンプは戻しが浅い)
    if fgi > 70 and btc_rising:
        verdict = f"⚠️ FGI{fgi}({mood}) + BTC+{btc_chg:.1f}% — BTCラリー中はショート慎重に"
        return verdict, False
    elif fgi <= 45 or (btc_chg is not None and btc_chg < -2.0):
        verdict = f"✅ FGI{fgi}({mood}) — ショート環境良好"
        return verdict, True
    else:
        verdict = f"🟡 FGI{fgi}({mood}) — 中立"
        return verdict, True


# ============================================================
# Layer 2: ポンプ検知
# ============================================================
def get_all_tickers():
    d = okx("/market/tickers?instType=SWAP")
    return [t for t in d if t["instId"].endswith("-USDT-SWAP")]

def get_candles(inst_id, bar, limit):
    d = okx(f"/market/candles?instId={inst_id}&bar={bar}&limit={limit}")
    rows = [{"ts":int(c[0]),"o":float(c[1]),"h":float(c[2]),"l":float(c[3]),
             "c":float(c[4]),"v":float(c[5]) if len(c)>5 else 0.0} for c in d]
    rows.reverse()
    return rows

def analyze_history(daily):
    """ポンプ後ダンプの再現性を分析 (pump-screenerのコアロジック)"""
    n = len(daily)
    if n < 30:
        return None
    events = []
    for i in range(1, n - 1):
        prev_close = daily[i - 1]["c"]
        if prev_close <= 0:
            continue
        peak = daily[i]["h"]
        pump_pct = (peak - prev_close) / prev_close * 100
        if pump_pct < PUMP_EVENT_PCT:
            continue
        fwd = daily[i + 1: i + 1 + FORWARD_WINDOW]
        if not fwd:
            continue
        trough = min(b["l"] for b in fwd)
        days_to_trough = 1 + min(range(len(fwd)), key=lambda k: fwd[k]["l"])
        denom = peak - prev_close
        if denom <= 0:
            continue
        retrace_frac = max(0.0, (peak - trough) / denom)
        events.append({"pump_pct": pump_pct, "retrace_frac": retrace_frac,
                       "days_to_trough": days_to_trough})
    if not events:
        return None
    dumped = [e for e in events if e["retrace_frac"] >= DUMP_RETRACE_MIN]
    N = len(events)
    dump_rate = len(dumped) / N
    avg_retrace = sum(e["retrace_frac"] for e in dumped) / len(dumped) if dumped else 0.0
    avg_days = sum(e["days_to_trough"] for e in dumped) / len(dumped) if dumped else 0.0
    sizes = sorted(e["pump_pct"] for e in events)
    typ_lo = sizes[len(sizes) // 4]
    typ_hi = sizes[(len(sizes) * 3) // 4]
    if N >= 15 and dump_rate >= 0.80:
        tier, tier_score = "極高", 4
    elif N >= 8 and dump_rate >= 0.70:
        tier, tier_score = "高", 3
    elif N >= 4 and dump_rate >= 0.60:
        tier, tier_score = "中", 2
    else:
        tier, tier_score = "低", 0
    tp_tendency = ("ほぼ全戻し傾向" if avg_retrace >= 0.85
                   else "半値〜全戻し傾向" if avg_retrace >= 0.5 else "浅めの戻し")
    return {"N": N, "dump_count": len(dumped), "dump_rate": dump_rate,
            "avg_retrace": avg_retrace, "avg_days": avg_days,
            "typ_lo": typ_lo, "typ_hi": typ_hi,
            "tier": tier, "tier_score": tier_score, "tp_tendency": tp_tendency,
            "hist_days": n}

def ema(values, period):
    if not values: return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def ema_series(values, period):
    if len(values) < period: return [None] * len(values)
    result = [None] * len(values)
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    result[period - 1] = e
    for i in range(period, len(values)):
        e = values[i] * k + e * (1 - k)
        result[i] = e
    return result

def trend_state_4h(c4h):
    if len(c4h) < 25: return None
    closes = [b["c"] for b in c4h]
    e20 = ema(closes, 20)
    price = closes[-1]
    recent = c4h[-7:-1]
    swing_low = min(b["l"] for b in recent) if recent else c4h[-1]["l"]
    above_ema = price > e20
    if above_ema:
        state = "4h EMA20上 → ブレイクダウン待ち"
    elif price < swing_low:
        state = "4h EMA20下+安値割れ → 下落トレンド確定"
    else:
        state = "4h EMA20下(失速中) → 安値を監視"
    return {"ema20": e20, "swing_low": swing_low, "above_ema": above_ema, "state": state}

def swing_low_1h(c1h):
    if len(c1h) < 7: return None
    return min(b["l"] for b in c1h[-7:-1])


# ============================================================
# Layer 3: 歪みスコア (FR + ベーシス + OI + L/S)
# ============================================================
def get_okx_funding(inst_id):
    try:
        d = okx(f"/public/funding-rate?instId={inst_id}")
        if not d: return None, None, 8
        fr = safe_float(d[0].get("fundingRate"))
        nft = safe_float(d[0].get("nextFundingTime"))
        ih = safe_float(d[0].get("fundingIntervalHours")) or 8
        return fr, int(nft) if nft else None, ih
    except Exception:
        return None, None, 8

def get_multi_exchange_fr(coin):
    """Bybit/MEXC/Bitget から一括取得済みのFRを返す。"""
    return {}  # 後でバッチから引く (main内で処理)

def get_oi_change(coin):
    try:
        d = okx(f"/rubik/stat/contracts/open-interest-volume?ccy={coin}&period=1H")
        if len(d) < 2: return None
        now = safe_float(d[0][1]); prev = safe_float(d[1][1])
        if not prev or prev == 0: return None
        return (now - prev) / prev * 100
    except Exception:
        return None

def get_ls_ratio(coin):
    try:
        d = okx(f"/rubik/stat/contracts/long-short-account-ratio?ccy={coin}&period=5m")
        return safe_float(d[0][1]) if d else None
    except Exception:
        return None

def calc_apy(fr, interval_h=8):
    if fr is None: return None
    return fr * (24 / interval_h) * 365 * 100

def score_distortion(max_fr, basis_pct, oi_chg, ls_ratio):
    """歪みスコア 0〜10 (FR/ベーシス/OI/L-S の4シグナル)"""
    score = 0
    reasons = []
    if max_fr is not None:
        fr_pct = max_fr * 100
        if fr_pct > 0.10:
            score += 3; reasons.append(f"FR超過熱 {fr_pct:+.3f}%(+3)")
        elif fr_pct > 0.05:
            score += 2; reasons.append(f"FR過熱 {fr_pct:+.3f}%(+2)")
        elif fr_pct > 0.02:
            score += 1; reasons.append(f"FR高め {fr_pct:+.3f}%(+1)")
    if basis_pct is not None:
        if basis_pct > 0.5:
            score += 2; reasons.append(f"ベーシス過熱 {basis_pct:+.2f}%(+2)")
        elif basis_pct > 0.2:
            score += 1; reasons.append(f"先物プレミアム {basis_pct:+.2f}%(+1)")
    if oi_chg is not None:
        if oi_chg > 15:
            score += 2; reasons.append(f"OI急増 {oi_chg:+.1f}%/1h(+2)")
        elif oi_chg > 7:
            score += 1; reasons.append(f"OI増加 {oi_chg:+.1f}%/1h(+1)")
    if ls_ratio is not None:
        if ls_ratio > 2.0:
            score += 2; reasons.append(f"L/S過熱 {ls_ratio:.2f}(+2)")
        elif ls_ratio > 1.5:
            score += 1; reasons.append(f"L/S偏重 {ls_ratio:.2f}(+1)")
    return min(score, 10), reasons


# ============================================================
# チャート描画 (pump-screenerから流用)
# ============================================================
def render_chart_png(coin, c1h, plan_levels):
    if not HAS_MPL or not c1h:
        return None
    try:
        fig, axes = plt.subplots(2, 1, figsize=(8, 6), dpi=110,
                                 gridspec_kw={"height_ratios": [3, 1]})
        ax, axv = axes
        n_show = 72
        all_bars = c1h
        bars = all_bars[-n_show:]
        closes_all = [b["c"] for b in all_bars]
        ema80_all = ema_series(closes_all, 80)
        ema80_show = ema80_all[-n_show:]
        widths = 0.7
        for i, b in enumerate(bars):
            up = b["c"] >= b["o"]
            color = "#26a69a" if up else "#ef5350"
            ax.plot([i, i], [b["l"], b["h"]], color=color, linewidth=0.7)
            top = max(b["o"], b["c"]); bot = min(b["o"], b["c"])
            height = max(top - bot, bars[-1]["c"] * 0.0005)
            ax.add_patch(Rectangle((i - widths/2, bot), widths, height,
                                   facecolor=color, edgecolor=color))
            axv.bar(i, b["v"], width=widths, color=color, alpha=0.7)
        xs_e = [i for i, v in enumerate(ema80_show) if v is not None]
        ys_e = [v for v in ema80_show if v is not None]
        if xs_e:
            ax.plot(xs_e, ys_e, color="#bb86fc", linewidth=1.2,
                    label=f"EMA80 {ys_e[-1]:.6g}")
        for key, color, label in [
            ("sl",    "#ff5252", "SL"),
            ("early_entry", "#ffeb3b", "1h早期"),
            ("tp1",   "#66bb6a", "TP1"),
            ("trend_break", "#ff9800", "4hブレイク"),
            ("tp2",   "#1e88e5", "TP2"),
            ("support_7d", "#9e9e9e", "7日サポ"),
        ]:
            v = plan_levels.get(key)
            if v:
                ls = ":" if key == "support_7d" else "--"
                ax.axhline(v, color=color, linestyle=ls, linewidth=0.9,
                           label=f"{label} {v:.6g}")
        ax.set_title(f"{coin}/USDT - 1h (直近72h) | OKX", color="#eeeeee", fontsize=11)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, alpha=0.2); axv.grid(True, alpha=0.2)
        for a in (ax, axv):
            a.set_facecolor("#1e222d")
            a.tick_params(colors="#aaaaaa", labelsize=7)
            for spine in a.spines.values():
                spine.set_color("#444444")
        fig.patch.set_facecolor("#131722")
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"  chart render failed: {e}")
        return None


# ============================================================
# Discord
# ============================================================
def discord_post(payload_json, attachments=None):
    if not DISCORD_WEBHOOK_URL:
        print("  [DRY-RUN] DISCORD_WEBHOOK_URL 未設定")
        return
    boundary = f"----unified{uuid.uuid4().hex}"
    if not attachments:
        data = json.dumps(payload_json).encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "unified-signal/1.0"})
    else:
        body = bytearray()
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="payload_json"\r\n'
        body += b"Content-Type: application/json\r\n\r\n"
        body += json.dumps(payload_json).encode()
        body += b"\r\n"
        for idx, (fname, content) in enumerate(attachments):
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="files[{idx}]"; filename="{fname}"\r\n'.encode()
            body += b"Content-Type: image/png\r\n\r\n"
            body += content
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL, data=bytes(body), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "User-Agent": "unified-signal/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15): pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")


def fmt_price(p):
    if p is None: return "n/a"
    a = abs(p)
    if a >= 100: return f"${p:,.2f}"
    if a >= 1:   return f"${p:,.4f}"
    if a >= 0.01: return f"${p:.5f}"
    return f"${p:.8f}"

def fmt_vol(v):
    if v is None: return "n/a"
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.2f}M"
    return f"${v/1e3:.0f}K"

def minutes_until(ms_ts):
    if not ms_ts: return None
    diff = (ms_ts - datetime.now(timezone.utc).timestamp() * 1000) / 60000
    return max(0, int(diff))


def build_macro_embed(fgi, fgi_label, btc_chg, btc_price, verdict, scan_time):
    now_jst = scan_time.strftime("%Y-%m-%d %H:%M JST")
    btc_str = f"BTC {btc_chg:+.1f}% (${btc_price:,.0f})" if btc_chg is not None else "BTC n/a"
    fgi_str = f"FGI: {fgi} ({fgi_label})" if fgi is not None else "FGI: n/a"
    return {
        "title": "📊 統合シグナル — マクロ環境",
        "description": f"{fgi_str}　{btc_str}\n{verdict}",
        "color": 0x00B0FF,
        "footer": {"text": f"Unified Signal v1 | {now_jst}"},
    }

def build_signal_embed(cand, scan_time):
    coin = cand["coin"]
    score_total = cand["score_total"]
    hist = cand["hist"]
    tr = cand["trend"]

    if score_total >= 8:    color, icon = 0xFF0000, "🔴"
    elif score_total >= 5:  color, icon = 0xFF8800, "🟠"
    else:                   color, icon = 0xFFCC00, "🟡"

    repro = hist["tier"] if hist else "N/A"
    title = f"{icon} {coin}/USDT — 総合スコア {score_total}/14 | 再現性 {repro}"

    now_jst = scan_time.strftime("%m-%d %H:%M JST")
    fr_str = f"FR {cand['max_fr']*100:+.3f}% @ {cand['max_ex']}" if cand["max_fr"] else "FR n/a"
    apy_str = f"APY {cand['max_apy']:.0f}%" if cand["max_apy"] else ""
    nft = minutes_until(cand["nft_ms"])
    nft_str = f"(次回{nft}分後)" if nft is not None else ""

    fields = [
        {"name": "値動き",
         "value": (f"{fmt_price(cand['price'])} | "
                   f"1h {cand['chg_1h']:+.1f}% | 24h {cand['chg_24h']:+.1f}% | "
                   f"出来高 {fmt_vol(cand['vol'])}"),
         "inline": False},
        {"name": "歪みシグナル",
         "value": ("\n".join(cand["distortion_reasons"]) if cand["distortion_reasons"]
                   else "歪みスコア低") + f"\n{fr_str} {apy_str} {nft_str}",
         "inline": False},
    ]

    if hist:
        fields.append({"name": f"過去パターン ({hist['hist_days']}日)",
                       "value": (f"急騰 {hist['N']}回 → {hist['dump_count']}回ダンプ "
                                 f"({hist['dump_rate']*100:.0f}%)\n"
                                 f"平均戻し {hist['avg_retrace']*100:.0f}% / "
                                 f"{hist['avg_days']:.1f}日 → {hist['tp_tendency']}"),
                       "inline": False})

    baseline = cand["baseline"]; peak = cand["day_high"]
    half_tp = peak - 0.5 * (peak - baseline)
    full_tp = baseline
    sl = peak * 1.02
    early = cand.get("swing_low_1h")
    trend_break = tr["swing_low"] if tr else None

    plan_lines = []
    if tr:
        plan_lines.append(f"4h足: {tr['state']}")
    if early:
        plan_lines.append(f"🟡 早期エントリー: 1h終値が {fmt_price(early)} 割れ")
    if trend_break:
        plan_lines.append(f"🟠 追加ショート: 4h終値が {fmt_price(trend_break)} 割れ")
    plan_lines.append(f"🔴 損切 > {fmt_price(sl)} (高値+2%)")
    plan_lines.append(f"🟢 利確1 {fmt_price(half_tp)} (半値)")
    plan_lines.append(f"🔵 利確2 {fmt_price(full_tp)} (全戻し)")
    sup7d = cand.get("support_7d")
    if sup7d:
        plan_lines.append(f"⚪ 7日サポ {fmt_price(sup7d)}")

    fields.append({"name": "エントリープラン", "value": "\n".join(plan_lines), "inline": False})
    fields.append({"name": "リンク",
                   "value": (f"[CoinGlass](https://www.coinglass.com/ja/currencies/{coin}) ・ "
                              f"[SoSoValue](https://sosovalue.com/ja/coins/{coin.lower()}) ・ "
                              f"[OKX](https://www.okx.com/trade-swap/{coin.lower()}-usdt-swap)"),
                   "inline": False})

    plan_levels = {"sl": sl, "tp1": half_tp, "tp2": full_tp,
                   "early_entry": early, "trend_break": trend_break, "support_7d": sup7d}
    return {"title": title, "color": color, "fields": fields,
            "footer": {"text": f"Unified Signal v1 | {now_jst}"}}, plan_levels


# ============================================================
# Main
# ============================================================
def main():
    scan_time = datetime.now(JST)
    print(f"Unified scan start {scan_time.strftime('%Y-%m-%d %H:%M JST')}")

    # ── Layer 1: マクロ環境 ──────────────────────────────
    print("  Layer1: マクロ取得...")
    fgi, fgi_label = get_fgi()
    btc_chg, btc_price = get_btc_direction()
    verdict, short_friendly = macro_verdict(fgi, btc_chg)
    print(f"  FGI={fgi}({fgi_label}) BTC{btc_chg:+.1f}% → {verdict}")

    # ── 多取引所FR一括取得 ──────────────────────────────
    print("  多取引所FR一括取得...")
    fr_bybit = {}; fr_mexc = {}; fr_bitget = {}
    try:
        for t in fetch(f"{BYBIT_BASE}/market/tickers?category=linear")["result"]["list"]:
            sym = t.get("symbol","")
            if sym.endswith("USDT"):
                fr = safe_float(t.get("fundingRate"))
                if fr: fr_bybit[sym[:-4]] = fr
        print(f"  Bybit: {len(fr_bybit)} FR")
    except Exception as e:
        print(f"  Bybit FR failed: {e}")
    try:
        for t in (fetch(f"{MEXC_BASE}/contract/funding_rate").get("data") or []):
            sym = t.get("symbol","")
            if sym.endswith("_USDT"):
                fr = safe_float(t.get("fundingRate"))
                if fr: fr_mexc[sym[:-5]] = fr
        print(f"  MEXC: {len(fr_mexc)} FR")
    except Exception as e:
        print(f"  MEXC FR failed: {e}")
    try:
        for t in (fetch(f"{BITGET_BASE}/mix/market/tickers?productType=USDT-FUTURES").get("data") or []):
            sym = t.get("symbol","")
            if sym.endswith("USDT"):
                fr = safe_float(t.get("fundingRate"))
                if fr: fr_bitget[sym[:-4]] = fr
        print(f"  Bitget: {len(fr_bitget)} FR")
    except Exception as e:
        print(f"  Bitget FR failed: {e}")

    # ── Layer 2: ポンプ検知 ──────────────────────────────
    print("  Layer2: ポンプ検知...")
    tickers = get_all_tickers()
    print(f"  OKX tickers: {len(tickers)}")

    btc_d = []
    try:
        btc_d = [b["c"] for b in get_candles("BTC-USDT-SWAP", "1D", 60)]
    except Exception:
        pass

    prelim = []
    for t in tickers:
        coin = t["instId"].replace("-USDT-SWAP", "")
        if coin in EXCLUDE or coin in STOCK_TOKENS:
            continue
        try:
            last = float(t["last"]); open24 = float(t.get("open24h", last))
            vol = float(t.get("volCcy24h", 0)) * last
        except (TypeError, ValueError):
            continue
        if vol < MIN_VOLUME_24H or open24 <= 0:
            continue
        chg_24h = (last - open24) / open24 * 100
        if chg_24h < PUMP_24H and chg_24h < 5:
            continue
        prelim.append({"inst_id": t["instId"], "coin": coin, "price": last,
                       "open24": open24, "vol": vol, "chg_24h": chg_24h})
    print(f"  Stage-1 prelim: {len(prelim)}")

    # ── Layer 3: 歪みスコア付与 ──────────────────────────
    candidates = []
    for p in prelim:
        inst = p["inst_id"]; coin = p["coin"]
        try:
            c1h = get_candles(inst, "1H", 168)
            chg_1h = 0.0
            if len(c1h) >= 2 and c1h[-2]["o"] > 0:
                chg_1h = (c1h[-1]["c"] - c1h[-2]["o"]) / c1h[-2]["o"] * 100
        except Exception:
            c1h = []; chg_1h = 0.0

        if not (abs(chg_1h) >= PUMP_1H or p["chg_24h"] >= PUMP_24H):
            continue

        try:
            daily = get_candles(inst, "1D", HIST_DAYS)
        except Exception:
            daily = []
        try:
            c4h = get_candles(inst, "4H", 50)
        except Exception:
            c4h = []

        hist = analyze_history(daily) if daily else None
        trend = trend_state_4h(c4h) if c4h else None

        # OKX FR + マルチ取引所FR
        okx_fr, nft_ms, ih = get_okx_funding(inst)
        all_frs = {"OKX": okx_fr} if okx_fr else {}
        for ex_name, fr_dict in [("Bybit", fr_bybit), ("MEXC", fr_mexc), ("Bitget", fr_bitget)]:
            if coin in fr_dict:
                all_frs[ex_name] = fr_dict[coin]
        max_ex = max(all_frs, key=all_frs.get) if all_frs else None
        max_fr = all_frs[max_ex] if max_ex else None
        max_apy = calc_apy(max_fr, ih)

        # ベーシス (swap - spot)
        basis_pct = None
        try:
            spot_d = okx(f"/market/ticker?instId={coin}-USDT")
            spot_price = safe_float(spot_d[0].get("last"))
            if spot_price and spot_price > 0:
                basis_pct = (p["price"] - spot_price) / spot_price * 100
        except Exception:
            pass

        oi_chg = get_oi_change(coin)
        ls_ratio = get_ls_ratio(coin)

        dist_score, dist_reasons = score_distortion(max_fr, basis_pct, oi_chg, ls_ratio)
        repro_score = hist["tier_score"] if hist else 0
        score_total = repro_score + dist_score

        if dist_score < MIN_DISTORTION or score_total < MIN_TOTAL_SCORE:
            print(f"  {coin}: skip (repro={repro_score} dist={dist_score} total={score_total})")
            continue

        sw1h = swing_low_1h(c1h) if c1h else None
        support_7d = min(b["l"] for b in c1h[-168:]) if len(c1h) >= 24 else None
        baseline = p["open24"]
        day_high = daily[-1]["h"] if daily else p["price"]

        candidates.append({
            "inst_id": inst, "coin": coin, "price": p["price"],
            "chg_1h": chg_1h, "chg_24h": p["chg_24h"], "vol": p["vol"],
            "hist": hist, "trend": trend,
            "max_fr": max_fr, "max_ex": max_ex, "max_apy": max_apy,
            "nft_ms": nft_ms, "all_frs": all_frs,
            "basis_pct": basis_pct, "oi_chg": oi_chg, "ls_ratio": ls_ratio,
            "dist_score": dist_score, "distortion_reasons": dist_reasons,
            "repro_score": repro_score, "score_total": score_total,
            "baseline": baseline, "day_high": day_high,
            "swing_low_1h": sw1h, "support_7d": support_7d,
            "_c1h": c1h, "_c4h": c4h,
        })
        print(f"  ✅ {coin}: total={score_total}/14 "
              f"(repro={repro_score} dist={dist_score}) "
              f"1h{chg_1h:+.1f}% 24h{p['chg_24h']:+.1f}% "
              + (f"FR{max_fr*100:+.3f}%@{max_ex}" if max_fr else ""))

    if not candidates:
        print(f"  条件を満たす銘柄なし。スキャン: {scan_time.strftime('%Y-%m-%d %H:%M JST')}")
        # 条件なしでもマクロ環境だけ送信
        if not short_friendly:
            macro_embed = build_macro_embed(fgi, fgi_label, btc_chg, btc_price,
                                            verdict, scan_time)
            discord_post({"content": "", "embeds": [macro_embed],
                         "allowed_mentions": {"parse": []}})
        return

    candidates.sort(key=lambda x: x["score_total"], reverse=True)
    signals = candidates[:MAX_SIGNALS]

    # Discord: マクロembedを1件目に、以降各銘柄embed+チャート
    mention = "@everyone" if MENTION_EVERYONE else ""
    allowed = {"parse": ["everyone"]} if MENTION_EVERYONE else {"parse": []}

    macro_embed = build_macro_embed(fgi, fgi_label, btc_chg, btc_price, verdict, scan_time)

    # 1件目: マクロ + 全銘柄サマリー
    summary_lines = []
    for s in signals:
        bar = "█" * s["score_total"] + "░" * (14 - s["score_total"])
        summary_lines.append(
            f"**{s['coin']}** `{bar}` {s['score_total']}/14  "
            f"1h{s['chg_1h']:+.1f}% 24h{s['chg_24h']:+.1f}%  "
            + (f"FR{s['max_fr']*100:+.3f}%@{s['max_ex']}" if s["max_fr"] else "")
        )
    summary_embed = {
        "title": f"🎯 統合シグナル — {len(signals)}件検知",
        "description": "\n".join(summary_lines),
        "color": 0xFF3333 if signals[0]["score_total"] >= 8 else 0xFF8800,
        "fields": [{"name": "集計条件",
                    "value": f"スキャン: {scan_time.strftime('%Y-%m-%d %H:%M JST')}\n"
                             f"ポンプ閾値: 1h≥{PUMP_1H}% / 24h≥{PUMP_24H}% | "
                             f"歪み下限: {MIN_DISTORTION} | 総合下限: {MIN_TOTAL_SCORE}",
                    "inline": False}],
    }
    discord_post({"content": mention,
                  "embeds": [macro_embed, summary_embed],
                  "allowed_mentions": allowed})

    # 各銘柄: 詳細embed + チャート
    for c in signals:
        embed, plan_levels = build_signal_embed(c, scan_time)
        png = render_chart_png(c["coin"], c["_c1h"], plan_levels)
        if png:
            fname = f"{c['coin']}_chart.png"
            embed["image"] = {"url": f"attachment://{fname}"}
            discord_post({"content": "", "embeds": [embed],
                         "allowed_mentions": {"parse": []}},
                        attachments=[(fname, png)])
        else:
            discord_post({"content": "", "embeds": [embed],
                         "allowed_mentions": {"parse": []}})

    print(f"Sent {len(signals)} signal(s). "
          f"Scan: {scan_time.strftime('%Y-%m-%d %H:%M JST')}")


if __name__ == "__main__":
    main()
