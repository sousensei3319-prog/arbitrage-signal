"""
戦略C (マクロスイング) の答え合わせ — APIキー不要の検証スクリプト (strategy_c_check.py)

RULES.md の戦略C「FGI(恐怖と欲望指数)≤30で現物を分割買い(DCA)・FGI≥70で段階利確」と、
そこに書かれた統計 (「FGI<10は過去6年の90日平均+48%・マイナス事例ゼロ」等) を、
公開データだけで検証する。発注はしない・APIキーは使わない。

データ (いずれも無料・キー不要。サンドボックスからは到達不可のため GitHub Actions 上で実行):
  - FGI: alternative.me (https://api.alternative.me/fng/?limit=0) 2018-02 以降の日次全履歴
  - 価格: OKX 現物 BTC-USDT / ETH-USDT 日足 (UTC区切り "1Dutc")。
          Bybit は Actions から403、Binance は451で使えないため OKX を主に使う。
          Coinbase (BTC-USD / ETH-USD) も取得し、終値の食い違いを品質チェックとして報告
          (OKXが失敗/期間不足のときは Coinbase を代わりに使う)

【比較方法と合否の基準 — 結果を見る前に固定 (2026-10-03)。後から変えない】
  毎週月曜(UTC)の終値で買う。手数料は売買ごとに 0.1% (取引所の板)。参考に 3%
  (販売所スプレッド相当) でも計算する。ドル建て (円との為替は含まない)。
    S1 固定積立      : 毎週 $100
    S2 恐怖で倍額    : 毎週 $100、その日のFGI≤30なら $200 (ユーザー案「恐怖で増額」)
    S3 RULES.md 戦略C: FGI≤30の週だけ $100 買い、FGI≥70の週に保有の25%を売る(段階利確)。
                       売却代金は現金で保持 (金利0)。「スイング安値割れで損切」は数値の定義が
                       無いため検証対象外
    S0 一括購入(参考): S1と同じ総額を初日に一括で買って持ち続ける
  評価: 投資倍率 MOIC = 最終評価額 ÷ 投入総額、内部収益率 IRR (お金の出し入れの時期を考慮した
        年率)、最大の含み損率 (評価額÷投入額 の最大下落率)。期間: 全期間と3区間
        (2018-02〜2020年 / 2021〜2023年 / 2024年〜) — 各区間はゼロから始め直す。
  合否:
    判定1 (S2を採用するか): BTCで S2 の MOIC が S1 を「全期間」かつ「3区間中2区間以上」で上回る
          → 採用。ETH でも同じなら「確度高」、ETHで不成立なら「条件付き」。不成立なら
          「固定積立のみ」(事前合意: 倍額ルールが負けたら固定積立だけにする)
    判定2 (S3=RULES.md戦略Cを採用するか): BTCで S3 の IRR が S1 を全期間かつ2区間以上で
          上回り、最大含み損率も S1 より悪くない → 採用。不成立なら「固定積立の方が良い」
    判定3 (RULES.md の統計の再現): FGI<10 の日から90日後の騰落を、FGI<10が続いた塊(エピソード)
          単位でも数え、平均・最小・マイナスの割合を報告。「平均+48%・マイナスゼロ」と照合

出力: data/strategy_c/result.json (指標・判定・グラフ用の週次系列) と fgi.csv / btc_usd.csv /
      eth_usd.csv (検証に使った生データ。後から誰でも再計算できるように保存)
実行: python strategy_c_check.py   (GitHub Actions: Strategy C Check を手動実行)
設定: FEE (0.001) / FEE_ALT (0.03) / WEEKLY_USD (100) / FETCH_DEADLINE_MIN (8)
依存なし (標準ライブラリのみ)。
"""

import bisect
import csv
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

socket.setdefaulttimeout(35)

