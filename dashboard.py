"""
Integrated Distortion Dashboard v1 (③統合ダッシュボード)

5つの歪みシグナルを統合してショートエントリー候補をスコアリングする本命ツール。
①②の個別スキャナーとは違い「稼ぐarb」ではなく「方向シグナル」が目的。

[5つの歪み]
  ① 価格スプレッド     : 取引所間の価格差   → 過熱の証拠
  ② FR過熱           : ファンディングレート高騰 → ロング積み上がり
  ③ ベーシスプレミアム  : 先物 > 現物       → 先高期待の過熱
  ④ OI急増           : 建玉急増          → レバレッジ積み上がり
  ⑤ L/S比率過熱      : 個人ロング偏り     → 逆噴射リスク

スコア 0〜10 の高い銘柄 = 複数シグナルが重なる = ショート候補の優先度高
→ validated_short_strategy (ポンプ後ショート) の点火フィルタとして使う

データソース (全て公開API・無認証):
  価格/FR/ベーシス : OKX (個別) + Bybit/MEXC/Bitget (一括)
  OI変化/L/S比率  : OKX rubik (既存pump-screenerで実績済み)
  価格スプレッド   : Bybit/MEXC/Bitget のbid-ask vs OKX

CLAUDE.md ルール: 出力には必ずスキャン時刻(集計期間)を明記する。
"""

import csv
import json
import os
import sys
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
# Config
# ============================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MENTION_EVERYONE    = os.environ.get("MENTION_EVERYONE", "0") == "1"

MIN_SCORE       = int(os.environ.get("MIN_SCORE", "3"))       # 通知スコア下限
MIN_VOL         = float(os.environ.get("MIN_VOL", "3000000")) # 24h出来高下限($)
MAX_SIGNALS     = int(os.environ.get("MAX_SIGNALS", "10"))
CSV_PATH        = os.environ.get("CSV_PATH", "signals_log.csv")

# ステーブルコイン除外
EXCLUDE = {"USDC","DAI","TUSD","FDUSD","USDD","USDE","BUSD","PYUSD"}

# 株式・ETFトークン除外 (取引所が上場している現物株/ETFトークン)
# これらは FR が異常高騰しやすいが仮想通貨ではない
STOCK_TOKENS = {
    # 米国株 テック
    "AAPL","MSFT","GOOGL","GOOG","AMZN","META","NVDA","TSLA","AMD","INTC",
    "IBM","ORCL","DELL","MU","SNDK","ARM","CRM","ADBE","NFLX","QCOM","TXN",
    "AVGO","AMAT","LRCX","KLAC","MRVL","SMCI","HPE","HPQ","WDC","STX",
    # 米国株 その他
    "COIN","HOOD","MSTR","MARA","RIOT","HUT","CLSK","CIFR","BTBT",
    "AAON","COST","WMT","TGT","HD","LOW","NKE","SBUX","MCD",
    "JPM","BAC","GS","MS","WFC","C","BLK","V","MA","PYPL",
    "JNJ","PFE","MRNA","ABBV","LLY","UNH","CVS",
    "XOM","CVX","COP","SLB","BP","SHEL",
    "GE","CAT","BA","LMT","RTX","NOC","GD",
    "TSMC","TSM","SAMSUNG","005930",
    # ETF・指数
    "QQQ","SPY","IWM","DIA","GLD","SLV","USO","TLT","HYG",
    "SOXS","SOXX","FNGU","TQQQ","SQQQ",
    # その他株系トークン (取引所独自)
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
        url, headers={"User-Agent": "dashboard/1.0", "Accept": "application/json"})
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
        return v if v == v else default  # NaN guard
    except (TypeError, ValueError):
        return default


# ============================================================
# データ取得
# ============================================================
def get_okx_swap_tickers():
    """OKX 全USDTスワップ: price, vol, inst_id"""
    data = okx("/market/tickers?instType=SWAP")
    out = {}
    for t in data:
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        coin = t["instId"].replace("-USDT-SWAP", "")
        if coin in EXCLUDE or coin in STOCK_TOKENS:
            continue
        last = safe_float(t.get("last"))
        vol  = (safe_float(t.get("volCcy24h")) or 0) * (last or 0)
        if not last or vol < MIN_VOL:
            continue
        out[coin] = {"price": last, "vol": vol, "inst_id": t["instId"]}
    return out


