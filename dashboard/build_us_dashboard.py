"""
US Dashboard builder — 収集済みCSVから米国株 資金集中ダッシュボードHTMLを生成する。
dashboard/build_dashboard.py (日本株版) の忠実な移植。

  - site/us/index.html … 全銘柄分の「スカラー値」だけを埋め込む (ランキング/ヒート/表/
    自動分析コメント + 初期選択銘柄のチャートデータのみ)。集計窓(1分〜月足)ごとの
    surge/シェア/勢いはこのビルド時にPythonで事前計算する(us_money_flow.pyの
    window_stats()を再利用。ブラウザ側では再計算しない)。
  - site/us/data/{code}.json … 銘柄別の生OHLCVチャートデータ。ブラウザが銘柄選択時に
    fetchで遅延取得する。

JP版との差分:
  - import us_money_flow as umf (window_stats/load_custom_groups等を再利用)
  - JST→ET、timestamp_jst→timestamp_et
  - ".T" 補完なし (codeがそのままYahooシンボル)
  - 空売り残高(JPX)バッジは米国に対応データが無いため完全に除去 (D_SUPPLY相当の
    受け渡しをしない。JP版のload_supply_demand()に相当する関数自体を持たない)
  - 出力先は既定 site/us (環境変数 SITE_DIR で上書き可)

data/us_stocks/{code}_1m.csv (分足) + {code}_1d.csv (日足) + universe.csv +
money_flow.json (自動分析コメント) を読み、dashboard/us_template.html の
__MFR__/__COMMENTARY__/__INITIAL__/__GROUPS__/__HOTCHANGES__ を差し込んで
site/us/index.html + site/us/data/*.json を書き出す。

依存なし (標準ライブラリのみ)。実行: python dashboard/build_us_dashboard.py
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # us_money_flow.py (リポジトリ直下) をimportするため
import us_money_flow as umf  # noqa: E402  window_stats()を再利用 (窓計算ロジックの単一化)

ET = ZoneInfo("America/New_York")
DATA = os.path.join(ROOT, "data", "us_stocks")
TEMPLATE = os.path.join(ROOT, "dashboard", "us_template.html")
HELP_TEMPLATE = os.path.join(ROOT, "dashboard", "us_help_template.html")
OUT_DIR = os.environ.get("SITE_DIR") or os.path.join(ROOT, "site", "us")
MF_JSON = os.path.join(DATA, "money_flow.json")
HOT_CHANGES_JSON = os.path.join(DATA, "hot_changes_latest.json")

# 集計窓の定義 (JP版と同じ粒度)。secは秒、daily=Trueは日足配列を使う。
WINS = [
    {"k": "1m",   "sec": 60},
    {"k": "5m",   "sec": 5 * 60},
    {"k": "15m",  "sec": 15 * 60},
    {"k": "30m",  "sec": 30 * 60},
    {"k": "60m",  "sec": 60 * 60},
    {"k": "240m", "sec": 240 * 60},
    {"k": "1d",   "sec": 1 * 86400, "daily": True},
    {"k": "1w",   "sec": 5 * 86400, "daily": True},
    {"k": "1M",   "sec": 21 * 86400, "daily": True},
]
DEFAULT_WKEY = "5m"


def load_universe():
    uni = []
    # 独自区分(custom_groups.csv、例:「半導体」)の上書きマップ。us_money_flow.py の
    # ロジックを再利用してスクリーナーとダッシュボードのグループ判定を単一化する。
    custom = umf.load_custom_groups()
    with open(os.path.join(DATA, "universe.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            code = r["code"].strip()
            sector = r.get("sector", "")
            # group = 独自区分 → GICSセクター(日本語訳) → sector(旧universe.csvの後方互換)
            # → 不明 (us_money_flow.pyの_group_of()と同じ優先順位)。
            group = umf._group_of(r, custom)
            uni.append((code, r["name"], r.get("bucket", "core"), sector, group))
    return uni


def load_1m(sym):
    """1分足を own timeline で返す: (t[], close[], vol[]) epoch昇順、欠損の穴埋めなし。"""
    p = os.path.join(DATA, f"{sym.replace('.', '_')}_1m.csv")
    if not os.path.exists(p):
        return [], [], []
    rows = []
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                rows.append((int(row["epoch"]), float(row["close"]), int(float(row["volume"] or 0))))
            except (ValueError, KeyError, TypeError):
                continue
    rows.sort(key=lambda r: r[0])
    if not rows:
        return [], [], []
    t, c, v = zip(*rows)
    return list(t), list(c), list(v)


def load_1d(sym, cutoff_date, rows1m):
    """日足2年 (cutoff_date までの1日1本)。rows1m は異常足を除いた1分足 (epoch, close, vol, turnover) の昇順。

    - 日足ファイルに無い日は1分足から組み立てる。米国の日足はその日の夜 (ET) に銘柄ごとに順に出るため、
      引け後処理が早く動いた日は前日分がまだ無く、旧版 (最終日だけ組み立て) では前日が抜けていた
      (2026-10-09: 738銘柄中715銘柄で 10/7 が無い)。引け後処理のデータ保存が消えた日も同じ
    - 最終日は、日足ファイルの値 (確定値) の出来高の方が大きければそちらを使う。1分足には大引けのクロス
      (16:00) の出来高が入らず、確定した日足の8割弱にしかならない (2026-09-28〜10-06 の実データの中央値 0.70〜0.80)
    """
    days = {}   # 日付 → (epoch, open, high, low, close, volume)
    p = os.path.join(DATA, f"{sym.replace('.', '_')}_1d.csv")
    if os.path.exists(p):
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = row["timestamp_et"][:10]
                if d > cutoff_date:
                    continue
                try:
                    days[d] = (int(row["epoch"]), float(row["open"]), float(row["high"]), float(row["low"]),
                               float(row["close"]), int(float(row["volume"] or 0)))
                except (ValueError, KeyError, TypeError):
                    continue
    by_day = {}
    for e, c, v, _to in rows1m:
        d = datetime.fromtimestamp(e, ET).strftime("%Y-%m-%d")
        if d <= cutoff_date:
            by_day.setdefault(d, []).append((e, c, v))
    for d, bars in by_day.items():
        cs = [b[1] for b in bars]
        rec = (bars[0][0], cs[0], max(cs), min(cs), cs[-1], int(sum(b[2] for b in bars)))
        if d not in days or (d == cutoff_date and rec[5] > days[d][5]):
            days[d] = rec
    if not days:
        return None
    keys = sorted(days)
    T = [_day_epoch(k) for k in keys]
    O, H, L, C, V = ([days[k][i] for k in keys] for i in range(1, 6))
    return {"t": T, "o": O, "h": H, "l": L, "c": C, "v": V}


def _day_epoch(day):
    """日付 'YYYY-MM-DD' の寄り (9:30 ET) の epoch。日足の時刻はこれにそろえる: Yahoo は当日分を引け後の仮の時刻
    (16:00〜16:04) で返すため、引け直後に取った日の足と次の日の足の間が24時間未満になり、1日窓の「直近」に2日分が
    入りうる (2026-10-12 時点の日足ファイルに4行)。1分足から組み立てた日も最初の足の時刻ではなくこれを使う。"""
    y, m, d = map(int, day.split("-"))
    return int(datetime(y, m, d, 9, 30, tzinfo=ET).timestamp())


def _raw_rows(t, c, v):
    """出来高0を除いた(epoch,close,vol,turnover)の昇順。異常足は build() で全銘柄が揃ってから除く
    (us_money_flow.load_barsと同じ規則。寄り付きの足 9:30 は窓統計の直前に除く)。"""
    return sorted(((tt, cc, vv, cc * vv) for tt, cc, vv in zip(t, c, v) if vv > 0), key=lambda r: r[0])


def _stat_rows_daily(d):
    if not d:
        return []
    return sorted(((t, c, v, c * v) for t, c, v in zip(d["t"], d["c"], d["v"]) if v > 0), key=lambda r: r[0])


# 場の時間 (ET・平日)。寄りのクロス 9:30 は窓統計に入れない (us_money_flow.OPEN_SKIP_UNTIL_HM) ので 9:31 から
SESSIONS = (("09:31", "16:00"),)


# 勢いを比べる条件: 直前の窓のうち場中が min(窓の半分, この秒数) 以上 (理由は compute_all_windows)
MOM_MIN_OPEN_SEC = 15 * 60


def _open_seconds(a, b):
    """(a, b] (epoch 秒) に入る場中の1分足の時刻の数 × 60 (= 場中の長さ・秒)。祝日は見ない (直前の窓は最後の足から
    最長8時間前までで、夜をまたいで前の営業日の場に届くことは無いため)。"""
    a, b = int(a), int(b)
    tot = 0
    d = datetime.fromtimestamp(a, ET).date()
    last = datetime.fromtimestamp(b, ET).date()
    while d <= last:
        if d.weekday() < 5:
            for st, en in SESSIONS:
                (hs, ms), (he, me) = map(int, st.split(":")), map(int, en.split(":"))
                t0 = int(datetime(d.year, d.month, d.day, hs, ms, tzinfo=ET).timestamp())
                t1 = int(datetime(d.year, d.month, d.day, he, me, tzinfo=ET).timestamp())
                first = t0 if a < t0 else t0 + ((a - t0) // 60 + 1) * 60   # (a, b] に入る最初の足の時刻
                hi = min(b, t1)
                if hi >= first:
                    tot += ((hi - first) // 60 + 1) * 60
        d += timedelta(days=1)
    return tot


def compute_all_windows(per_ticker_rows):
    """per_ticker_rows: {sym: {"intraday": rows, "daily": rows}} から
    全銘柄×全窓の (recent, prev, med, total) を計算し、続けてクロス銘柄シェアを正規化する。
    戻り値: {sym: {wkey: {surge, shareR, momentum, dBase, recent}}} (momentum は比べない時 None)
    """
    raw = {}
    intraday_total, daily_total = {}, {}
    for sym, rows in per_ticker_rows.items():
        wraw = {}
        for w in WINS:
            src = rows["daily"] if w.get("daily") else rows["intraday"]
            recent, prev, med = umf.window_stats(src, w["sec"])
            wraw[w["k"]] = {"recent": recent, "prev": prev, "med": med,
                           "prev_open": (_open_seconds(src[-1][0] - 2 * w["sec"], src[-1][0] - w["sec"])
                                         if src and not w.get("daily") else None)}
        raw[sym] = wraw
        intraday_total[sym] = sum(r[3] for r in rows["intraday"])
        daily_total[sym] = sum(r[3] for r in rows["daily"])

    grand_intraday = sum(intraday_total.values()) or 1.0
    grand_daily = sum(daily_total.values()) or 1.0
    sums = {}
    for w in WINS:
        s_r = sum(raw[sym][w["k"]]["recent"] for sym in raw) or 1.0
        s_p = sum(raw[sym][w["k"]]["prev"] for sym in raw)
        sums[w["k"]] = (s_r, s_p)

    result = {}
    for sym in raw:
        wres = {}
        for w in WINS:
            k = w["k"]
            recent, prev, med = raw[sym][k]["recent"], raw[sym][k]["prev"], raw[sym][k]["med"]
            surge = (recent / med) if med > 0 else 0.0
            s_r, s_p = sums[k]
            shareR = recent / s_r * 100
            shareP = (prev / s_p * 100) if s_p > 0 else 0.0
            # 勢いは「直前の同じ長さの窓」と比べる。その窓のうち場中が min(窓の半分, 15分) に満たない銘柄は比べず
            # null (画面は「—」・全体の過半なら「⏸ 判定待ち」)。旧版は直前の窓が場の外でもシェアそのものを勢いにし、
            # 毎朝売買代金の大きい約35銘柄が🚨急騰に並んでいた (10/5〜10/9 の再現: 30分・60分・4時間窓の 9:35 で
            # 34〜36件・4時間窓は寄りから13時半まで約30件)。「丸ごと場の外なら比べない」だけでは比べ始めの直後
            # (直前の窓に場中が1〜2分だけ) に🚨が普段の1.5〜3倍に跳ねた (10/6〜10/19 の再現: 30分窓 10:01 で中央値8件・
            # 4時間窓 13:31 で8件、普段は4〜6件・3〜4件)。半分 (最大15分) を条件にするとどれも普段の水準になる
            # (最大30分も試したが、判定待ちが長くなるだけで件数は変わらない)。
            # 直前の窓が場中で約定が無かっただけの銘柄は従来どおり比べる (本物の急増)
            po = raw[sym][k]["prev_open"]
            comparable = po is None or po >= min(w["sec"] / 2, MOM_MIN_OPEN_SEC)
            grand = grand_daily if w.get("daily") else grand_intraday
            total = daily_total[sym] if w.get("daily") else intraday_total[sym]
            shareBase = total / grand * 100
            wres[k] = {
                "recent": round(recent),
                "surge": round(surge, 3),
                "shareR": round(shareR, 3),
                "momentum": round(shareR - shareP, 3) if comparable else None,
                "dBase": round(shareR - shareBase, 3),
            }
        result[sym] = wres
    return result


def compute_group_windows(summary_tickers):
    """業種グループ(universe.csvのgroup列、GICSセクター日本語訳)別に、
    集計窓(1分〜月足)ごとの売買代金シェア%とΔpp(通常比)を事前計算する。

    銘柄単位でcompute_all_windows()が既に算出済みのshareR(直近窓シェア)と
    dBase(=shareR-shareBase、通常比の差分)をグループ内で合算するだけ
    (二重計算しない。us_money_flow.pyのgroup集計と同じ「合算するだけ」設計)。
    戻り値: {wkey: [{group, share_pct, delta_pp, n}, ...]} (share_pct降順)
    """
    by_group = {}
    for tk in summary_tickers:
        by_group.setdefault(tk["group"], []).append(tk)

    result = {}
    for w in WINS:
        k = w["k"]
        stats = []
        for g, tks in by_group.items():
            share = sum(tk["w"].get(k, {}).get("shareR", 0.0) for tk in tks)
            delta = sum(tk["w"].get(k, {}).get("dBase", 0.0) for tk in tks)
            stats.append({"group": g, "share_pct": round(share, 3),
                          "delta_pp": round(delta, 3), "n": len(tks)})
        stats.sort(key=lambda x: -x["share_pct"])
        result[k] = stats
    return result


def build(uni):
    per_ticker_rows = {}
    ticker_meta = {}
    latest_ts = 0

    raw_rows = {}
    for sym, name, bucket, sector, group in uni:
        t, c, v = load_1m(sym)
        if not t:
            continue
        latest_ts = max(latest_ts, t[-1])
        raw_rows[sym] = _raw_rows(t, c, v)
        pct = (c[-1] / c[0] - 1) * 100 if c and c[0] else 0.0
        ticker_meta[sym] = {
            "code": sym, "name": name, "bucket": bucket, "sector": sector, "group": group,
            "last": c[-1] if c else None, "pct": round(pct, 2),
            "t": t, "close": c, "vol": v, "d": None,
        }

    # 複数銘柄で同時に起きた「累計出来高入り」異常足を除いてから、日足の組み立てと窓統計に使う
    # (us_money_flow と同じ規則。チャートの生データは残す。旧版は日足の組み立てに異常足が入ったままだった)
    cleaned = umf.apply_glitch_drop(raw_rows)
    for sym, meta in ticker_meta.items():
        cutoff = datetime.fromtimestamp(meta["t"][-1], ET).strftime("%Y-%m-%d")
        d = load_1d(sym, cutoff, cleaned[sym])
        meta["d"] = d
        per_ticker_rows[sym] = {
            "intraday": umf.drop_opening_bars(cleaned[sym]),
            "daily": _stat_rows_daily(d),
        }

    if not per_ticker_rows:
        raise SystemExit("1分足データが無い。先に収集を実行すること。")

    windows = compute_all_windows(per_ticker_rows)

    # サマリー用 tickers[] (site/us/index.htmlに埋め込む。生配列(t/close/vol/d)は含めない)
    # 「本日約定なし」判定: 銘柄の最終1分足バーの日付が、市場全体の最新日付より前なら、
    # その銘柄は最新セッションで一度も約定していない(売買停止・データ遅延等)。
    mkt_date = datetime.fromtimestamp(latest_ts, ET).date() if latest_ts else None
    summary_tickers = []
    for sym, meta in ticker_meta.items():
        entry = {
            "code": meta["code"], "name": meta["name"], "bucket": meta["bucket"],
            "sector": meta["sector"], "group": meta["group"],
            "last": meta["last"], "pct": meta["pct"],
            "w": windows[sym],
        }
        t = meta["t"]
        st_date = datetime.fromtimestamp(t[-1], ET).date() if t else None
        if mkt_date and st_date and st_date < mkt_date:
            d = meta.get("d") or {}
            oo, cc = d.get("o") or [], d.get("c") or []
            # 最終取引日の寄り→引けの騰落を参考値として併記(米国には値幅制限が無いため
            # 「ストップ高/安」の推定はしない。単に「最後の約定はいつでどう動いたか」の事実)
            move = ((cc[-1] / oo[-1] - 1) * 100) if (oo and cc and oo[-1]) else 0.0
            entry["stale"] = True
            entry["last_date"] = f"{st_date.month}/{st_date.day}"
            entry["move_pct"] = round(move, 1)
        summary_tickers.append(entry)

    # 初期選択銘柄: 既定窓(5m)でsurgeが最大の銘柄 (旧クライアントの既定挙動を踏襲)
    initial_sym = max(summary_tickers, key=lambda t: t["w"].get(DEFAULT_WKEY, {}).get("surge", 0))["code"]

    group_windows = compute_group_windows(summary_tickers)

    d0 = min(m["t"][0] for m in ticker_meta.values() if m["t"])
    d1 = latest_ts
    meta = {
        "start": datetime.fromtimestamp(d0, ET).strftime("%Y-%m-%d %H:%M"),
        "end": datetime.fromtimestamp(d1, ET).strftime("%Y-%m-%d %H:%M") if d1 else "-",
        "n": len(summary_tickers),
        # 話題枠(hot)銘柄数 — ヘッダーの「話題枠 +N」表示用。leader/core(Nasdaq100/S&P500)は
        # 固定の指数母数で、増減するのは話題枠だけなので、その分を可視化する。
        "hot_n": sum(1 for t in summary_tickers if t.get("bucket") == "hot"),
        "source": "Yahoo Finance",
        "default_win": DEFAULT_WKEY,
        # 独自区分の名前一覧 (テンプレート側がツールチップに「独自区分」注記を
        # 付けるために使う。custom_groups.csvに区分を足してもテンプレ変更不要)
        "custom_groups": sorted(set(umf.load_custom_groups().values())),
    }
    return meta, summary_tickers, ticker_meta, initial_sym, group_windows


def write_ticker_json(out_data_dir, sym, meta):
    safe = sym.replace(".", "_")
    payload = {
        "code": meta["code"], "name": meta["name"], "bucket": meta["bucket"], "sector": meta["sector"],
        "t": meta["t"], "close": meta["close"], "vol": meta["vol"], "d": meta["d"],
    }
    with open(os.path.join(out_data_dir, f"{safe}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


def main():
    t0 = time.time()
    uni = load_universe()
    meta, summary_tickers, ticker_meta, initial_sym, group_windows = build(uni)

    out_data_dir = os.path.join(OUT_DIR, "data")
    os.makedirs(out_data_dir, exist_ok=True)
    for sym, tm in ticker_meta.items():
        write_ticker_json(out_data_dir, sym, tm)

    initial_payload = {
        "code": ticker_meta[initial_sym]["code"], "name": ticker_meta[initial_sym]["name"],
        "bucket": ticker_meta[initial_sym]["bucket"], "sector": ticker_meta[initial_sym]["sector"],
        "t": ticker_meta[initial_sym]["t"], "close": ticker_meta[initial_sym]["close"],
        "vol": ticker_meta[initial_sym]["vol"], "d": ticker_meta[initial_sym]["d"],
    }

    commentary = []
    if os.path.exists(MF_JSON):
        try:
            commentary = json.load(open(MF_JSON, encoding="utf-8")).get("commentary", [])
        except (ValueError, OSError):
            commentary = []

    # 今週の話題枠 入れ替え (us_hot_refresh.py が出力。無ければ空)
    hot_changes = {}
    if os.path.exists(HOT_CHANGES_JSON):
        try:
            hot_changes = json.load(open(HOT_CHANGES_JSON, encoding="utf-8"))
        except (ValueError, OSError):
            hot_changes = {}

    rich = {"meta": meta, "tickers": summary_tickers, "wins": [w["k"] for w in WINS]}
    tpl = open(TEMPLATE, encoding="utf-8").read()
    html = (tpl.replace("__MFR__", json.dumps(rich, ensure_ascii=False, separators=(",", ":")))
               .replace("__COMMENTARY__", json.dumps(commentary, ensure_ascii=False))
               .replace("__INITIAL__", json.dumps(initial_payload, ensure_ascii=False, separators=(",", ":")))
               .replace("__GROUPS__", json.dumps(group_windows, ensure_ascii=False, separators=(",", ":")))
               .replace("__HOTCHANGES__", json.dumps(hot_changes, ensure_ascii=False, separators=(",", ":"))))
    os.makedirs(OUT_DIR, exist_ok=True)
    outp = os.path.join(OUT_DIR, "index.html")
    with open(outp, "w", encoding="utf-8") as f:
        f.write(html)

    # 使い方ガイド (help.html) — 静的ページ。銘柄数と最新時刻だけ差し込む。
    help_html = (open(HELP_TEMPLATE, encoding="utf-8").read()
                 .replace("__N__", str(len(summary_tickers)))
                 .replace("__UPDATED__", str(meta["end"])))
    with open(os.path.join(OUT_DIR, "help.html"), "w", encoding="utf-8") as f:
        f.write(help_html)

    elapsed = time.time() - t0
    idx_kb = round(os.path.getsize(outp) / 1024, 1)
    data_kb = round(sum(os.path.getsize(os.path.join(out_data_dir, fn))
                         for fn in os.listdir(out_data_dir)) / 1024, 1)
    print(f"built {outp} (+ help.html) ({idx_kb} KB) + site/us/data/*.json ({len(ticker_meta)}ファイル, 合計{data_kb} KB), "
          f"{len(summary_tickers)}銘柄, コメント{len(commentary)}行, 所要{elapsed:.1f}秒")


if __name__ == "__main__":
    main()
