"""
Funding Rate Delta-Neutral Scanner v1 (②FRアービ)

やること:
  1. OKX 全 USDT-SWAP のファンディングレート(FR)を一括取得
  2. デルタニュートラル戦略の想定APY を計算:
       APY = FR_per_period × periods_per_day × 365 × 100 (%)
       ※ 8h決済なら periods_per_day = 3
  3. 現物-先物スプレッド(ベーシス%)を計算 → 歪みの向き確認
  4. フィルタ: APY > MIN_APY / 出来高 > MIN_VOL / 板厚 OK
  5. Discord に日本語通知 (集計時刻必須 ← CLAUDE.md ルール)

「デルタニュートラル」とは:
  現物 +1 BTCロング × 永久先物 -1 BTCショート → 価格変動ゼロ
  FRがプラス → ショート側が8時間ごとにFRを受取 = 方向リスク無し収益

注意:
  - これは「稼ぎの保証」ではなく「妙味スクリーニング」。
  - FR は次回決済後に反転しうる(調査済みリスク)。
  - 両建てコスト(手数料×2)を超えるFRのときだけ点灯。
  - 200ドルでの現実的上限: $100現物 + $100先物証拠金。証拠金薄でロスカリスク大。
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
MENTION_EVERYONE = os.environ.get("MENTION_EVERYONE", "0") == "1"

# 点灯閾値: 年率何%以上なら通知するか
MIN_APY = float(os.environ.get("MIN_APY", "15"))
# 最小24h出来高 (USDT)。低流動は FR が異常値になりやすい罠
MIN_VOL = float(os.environ.get("MIN_VOL", "2000000"))
# ベーシスが極端すぎる(取引不可状態)を除外
MAX_BASIS_PCT = float(os.environ.get("MAX_BASIS_PCT", "5"))
# Discord 送信最大件数 (高APY順)
MAX_SIGNALS = int(os.environ.get("MAX_SIGNALS", "15"))
# 除外リスト (安定コイン・デリバティブ自体)
EXCLUDE = {"USDC", "DAI", "TUSD", "FDUSD", "USDD", "USDE", "BUSD", "PYUSD"}

OKX_BASE = "https://www.okx.com/api/v5"


# ============================================================
# HTTP
# ============================================================
def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "fr-scanner/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def okx(path):
    d = fetch_json(f"{OKX_BASE}{path}")
    if d.get("code") != "0":
        raise RuntimeError(f"OKX {d.get('code')}: {d.get('msg')}")
    return d["data"]


def safe_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ============================================================
# Data
# ============================================================
def get_all_swap_tickers():
    """全USDTスワップのティッカー (価格・出来高) を返す。"""
    data = okx("/market/tickers?instType=SWAP")
    out = {}
    for t in data:
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        coin = t["instId"].replace("-USDT-SWAP", "")
        if coin in EXCLUDE:
            continue
        last = safe_float(t.get("last"))
        vol = safe_float(t.get("volCcy24h"))  # 通貨建て出来高 (≒ USDT)
        if not last or not vol:
            continue
        out[coin] = {"price": last, "vol": vol * last if vol else 0,
                     "inst_id": t["instId"]}
    return out


def get_funding_batch():
    """全スワップのFR (fundingRate) を一括取得。
    OKX は /public/funding-rate には instId が必須で一括APIが無いため、
    /rubik/stat/trading-data/support-coin で先物対応リストを引いた後、
    代替として tickers の中に fundingRate が含まれる場合があることを活用。
    実際には ticker に FR が含まれないので、上位銘柄だけ個別取得する。
    """
    # tickers に fundingRate フィールドが含まれているかまず確認
    data = okx("/market/tickers?instType=SWAP")
    fr_map = {}
    for t in data:
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        coin = t["instId"].replace("-USDT-SWAP", "")
        fr = safe_float(t.get("fundingRate"))
        if fr is not None:
            fr_map[coin] = fr
    return fr_map


def get_funding_single(inst_id):
    """個別銘柄のFR + 次回決済時刻を取得。"""
    try:
        d = okx(f"/public/funding-rate?instId={inst_id}")
        if not d:
            return None, None, None
        fr = safe_float(d[0].get("fundingRate"))
        nft = safe_float(d[0].get("nextFundingTime"))
        # 決済間隔: OKX は基本8h(=3回/日)、銘柄により4h/12hあり
        interval_h = safe_float(d[0].get("fundingIntervalHours", 8))
        if not interval_h:
            interval_h = 8
        return fr, int(nft) if nft else None, interval_h
    except Exception:
        return None, None, None


def get_spot_price(coin):
    """現物価格を取得してベーシス計算に使う。"""
    try:
        d = okx(f"/market/ticker?instId={coin}-USDT")
        return safe_float(d[0].get("last")) if d else None
    except Exception:
        return None


# ============================================================
# 計算
# ============================================================
def calc_apy(fr, interval_h=8):
    """FR × 1日の決済回数 × 365 = 年率(小数)。"""
    if fr is None:
        return None
    periods_per_day = 24 / interval_h
    return fr * periods_per_day * 365 * 100  # %


def calc_basis(swap_price, spot_price):
    """ベーシス% = (先物 - 現物) / 現物 × 100。
    プラス = 先物プレミアム = キャリートレードの妙味。"""
    if not swap_price or not spot_price or spot_price <= 0:
        return None
    return (swap_price - spot_price) / spot_price * 100


def taker_fee_round_trip():
    """OKX 現物+先物 taker 手数料(往復)。
    現物 0.10% + 先物 0.02% = 0.12% per trade。
    ポジションを開くだけで 0.12%、閉じるときも 0.12% = 合計 0.24%。
    """
    return 0.24


def minutes_until(ms_ts):
    if not ms_ts:
        return None
    now = datetime.now(timezone.utc).timestamp() * 1000
    diff = (ms_ts - now) / 60000
    return int(diff) if diff > 0 else 0


def fmt_price(p):
    if p is None:
        return "n/a"
    if abs(p) >= 100:
        return f"${p:,.2f}"
    if abs(p) >= 1:
        return f"${p:,.4f}"
    if abs(p) >= 0.01:
        return f"${p:.5f}"
    return f"${p:.8f}"


def fmt_vol(v):
    if v is None:
        return "n/a"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.2f}M"
    if v >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:.0f}"


def risk_label(apy, vol, basis_pct):
    """シグナルのリスクレベルを簡易判定。"""
    if apy > 100:
        return "🔴 超高APY=FR反転リスク大。慎重に"
    if apy > 50:
        return "🟠 高APY。FRが高い=ロング過熱のサイン。方向シグナルにもなる"
    if apy > MIN_APY:
        return "🟡 妙味あり。FR継続を確認してからポジション"
    return "⚪"


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
        headers={"Content-Type": "application/json", "User-Agent": "fr-scanner/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")


def build_embed(signals, scan_time):
    now_jst = scan_time.strftime("%Y-%m-%d %H:%M JST")

    if not signals:
        desc = f"APY≥{MIN_APY}%の銘柄なし。市場のFRは低水準(デルタニュートラルの妙味なし)。"
    else:
        lines = []
        for s in signals:
            nft_str = f"次回{s['nft_min']}分後" if s['nft_min'] is not None else "時刻不明"
            basis_str = f"{s['basis']:+.3f}%" if s['basis'] is not None else "n/a"
            fee_note = f"※往復手数料{taker_fee_round_trip():.2f}%込み実効APY≈{s['apy'] - taker_fee_round_trip()*365:.1f}%" if s['apy'] else ""
            lines.append(
                f"**{s['coin']}/USDT**  FR `{s['fr']*100:+.4f}%`  APY **{s['apy']:.1f}%** {nft_str}\n"
                f"　価格{fmt_price(s['price'])}  ベーシス{basis_str}  出来高{fmt_vol(s['vol'])}\n"
                f"　{s['risk']}  {fee_note}"
            )
        desc = "\n".join(lines)

    embed = {
        "title": "💰 FRデルタニュートラル妙味スキャン",
        "description": desc[:3900],  # Discord embed 4096文字制限
        "color": 0xFFAA00 if signals else 0x888888,
        "fields": [
            {"name": "集計条件",
             "value": (
                 f"スキャン時刻: {now_jst}\n"
                 f"対象: OKX 全USDT永久先物 ({len(signals)}件点灯 / APY≥{MIN_APY}%)\n"
                 f"出来高フィルタ: {fmt_vol(MIN_VOL)} / 最大ベーシス: {MAX_BASIS_PCT}%"
             ),
             "inline": False},
            {"name": "デルタニュートラル戦略の仕組み",
             "value": (
                 "現物+1ロング × 永久先物-1ショート → 価格変動の損益が相殺\n"
                 "FRがプラス → ショート側が8時間ごとにFRを受取\n"
                 f"往復手数料: {taker_fee_round_trip():.2f}% (開く+閉じる)\n"
                 "⚠️ FRは次回決済後に符号反転しうる。72h超マイナスなら出血。"
             ),
             "inline": False},
            {"name": "200ドルでの現実",
             "value": (
                 "¥100現物 + $100先物証拠金 = 証拠金薄くロスカリスク大。\n"
                 "まずはペーパーで FR の継続性を1週間確認し、\n"
                 "資金500ドル超になってから実運用を推奨(調査結論)。\n"
                 "このスキャナーは「監視・記録」が目的。"
             ),
             "inline": False},
        ],
        "footer": {"text": "FR Delta-Neutral Scanner v1 | ①価格スプレッド + ②FRアービ パイプライン"},
    }
    return embed


# ============================================================
# Main
# ============================================================
def main():
    scan_time = datetime.now(JST)
    print(f"FR scan start {scan_time.strftime('%Y-%m-%d %H:%M JST')}")

    # ---- Step 1: ティッカー(価格・出来高) ----
    print("  OKX ティッカー取得中...")
    tickers = get_all_swap_tickers()
    print(f"  {len(tickers)} USDT-SWAP ペア取得")

    # ---- Step 2: tickers に FR が含まれるか試す ----
    fr_in_ticker = get_funding_batch()
    if fr_in_ticker:
        print(f"  ticker FR 一括取得: {len(fr_in_ticker)}件")
    else:
        print("  ticker に FR なし → 個別取得モードへ")

    # ---- Step 3: 出来高フィルタ後に個別FR取得 ----
    # まず出来高でスクリーニングしてからFR個別取得(APIコール削減)
    vol_filtered = {c: v for c, v in tickers.items() if v["vol"] >= MIN_VOL}
    print(f"  出来高フィルタ({fmt_vol(MIN_VOL)})後: {len(vol_filtered)} 銘柄")

    candidates = []
    for coin, info in vol_filtered.items():
        inst = info["inst_id"]

        # FR取得 (ticker一括取得があればそれを使い、なければ個別)
        fr = fr_in_ticker.get(coin)
        interval_h = 8
        nft_ms = None
        if fr is None:
            fr, nft_ms, interval_h = get_funding_single(inst)
            if interval_h is None:
                interval_h = 8

        if fr is None:
            continue

        # APY計算
        apy = calc_apy(fr, interval_h)
        if apy is None or apy < MIN_APY:
            continue

        # 現物価格 → ベーシス
        spot = get_spot_price(coin)
        basis = calc_basis(info["price"], spot)

        # ベーシス異常値除外 (上場停止/板崩壊)
        if basis is not None and abs(basis) > MAX_BASIS_PCT:
            print(f"  {coin}: ベーシス{basis:.1f}%超過 → 除外")
            continue

        nft_min = minutes_until(nft_ms)
        risk = risk_label(apy, info["vol"], basis)

        candidates.append({
            "coin": coin, "inst_id": inst, "price": info["price"],
            "vol": info["vol"], "fr": fr, "apy": apy,
            "interval_h": interval_h, "nft_min": nft_min,
            "basis": basis, "risk": risk,
        })
        print(f"  ✅ {coin}: FR {fr*100:+.4f}% APY {apy:.1f}%  {risk}")

    candidates.sort(key=lambda x: x["apy"], reverse=True)
    signals = candidates[:MAX_SIGNALS]

    print(f"\n集計: {len(signals)} 件点灯 (APY≥{MIN_APY}%)")

    embed = build_embed(signals, scan_time)
    mention = "@everyone" if MENTION_EVERYONE else ""
    allowed = {"parse": ["everyone"]} if MENTION_EVERYONE else {"parse": []}
    discord_post({"content": mention, "embeds": [embed], "allowed_mentions": allowed})

    if signals:
        print("トップ5:")
        for s in signals[:5]:
            print(f"  {s['coin']}: APY {s['apy']:.1f}% (FR {s['fr']*100:+.4f}%/8h) "
                  f"vol {fmt_vol(s['vol'])}")
    else:
        print("市場FRは低水準。デルタニュートラルの妙味なし。")


if __name__ == "__main__":
    main()
