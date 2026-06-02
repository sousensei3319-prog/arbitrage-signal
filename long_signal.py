"""
Long Signal Scanner v1 — ロング戦略 統合シグナル (A/B/C 全部入り)

ショート用 unified_signal.py の対になるロング専用ツール。
3つのロング戦略を同時に走らせ、使い分け・ルールを通知に組み込む。

=== 3戦略 ===
  A. 踏み上げ逆張り (Short Squeeze Reversal)
     FRマイナス過熱 + L/S<1 (ショート偏り) + 暴落後の底値圏
     → ショートの買い戻しで急騰する「踏み上げ」を先回り

  B. モメンタム順張り (Breakout Momentum)
     ポンプ初動 + OI増加 + 出来高急増 + 上昇トレンド
     → 上がり始めに乗ってトレンドの伸びを取る

  C. マクロスイング (Macro Swing)
     FGI極度の恐怖 + BTC環境 → BTC/主要アルトを数日〜数週間保有
     → 統計的に最強(FGI<25で7年+1,145%)。年数回の高確度ロング

=== マクロ環境フィルタ (Layer 0) ===
  FGI + BTC方向で「今どの戦略がON/OFFか」を自動判定:
    FGI<25  → C(マクロ)強ON + A(踏み上げ)ON
    FGI 25-55 → A/B 中立
    FGI>70 + BTC急騰 → ロング全般OFF (ショート環境)

CLAUDE.md: 集計期間(スキャン時刻)を必ず明記する。
"""

import io
import os
import sys
import json
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

# 共通フィルタ
MIN_VOLUME_24H = float(os.environ.get("MIN_VOLUME_24H", "3000000"))
MAX_SIGNALS    = int(os.environ.get("MAX_SIGNALS", "6"))

# A 踏み上げ: 点灯スコア下限
MIN_SCORE_A = int(os.environ.get("MIN_SCORE_A", "5"))
# B モメンタム: 点灯スコア下限
MIN_SCORE_B = int(os.environ.get("MIN_SCORE_B", "5"))

# C マクロ: FGI閾値
FGI_STRONG_BUY = int(os.environ.get("FGI_STRONG_BUY", "20"))  # これ以下=強い買い場
FGI_BUY        = int(os.environ.get("FGI_BUY", "30"))         # これ以下=買い場

EXCLUDE = {"USDC","DAI","TUSD","FDUSD","USDD","USDE","BUSD","PYUSD","BTC","ETH"}
STOCK_TOKENS = {
    "AAPL","MSFT","GOOGL","GOOG","AMZN","META","NVDA","TSLA","AMD","INTC",
    "IBM","ORCL","DELL","MU","SNDK","ARM","CRM","ADBE","NFLX","QCOM","TXN",
    "AVGO","AMAT","LRCX","KLAC","MRVL","SMCI","HPE","HPQ","WDC","STX",
    "COIN","HOOD","MSTR","MARA","RIOT","HUT","CLSK","CIFR","BTBT",
    "COST","WMT","TGT","HD","LOW","NKE","SBUX","MCD",
    "JPM","BAC","GS","MS","WFC","C","BLK","V","MA","PYPL",
    "JNJ","PFE","MRNA","ABBV","LLY","UNH","CVS",
    "XOM","CVX","COP","SLB","BP","SHEL","GE","CAT","BA","LMT","RTX","NOC","GD",
    "QQQ","SPY","IWM","DIA","GLD","SLV","USO","TLT","HYG",
    "SOXS","SOXX","SOXL","FNGU","TQQQ","SQQQ","TSLL","NVDL","TSLQ","MSTU","MSTX",
    "DRAM","CRCL","H","BIDU","BABA","JD","PDD","NIO","XPEV","LI",
}

OKX_BASE    = "https://www.okx.com/api/v5"
BYBIT_BASE  = "https://api.bybit.com/v5"
MEXC_BASE   = "https://contract.mexc.com/api/v1"
BITGET_BASE = "https://api.bitget.com/api/v2"