def get_okx_spot_prices(coins):
    """OKX 現物価格を一括取得 → ベーシス計算用"""
    out = {}
    # tickers 一括取得
    try:
        data = okx("/market/tickers?instType=SPOT")
        for t in data:
            if not t["instId"].endswith("-USDT"):
                continue
            coin = t["instId"][:-5]
            if coin in coins:
                out[coin] = safe_float(t.get("last"))
    except Exception:
        pass
    return out


def get_fr_multi(coins):
    """OKX個別 + Bybit/MEXC/Bitget一括 → {coin: {ex: fr}}"""
    fr_map = {c: {} for c in coins}

    # Bybit
    try:
        data = fetch(f"{BYBIT_BASE}/market/tickers?category=linear")["result"]["list"]
        for t in data:
            sym = t.get("symbol","")
            if not sym.endswith("USDT"): continue
            coin = sym[:-4]
            fr = safe_float(t.get("fundingRate"))
            if coin in fr_map and fr is not None:
                fr_map[coin]["Bybit"] = fr
    except Exception as e:
        print(f"  Bybit FR: {e}")

    # MEXC
    try:
        data = fetch(f"{MEXC_BASE}/contract/funding_rate").get("data", [])
        for t in data:
            sym = t.get("symbol","")
            if not sym.endswith("_USDT"): continue
            coin = sym[:-5]
            fr = safe_float(t.get("fundingRate"))
            if coin in fr_map and fr is not None:
                fr_map[coin]["MEXC"] = fr
    except Exception as e:
        print(f"  MEXC FR: {e}")

    # Bitget
    try:
        data = fetch(f"{BITGET_BASE}/mix/market/tickers?productType=USDT-FUTURES").get("data",[])
        for t in data:
            sym = t.get("symbol","")
            if not sym.endswith("USDT"): continue
            coin = sym[:-4]
            fr = safe_float(t.get("fundingRate"))
            if coin in fr_map and fr is not None:
                fr_map[coin]["Bitget"] = fr
    except Exception as e:
        print(f"  Bitget FR: {e}")

    # OKX (個別 — 出来高フィルタ済みの銘柄だけ)
    for coin in list(coins)[:80]:  # API負荷軽減のため上位80銘柄
        try:
            d = okx(f"/public/funding-rate?instId={coin}-USDT-SWAP")
            if d:
                fr_map[coin]["OKX"] = safe_float(d[0].get("fundingRate"))
        except Exception:
            pass

    return fr_map


def get_oi_change(coin):
    """OKX: 直近1時間のOI変化率(%)"""
    try:
        d = okx(f"/rubik/stat/contracts/open-interest-volume?ccy={coin}&period=1H")
        if len(d) < 2: return None
        now  = safe_float(d[0][1])
        prev = safe_float(d[1][1])
        if not prev or prev == 0: return None
        return (now - prev) / prev * 100
    except Exception:
        return None


def get_ls_ratio(coin):
    """OKX: 個人L/S比率 (>1 = ロング優勢)"""
    try:
        d = okx(f"/rubik/stat/contracts/long-short-account-ratio?ccy={coin}&period=5m")
        return safe_float(d[0][1]) if d else None
    except Exception:
        return None


def get_price_spread(coin, okx_price, ex_prices):
    """取引所間の価格スプレッド% (max_bid - min_ask) / min_ask × 100"""
    prices = [okx_price] + [p for p in ex_prices.values() if p]
    if len(prices) < 2: return None
    return (max(prices) - min(prices)) / min(prices) * 100


def get_bybit_spot_prices(coins):
    """Bybit 現物価格 (スプレッド計算用)"""
    out = {}
    try:
        data = fetch(f"{BYBIT_BASE}/market/tickers?category=spot")["result"]["list"]
        for t in data:
            sym = t.get("symbol","")
            if not sym.endswith("USDT"): continue
            coin = sym[:-4]
            if coin in coins:
                out[coin] = safe_float(t.get("lastPrice"))
    except Exception:
        pass
    return out


# ============================================================
# スコアリング
# ============================================================
def calc_max_fr(fr_by_ex):
    """取引所間の最大FR"""
    frs = {ex: fr for ex, fr in fr_by_ex.items() if fr is not None}
    if not frs: return None, None
    max_ex = max(frs, key=frs.get)
    return frs[max_ex], max_ex


def calc_apy(fr, interval_h=8):
    if fr is None: return None
    return fr * (24 / interval_h) * 365 * 100