OUT_DIR = os.environ.get("STRATEGY_C_DIR") or "data/strategy_c"
FEE = float(os.environ.get("FEE") or "0.001")
FEE_ALT = float(os.environ.get("FEE_ALT") or "0.03")
WEEKLY = float(os.environ.get("WEEKLY_USD") or "100")
DEADLINE_MIN = float(os.environ.get("FETCH_DEADLINE_MIN") or "8")
START = date(2018, 2, 1)
PERIODS = [("2018-02〜2020", date(2018, 2, 1), date(2020, 12, 31)),
           ("2021〜2023", date(2021, 1, 1), date(2023, 12, 31)),
           ("2024〜", date(2024, 1, 1), None)]
FEAR, GREED, TP_FRAC = 30, 70, 0.25

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
_deadline = time.time() + DEADLINE_MIN * 60


def get_json(url, timeout=25):
    if time.time() > _deadline:
        raise TimeoutError("全体デッドライン超過")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- データ取得
def fetch_fgi():
    """{date: value(int)} — alternative.me の全履歴。"""
    data = get_json("https://api.alternative.me/fng/?limit=0&format=json").get("data") or []
    out = {}
    for x in data:
        try:
            d = datetime.fromtimestamp(int(x["timestamp"]), tz=timezone.utc).date()
            out[d] = int(x["value"])
        except (KeyError, ValueError, TypeError):
            continue
    return out


def fetch_okx(inst):
    """{date: close} — OKX 現物の日足 (UTC区切り) を古い方へページ送りで全取得。"""
    out, after = {}, None
    for _ in range(80):
        url = (f"https://www.okx.com/api/v5/market/history-candles?instId={inst}&bar=1Dutc&limit=100"
               + (f"&after={after}" if after else ""))
        j = get_json(url)
        if str(j.get("code")) != "0":
            raise ValueError(f"OKX error {j.get('code')}: {j.get('msg')}")
        rows = j.get("data") or []
        if not rows:
            break
        for r in rows:
            ts = int(r[0])
            confirmed = (len(r) < 9) or r[8] == "1"   # 未確定(当日)の足は使わない
            if confirmed:
                out[datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()] = float(r[4])
        oldest = min(int(r[0]) for r in rows)
        if after is not None and oldest >= after:
            break
        after = oldest
        if datetime.fromtimestamp(oldest / 1000, tz=timezone.utc).date() < date(2017, 12, 1):
            break
        time.sleep(0.15)
    return out