# ============================================================
# HTTP
# ============================================================
def fetch(url, timeout=20):
    req = urllib.request.Request(
        url, headers={"User-Agent": "long-signal/1.0", "Accept": "application/json"})
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
# Layer 0: マクロ環境
# ============================================================
def get_fgi():
    try:
        d = fetch("https://api.alternative.me/fng/?limit=2", timeout=10)
        now = int(d["data"][0]["value"])
        label = d["data"][0]["value_classification"]
        prev = int(d["data"][1]["value"]) if len(d["data"]) > 1 else now
        return now, label, prev
    except Exception:
        return None, None, None

def get_btc_direction():
    try:
        d = okx("/market/ticker?instId=BTC-USDT-SWAP")
        last = safe_float(d[0].get("last")); open24 = safe_float(d[0].get("open24h"))
        if last and open24 and open24 > 0:
            return (last - open24) / open24 * 100, last
    except Exception:
        pass
    return None, None

def macro_layer(fgi, btc_chg):
    """各戦略のON/OFFを判定。戻り値: dict"""
    btc_rising = btc_chg is not None and btc_chg > 2.0
    btc_crash  = btc_chg is not None and btc_chg < -5.0

    if fgi is None:
        return {"mood": "不明", "A": True, "B": True, "C": False,
                "verdict": "FGI取得失敗 — A/B中立で稼働"}

    if fgi <= 10:        mood = "極度の恐怖(底値圏)"
    elif fgi <= 25:      mood = "極度の恐怖"
    elif fgi <= 45:      mood = "恐怖"
    elif fgi <= 55:      mood = "中立"
    elif fgi <= 75:      mood = "強欲"
    else:                mood = "極度の強欲"

    # C(マクロ): FGI<30 でON、<20 で強ON
    c_on = fgi <= FGI_BUY
    # A(踏み上げ): 恐怖局面 or BTC急落後 = ショート過剰になりやすい
    a_on = fgi <= 55 or btc_crash
    # B(モメンタム): BTC上昇トレンド時のみ (順張りは地合い必須)
    b_on = btc_rising or (fgi >= 45 and not btc_crash)

    # 強欲ピーク+BTC急騰 = ロング全般リスク高
    if fgi > 78 and btc_rising:
        verdict = f"⚠️ FGI{fgi}({mood})+BTC+{btc_chg:.1f}% — 高値掴みリスク。ロング慎重に"
        a_on = False; c_on = False
    elif fgi <= FGI_STRONG_BUY:
        verdict = f"🟢🟢 FGI{fgi}({mood}) — 統計的に最強の買い場(C強ON)"
    elif c_on:
        verdict = f"🟢 FGI{fgi}({mood}) — 買い場(C ON)"
    elif b_on:
        verdict = f"🟡 FGI{fgi}({mood}) BTC{btc_chg:+.1f}% — 順張り環境(B寄り)"
    else:
        verdict = f"⚪ FGI{fgi}({mood}) — 中立"

    return {"mood": mood, "A": a_on, "B": b_on, "C": c_on, "verdict": verdict,
            "btc_rising": btc_rising, "btc_crash": btc_crash}


# ============================================================
# データ取得
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

def ema(values, period):
    if not values: return None
    k = 2 / (period + 1); e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def ema_series(values, period):
    if len(values) < period: return [None]*len(values)
    result = [None]*len(values); k = 2/(period+1)
    e = sum(values[:period])/period; result[period-1] = e
    for i in range(period, len(values)):
        e = values[i]*k + e*(1-k); result[i] = e
    return result


