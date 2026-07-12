"""
Dashboard builder — 収集済みCSVから資金集中ダッシュボードHTMLを生成する。

850銘柄化に伴い、全銘柄の生1分足をHTMLに埋め込む方式(旧実装)は10MB超で破綻するため
アーキテクチャを変更した:
  - site/index.html … 全銘柄分の「スカラー値」だけを埋め込む (ランキング/ヒート/表/
    自動分析コメント/需給バッジ + 初期選択銘柄のチャートデータのみ)。集計窓(1分〜月足)
    ごとのsurge/シェア/勢いはこのビルド時にPythonで事前計算する(jp_money_flow.pyの
    window_stats()を再利用。ブラウザ側では再計算しない)。
  - site/data/{code}.json … 銘柄別の生OHLCVチャートデータ。ブラウザが銘柄選択時に
    fetchで遅延取得する(index.htmlには初期選択銘柄の分だけ埋め込み、それ以外は
    このファイル群から都度取得)。

data/jp_stocks/{code}_1m.csv (分足) + {code}_1d.csv (日足) + universe.csv +
money_flow.json (自動分析コメント) + supply_demand/short_positions.csv を読み、
dashboard/template.html の __MFR__/__COMMENTARY__/__SUPPLY__/__INITIAL__/__GROUPS__ を
差し込んで site/index.html + site/data/*.json を書き出す。GitHub Pagesワークフローが
これを実行してデプロイする。

__GROUPS__ (compute_group_windows()) は universe.csv の group 列(JPX正式33業種区分。
旧ファイルはsectorへフォールバック)に custom_groups.csv の独自区分(例:「半導体」、
jmf.load_custom_groups()で上書き)を適用したグループで銘柄単位のshareR/dBaseを合算した
{wkey: [{group, share_pct, delta_pp, n}, ...]} で、ダッシュボードの「業種別シェア」
パネルが既存の集計窓セレクタと連動して参照する。34業種×窓数程度でサイズは極小。

依存なし (標準ライブラリのみ)。実行: python dashboard/build_dashboard.py
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # jp_money_flow.py (リポジトリ直下) をimportするため
import jp_money_flow as jmf  # noqa: E402  window_stats()を再利用 (窓計算ロジックの単一化)

JST = timezone(timedelta(hours=9))
WD = ["月", "火", "水", "木", "金", "土", "日"]
DATA = os.path.join(ROOT, "data", "jp_stocks")
TEMPLATE = os.path.join(ROOT, "dashboard", "template.html")
HELP_TEMPLATE = os.path.join(ROOT, "dashboard", "help_template.html")
OUT_DIR = os.environ.get("SITE_DIR") or os.path.join(ROOT, "site")
MF_JSON = os.path.join(DATA, "money_flow.json")
HOT_CHANGES_JSON = os.path.join(DATA, "hot_changes_latest.json")
SD_CSV = os.path.join(DATA, "supply_demand", "short_positions.csv")

# 集計窓の定義 (旧クライアント側WINSと同じ粒度)。secは秒、daily=Trueは日足配列を使う。
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
    # 独自区分(custom_groups.csv、例:「半導体」)の上書きマップ。jp_money_flow.py の
    # ロジックを再利用してスクリーナーとダッシュボードのグループ判定を単一化する。
    custom = jmf.load_custom_groups()
    with open(os.path.join(DATA, "universe.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            code = r["code"].strip()
            sym = code if "." in code else code + ".T"
            sector = r.get("sector", "")
            # group = 独自区分 → JPX正式33業種区分 → sector(旧universe.csvの後方互換)
            # → 不明 (jp_money_flow.pyの_group_of()と同じ優先順位)。
            group = jmf._group_of(r, custom)
            uni.append((sym, r["name"], r.get("bucket", "core"), sector, group))
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


def load_1d(sym, cutoff_date, t1m, c1m, v1m):
    """日足2年。最終日(cutoff)は寄り直後取得で不完全なので1分足から再構成。"""
    p = os.path.join(DATA, f"{sym.replace('.', '_')}_1d.csv")
    T, O, H, L, C, V = [], [], [], [], [], []
    if os.path.exists(p):
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["timestamp_jst"][:10] >= cutoff_date:
                    continue
                try:
                    T.append(int(row["epoch"])); O.append(float(row["open"])); H.append(float(row["high"]))
                    L.append(float(row["low"])); C.append(float(row["close"])); V.append(int(float(row["volume"] or 0)))
                except (ValueError, KeyError, TypeError):
                    continue
    todays = sorted((t, c, v) for t, c, v in zip(t1m, c1m, v1m)
                     if datetime.fromtimestamp(t, JST).strftime("%Y-%m-%d") == cutoff_date and v > 0)
    if todays:
        cs = [r[1] for r in todays]
        T.append(todays[0][0]); O.append(cs[0]); H.append(max(cs)); L.append(min(cs))
        C.append(cs[-1]); V.append(sum(r[2] for r in todays))
    return {"t": T, "o": O, "h": H, "l": L, "c": C, "v": V} if T else None


def load_supply_demand():
    """JPX空売り残高報告(大口0.5%以上・日次、jp_supply_demand.py が蓄積)を
    銘柄(sym形式、例 "7203.T")別に集計する。データが無い/該当銘柄が無ければ
    空dict(=ダッシュボード側は該当銘柄を「データ無し」として素通し表示)。
    戻り値: {sym: {"date": "YYYY-MM-DD", "n": 報告者数, "pct": 合計比率%}}
    """
    if not os.path.exists(SD_CSV):
        return {}
    latest_date = {}
    with open(SD_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            code, date = r.get("code"), r.get("disclosure_date")
            if not code or not date:
                continue
            if date > latest_date.get(code, ""):
                latest_date[code] = date
    agg = {}
    with open(SD_CSV, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            code = r.get("code")
            if not code or r.get("disclosure_date") != latest_date.get(code):
                continue
            try:
                ratio = float(r.get("ratio_pct") or 0)
            except (ValueError, TypeError):
                ratio = 0.0
            sym = code if "." in code else code + ".T"
            a = agg.setdefault(sym, {"date": latest_date[code], "n": 0, "pct": 0.0})
            a["n"] += 1
            a["pct"] += ratio
    for v in agg.values():
        v["pct"] = round(v["pct"], 2)
    return agg


def _stat_rows_intraday(t, c, v):
    """window_stats向け: 出来高0を除いた(epoch,close,vol,turnover)。jp_money_flow.load_barsと同じ規則。"""
    return sorted(((tt, cc, vv, cc * vv) for tt, cc, vv in zip(t, c, v) if vv > 0), key=lambda r: r[0])


def _stat_rows_daily(d):
    if not d:
        return []
    return sorted(((t, c, v, c * v) for t, c, v in zip(d["t"], d["c"], d["v"]) if v > 0), key=lambda r: r[0])


def compute_all_windows(per_ticker_rows):
    """per_ticker_rows: {sym: {"intraday": rows, "daily": rows}} から
    全銘柄×全窓の (recent, prev, med, total) を計算し、続けてクロス銘柄シェアを正規化する。
    戻り値: {sym: {wkey: {surge, shareR, momentum, dBase, recent}}}
    """
    raw = {}
    intraday_total, daily_total = {}, {}
    for sym, rows in per_ticker_rows.items():
        wraw = {}
        for w in WINS:
            src = rows["daily"] if w.get("daily") else rows["intraday"]
            recent, prev, med = jmf.window_stats(src, w["sec"])
            wraw[w["k"]] = {"recent": recent, "prev": prev, "med": med}
        raw[sym] = wraw
        intraday_total[sym] = sum(r[3] for r in rows["intraday"])
        daily_total[sym] = sum(r[3] for r in rows["daily"])

    grand_intraday = sum(intraday_total.values()) or 1.0
    grand_daily = sum(daily_total.values()) or 1.0
    sums = {}
    for w in WINS:
        s_r = sum(raw[sym][w["k"]]["recent"] for sym in raw) or 1.0
        s_p = sum(raw[sym][w["k"]]["prev"] for sym in raw) or 1.0
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
            shareP = prev / s_p * 100
            grand = grand_daily if w.get("daily") else grand_intraday
            total = daily_total[sym] if w.get("daily") else intraday_total[sym]
            shareBase = total / grand * 100
            wres[k] = {
                "recent": round(recent),
                "surge": round(surge, 3),
                "shareR": round(shareR, 3),
                "momentum": round(shareR - shareP, 3),
                "dBase": round(shareR - shareBase, 3),
            }
        result[sym] = wres
    return result


def compute_group_windows(summary_tickers):
    """業種グループ(universe.csvのgroup列、JPX正式33業種区分)別に、
    集計窓(1分〜月足)ごとの売買代金シェア%とΔpp(通常比)を事前計算する。

    銘柄単位でcompute_all_windows()が既に算出済みのshareR(直近窓シェア)と
    dBase(=shareR-shareBase、通常比の差分)をグループ内で合算するだけ
    (二重計算しない。jp_money_flow.pyのgroup集計と同じ「合算するだけ」設計)。
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
    supply = load_supply_demand()

    for sym, name, bucket, sector, group in uni:
        t, c, v = load_1m(sym)
        if not t:
            continue
        latest_ts = max(latest_ts, t[-1])
        cutoff = datetime.fromtimestamp(t[-1], JST).strftime("%Y-%m-%d")
        d = load_1d(sym, cutoff, t, c, v)
        per_ticker_rows[sym] = {
            "intraday": _stat_rows_intraday(t, c, v),
            "daily": _stat_rows_daily(d),
        }
        pct = (c[-1] / c[0] - 1) * 100 if c and c[0] else 0.0
        ticker_meta[sym] = {
            "code": sym, "name": name, "bucket": bucket, "sector": sector, "group": group,
            "last": c[-1] if c else None, "pct": round(pct, 2),
            "t": t, "close": c, "vol": v, "d": d,
        }

    if not per_ticker_rows:
        raise SystemExit("1分足データが無い。先に収集を実行すること。")

    windows = compute_all_windows(per_ticker_rows)

    # サマリー用 tickers[] (site/index.htmlに埋め込む。生配列(t/close/vol/d)は含めない)
    summary_tickers = []
    for sym, meta in ticker_meta.items():
        summary_tickers.append({
            "code": meta["code"], "name": meta["name"], "bucket": meta["bucket"],
            "sector": meta["sector"], "group": meta["group"],
            "last": meta["last"], "pct": meta["pct"],
            "w": windows[sym],
        })

    # 初期選択銘柄: 既定窓(5m)でsurgeが最大の銘柄 (旧クライアントの既定挙動を踏襲)
    initial_sym = max(summary_tickers, key=lambda t: t["w"].get(DEFAULT_WKEY, {}).get("surge", 0))["code"]

    group_windows = compute_group_windows(summary_tickers)

    d0 = min(m["t"][0] for m in ticker_meta.values() if m["t"])
    d1 = latest_ts
    meta = {
        "start": datetime.fromtimestamp(d0, JST).strftime("%Y-%m-%d %H:%M"),
        "end": datetime.fromtimestamp(d1, JST).strftime("%Y-%m-%d %H:%M") if d1 else "-",
        "n": len(summary_tickers),
        # 話題枠(hot)銘柄数 — ヘッダーの「話題枠 +N」表示用。leader/core(TOPIX500/日経225)は
        # 固定の指数母数で、増減するのは話題枠だけなので、その分を可視化する。
        "hot_n": sum(1 for t in summary_tickers if t.get("bucket") == "hot"),
        "source": "Yahoo Finance",
        "default_win": DEFAULT_WKEY,
        # 独自区分の名前一覧 (テンプレート側がツールチップに「独自区分」注記を
        # 付けるために使う。custom_groups.csvに区分を足してもテンプレ変更不要)
        "custom_groups": sorted(set(jmf.load_custom_groups().values())),
    }
    return meta, summary_tickers, ticker_meta, initial_sym, supply, group_windows


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
    meta, summary_tickers, ticker_meta, initial_sym, supply, group_windows = build(uni)

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

    # 今週の話題枠 入れ替え (hot_refresh.py が出力。無ければ空)
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
               .replace("__SUPPLY__", json.dumps(supply, ensure_ascii=False, separators=(",", ":")))
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
    print(f"built {outp} (+ help.html) ({idx_kb} KB) + site/data/*.json ({len(ticker_meta)}ファイル, 合計{data_kb} KB), "
          f"{len(summary_tickers)}銘柄, コメント{len(commentary)}行, 空売り残高データ{len(supply)}銘柄, "
          f"所要{elapsed:.1f}秒")


if __name__ == "__main__":
    main()