def score_distortion(max_fr, basis_pct, oi_chg, ls_ratio, spread_pct):
    """
    5つの歪みをスコアリング (0〜10点)
    高スコア = 複数シグナル重なり = ショート候補優先度高
    """
    score = 0
    reasons = []

    # ② FR過熱
    if max_fr is not None:
        fr_pct = max_fr * 100
        if fr_pct > 0.10:
            score += 3; reasons.append(f"FR超過熱 {fr_pct:+.3f}%(+3)")
        elif fr_pct > 0.05:
            score += 2; reasons.append(f"FR過熱 {fr_pct:+.3f}%(+2)")
        elif fr_pct > 0.02:
            score += 1; reasons.append(f"FR高め {fr_pct:+.3f}%(+1)")

    # ③ ベーシスプレミアム
    if basis_pct is not None:
        if basis_pct > 0.5:
            score += 2; reasons.append(f"ベーシス過熱 {basis_pct:+.2f}%(+2)")
        elif basis_pct > 0.2:
            score += 1; reasons.append(f"先物プレミアム {basis_pct:+.2f}%(+1)")

    # ④ OI急増
    if oi_chg is not None:
        if oi_chg > 15:
            score += 2; reasons.append(f"OI急増 {oi_chg:+.1f}%/1h(+2)")
        elif oi_chg > 7:
            score += 1; reasons.append(f"OI増加 {oi_chg:+.1f}%/1h(+1)")

    # ⑤ L/S比率
    if ls_ratio is not None:
        if ls_ratio > 2.0:
            score += 2; reasons.append(f"L/S過熱 {ls_ratio:.2f}(+2)")
        elif ls_ratio > 1.5:
            score += 1; reasons.append(f"L/S偏重 {ls_ratio:.2f}(+1)")

    # ① 価格スプレッド
    if spread_pct is not None:
        if spread_pct > 1.0:
            score += 1; reasons.append(f"価格乖離 {spread_pct:.2f}%(+1)")
        elif spread_pct > 0.5:
            score += 1; reasons.append(f"スプレッド {spread_pct:.2f}%(+1)")

    return min(score, 10), reasons


# ============================================================
# フォーマット
# ============================================================
def fmt_vol(v):
    if v is None: return "n/a"
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.2f}M"
    return f"${v/1e3:.0f}K"

def fmt_price(p):
    if p is None: return "n/a"
    if abs(p) >= 100: return f"${p:,.2f}"
    if abs(p) >= 1:   return f"${p:,.4f}"
    return f"${p:.6f}"

def score_color(score):
    if score >= 7: return 0xFF3333   # 赤: 強推奨
    if score >= 5: return 0xFF8800   # オレンジ: 要注意
    if score >= 3: return 0xFFCC00   # 黄: 監視
    return 0x888888


# ============================================================
# CSV 記録
# ============================================================
CSV_HEADER = [
    "timestamp_jst","coin","score","max_fr_pct","max_ex","apy_pct",
    "basis_pct","oi_chg_pct","ls_ratio","spread_pct","vol_usd","reasons"
]

def append_csv(signals, scan_time):
    """シグナルをCSVに追記。ファイルなければヘッダーを先に書く。"""
    ts = scan_time.strftime("%Y-%m-%d %H:%M JST")
    write_header = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    try:
        with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(CSV_HEADER)
            for s in signals:
                w.writerow([
                    ts,
                    s["coin"],
                    s["score"],
                    round(s["max_fr"] * 100, 4),
                    s["max_ex"],
                    round(s["max_apy"], 1),
                    round(s["basis_pct"], 3) if s["basis_pct"] is not None else "",
                    round(s["oi_chg"], 1) if s["oi_chg"] is not None else "",
                    round(s["ls_ratio"], 2) if s["ls_ratio"] is not None else "",
                    round(s["spread_pct"], 3) if s["spread_pct"] is not None else "",
                    int(s["vol"]),
                    " | ".join(s["reasons"]),
                ])
        print(f"  CSV追記: {len(signals)}件 → {CSV_PATH}")
    except Exception as e:
        print(f"  CSV書き込みエラー: {e}")


# ============================================================
# Discord
# ============================================================
def discord_post(payload):
    if not DISCORD_WEBHOOK_URL:
        print("  [DRY-RUN] DISCORD_WEBHOOK_URL 未設定")
        return
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15): pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")