# ============================================================
# 戦略 A: 踏み上げ逆張りロング
# ============================================================
def score_squeeze_long(min_fr, ls_ratio, drawdown_pct, oi_chg, fgi):
    """ショートスクイーズ・ロングのスコア (0〜10)。
    min_fr: 取引所間で最も低いFR (マイナスほど良い)
    ls_ratio: 個人L/S比 (低いほどショート偏り=良い)
    drawdown_pct: 7日高値からの下落率 (マイナス。深いほど底値圏)
    """
    score = 0; reasons = []
    # FRマイナス過熱 = ショートが過剰に積み上がりFRを払っている
    if min_fr is not None:
        fr_pct = min_fr * 100
        if fr_pct < -0.05:
            score += 3; reasons.append(f"FRマイナス過熱 {fr_pct:.3f}%(+3)")
        elif fr_pct < -0.02:
            score += 2; reasons.append(f"FRマイナス {fr_pct:.3f}%(+2)")
        elif fr_pct < -0.005:
            score += 1; reasons.append(f"FRやや弱気 {fr_pct:.3f}%(+1)")
    # L/S比率が低い = 個人がショートに偏り = 踏み上げ燃料
    if ls_ratio is not None:
        if ls_ratio < 0.5:
            score += 3; reasons.append(f"L/S極端ショート偏り {ls_ratio:.2f}(+3)")
        elif ls_ratio < 0.8:
            score += 2; reasons.append(f"L/Sショート偏り {ls_ratio:.2f}(+2)")
        elif ls_ratio < 1.0:
            score += 1; reasons.append(f"L/Sやや弱気 {ls_ratio:.2f}(+1)")
    # 暴落後の底値圏 = 反発余地
    if drawdown_pct is not None:
        if drawdown_pct < -25:
            score += 2; reasons.append(f"7日高値から{drawdown_pct:.0f}%暴落(+2)")
        elif drawdown_pct < -12:
            score += 1; reasons.append(f"7日高値から{drawdown_pct:.0f}%下落(+1)")
    # FGI恐怖ボーナス
    if fgi is not None and fgi <= 30:
        score += 1; reasons.append(f"FGI恐怖{fgi}(+1)")
    return min(score, 10), reasons


# ============================================================
# 戦略 B: モメンタム順張りロング
# ============================================================
def score_momentum_long(chg_1h, chg_24h, oi_chg, vol_surge, above_ema, btc_rising):
    """ブレイクアウト・モメンタムのスコア (0〜10)。
    vol_surge: 直近1h出来高 / 平均出来高 の倍率
    """
    score = 0; reasons = []
    # 初動 (上げ始めだが過熱前: 1h +3〜12%)
    if chg_1h is not None:
        if 3 <= chg_1h <= 12:
            score += 3; reasons.append(f"初動 1h{chg_1h:+.1f}%(+3)")
        elif 12 < chg_1h <= 25:
            score += 1; reasons.append(f"上昇中だがやや過熱 1h{chg_1h:+.1f}%(+1)")
    # OI増加 = 新規ロング流入
    if oi_chg is not None:
        if oi_chg > 15:
            score += 2; reasons.append(f"OI急増 {oi_chg:+.0f}%/1h(+2)")
        elif oi_chg > 5:
            score += 1; reasons.append(f"OI増加 {oi_chg:+.0f}%/1h(+1)")
    # 出来高急増
    if vol_surge is not None:
        if vol_surge > 3.0:
            score += 2; reasons.append(f"出来高{vol_surge:.1f}倍急増(+2)")
        elif vol_surge > 1.8:
            score += 1; reasons.append(f"出来高{vol_surge:.1f}倍増(+1)")
    # 上昇トレンド (4h EMA上)
    if above_ema:
        score += 1; reasons.append("4hEMA20上=上昇トレンド(+1)")
    # BTCラリー地合い
    if btc_rising:
        score += 1; reasons.append("BTCラリー地合い(+1)")
    return min(score, 10), reasons


