"""
HyperTracker enrichment module (無料枠100回/日を守る設計)

シグナル点灯銘柄のうち「高スコアの厳選銘柄だけ」をスポット確認する。
1銘柄 = 1 APIコール (/positions/open/coin/{coin})。
予算管理: 各スキャナーが HT_MAX_LOOKUPS 件まで・高スコアのみ呼ぶ前提。

返すエッジ:
  ① 勝ち組 vs 負け組 のL/S乖離 (profile_pnl で分離) ← killer signal
  ② 清算クラスター (liquidationPrice をバケット集計)
  ③ Discord用の短いサマリーテキスト

HT_API_TOKEN が未設定なら全て None を返す (組み込みOFF)。
"""

import csv
import io
import os
import time
import collections
import urllib.error
import urllib.request

HT_TOKEN = os.environ.get("HT_API_TOKEN", "")
HT_BASE = "https://ht-api.coinmarketman.com"


def _f(x):
    try:
        v = float(x)
        return v if v == v else 0.0
    except (TypeError, ValueError):
        return 0.0


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """S3署名付きURLへのリダイレクト時にAuthorizationヘッダーを外す。
    付けたままだとS3が 'Only one auth mechanism allowed' で400を返す。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newreq = super().redirect_request(req, fp, code, msg, headers, newurl)
        if newreq is not None:
            for k in list(newreq.headers.keys()):
                if k.lower() == "authorization":
                    del newreq.headers[k]
            newreq.unredirected_hdrs.pop("Authorization", None)
        return newreq

_OPENER = urllib.request.build_opener(_StripAuthOnRedirect)


def _fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {HT_TOKEN}",
        "User-Agent": "arb-signal/1.0"})
    with _OPENER.open(req, timeout=timeout) as r:
        return r.read().decode()


def _fetch_csv(coin, timeout=30):
    return _fetch(f"{HT_BASE}/api/external/positions/open/coin/{coin}", timeout)


_HL_UNIVERSE = None  # プロセス内キャッシュ

def hl_universe():
    """HL上場全銘柄のsetを1コールで取得(キャッシュ)。未設定/失敗ならNone。"""
    global _HL_UNIVERSE
    if _HL_UNIVERSE is not None:
        return _HL_UNIVERSE
    if not HT_TOKEN:
        return None
    try:
        import json
        data = json.loads(_fetch(f"{HT_BASE}/api/external/positions/coins"))
        _HL_UNIVERSE = {d["coin"] for d in data if d.get("coin")}
        print(f"    HL universe: {len(_HL_UNIVERSE)} coins")
        return _HL_UNIVERSE
    except Exception as e:
        print(f"    HL universe fail: {type(e).__name__} {str(e)[:50]}")
        _HL_UNIVERSE = set()
        return _HL_UNIVERSE


def hl_filter(coins):
    """与えた銘柄リストのうちHL上場のものだけ返す(順序維持)。"""
    uni = hl_universe()
    if not uni:
        return []
    return [c for c in coins if c in uni]


def analyze(coin):
    """coin(例 'ARB') のHL建玉を分析。HL未上場/エラー/未設定なら None。"""
    if not HT_TOKEN:
        return None
    text = None
    for attempt in range(2):  # 一時的な400/レート制限に1回リトライ
        try:
            text = _fetch_csv(coin)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print(f"    HT {coin}: HTTP 404 (HL未上場)")
                return None
            if attempt == 0:
                time.sleep(2.0)  # バースト緩和して再試行
                continue
            print(f"    HT {coin}: HTTP {e.code} (リトライ後も失敗)")
            return None
        except Exception as e:
            if attempt == 0:
                time.sleep(2.0)
                continue
            print(f"    HT {coin}: {type(e).__name__} {str(e)[:50]}")
            return None
    if text is None:
        return None

    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "side" not in (rows[0] if rows else {}):
        return None

    # 推定現在価格 = value/size の中央値
    prices = sorted(_f(r["value"]) / _f(r["size"]) for r in rows if _f(r["size"]) > 0)
    if not prices:
        return None
    px = prices[len(prices) // 2]

    # ① 勝ち組(累積pnl>0) vs 負け組(<0) の long/short notional
    win_l = win_s = lose_l = lose_s = 0.0
    for r in rows:
        v = _f(r["value"]); pnl = _f(r.get("profile_pnl")); side = r["side"]
        if v <= 0:
            continue
        if pnl > 0:
            if side == "long": win_l += v
            else: win_s += v
        else:
            if side == "long": lose_l += v
            else: lose_s += v

    win_ls = win_l / win_s if win_s > 0 else (99.0 if win_l > 0 else None)
    lose_ls = lose_l / lose_s if lose_s > 0 else (99.0 if lose_l > 0 else None)

    # 乖離判定
    win_net = "LONG" if win_l > win_s else "SHORT"
    lose_net = "LONG" if lose_l > lose_s else "SHORT"
    if win_net == "SHORT" and lose_net == "LONG":
        bias = "bearish"   # 賢い金売り・養分買い → 下
        bias_jp = "🔴弱気(勝ち組SHORT/負け組LONG=ダンプ燃料)"
    elif win_net == "LONG" and lose_net == "SHORT":
        bias = "bullish"   # 賢い金買い・養分売り → 上(踏み上げ燃料)
        bias_jp = "🟢強気(勝ち組LONG/負け組SHORT=踏み上げ燃料)"
    else:
        bias = "neutral"
        bias_jp = f"⚪中立(勝ち組{win_net}/負け組{lose_net})"

    # ② 清算クラスター (現在値±50%内のみ。それ外は異常値として除外)
    long_liq = collections.Counter()   # 下落で連鎖
    short_liq = collections.Counter()  # 上昇で踏み上げ
    for r in rows:
        lp = _f(r.get("liquidationPrice")); v = _f(r["value"]); side = r["side"]
        if lp <= 0 or v <= 0 or not (px * 0.5 < lp < px * 1.5):
            continue
        band = round(lp, 6)
        if side == "long": long_liq[band] += v
        else: short_liq[band] += v

    def top_cluster(counter):
        if not counter:
            return None, 0.0
        p, val = max(counter.items(), key=lambda x: x[1])
        return p, val

    long_liq_px, long_liq_val = top_cluster(long_liq)    # 下の磁石
    short_liq_px, short_liq_val = top_cluster(short_liq)  # 上の磁石

    return {
        "coin": coin, "price": px, "positions": len(rows),
        "win_ls": win_ls, "lose_ls": lose_ls,
        "win_net": win_net, "lose_net": lose_net,
        "bias": bias, "bias_jp": bias_jp,
        "long_liq_px": long_liq_px, "long_liq_val": long_liq_val,
        "short_liq_px": short_liq_px, "short_liq_val": short_liq_val,
    }


def summary_text(a, direction):
    """Discord埋め込み用の短いサマリー。direction: 'long' or 'short'。
    シグナル方向とHL bias が一致してるか(整合/逆行)も判定。"""
    if not a:
        return None
    lines = [f"勝ち組L/S {a['win_ls']:.2f} / 負け組L/S {a['lose_ls']:.2f}" if a['win_ls'] and a['lose_ls']
             else f"勝ち組ネット{a['win_net']} / 負け組ネット{a['lose_net']}"]
    lines.append(a["bias_jp"])

    # 清算磁石
    if a["short_liq_px"]:
        lines.append(f"↑踏み上げ燃料(ショート清算) ${a['short_liq_px']:.6g} (${a['short_liq_val']:,.0f})")
    if a["long_liq_px"]:
        lines.append(f"↓下落磁石(ロング清算) ${a['long_liq_px']:.6g} (${a['long_liq_val']:,.0f})")

    # 方向整合チェック
    if direction == "long":
        if a["bias"] == "bullish":
            lines.append("✅シグナル(ロング)とHL一致 → 確度UP")
        elif a["bias"] == "bearish":
            lines.append("⚠️警告: ロングだがHLは弱気 → 落ちるナイフ注意")
    elif direction == "short":
        if a["bias"] == "bearish":
            lines.append("✅シグナル(ショート)とHL一致 → 確度UP")
        elif a["bias"] == "bullish":
            lines.append("⚠️警告: ショートだがHLは強気 → 踏み上げ注意")

    return "\n".join(lines)