def build_embed(signals, scan_time):
    now_jst = scan_time.strftime("%Y-%m-%d %H:%M JST")

    if not signals:
        return {
            "title": "🎯 統合歪みダッシュボード — 異常なし",
            "description": f"スコア{MIN_SCORE}以上の銘柄なし。市場は均衡状態。\nスキャン: {now_jst}",
            "color": 0x444444,
            "footer": {"text": "Integrated Distortion Dashboard v1"},
        }

    # 上位3件は個別embed、残りはリスト
    embeds = []

    # サマリーembed
    summary_lines = []
    for s in signals:
        bar = "█" * s["score"] + "░" * (10 - s["score"])
        summary_lines.append(
            f"**{s['coin']}** `{bar}` {s['score']}/10  "
            f"FR{s['max_fr']*100:+.3f}% APY{s['max_apy']:.0f}%  "
            f"OI{s['oi_chg']:+.0f}%  L/S{s['ls_str']}"
        )

    summary = {
        "title": f"🎯 統合歪みダッシュボード — {len(signals)}件検知",
        "description": "\n".join(summary_lines),
        "color": score_color(signals[0]["score"]),
        "fields": [
            {"name": "集計条件",
             "value": f"スキャン時刻: {now_jst}\n対象: OKX+Bybit+MEXC+Bitget / 出来高>${fmt_vol(MIN_VOL)}\nスコア{MIN_SCORE}以上 = 複数歪みが重なる銘柄",
             "inline": False},
            {"name": "読み方",
             "value": (
                 "高スコア = FR過熱+ベーシス+OI急増+L/S偏り が重なる = **ロング積み上がり→逆噴射リスク**\n"
                 "validated_short_strategy の点火フィルタ:\n"
                 "　スコア≥5 + ポンプ確認 → ショート検討\n"
                 "　スコア≥7 → 高優先度シグナル"
             ), "inline": False},
        ],
        "footer": {"text": "Integrated Distortion Dashboard v1"},
    }
    embeds.append(summary)

    # 上位3件の詳細embed
    for s in signals[:3]:
        fr_lines = " / ".join(f"{ex}:{fr*100:+.3f}%" for ex,fr in s["fr_by_ex"].items() if fr is not None)
        fields = [
            {"name": "スコア根拠",
             "value": "\n".join(s["reasons"]) or "なし", "inline": False},
            {"name": "FR詳細 (マルチ取引所)",
             "value": f"{fr_lines}\n最高FR: **{s['max_fr']*100:+.3f}%** @ {s['max_ex']}  APY: **{s['max_apy']:.1f}%**",
             "inline": False},
            {"name": "マーケット状態",
             "value": "\n".join([
                 f"価格: {fmt_price(s['price'])}  出来高: {fmt_vol(s['vol'])}",
                 "ベーシス(先物-現物): " + (f"{s['basis_pct']:+.3f}%" if s['basis_pct'] is not None else "n/a"),
                 "OI変化: " + (f"{s['oi_chg']:+.1f}%/1h" if s['oi_chg'] is not None else "n/a"),
                 f"L/S比率: {s['ls_str']}",
                 "価格スプレッド: " + (f"{s['spread_pct']:.2f}%" if s['spread_pct'] is not None else "n/a"),
             ]), "inline": False},
            {"name": "ショート戦略との照合",
             "value": (
                 f"{'✅' if s['score'] >= 7 else '🟡' if s['score'] >= 5 else '⚪'} "
                 f"優先度: {'高（即監視）' if s['score'] >= 7 else '中（ポンプ待ち）' if s['score'] >= 5 else '低（記録のみ）'}\n"
                 "→ エントリー条件: ポンプ確認 + 1h終値ブレイクダウン\n"
                 f"→ [CoinGlass](https://www.coinglass.com/ja/currencies/{s['coin']}) ・ "
                 f"[OKX](https://www.okx.com/trade-swap/{s['coin'].lower()}-usdt-swap)"
             ), "inline": False},
        ]
        embeds.append({
            "title": f"{'🔴' if s['score']>=7 else '🟠' if s['score']>=5 else '🟡'} "
                     f"{s['coin']}/USDT  スコア {s['score']}/10",
            "color": score_color(s["score"]),
            "fields": fields,
        })

    return embeds