# ============================================================
# チャート描画
# ============================================================
def render_chart(coin, c1h, levels, direction="long"):
    if not HAS_MPL or not c1h: return None
    try:
        fig, axes = plt.subplots(2, 1, figsize=(8,6), dpi=110,
                                 gridspec_kw={"height_ratios":[3,1]})
        ax, axv = axes
        n_show = 72; bars = c1h[-n_show:]
        closes_all = [b["c"] for b in c1h]
        ema80 = ema_series(closes_all, 80)[-n_show:]
        w = 0.7
        for i, b in enumerate(bars):
            up = b["c"] >= b["o"]
            color = "#26a69a" if up else "#ef5350"
            ax.plot([i,i],[b["l"],b["h"]], color=color, linewidth=0.7)
            top=max(b["o"],b["c"]); bot=min(b["o"],b["c"])
            ax.add_patch(Rectangle((i-w/2,bot), w, max(top-bot,bars[-1]["c"]*0.0005),
                                   facecolor=color, edgecolor=color))
            axv.bar(i, b["v"], width=w, color=color, alpha=0.7)
        xs=[i for i,v in enumerate(ema80) if v]; ys=[v for v in ema80 if v]
        if xs: ax.plot(xs, ys, color="#bb86fc", linewidth=1.2, label=f"EMA80 {ys[-1]:.6g}")
        for key, color, label in [
            ("entry", "#ffeb3b", "エントリー"),
            ("sl",    "#ff5252", "損切り"),
            ("tp1",   "#66bb6a", "TP1"),
            ("tp2",   "#1e88e5", "TP2"),
        ]:
            v = levels.get(key)
            if v: ax.axhline(v, color=color, linestyle="--", linewidth=0.9,
                             label=f"{label} {v:.6g}")
        ax.set_title(f"{coin}/USDT - 1h (直近72h) | {'踏み上げ/モメンタムLONG' if direction=='long' else ''}",
                     color="#eee", fontsize=11)
        ax.legend(fontsize=7, loc="upper left")
        ax.grid(True, alpha=0.2); axv.grid(True, alpha=0.2)
        for a in (ax,axv):
            a.set_facecolor("#1e222d"); a.tick_params(colors="#aaa", labelsize=7)
            for s in a.spines.values(): s.set_color("#444")
        fig.patch.set_facecolor("#131722"); fig.tight_layout()
        buf = io.BytesIO(); fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig); buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        print(f"  chart fail: {e}")
        return None


# ============================================================
# Discord
# ============================================================
def discord_post(payload, attachments=None):
    if not DISCORD_WEBHOOK_URL:
        print("  [DRY-RUN] webhook未設定")
        return
    boundary = f"----long{uuid.uuid4().hex}"
    if not attachments:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=data, method="POST",
            headers={"Content-Type":"application/json","User-Agent":"long-signal/1.0"})
    else:
        body = bytearray()
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="payload_json"\r\n'
        body += b"Content-Type: application/json\r\n\r\n"
        body += json.dumps(payload).encode(); body += b"\r\n"
        for idx,(fn,ct) in enumerate(attachments):
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="files[{idx}]"; filename="{fn}"\r\n'.encode()
            body += b"Content-Type: image/png\r\n\r\n"; body += ct; body += b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=bytes(body), method="POST",
            headers={"Content-Type":f"multipart/form-data; boundary={boundary}",
                     "User-Agent":"long-signal/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15): pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")

def fmt_price(p):
    if p is None: return "n/a"
    a=abs(p)
    if a>=100: return f"${p:,.2f}"
    if a>=1: return f"${p:,.4f}"
    if a>=0.01: return f"${p:.5f}"
    return f"${p:.8f}"

def fmt_vol(v):
    if v is None: return "n/a"
    if v>=1e9: return f"${v/1e9:.2f}B"
    if v>=1e6: return f"${v/1e6:.2f}M"
    return f"${v/1e3:.0f}K"


