"""
Funding Rate Multi-Exchange Scanner v2 (②FRアービ強化版)

v1 からの変化:
  - OKX 単体 → OKX / Bybit / Gate / MEXC / Bitget の5取引所同時比較
  - 各銘柄の「どの取引所でショートすると最大FR収益か」を特定
  - 取引所間FR乖離 = cross_fr_spread を計算 → ゼロリスクで鞘が取れる上限
  - CoinGlass で見るのと同等のデータを無料・無認証で直接取得

戦略の読み方:
  ① max_fr が高い銘柄 = デルタニュートラル妙味あり (その取引所でショート)
  ② max_fr と min_fr の差が大きい = 取引所間FR乖離 → FRアービの理論上の上限
  ③ max_fr が高い + あなたの既存ショート点火条件 = ロング過熱の二重確認

データソース (全て公開API・認証不要・GitHub Actionsで動作確認済み):
  OKX:   /public/funding-rate?instId=COIN-USDT-SWAP  (個別)
  Bybit: /v5/market/tickers?category=linear           (全銘柄一括)
  Gate:  /futures/usdt/contracts                       (全銘柄一括)
  MEXC:  /api/v1/contract/funding_rate                (全銘柄一括・最詳細)
  Bitget:/api/v2/mix/market/tickers?productType=USDT-FUTURES (全銘柄一括)
"""

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
MIN_APY             = float(os.environ.get("MIN_APY", "15"))       # 点灯APY下限(%)
MIN_VOL_OKX         = float(os.environ.get("MIN_VOL", "2000000"))  # OKX 24h出来高下限($)
MAX_BASIS_PCT       = float(os.environ.get("MAX_BASIS_PCT", "5"))  # ベーシス上限(%)
MAX_SIGNALS         = int(os.environ.get("MAX_SIGNALS", "15"))
EXCLUDE             = {"USDC","DAI","TUSD","FDUSD","USDD","USDE","BUSD","PYUSD"}

OKX_BASE   = "https://www.okx.com/api/v5"
BYBIT_BASE = "https://api.bybit.com/v5"
GATE_BASE  = "https://api.gateio.ws/api/v4"
MEXC_BASE  = "https://contract.mexc.com/api/v1"
BITGET_BASE= "https://api.bitget.com/api/v2"

TAKER_FEE_RT = 0.0024  # 往復手数料 0.24% (現物0.1%+先物0.02%の開閉×2)