# ============================================================
# Main
# ============================================================
def main():
    scan_time = datetime.now(JST)
    print(f"Dashboard scan start {scan_time.strftime('%Y-%m-%d %H:%M JST')}")

    # ---- Step 1: OKX出来高フィルタ済みの銘柄リスト ----
    print("  OKX スワップ一覧取得...")
    tickers = get_okx_swap_tickers()
    coins = set(tickers.keys())
    print(f"  出来高フィルタ後: {len(coins)} 銘柄")

    # ---- Step 2: 現物価格 (ベーシス用) ----
    print("  OKX 現物価格取得...")
    spot_prices = get_okx_spot_prices(coins)

    # ---- Step 3: マルチ取引所FR取得 ----
    print("  FR マルチ取引所取得...")
    fr_map = get_fr_multi(coins)

    # ---- Step 4: Bybit現物価格 (スプレッド用) ----
    print("  Bybit 現物価格取得...")
    bybit_prices = get_bybit_spot_prices(coins)

    # ---- Step 5: 銘柄ごとに5シグナルを計算・スコアリング ----
    candidates = []
    print(f"  銘柄スコアリング中 (OI/L/S は上位候補のみ)...")

    # まずFR/ベーシス/スプレッドだけで一次スクリーニング
    prelim = []
    for coin in coins:
        info = tickers[coin]
        fr_by_ex = fr_map.get(coin, {})
        max_fr, max_ex = calc_max_fr(fr_by_ex)
        if max_fr is None or max_fr <= 0:
            continue
        max_apy = calc_apy(max_fr)

        # スワップ価格 vs 現物価格 → ベーシス
        swap_price = info["price"]
        spot_price = spot_prices.get(coin)
        basis_pct = None
        if spot_price and spot_price > 0:
            basis_pct = (swap_price - spot_price) / spot_price * 100

        # 価格スプレッド
        bybit_p = bybit_prices.get(coin)
        spread_pct = get_price_spread(coin, swap_price,
                                      {"Bybit": bybit_p} if bybit_p else {})

        # 一次スコア (OI/L/Sなし)
        pre_score, _ = score_distortion(max_fr, basis_pct, None, None, spread_pct)
        if pre_score < 1:
            continue

        prelim.append({
            "coin": coin, "info": info, "fr_by_ex": fr_by_ex,
            "max_fr": max_fr, "max_ex": max_ex, "max_apy": max_apy,
            "basis_pct": basis_pct, "spread_pct": spread_pct,
            "pre_score": pre_score,
        })

    prelim.sort(key=lambda x: x["pre_score"], reverse=True)
    print(f"  一次スクリーニング通過: {len(prelim)} 銘柄 → 上位{min(len(prelim),40)}件にOI/L/S取得")

    # 上位40件だけOI/L/S取得 (API節約)
    for p in prelim[:40]:
        coin = p["coin"]
        oi_chg = get_oi_change(coin)
        ls_ratio = get_ls_ratio(coin)

        score, reasons = score_distortion(
            p["max_fr"], p["basis_pct"], oi_chg, ls_ratio, p["spread_pct"])

        if score < MIN_SCORE:
            continue

        ls_str = f"{ls_ratio:.2f}" if ls_ratio is not None else "n/a"
        oi_val = oi_chg if oi_chg is not None else 0.0

        candidates.append({
            **p,
            "score": score, "reasons": reasons,
            "oi_chg": oi_val, "ls_ratio": ls_ratio, "ls_str": ls_str,
            "price": p["info"]["price"], "vol": p["info"]["vol"],
        })
        basis_str = f"  ベーシス{p['basis_pct']:+.2f}%" if p["basis_pct"] else ""
        print(f"  ✅ {coin}: {score}/10  FR{p['max_fr']*100:+.3f}%@{p['max_ex']}{basis_str}")

    candidates.sort(key=lambda x: x["score"], reverse=True)
    signals = candidates[:MAX_SIGNALS]

    print(f"\n集計: {len(signals)} 件点灯 (スコア≥{MIN_SCORE})"
          f"  スキャン時刻 {scan_time.strftime('%Y-%m-%d %H:%M JST')}")

    if signals:
        print("トップ5:")
        for s in signals[:5]:
            print(f"  {s['coin']}: {s['score']}/10  "
                  f"FR{s['max_fr']*100:+.3f}%@{s['max_ex']}  "
                  f"APY{s['max_apy']:.0f}%  "
                  f"OI{s['oi_chg']:+.1f}%  L/S{s['ls_str']}"
                  f"  {', '.join(s['reasons'][:2])}")

    # CSV に記録
    if signals:
        append_csv(signals, scan_time)

    embeds = build_embed(signals, scan_time)
    if isinstance(embeds, list):
        # 最大10 embeds/メッセージ制限に対応 (サマリー+詳細3=4)
        mention = "@everyone" if MENTION_EVERYONE else ""
        allowed = {"parse": ["everyone"]} if MENTION_EVERYONE else {"parse": []}
        discord_post({"content": mention, "embeds": embeds[:10], "allowed_mentions": allowed})
    else:
        discord_post({"content": "", "embeds": [embeds], "allowed_mentions": {"parse": []}})


if __name__ == "__main__":
    main()