def build_macro_embed(fgi, label, fgi_prev, btc_chg, btc_price, ml, scan_time):
    now_jst = scan_time.strftime("%Y-%m-%d %H:%M JST")
    active = []
    if ml["A"]: active.append("A踏み上げ")
    if ml["B"]: active.append("Bモメンタム")
    if ml["C"]: active.append("Cマクロ")
    active_str = " / ".join(active) if active else "なし(待機)"
    fgi_str = f"FGI {fgi}({label})" + (f" ←前日{fgi_prev}" if fgi_prev is not None else "") if fgi else "FGI n/a"
    btc_str = f"BTC {btc_chg:+.1f}% (${btc_price:,.0f})" if btc_chg is not None else ""
    return {
        "title": "📈 ロングシグナル — マクロ環境",
        "description": f"{ml['verdict']}\n{fgi_str}　{btc_str}\n**稼働中の戦略: {active_str}**",
        "color": 0x00C853,
        "fields": [{
            "name": "ETFフロー手動確認 (SoSoValue)",
            "value": "→ [BTC ETFフロー](https://sosovalue.com/ja/assets/etf/us-btc-spot) が連続プラス = 機関買い = ロング追い風\n→ 連続マイナス = 機関撤退 = Cマクロは見送り推奨",
            "inline": False
        }],
        "footer": {"text": f"Long Signal v1 | {now_jst}"},
    }

def build_rules_embed():
    return {
        "title": "📖 ロング3戦略 使い分け & ルール",
        "color": 0x607D8B,
        "fields": [
            {"name": "A. 踏み上げ逆張り (Short Squeeze)",
             "value": ("**条件**: FRマイナス過熱 + L/Sショート偏り + 暴落後の底値\n"
                       "**入**: 下げ止まり確認 (1h終値が直近高値/EMA上抜け)\n"
                       "**利確**: TP1=直近戻り高値 / TP2=暴落の半値戻し\n"
                       "**損切**: 直近安値割れ\n"
                       "**注意**: 落ちるナイフ厳禁。下げ止まり確認してから入る"),
             "inline": False},
            {"name": "B. モメンタム順張り (Breakout)",
             "value": ("**条件**: 初動(1h+3〜12%) + OI増 + 出来高急増 + 上昇トレンド\n"
                       "**入**: レジスタンス上抜けの1h終値確定\n"
                       "**利確**: トレイリング or 直近の値幅分(measured move)\n"
                       "**損切**: ブレイク水準割れ (浅く・速く)\n"
                       "**注意**: 勝率低め。損切り厳守。BTC地合い必須"),
             "inline": False},
            {"name": "C. マクロスイング (統計最強)",
             "value": ("**条件**: FGI<25 (極度の恐怖) + ETFフロー回復\n"
                       "**入**: 恐怖の底でBTC/ETH/主要アルトを分割買い(DCA)\n"
                       "**利確**: FGIが強欲(70+)に戻ったら段階利確\n"
                       "**損切**: スイング安値割れ(広め)\n"
                       "**統計**: FGI<25で7年+1,145% / FGI<10は90日平均+48%"),
             "inline": False},
            {"name": "🔄 ショート戦略との使い分け",
             "value": ("FGI高(>70) = ショート環境 → unified_signal.py\n"
                       "FGI低(<30) = ロング環境 → long_signal.py(これ)\n"
                       "中立(30-70) = 個別銘柄の歪み次第で両建て判断"),
             "inline": False},
        ],
        "footer": {"text": "実弾前に必ずペーパーで検証。200ドルはCマクロ中心推奨"},
    }

