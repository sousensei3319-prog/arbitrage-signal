"""
戦略A (踏み上げ逆張りロング) の「FGI恐怖で+1点」の答え合わせ — 一時的な調査 (main には入れない)。
発注しない・APIキー不要。結果はログに出すだけ (RESULT_JSON_BEGIN〜END の間が要約)。

事前登録 (2026-10-13 20:54 UTC 記入・20:57 UTC にデータ源を決定。成績を見る前に固定) は PREREG の全文のとおり。
"""
import json
import math
import socket
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

socket.setdefaulttimeout(35)
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
START = datetime(2023, 6, 1, tzinfo=timezone.utc)
FETCH_DEADLINE_SEC = 50 * 60
MIN_VOL_USD = 3_000_000
HORIZONS = (1, 3, 7)

PREREG = r"""
問い: score_squeeze_long の「FGI≤30 なら +1点」に、恐怖の日の候補がそうでない日の候補より上がりやすいという根拠があるか。
単位: 毎日 0:00 UTC の時点の各銘柄 (直前24時間の出来高 ≥ 300万ドル)。24時間騰落 < 0 かつ FGI ≤ 55 の銘柄・日が母集団。
部分スコア = FR の点 (8時間あたり < −0.05% → 3 / < −0.02% → 2 / < −0.005% → 1) + 下落の点 (7日高値から < −25% → 2 / < −12% → 1)。
セットアップ = 部分スコア ≥ 2。物差し: その日の終値で買って h 日後の終値までの騰落 (h=1,3,7)。BTC 超過も出す。
主要な比較 (1本だけ): セットアップの +3日騰落を日ごとに平均し、恐怖の日 (FGI ≤ 30) の日平均の平均 − それ以外 (31〜55) の
日平均の平均。日を単位にした Welch の t。
合否: 支持 = 差 ≥ +1.0 ポイント かつ t ≥ 2.0 かつ BTC 超過の差もプラス / 有害 = 差 ≤ −1.0 かつ t ≤ −2.0 / それ以外 = 根拠なし。
データ源 (調査 crypto_data_probe の結果で決定): 日足 OKX 1Dutc / FR Hyperliquid の1時間値を直前8時間合計 / 銘柄 OKX USDT 無期限で
Hyperliquid に同名がある銘柄を今の出来高の多い順に取得の締め切りまで / 期間 2023-07〜。L/S (OKX 銘柄別口座比・日次、2024-03〜)
を入れた本番に近い点数での比較は事前登録外の補足 (判定に使わない)。
"""