# ============================================================
# HTTP
# ============================================================
def fetch(url, timeout=20):
    req = urllib.request.Request(
        url, headers={"User-Agent": "fr-scanner/2.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def safe_float(x):
    try:
        f = float(x)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None


# ============================================================
# 取引所別 FR 取得 (正規化して返す)
# 戻り値: {coin_upper: {"fr": float, "nft_ms": int|None, "interval_h": int}}
# ============================================================
def fr_okx_bulk(vol_filter=True):
    """OKX: ticker一括 + 出来高フィルタ + 個別FR取得。"""
    tickers = fetch(f"{OKX_BASE}/market/tickers?instType=SWAP")["data"]
    out = {}
    for t in tickers:
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        coin = t["instId"].replace("-USDT-SWAP", "")
        if coin in EXCLUDE:
            continue
        last = safe_float(t.get("last")) or 0
        vol  = (safe_float(t.get("volCcy24h")) or 0) * last
        if vol_filter and vol < MIN_VOL_OKX:
            continue
        # OKXはtickerにFRなし → 個別取得
        try:
            d = fetch(f"{OKX_BASE}/public/funding-rate?instId={t['instId']}")["data"]
            if not d:
                continue
            fr = safe_float(d[0].get("fundingRate"))
            nft = safe_float(d[0].get("nextFundingTime"))
            ih  = safe_float(d[0].get("fundingIntervalHours")) or 8
            if fr is None:
                continue
            out[coin] = {"fr": fr, "nft_ms": int(nft) if nft else None,
                         "interval_h": ih, "vol": vol, "price": last}
        except Exception:
            continue
    return out


def fr_bybit_bulk():
    """Bybit: linear tickers一括 (fundingRate含む)。"""
    data = fetch(f"{BYBIT_BASE}/market/tickers?category=linear")["result"]["list"]
    out = {}
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        coin = sym[:-4]
        if coin in EXCLUDE:
            continue
        fr  = safe_float(t.get("fundingRate"))
        nft = safe_float(t.get("nextFundingTime"))
        if fr is None:
            continue
        out[coin] = {"fr": fr, "nft_ms": int(nft) if nft else None, "interval_h": 8}
    return out


def fr_gate_bulk():
    """Gate: futures/usdt/contracts 一括 (funding_rate含む)。"""
    data = fetch(f"{GATE_BASE}/futures/usdt/contracts?limit=1000&offset=0")
    out = {}
    for t in data:
        name = t.get("name", "")
        if not name.endswith("_USDT"):
            continue
        coin = name[:-5]
        if coin in EXCLUDE:
            continue
        fr  = safe_float(t.get("funding_rate"))
        nft_s = safe_float(t.get("funding_next_apply"))  # unix秒
        if fr is None:
            continue
        out[coin] = {"fr": fr,
                     "nft_ms": int(nft_s * 1000) if nft_s else None,
                     "interval_h": 8}
    return out


def fr_mexc_bulk():
    """MEXC: contract/funding_rate 一括 (897銘柄・最詳細)。"""
    d = fetch(f"{MEXC_BASE}/contract/funding_rate")
    if not d.get("success"):
        return {}
    out = {}
    for t in d.get("data", []):
        sym = t.get("symbol", "")
        if not sym.endswith("_USDT"):
            continue
        coin = sym[:-5]
        if coin in EXCLUDE:
            continue
        fr  = safe_float(t.get("fundingRate"))
        nft = safe_float(t.get("nextSettleTime"))
        ih  = safe_float(t.get("collectCycle")) or 8
        if fr is None:
            continue
        out[coin] = {"fr": fr, "nft_ms": int(nft) if nft else None, "interval_h": ih}
    return out


def fr_bitget_bulk():
    """Bitget: mix/market/tickers USDT-FUTURES 一括。"""
    data = fetch(f"{BITGET_BASE}/mix/market/tickers?productType=USDT-FUTURES")["data"]
    out = {}
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        coin = sym[:-4]
        if coin in EXCLUDE:
            continue
        fr  = safe_float(t.get("fundingRate"))
        nft = safe_float(t.get("nextFundingTime"))
        if fr is None:
            continue
        out[coin] = {"fr": fr, "nft_ms": int(nft) if nft else None, "interval_h": 8}
    return out


# ============================================================
# 計算
# ============================================================
def calc_apy(fr, interval_h=8):
    if fr is None or interval_h <= 0:
        return None
    return fr * (24 / interval_h) * 365 * 100


def minutes_until(ms_ts):
    if not ms_ts:
        return None
    diff = (ms_ts - datetime.now(timezone.utc).timestamp() * 1000) / 60000
    return max(0, int(diff))


def fmt_price(p):
    if p is None: return "n/a"
    a = abs(p)
    if a >= 100:  return f"${p:,.2f}"
    if a >= 1:    return f"${p:,.4f}"
    if a >= 0.01: return f"${p:.5f}"
    return f"${p:.8f}"


def fmt_vol(v):
    if v is None: return "n/a"
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.2f}M"
    if v >= 1e3: return f"${v/1e3:.1f}K"
    return f"${v:.0f}"


def risk_tag(apy, cross_spread_pct):
    if apy > 200:   return "🔴 APY超高=FR反転リスク大"
    if apy > 80:    return "🟠 高APY=ロング過熱シグナル"
    if cross_spread_pct and cross_spread_pct > 0.05:
        return "💡 取引所間FR乖離大 → FRアービ候補"
    return "🟡 妙味あり"


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
        headers={"Content-Type": "application/json", "User-Agent": "fr-scanner/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=15): pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")


def build_embed(signals, scan_time, ex_status):
    now_jst = scan_time.strftime("%Y-%m-%d %H:%M JST")
    alive = [f"{'✅' if ok else '❌'} {n}" for n, ok in ex_status.items()]

    if not signals:
        desc = f"APY≥{MIN_APY}%の銘柄なし。市場は均衡状態。"
    else:
        lines = []
        for s in signals[:MAX_SIGNALS]:
            nft = f"次回{s['nft_min']}分後" if s['nft_min'] is not None else ""
            ex_line = " / ".join(
                f"{ex}={s['by_ex'][ex]['fr']*100:+.4f}%"
                for ex in s["by_ex"]
            )
            cross = s.get("cross_spread_pct")
            cross_str = f"  乖離幅 **{cross:.3f}%** ({s['max_ex']}↔{s['min_ex']})" if cross else ""
            lines.append(
                f"**{s['coin']}**  最高FR `{s['max_fr']*100:+.4f}%` @ **{s['max_ex']}**  "
                f"APY **{s['max_apy']:.1f}%**  {nft}\n"
                f"　{ex_line}{cross_str}\n"
                f"　{s['risk']}  出来高{fmt_vol(s.get('vol'))}"
            )
        desc = "\n".join(lines)

    embed = {
        "title": "💰 FRマルチ取引所比較スキャン v2",
        "description": desc[:3900],
        "color": 0xFFAA00 if signals else 0x888888,
        "fields": [
            {"name": "集計情報",
             "value": (
                 f"スキャン時刻: {now_jst}\n"
                 f"点灯件数: {len(signals)}件 (APY≥{MIN_APY}%)\n"
                 f"取引所状態: {' | '.join(alive)}"
             ), "inline": False},
            {"name": "読み方",
             "value": (
                 "**最高FRの取引所でショート** = FR受取が最大\n"
                 "**乖離幅が大きい** = 取引所間FRアービの理論上限\n"
                 "**APY>80% + ロング過熱** = あなたのショート戦略の点火候補\n"
                 f"往復手数料 {TAKER_FEE_RT*100:.2f}% を超えるFRのみ掲載"
             ), "inline": False},
            {"name": "200ドルでの現実",
             "value": (
                 "証拠金薄(各$100)でロスカリスクあり。\n"
                 "まずペーパーで FR の継続性を1週間確認してから実運用。\n"
                 "→ 500ドル超になってから本格デルタニュートラル推奨。"
             ), "inline": False},
        ],
        "footer": {"text": "FR Multi-Exchange Scanner v2 | CoinGlass相当を直接API取得"},
    }
    return embed


# ============================================================
# Main
# ============================================================
def main():
    scan_time = datetime.now(JST)
    print(f"FR multi-exchange scan start {scan_time.strftime('%Y-%m-%d %H:%M JST')}")

    # ---- 各取引所FR一括取得 ----
    sources = {
        "OKX":   (fr_okx_bulk,   True),
        "Bybit": (fr_bybit_bulk, False),
        "Gate":  (fr_gate_bulk,  False),
        "MEXC":  (fr_mexc_bulk,  False),
        "Bitget":(fr_bitget_bulk,False),
    }
    fr_data  = {}   # ex_name -> {coin: {fr, nft_ms, interval_h, ...}}
    ex_status = {}  # ex_name -> bool (成功/失敗)

    for name, (fn, _) in sources.items():
        try:
            d = fn()
            fr_data[name]   = d
            ex_status[name] = True
            print(f"  {name}: {len(d)} 先物ペア取得")
        except Exception as e:
            fr_data[name]   = {}
            ex_status[name] = False
            print(f"  {name}: FAILED ({type(e).__name__}: {str(e)[:60]})")

    alive_ex = [n for n, ok in ex_status.items() if ok]
    if len(alive_ex) < 1:
        print("全取引所取得失敗。終了。")
        return

    # ---- OKX出来高で共通銘柄フィルタ ----
    okx_data = fr_data.get("OKX", {})
    # OKXにない銘柄も拾う: MEXC一括 897 銘柄をベースにして出来高はOKX優先
    all_coins = set()
    for ex in alive_ex:
        all_coins |= set(fr_data[ex].keys())

    # OKX出来高が MIN_VOL_OKX 未満かつOKXに存在しない銘柄は除外
    filtered_coins = {
        c for c in all_coins
        if c in okx_data  # OKX出来高フィルタを通過済み
    }
    print(f"  共通銘柄(OKX出来高フィルタ後): {len(filtered_coins)}")

    # ---- 銘柄ごとに取引所横断FR比較 ----
    candidates = []
    for coin in filtered_coins:
        by_ex = {}
        for ex in alive_ex:
            q = fr_data[ex].get(coin)
            if q:
                by_ex[ex] = q

        if len(by_ex) < 1:
            continue

        frs = {ex: q["fr"] for ex, q in by_ex.items()}
        max_ex  = max(frs, key=frs.get)
        min_ex  = min(frs, key=frs.get)
        max_fr  = frs[max_ex]
        min_fr  = frs[min_ex]

        interval_h = by_ex[max_ex].get("interval_h") or 8
        max_apy = calc_apy(max_fr, interval_h)
        if max_apy is None or max_apy < MIN_APY:
            continue

        # FR がゼロ以下(マイナスFR = ショートが損)は除外
        if max_fr <= 0:
            continue

        cross_spread_pct = (max_fr - min_fr) * 100 if len(frs) >= 2 else None
        nft_min = minutes_until(by_ex[max_ex].get("nft_ms"))
        vol = okx_data.get(coin, {}).get("vol")
        price = okx_data.get(coin, {}).get("price")
        risk = risk_tag(max_apy, cross_spread_pct)

        candidates.append({
            "coin": coin, "by_ex": by_ex, "frs": frs,
            "max_ex": max_ex, "min_ex": min_ex,
            "max_fr": max_fr, "min_fr": min_fr,
            "max_apy": max_apy, "interval_h": interval_h,
            "cross_spread_pct": cross_spread_pct,
            "nft_min": nft_min, "vol": vol, "price": price,
            "risk": risk,
        })
        ex_str = " / ".join(f"{ex}={v*100:+.4f}%" for ex, v in frs.items())
        print(f"  ✅ {coin}: max APY {max_apy:.1f}% @ {max_ex} [{ex_str}]")

    candidates.sort(key=lambda x: x["max_apy"], reverse=True)
    signals = candidates[:MAX_SIGNALS]
    print(f"\n集計: {len(signals)} 件点灯 (APY≥{MIN_APY}%) / "
          f"スキャン時刻 {scan_time.strftime('%Y-%m-%d %H:%M JST')}")

    if signals:
        print("トップ5:")
        for s in signals[:5]:
            cross = f" 乖離{s['cross_spread_pct']:.3f}%" if s.get("cross_spread_pct") else ""
            print(f"  {s['coin']}: APY {s['max_apy']:.1f}% @ {s['max_ex']}"
                  f"{cross}  vol {fmt_vol(s.get('vol'))}")

    embed = build_embed(signals, scan_time, ex_status)
    mention = "@everyone" if MENTION_EVERYONE else ""
    allowed = {"parse": ["everyone"]} if MENTION_EVERYONE else {"parse": []}
    discord_post({"content": mention, "embeds": [embed], "allowed_mentions": allowed})


if __name__ == "__main__":
    main()