def build_signal_embed(c, scan_time):
    strat = c["strategy"]
    icon = {"A":"🔄","B":"🚀"}[strat]
    sname = {"A":"踏み上げ逆張り","B":"モメンタム順張り"}[strat]
    score = c["score"]
    color = 0x00E676 if score >= 7 else 0x76FF03 if score >= 5 else 0xCDDC39
    now_jst = scan_time.strftime("%m-%d %H:%M JST")
    fields = [
        {"name":"値動き",
         "value":(f"{fmt_price(c['price'])} | 1h {c['chg_1h']:+.1f}% | 24h {c['chg_24h']:+.1f}% | "
                  f"出来高 {fmt_vol(c['vol'])}"), "inline":False},
        {"name":"スコア根拠", "value":"\n".join(c["reasons"]) or "なし", "inline":False},
    ]
    lv = c["levels"]
    plan = [
        f"🟡 エントリー: {fmt_price(lv['entry'])} ({c['entry_note']})",
        f"🟢 利確1: {fmt_price(lv['tp1'])}",
        f"🔵 利確2: {fmt_price(lv['tp2'])}",
        f"🔴 損切り: {fmt_price(lv['sl'])}",
    ]
    fields.append({"name":"エントリープラン", "value":"\n".join(plan), "inline":False})
    fields.append({"name":"リンク",
        "value":(f"[CoinGlass](https://www.coinglass.com/ja/currencies/{c['coin']}) ・ "
                 f"[SoSoValue](https://sosovalue.com/ja/coins/{c['coin'].lower()}) ・ "
                 f"[OKX](https://www.okx.com/trade-swap/{c['coin'].lower()}-usdt-swap)"),
        "inline":False})
    return {"title": f"{icon} {c['coin']}/USDT — {sname} スコア{score}/10",
            "color": color, "fields": fields,
            "footer":{"text":f"Long Signal v1 | {now_jst}"}}