# ---------------------------------------------------------------- 取得
def http_json(url, data=None, headers=None, tries=4):
    h = dict(UA)
    if headers:
        h.update(headers)
    wait = 2.0
    for i in range(tries):
        req = urllib.request.Request(url, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(wait); wait *= 2; continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if i < tries - 1:
                time.sleep(wait); wait *= 2; continue
            raise


def day_of(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d")


def fetch_fgi():
    d = http_json("https://api.alternative.me/fng/?limit=0")
    return {datetime.fromtimestamp(int(x["timestamp"]), timezone.utc).strftime("%Y-%m-%d"): int(x["value"]) for x in d["data"]}


def okx(path):
    d = http_json("https://www.okx.com/api/v5" + path)
    if str(d.get("code")) != "0":
        raise ValueError(f"OKX {path}: {d.get('msg')}")
    return d.get("data") or []


def okx_daily(inst):
    """{日付: (open, high, low, close, quote_vol)} を START まで。"""
    out, after = {}, ""
    while True:
        d = okx(f"/market/history-candles?instId={inst}&bar=1Dutc&limit=100" + (f"&after={after}" if after else ""))
        if not d:
            break
        for c in d:
            if c[8] != "1":          # 確定していない足 (今日) は使わない
                continue
            out[day_of(c[0])] = (float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[7] or 0))
        after = d[-1][0]
        if int(after) < START.timestamp() * 1000:
            break
        time.sleep(0.12)
    return out


def okx_ls(inst):
    """[(ms, 口座比)] 昇順 (OKX 銘柄別の口座比・日次)。"""
    out, end = [], ""
    while True:
        try:
            d = okx(f"/rubik/stat/contracts/long-short-account-ratio-contract?instId={inst}&period=1D&limit=100" + (f"&end={end}" if end else ""))
        except (ValueError, urllib.error.HTTPError):
            break
        if not d:
            break
        out += [(int(x[0]), float(x[1])) for x in d]
        end = str(int(d[-1][0]) - 1)
        if int(d[-1][0]) < START.timestamp() * 1000 or len(d) < 100:
            break
        time.sleep(0.45)
    return sorted(set(out))


def hl_funding(coin):
    """[(ms, 1時間の資金調達率)] 昇順 (Hyperliquid)。"""
    out, start = [], int(START.timestamp() * 1000)
    now = int(time.time() * 1000)
    while start < now:
        d = http_json("https://api.hyperliquid.xyz/info", data=json.dumps({"type": "fundingHistory", "coin": coin, "startTime": start}).encode(),
                      headers={"Content-Type": "application/json"})
        if not d:
            break
        out += [(int(x["time"]), float(x["fundingRate"])) for x in d]
        nxt = int(d[-1]["time"]) + 1
        if nxt <= start or len(d) < 500:
            break
        start = nxt
        time.sleep(0.25)
    return out


# ---------------------------------------------------------------- 計算
def fr_points(fr8h):
    if fr8h is None:
        return 0
    pct = fr8h * 100
    return 3 if pct < -0.05 else 2 if pct < -0.02 else 1 if pct < -0.005 else 0


def dd_points(dd_pct):
    if dd_pct is None:
        return 0
    return 2 if dd_pct < -25 else 1 if dd_pct < -12 else 0


def ls_points(ls):
    if ls is None:
        return 0
    return 3 if ls < 0.5 else 2 if ls < 0.8 else 1 if ls < 1.0 else 0


def welch(a, b):
    if len(a) < 2 or len(b) < 2:
        return None, None
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    se = math.sqrt(statistics.variance(a) / len(a) + statistics.variance(b) / len(b))
    return ma - mb, ((ma - mb) / se if se > 0 else None)


def verdict(diff, t, diff_ex):
    if diff is None or t is None:
        return "判定不能 (件数不足)"
    if diff >= 1.0 and t >= 2.0 and (diff_ex or 0) > 0:
        return "支持 (+1 は根拠あり)"
    if diff <= -1.0 and t <= -2.0:
        return "有害 (+1 は逆効果)"
    return "根拠なし"


def snapshots(coin, daily, funding, ls_series, fgi, btc):
    """判定の時点 (日 i の引け = 翌日 0:00 UTC) ごとの行。FGI は日 i の値 (その時点で公表済み) を使う。"""
    days = sorted(daily)
    fts = [f[0] for f in funding]
    rows = []
    import bisect
    for i in range(7, len(days) - 1):
        d = days[i]
        o, h, l, c, qv = daily[d]
        prev_c = daily[days[i - 1]][3]
        if prev_c <= 0 or c <= 0 or d < "2023-07-01":
            continue
        snap = int((datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp() * 1000)
        j = bisect.bisect_right(fts, snap)
        last8 = [f[1] for f in funding[max(0, j - 8):j] if snap - f[0] <= 8 * 3600 * 1000]
        fr8h = sum(last8) if len(last8) >= 6 else None
        high7 = max(daily[days[k]][1] for k in range(i - 6, i + 1))
        dd = (c / high7 - 1) * 100 if high7 > 0 else None
        k = bisect.bisect_right([x[0] for x in ls_series], snap) - 1 if ls_series else -1
        ls = ls_series[k][1] if k >= 0 and snap - ls_series[k][0] <= 2 * 86400 * 1000 else None
        f = fgi.get(d)
        r = {"day": d, "coin": coin, "chg": c / prev_c - 1, "vol": qv, "fr8h": fr8h, "dd": dd, "ls": ls, "fgi": f}
        for hh in HORIZONS:
            if i + hh < len(days):
                c2 = daily[days[i + hh]][3]
                r[f"ret{hh}"] = (c2 / c - 1) * 100
                b0, b1 = btc.get(d), btc.get(days[i + hh])
                r[f"ex{hh}"] = r[f"ret{hh}"] - (b1 / b0 - 1) * 100 if b0 and b1 else None
        rows.append(r)
    return rows


def compare(rows, key, cond):
    """cond を満たす行について、恐怖の日 vs それ以外 (31〜55) の日平均の差と t。"""
    fear, other = {}, {}
    for r in rows:
        if not cond(r) or r.get(key) is None or r["fgi"] is None:
            continue
        tgt = fear if r["fgi"] <= 30 else other if r["fgi"] <= 55 else None
        if tgt is not None:
            tgt.setdefault(r["day"], []).append(r[key])
    a = [statistics.fmean(v) for v in fear.values()]
    b = [statistics.fmean(v) for v in other.values()]
    diff, t = welch(a, b)
    return {"diff": None if diff is None else round(diff, 3), "t": None if t is None else round(t, 2),
            "fear_mean": round(statistics.fmean(a), 3) if a else None, "other_mean": round(statistics.fmean(b), 3) if b else None,
            "fear_days": len(a), "other_days": len(b),
            "fear_n": sum(len(v) for v in fear.values()), "other_n": sum(len(v) for v in other.values())}


def analyze(rows):
    base = [r for r in rows if r["vol"] >= MIN_VOL_USD and r["chg"] < 0 and r["fgi"] is not None and r["fgi"] <= 55]
    for r in base:
        r["ps"] = fr_points(r["fr8h"]) + dd_points(r["dd"])
    setup = lambda r: r["ps"] >= 2
    res = {"n_rows": len(rows), "n_base": len(base),
           "period": [min(r["day"] for r in base), max(r["day"] for r in base)] if base else None}
    res["primary"] = compare(base, "ret3", setup)
    res["primary_ex"] = compare(base, "ex3", setup)
    res["verdict"] = verdict(res["primary"]["diff"], res["primary"]["t"], res["primary_ex"]["diff"])
    sup = {}
    for hh in (1, 7):
        sup[f"setup_ret{hh}"] = compare(base, f"ret{hh}", setup)
    sup["nonsetup_ret3"] = compare(base, "ret3", lambda r: r["ps"] <= 1)
    for ps in (2, 3, 4, 5):
        sup[f"ps{ps}_ret3"] = compare(base, "ret3", lambda r, ps=ps: r["ps"] == ps)
    days = sorted({r["day"] for r in base})
    if days:
        mid = days[len(days) // 2]
        sup["first_half"] = compare([r for r in base if r["day"] < mid], "ret3", setup)
        sup["second_half"] = compare([r for r in base if r["day"] >= mid], "ret3", setup)
    # 事前登録外: L/S を入れた本番に近い点数 (2024-03 以降・L/S のある行だけ)
    ls_rows = [r for r in base if r["ls"] is not None]
    for r in ls_rows:
        r["full"] = r["ps"] + ls_points(r["ls"])
    sup["posthoc_full_ge4"] = compare(ls_rows, "ret3", lambda r: r["full"] >= 4)
    extra = [r for r in ls_rows if r["full"] == 4 and r["fgi"] <= 30 and r.get("ret3") is not None]
    fired = [r for r in ls_rows if r["full"] >= 5 and r.get("ret3") is not None]
    sup["posthoc_extra_by_plus1"] = {"n": len(extra), "days": len({r["day"] for r in extra}),
                                     "mean_ret3": round(statistics.fmean([r["ret3"] for r in extra]), 3) if extra else None,
                                     "fired_score5plus_n": len(fired),
                                     "fired_mean_ret3": round(statistics.fmean([r["ret3"] for r in fired]), 3) if fired else None}
    res["supplementary"] = sup
    return res


def main():
    t0 = time.time()
    print("事前登録:", PREREG.strip().replace("\n", " "))
    fgi = fetch_fgi()
    print(f"FGI {len(fgi)}日 ({min(fgi)}〜{max(fgi)})")
    insts = {x["instId"] for x in okx("/public/instruments?instType=SWAP") if x["instId"].endswith("-USDT-SWAP")}
    tick = {t["instId"]: float(t.get("volCcy24h") or 0) * float(t.get("last") or 0) for t in okx("/market/tickers?instType=SWAP")}
    hl = {u["name"] for u in http_json("https://api.hyperliquid.xyz/info", data=json.dumps({"type": "meta"}).encode(),
                                       headers={"Content-Type": "application/json"})["universe"]}
    coins = sorted((i for i in insts if i.split("-")[0] in hl), key=lambda i: -tick.get(i, 0))
    print(f"OKX USDT無期限 {len(insts)} / うち Hyperliquid に同名 {len(coins)}")
    btc_daily = okx_daily("BTC-USDT-SWAP")
    btc = {d: v[3] for d, v in btc_daily.items()}
    rows, done = [], []
    for inst in coins:
        if time.time() - t0 > FETCH_DEADLINE_SEC:
            print(f"取得の締め切り ({FETCH_DEADLINE_SEC // 60}分) — {len(done)}銘柄で打ち切り"); break
        coin = inst.split("-")[0]
        try:
            daily = btc_daily if coin == "BTC" else okx_daily(inst)
            funding = hl_funding(coin)
            ls_series = okx_ls(inst)
        except Exception as e:  # 1銘柄の失敗で止めない
            print(f"  {coin}: 取得失敗 {type(e).__name__}: {e}"); continue
        rows += snapshots(coin, daily, funding, ls_series, fgi, btc)
        done.append(coin)
        if len(done) % 10 == 0:
            print(f"  {len(done)}銘柄 ({time.time() - t0:.0f}秒) 最後 {coin} 日足{len(daily)} FR{len(funding)} LS{len(ls_series)}")
    res = analyze(rows)
    res["coins"] = done
    res["fetch_sec"] = round(time.time() - t0)
    print("RESULT_JSON_BEGIN")
    print(json.dumps(res, ensure_ascii=False))
    print("RESULT_JSON_END")
    p = res["primary"]
    print(f"主要: 恐怖の日 {p['fear_mean']}% ({p['fear_days']}日) − それ以外 {p['other_mean']}% ({p['other_days']}日) = {p['diff']} (t={p['t']}) → {res['verdict']}")


if __name__ == "__main__":
    main()