def fetch_coinbase(product):
    """{date: close} — Coinbase Exchange の日足 (1回300本まで) を2018年から順に取得。"""
    out = {}
    start = datetime(2017, 12, 1, tzinfo=timezone.utc)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    while start < today:
        end = min(start + timedelta(days=299), today)
        url = (f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=86400"
               f"&start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}&end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        for r in get_json(url):
            d = datetime.fromtimestamp(int(r[0]), tz=timezone.utc).date()
            if d < today.date():   # 当日(未確定)は除く
                out[d] = float(r[4])
        start = end + timedelta(days=1)
        time.sleep(0.35)
    return out


def load_prices(asset):
    """(prices{date: close}, source, cross_check) — OKX優先、Coinbaseで照合/代替。"""
    okx = cb = None
    errs = []
    try:
        okx = fetch_okx(f"{asset}-USDT")
        print(f"  OKX {asset}-USDT: {len(okx)}日 ({min(okx)}〜{max(okx)})" if okx else f"  OKX {asset}: 0日")
    except Exception as e:
        errs.append(f"OKX: {type(e).__name__}: {e}")
        print(f"  OKX {asset} 取得失敗: {type(e).__name__}: {e}")
    try:
        cb = fetch_coinbase(f"{asset}-USD")
        print(f"  Coinbase {asset}-USD: {len(cb)}日 ({min(cb)}〜{max(cb)})" if cb else f"  Coinbase {asset}: 0日")
    except Exception as e:
        errs.append(f"Coinbase: {type(e).__name__}: {e}")
        print(f"  Coinbase {asset} 取得失敗: {type(e).__name__}: {e}")
    cross = None
    if okx and cb:
        common = sorted(set(okx) & set(cb))
        diffs = sorted(abs(okx[d] / cb[d] - 1) for d in common if cb[d] > 0)
        if diffs:
            cross = {"n_days": len(diffs), "median_abs_diff_pct": round(diffs[len(diffs) // 2] * 100, 3),
                     "p99_abs_diff_pct": round(diffs[int(len(diffs) * 0.99)] * 100, 3),
                     "max_abs_diff_pct": round(diffs[-1] * 100, 3)}
    # 主系列: OKXが2018-02から揃っていればOKX、足りなければCoinbase
    if okx and min(okx) <= START + timedelta(days=7):
        return okx, "OKX " + asset + "-USDT", cross, errs
    if cb and min(cb) <= START + timedelta(days=7):
        return cb, "Coinbase " + asset + "-USD", cross, errs
    best = okx or cb or {}
    return best, ("OKX" if best is okx else "Coinbase") + f" {asset} (期間不足)", cross, errs


# ---------------------------------------------------------------- シミュレーション
def xirr(flows):
    """flows: [(date, amount)] (投入は負、回収と最終評価額は正) → 年率。解なしは None。"""
    if not flows:
        return None
    t0 = flows[0][0]

    def npv(r):
        return sum(a / (1 + r) ** ((d - t0).days / 365.0) for d, a in flows)
    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def simulate(prices, fgi, start, end, strat, fee, lump_total=None):
    """週次(月曜)のルールで売買し、指標と週次の評価系列を返す。
    IRRは口座全体の資金加重利回り: 出金は無く、投入(負)と最終の口座評価額(保有コイン+現金, 正)
    だけで計算する (売却代金は口座内の現金として最終評価額に含める。二重計上しない)。"""
    days = sorted(d for d in prices if start <= d <= end and d in fgi)
    mondays = [d for d in days if d.weekday() == 0]
    if not mondays:
        return None
    coins = cash = invested = 0.0
    flows, curve = [], []
    n_buy = n_sell = 0
    peak_moic, max_under = 0.0, 0.0
    if strat == "S0":
        amt = lump_total
        coins = amt * (1 - fee) / prices[mondays[0]]
        invested = amt
        flows.append((mondays[0], -amt))
        n_buy = 1
    for d in mondays:
        p, f = prices[d], fgi[d]
        if strat == "S1":
            buy = WEEKLY
        elif strat == "S2":
            buy = WEEKLY * (2 if f <= FEAR else 1)
        elif strat == "S3":
            buy = WEEKLY if f <= FEAR else 0.0
            if f >= GREED and coins > 0:
                sell = coins * TP_FRAC
                proceeds = sell * p * (1 - fee)
                coins -= sell
                cash += proceeds   # 売却代金は口座内に現金で残す (外へは出さない)
                n_sell += 1
        else:
            buy = 0.0
        if buy > 0:
            coins += buy * (1 - fee) / p
            invested += buy
            flows.append((d, -buy))
            n_buy += 1
        value = coins * p + cash
        if invested > 0:
            moic = value / invested
            curve.append((d.isoformat(), round(moic, 4)))
            peak_moic = max(peak_moic, moic)
            if peak_moic > 0:
                max_under = min(max_under, moic / peak_moic - 1)
    last = mondays[-1]
    final = coins * prices[last] + cash
    irr = xirr(flows + [(last, final)]) if invested > 0 else None
    return {"invested": round(invested, 2), "final_value": round(final, 2),
            "moic": round(final / invested, 4) if invested else None,
            "irr": round(irr, 4) if irr is not None else None,
            "max_drawdown_of_moic": round(max_under, 4),
            "n_buy_weeks": n_buy, "n_sell_weeks": n_sell, "end": last.isoformat(),
            "avg_cost": round(invested / coins, 2) if strat != "S3" and coins > 0 else None,
            "curve": curve}


def run_asset(prices, fgi, fee, keep_curve=True):
    """全期間+3区間 × S0〜S3 の結果。グラフ用の週次系列は全期間(keep_curve時)だけ残す。"""
    last_day = max(d for d in prices if d in fgi)
    out = {}
    for label, s, e in [("全期間", START, None)] + PERIODS:
        e = e or last_day
        res = {}
        for st in ("S1", "S2", "S3"):
            res[st] = simulate(prices, fgi, s, e, st, fee)
        if res["S1"]:
            res["S0"] = simulate(prices, fgi, s, e, "S0", fee, lump_total=res["S1"]["invested"])
        for v in res.values():
            if v and not (keep_curve and label == "全期間"):
                v.pop("curve", None)
        out[label] = res
    return out


def verdicts(btc, eth):
    """事前に固定した合否基準を機械的に当てはめる。"""
    def wins(res, a, b, key):
        full = res["全期間"]
        sub = [res[p[0]] for p in PERIODS]
        ok_full = (full[a] or {}).get(key) is not None and (full[b] or {}).get(key) is not None \
            and full[a][key] > full[b][key]
        n_sub = sum(1 for r in sub if r.get(a) and r.get(b) and r[a].get(key) is not None
                    and r[b].get(key) is not None and r[a][key] > r[b][key])
        return ok_full, n_sub

    v = {}
    f_b, n_b = wins(btc, "S2", "S1", "moic")
    f_e, n_e = wins(eth, "S2", "S1", "moic") if eth else (False, 0)
    pass_b = f_b and n_b >= 2
    pass_e = f_e and n_e >= 2
    v["判定1_恐怖で倍額(S2)"] = {
        "BTC_全期間で勝ち": f_b, "BTC_勝ち区間数": n_b, "ETH_全期間で勝ち": f_e, "ETH_勝ち区間数": n_e,
        "結論": ("採用 (確度高: BTC・ETHとも基準を満たす)" if pass_b and pass_e else
                 "条件付き採用 (BTCは基準を満たすがETHでは不成立)" if pass_b else
                 "不採用 → 固定積立のみ (事前合意どおり)")}
    f3, n3 = wins(btc, "S3", "S1", "irr")
    dd_ok = (btc["全期間"]["S3"] or {}).get("max_drawdown_of_moic", -9) >= \
        (btc["全期間"]["S1"] or {}).get("max_drawdown_of_moic", 0)
    v["判定2_RULES戦略C(S3)"] = {
        "BTC_全期間でIRR勝ち": f3, "BTC_IRR勝ち区間数": n3, "最大含み損がS1より悪くない": dd_ok,
        "結論": "採用 (RULES.mdの戦略Cは固定積立より良い)" if (f3 and n3 >= 2 and dd_ok)
                else "不採用 → 固定積立の方が良い (または同等以下)"}
    return v


def event_study(prices, fgi, thr, horizon=90, gap_days=30):
    """FGI<thr の日から horizon 日後の騰落。日単位と、塊(エピソード)単位の両方。"""
    days = sorted(d for d in fgi if d in prices)
    pdays = sorted(prices)

    def fwd(d):
        t = d + timedelta(days=horizon)
        i = bisect.bisect_left(pdays, t)
        if i >= len(pdays) or (pdays[i] - t).days > 3:
            return None
        return prices[pdays[i]] / prices[d] - 1
    hits = [d for d in days if fgi[d] < thr]
    day_rets = [r for r in (fwd(d) for d in hits) if r is not None]
    episodes, last = [], None
    for d in hits:
        if last is None or (d - last).days > gap_days:
            episodes.append(d)
        last = d
    ep = [(d, fwd(d)) for d in episodes]
    ep_rets = [r for _, r in ep if r is not None]
    base = [r for r in (fwd(d) for d in days) if r is not None]

    def stats(xs):
        if not xs:
            return None
        s = sorted(xs)
        return {"n": len(xs), "mean": round(sum(xs) / len(xs), 4), "median": round(s[len(s) // 2], 4),
                "min": round(s[0], 4), "max": round(s[-1], 4), "share_negative": round(sum(1 for x in xs if x < 0) / len(xs), 3)}
    return {"threshold": thr, "horizon_days": horizon, "days": stats(day_rets), "episodes": stats(ep_rets),
            "episode_list": [{"date": d.isoformat(), "fgi": fgi[d], "ret": round(r, 4) if r is not None else None}
                             for d, r in ep],
            "base_all_days": stats(base)}


def save_series(path, series, col):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", col])
        for d in sorted(series):
            w.writerow([d.isoformat(), series[d]])


def main():
    t0 = time.time()
    print("FGI (alternative.me) を取得中…")
    fgi = fetch_fgi()
    if len(fgi) < 1500:
        print(f"FGIが{len(fgi)}日分しか取れなかった。中止。"); sys.exit(1)
    print(f"  FGI: {len(fgi)}日 ({min(fgi)}〜{max(fgi)})")
    assets, meta = {}, {}
    for a in ("BTC", "ETH"):
        print(f"{a} 日足を取得中…")
        prices, src, cross, errs = load_prices(a)
        assets[a] = prices
        meta[a] = {"source": src, "n_days": len(prices), "first": str(min(prices)) if prices else None,
                   "last": str(max(prices)) if prices else None, "okx_vs_coinbase": cross, "errors": errs}
    if len(assets["BTC"]) < 1500:
        print("BTCの価格が足りない。中止。"); sys.exit(1)

    save_series(os.path.join(OUT_DIR, "fgi.csv"), fgi, "fgi")
    save_series(os.path.join(OUT_DIR, "btc_usd.csv"), assets["BTC"], "close")
    if assets["ETH"]:
        save_series(os.path.join(OUT_DIR, "eth_usd.csv"), assets["ETH"], "close")

    result = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "params": {"weekly_usd": WEEKLY, "fee": FEE, "fee_alt": FEE_ALT, "fear": FEAR, "greed": GREED,
                         "tp_frac": TP_FRAC, "start": START.isoformat(),
                         "periods": [[p[0], p[1].isoformat(), p[2].isoformat() if p[2] else None] for p in PERIODS]},
              "data": {"fgi": {"n_days": len(fgi), "first": str(min(fgi)), "last": str(max(fgi))}, **meta},
              "results": {}, "results_fee_alt": {}}
    for a, prices in assets.items():
        if len(prices) < 1500:
            continue
        result["results"][a] = run_asset(prices, fgi, FEE)
        result["results_fee_alt"][a] = run_asset(prices, fgi, FEE_ALT, keep_curve=False)
    result["verdicts"] = verdicts(result["results"]["BTC"], result["results"].get("ETH"))
    result["verdicts_fee_alt"] = verdicts(result["results_fee_alt"]["BTC"], result["results_fee_alt"].get("ETH"))
    result["event_study"] = {f"BTC_FGI<{t}": event_study(assets["BTC"], fgi, t) for t in (10, 20, 25, 30)}
    # グラフ用: 週次の価格とFGI
    mondays = sorted(d for d in assets["BTC"] if d in fgi and d.weekday() == 0 and d >= START)
    result["weekly"] = [{"date": d.isoformat(), "btc": round(assets["BTC"][d], 2), "fgi": fgi[d]} for d in mondays]

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    print("\n=== 戦略C 答え合わせ (BTC, 手数料0.1%) ===")
    for label, res in result["results"]["BTC"].items():
        line = " / ".join(f"{k}: 倍率{v['moic']:.2f} IRR{(v['irr'] or 0):+.1%}" for k, v in res.items() if v)
        print(f"  {label}: {line}")
    for k, v in result["verdicts"].items():
        print(f"  {k}: {v['結論']}")
    es = result["event_study"]["BTC_FGI<10"]
    if es["episodes"]:
        print(f"  FGI<10 → 90日後: 日単位 平均{es['days']['mean']:+.1%} (n={es['days']['n']}) / "
              f"塊単位 平均{es['episodes']['mean']:+.1%} 最小{es['episodes']['min']:+.1%} "
              f"マイナス{es['episodes']['share_negative']:.0%} (n={es['episodes']['n']}) / "
              f"全日平均{es['base_all_days']['mean']:+.1%}")
    print(f"完了 ({time.time() - t0:.0f}秒) → {OUT_DIR}/result.json")


if __name__ == "__main__":
    main()