# ============================================================
# Main
# ============================================================
def main():
    scan_time = datetime.now(JST)
    print(f"Long scan start {scan_time.strftime('%Y-%m-%d %H:%M JST')}")

    # ── Layer 0: マクロ ──
    fgi, fgi_label, fgi_prev = get_fgi()
    btc_chg, btc_price = get_btc_direction()
    ml = macro_layer(fgi, btc_chg)
    print(f"  FGI={fgi}({fgi_label}) BTC{btc_chg:+.1f}% → {ml['verdict']}")
    print(f"  戦略ON: A={ml['A']} B={ml['B']} C={ml['C']}")

    # ── 多取引所FR一括 (Aの最小FR用) ──
    fr_bybit={}; fr_mexc={}; fr_bitget={}
    try:
        for t in fetch(f"{BYBIT_BASE}/market/tickers?category=linear")["result"]["list"]:
            s=t.get("symbol","")
            if s.endswith("USDT"):
                v=safe_float(t.get("fundingRate"))
                if v is not None: fr_bybit[s[:-4]]=v
    except Exception as e: print(f"  Bybit FR fail: {e}")
    try:
        for t in (fetch(f"{MEXC_BASE}/contract/funding_rate").get("data") or []):
            s=t.get("symbol","")
            if s.endswith("_USDT"):
                v=safe_float(t.get("fundingRate"))
                if v is not None: fr_mexc[s[:-5]]=v
    except Exception as e: print(f"  MEXC FR fail: {e}")
    try:
        for t in (fetch(f"{BITGET_BASE}/mix/market/tickers?productType=USDT-FUTURES").get("data") or []):
            s=t.get("symbol","")
            if s.endswith("USDT"):
                v=safe_float(t.get("fundingRate"))
                if v is not None: fr_bitget[s[:-4]]=v
    except Exception as e: print(f"  Bitget FR fail: {e}")
    print(f"  FR: Bybit{len(fr_bybit)} MEXC{len(fr_mexc)} Bitget{len(fr_bitget)}")

    tickers = get_all_tickers()
    print(f"  OKX tickers: {len(tickers)}")

    # ── 候補抽出 ──
    cand_A = []; cand_B = []
    for t in tickers:
        coin = t["instId"].replace("-USDT-SWAP","")
        if coin in EXCLUDE or coin in STOCK_TOKENS:
            continue
        try:
            last=float(t["last"]); open24=float(t.get("open24h",last))
            vol=float(t.get("volCcy24h",0))*last
        except (TypeError,ValueError):
            continue
        if vol < MIN_VOLUME_24H or open24<=0:
            continue
        chg_24h=(last-open24)/open24*100

        # A候補: 24h下落 or FRマイナス気配 / B候補: 24h上昇
        is_A_cand = ml["A"] and chg_24h < 0
        is_B_cand = ml["B"] and chg_24h > 5
        if not (is_A_cand or is_B_cand):
            continue

        inst=t["instId"]
        try:
            c1h=get_candles(inst,"1H",168)
            chg_1h=0.0
            if len(c1h)>=2 and c1h[-2]["o"]>0:
                chg_1h=(c1h[-1]["c"]-c1h[-2]["o"])/c1h[-2]["o"]*100
        except Exception:
            continue
        if len(c1h) < 30:
            continue

        # 共通指標
        okx_fr,_,_ = get_okx_funding(inst)
        frs=[v for v in [okx_fr, fr_bybit.get(coin), fr_mexc.get(coin), fr_bitget.get(coin)] if v is not None]
        min_fr = min(frs) if frs else None
        max_fr = max(frs) if frs else None
        oi_chg = get_oi_change(coin)
        ls = get_ls_ratio(coin)

        high_7d = max(b["h"] for b in c1h[-168:]) if len(c1h)>=24 else last
        low_7d  = min(b["l"] for b in c1h[-168:]) if len(c1h)>=24 else last
        drawdown = (last - high_7d)/high_7d*100 if high_7d>0 else 0

        # 4h trend
        try:
            c4h=get_candles(inst,"4H",50)
            above_ema = c4h[-1]["c"] > (ema([b["c"] for b in c4h],20) or c4h[-1]["c"])
        except Exception:
            above_ema=False

        # 出来高サージ
        vols=[b["v"] for b in c1h[-25:-1]]
        avg_vol=sum(vols)/len(vols) if vols else 0
        vol_surge = c1h[-1]["v"]/avg_vol if avg_vol>0 else None

        # ── A スコア ──
        if is_A_cand:
            sa, ra = score_squeeze_long(min_fr, ls, drawdown, oi_chg, fgi)
            if sa >= MIN_SCORE_A:
                entry = max(b["h"] for b in c1h[-7:-1])  # 直近6h高値の上抜け
                tp1 = high_7d - 0.5*(high_7d-low_7d)      # 半値戻し
                tp2 = high_7d                              # 全戻し
                sl  = low_7d * 0.99
                cand_A.append({"strategy":"A","coin":coin,"price":last,
                    "chg_1h":chg_1h,"chg_24h":chg_24h,"vol":vol,"score":sa,"reasons":ra,
                    "levels":{"entry":entry,"tp1":tp1,"tp2":tp2,"sl":sl},
                    "entry_note":"直近高値上抜けで","_c1h":c1h})
                print(f"  🔄A {coin}: {sa}/10 FRmin{(min_fr or 0)*100:+.3f}% L/S{ls} DD{drawdown:.0f}%")

        # ── B スコア ──
        if is_B_cand:
            sb, rb = score_momentum_long(chg_1h, chg_24h, oi_chg, vol_surge, above_ema, ml["btc_rising"])
            if sb >= MIN_SCORE_B:
                entry = c1h[-1]["h"]                       # 直近高値ブレイク
                atr = (high_7d-low_7d)
                tp1 = last + 0.5*abs(last-low_7d)
                tp2 = last + 1.0*abs(last-low_7d)
                sl  = min(b["l"] for b in c1h[-4:])        # 直近3hの安値
                cand_B.append({"strategy":"B","coin":coin,"price":last,
                    "chg_1h":chg_1h,"chg_24h":chg_24h,"vol":vol,"score":sb,"reasons":rb,
                    "levels":{"entry":entry,"tp1":tp1,"tp2":tp2,"sl":sl},
                    "entry_note":"レジスタンス上抜けで","_c1h":c1h})
                print(f"  🚀B {coin}: {sb}/10 1h{chg_1h:+.1f}% OI{oi_chg} vol surge{vol_surge}")

    cand_A.sort(key=lambda x:x["score"], reverse=True)
    cand_B.sort(key=lambda x:x["score"], reverse=True)
    signals = (cand_A[:MAX_SIGNALS] + cand_B[:MAX_SIGNALS])

    print(f"\n集計: A{len(cand_A)}件 B{len(cand_B)}件  "
          f"スキャン {scan_time.strftime('%Y-%m-%d %H:%M JST')}")

    # ── Discord ──
    # メンションは「個別銘柄シグナルあり」or「FGIが買い場に新規突入」時のみ。
    # 地合い(C)が継続中なだけで毎回鳴らさない (通知疲れ防止)。
    has_coin_signal = bool(signals)
    c_fresh_cross = (ml["C"] and fgi is not None and fgi_prev is not None
                     and fgi_prev > FGI_BUY and fgi <= FGI_BUY)
    should_mention = MENTION_EVERYONE and (has_coin_signal or c_fresh_cross)
    mention = "@everyone" if should_mention else ""
    allowed = {"parse":["everyone"]} if should_mention else {"parse":[]}
    if c_fresh_cross:
        print("  → FGI買い場に新規突入 → メンション")

    macro_embed = build_macro_embed(fgi, fgi_label, fgi_prev, btc_chg, btc_price, ml, scan_time)
    rules_embed = build_rules_embed()

    # C マクロは銘柄でなく地合い通知
    embeds_first = [macro_embed]
    if ml["C"]:
        c_strength = "🟢🟢 強い買い場" if (fgi and fgi <= FGI_STRONG_BUY) else "🟢 買い場"
        embeds_first.append({
            "title": f"💎 Cマクロスイング発動 — {c_strength}",
            "description": (f"FGI {fgi}({fgi_label}) = 統計的買い場。\n"
                            "**BTC/ETH/主要アルトを分割買い(DCA)推奨**\n"
                            "利確: FGIが70+(強欲)に戻るまで保有\n"
                            "→ ETFフロー要確認: https://sosovalue.com/ja/assets/etf/us-btc-spot"),
            "color": 0x00BFA5,
        })

    if not signals and not ml["C"]:
        # 何もなければマクロ+ルールだけ簡潔に
        discord_post({"content":mention, "embeds":[macro_embed],
                     "allowed_mentions":allowed})
        print("個別シグナルなし。マクロ環境のみ通知。")
        return

    # サマリー
    if signals:
        lines=[]
        for s in signals:
            bar="█"*s["score"]+"░"*(10-s["score"])
            tag={"A":"🔄踏上","B":"🚀勢い"}[s["strategy"]]
            lines.append(f"{tag} **{s['coin']}** `{bar}` {s['score']}/10 1h{s['chg_1h']:+.1f}%")
        embeds_first.append({
            "title": f"🎯 ロング候補 {len(signals)}件",
            "description":"\n".join(lines),
            "color":0x00E676,
            "fields":[{"name":"集計","value":f"スキャン: {scan_time.strftime('%Y-%m-%d %H:%M JST')}\n"
                       f"A閾値{MIN_SCORE_A} / B閾値{MIN_SCORE_B} / 出来高>{fmt_vol(MIN_VOLUME_24H)}","inline":False}],
        })

    # Discord embed上限10。先頭群(マクロ+C+サマリー) + ルール
    discord_post({"content":mention, "embeds":embeds_first[:10]+[rules_embed],
                 "allowed_mentions":allowed})

    # 各銘柄 詳細+チャート
    for c in signals:
        embed = build_signal_embed(c, scan_time)
        png = render_chart(c["coin"], c["_c1h"], c["levels"])
        if png:
            fn=f"{c['coin']}_long.png"
            embed["image"]={"url":f"attachment://{fn}"}
            discord_post({"content":"","embeds":[embed],"allowed_mentions":{"parse":[]}},
                        attachments=[(fn,png)])
        else:
            discord_post({"content":"","embeds":[embed],"allowed_mentions":{"parse":[]}})

    print(f"Sent macro + {len(signals)} signal(s). "
          f"Scan: {scan_time.strftime('%Y-%m-%d %H:%M JST')}")


if __name__ == "__main__":
    main()
