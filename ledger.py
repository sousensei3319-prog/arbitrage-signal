"""
仮想の約定記録 (ledger.py) — 日米株スクリーナーの「その日の判定」を毎日保存し、
後日の値動きで自動的に答え合わせする台帳。発注はしない (記録と集計だけ)。

なぜ必要か (2026-10-03 のML実現性検証で判明したこと):
  - 今のデータで「資金集中シグナルは儲かるか」を検証すると、ユニバース(監視銘柄)が
    「後から見て話題になった銘柄」を含むため結果が良く見える (後知恵バイアス)。
  - 判定に使った入力は後から変わる (Yahooの訂正・分割調整・ユニバース入替)。
  → 「その日・その時点で何をどう判定したか」を毎日そのまま残し、+1/+5/+20営業日後の
    値動きを後から機械的に書き足す。数か月たまれば、後知恵の無い成績表になる。

記録の単位: 1行 = (判定日, 銘柄)。その日のユニバース全銘柄を記録する (候補だけを残すと
「候補にならなかった銘柄との比較」ができず、選び方そのものに偏りが入るため)。
  flag = candidate : 日次の売買代金集中度 surge_1d >= LEDGER_FLAG_SURGE (既定2.0倍)
         near      : LEDGER_NEAR_SURGE (既定1.5倍) 以上 2.0倍未満 (惜しくも候補外)
         (空)      : それ以外
  surge_1d = その日の売買代金(終値×出来高) ÷ 直前20営業日の売買代金の中央値
  mf_*     = 判定日の引け時点の資金集中スクリーナー(money_flow.csv)の値 (引け前30分窓)。
             money_flow は5分ごとに上書きされるので、毎回の実行で「引け後の値」だけを
             ledger/mf_pending.json に日付別に保存しておき、その日を記録する時に使う
             (保存が無い日は空。場中の途中値は使わない)

答え合わせ (後日自動記入):
  ret_h = (h営業日後の終値) ÷ (判定翌営業日の始値) − 1   … h = 1, 5, 20
          「引け後に判定 → 翌朝の寄りで買い → h日後の引けで手仕舞い」の仮想損益
          (手数料・スリッページ・配当は含まない)。判定日の翌営業日以降の足だけを使い、
          判定時点の情報と混ざらないようにする。値は記入時点の日足ファイル(分割調整済みで
          入口と出口が同じ系列)から計算する。
  ex_h  = ret_h − (同じ判定日の記録銘柄の ret_h 平均)  … 地合いの影響を除いた超過分

保存先: data/{jp,us}_stocks/ledger/ledger_YYYY-MM.csv (判定日の月ごと) と
        ledger_summary.json (flag別の件数・平均・勝率。過去分の集計)。
同じ判定日を何度実行しても行は増えない (最初の記録を正とする = その時点の値を保つ)。
ジョブが落ちて記録できなかった日は、直近 LEDGER_BACKFILL_DAYS 営業日以内なら翌回に
日足だけで補完し backfilled=1 を付ける (mf_* は空。分析時に除外できるように)。

実行: python ledger.py jp   /   python ledger.py us
      (各 history workflow の最後に実行。日足が確定した引け後に走らせる前提)
設定 (環境変数、pushイベントで空文字になるケースに備えて `or` で既定値):
  LEDGER_FLAG_SURGE (2.0) / LEDGER_NEAR_SURGE (1.5) / LEDGER_HORIZONS ("1,5,20")
  LEDGER_BACKFILL_DAYS (3) / LEDGER_DEADLINE_MIN (5)
依存なし (標準ライブラリのみ)。
"""

import csv
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FLAG_SURGE = float(os.environ.get("LEDGER_FLAG_SURGE") or "2.0")
NEAR_SURGE = float(os.environ.get("LEDGER_NEAR_SURGE") or "1.5")
HORIZONS = [int(x) for x in (os.environ.get("LEDGER_HORIZONS") or "1,5,20").split(",") if x.strip()]
BACKFILL_DAYS = int(os.environ.get("LEDGER_BACKFILL_DAYS") or "3")
DEADLINE_MIN = float(os.environ.get("LEDGER_DEADLINE_MIN") or "5")
MED_WINDOW = 20     # 平常時の売買代金 = 直前20営業日の中央値
MED_MIN = 10        # 中央値の計算に必要な最低営業日数 (未満なら surge は空)

MARKETS = {
    # 日足が確定したとみなす現地時刻 (引け後の余裕込み): JP 15:30引け→16:00 / US 16:00引け→16:30
    # mf_close_hm: money_flow の最新バー時刻がこれ以降なら「引け時点の値」とみなして保存する
    "jp": {"dir": "data/jp_stocks", "tz": timezone(timedelta(hours=9)), "ts_col": "timestamp_jst",
           "suffix": "_T", "mf_suffix": ".T", "final_hm": (16, 0), "mf_close_hm": "15:20"},
    "us": {"dir": "data/us_stocks", "tz": ZoneInfo("America/New_York"), "ts_col": "timestamp_et",
           "suffix": "", "mf_suffix": "", "final_hm": (16, 30), "mf_close_hm": "15:50"},
}

SNAP_COLS = ["date", "code", "name", "bucket", "group", "close", "turnover", "med20_turnover",
             "surge_1d", "ret_0", "gap", "flag", "mf_surge", "mf_z", "mf_share_delta",
             "universe_n", "backfilled", "recorded_at"]
FILL_COLS = ["entry_date", "entry_open"] + [f"ret_{h}" for h in HORIZONS] + [f"ex_{h}" for h in HORIZONS]
COLS = SNAP_COLS + FILL_COLS


def fprice(x):
    """価格: 指数表記にならない有効10桁 (例 2992.5 / 72.45)。"""
    return "" if x is None else f"{x:.10g}"


def fret(x):
    """騰落率・比率: 小数5桁固定 (例 0.01234)。"""
    return "" if x is None else f"{x:.5f}"


def load_universe(d):
    with open(os.path.join(d, "universe.csv"), newline="", encoding="utf-8") as f:
        return [{"code": (r.get("code") or "").strip(), "name": (r.get("name") or "").strip(),
                 "bucket": (r.get("bucket") or "").strip(), "group": (r.get("group") or "").strip()}
                for r in csv.DictReader(f) if (r.get("code") or "").strip()]


def load_daily(path, ts_col, cutoff_date):
    """日足CSV → 日付昇順の [(date, open, high, low, close, volume)]。同じ日付は後勝ち、
    cutoff_date 以降(=まだ確定していない当日分)は使わない。"""
    if not os.path.exists(path):
        return []
    by_date = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                d = (r.get(ts_col) or "")[:10]
                o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
                v = float(r.get("volume") or 0)
            except (KeyError, ValueError, TypeError):
                continue
            if not d or d >= cutoff_date or c <= 0 or o <= 0:
                continue
            by_date[d] = (d, o, h, l, c, v)
    return [by_date[k] for k in sorted(by_date)]


MF_KEEP_DAYS = 10   # 引け時点の money_flow スナップショットを保持する日数 (記録に使い終わるまで)


def update_mf_pending(d, mf_suffix, close_hm):
    """money_flow(資金集中スクリーナー)の「引け時点の値」を判定日ごとに保留ファイルへ保存し、
    {date: {code: [surge, z, share_delta]}} を返す。

    money_flow.csv は5分ごとに上書きされるため、日足が確定してから記録する頃には別の日の値に
    なっていることがある (米国は日足の当日分が翌日にしか届かないため常に1日ずれる)。
    そこで、毎回の実行時に money_flow の最新バーが引け後なら、その日付の値として保存しておき、
    台帳にその日を記録するときに参照する。場中の途中値は保存しない (引け時点の値だけを使う)。"""
    path = os.path.join(d, "ledger", "mf_pending.json")
    try:
        pending = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        pending = {}
    try:
        meta = json.load(open(os.path.join(d, "money_flow.json"), encoding="utf-8")).get("meta") or {}
    except (OSError, ValueError):
        meta = {}
    latest = meta.get("latest") or ""
    mf_date, mf_hm = latest[:10], latest[11:16]
    p = os.path.join(d, "money_flow.csv")
    if mf_date and mf_hm >= close_hm and os.path.exists(p):
        vals = {}
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                code = (r.get("code") or "").strip()
                if mf_suffix and code.endswith(mf_suffix):
                    code = code[: -len(mf_suffix)]
                vals[code] = [r.get("surge_ratio", ""), r.get("zscore", ""), r.get("share_delta_pp", "")]
        if vals:
            pending[mf_date] = vals
    for k in sorted(pending)[:-MF_KEEP_DAYS]:
        pending.pop(k, None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, separators=(",", ":"))
    return pending, (mf_date if mf_hm >= close_hm else None)


def read_ledger(d):
    """全月の台帳 → {(date, code): row(dict)} と、月ファイルごとのキー集合。"""
    out = {}
    ld = os.path.join(d, "ledger")
    if not os.path.isdir(ld):
        return out
    for fn in sorted(os.listdir(ld)):
        if fn.startswith("ledger_") and fn.endswith(".csv"):
            with open(os.path.join(ld, fn), newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    out[(r["date"], r["code"])] = {c: r.get(c, "") for c in COLS}
    return out


def write_ledger(d, rows, months):
    """指定月のファイルだけを書き直す (日付・コード順、全列)。"""
    os.makedirs(os.path.join(d, "ledger"), exist_ok=True)
    for ym in sorted(months):
        keys = sorted(k for k in rows if k[0][:7] == ym)
        path = os.path.join(d, "ledger", f"ledger_{ym}.csv")
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            for k in keys:
                w.writerow(rows[k])
        os.replace(tmp, path)


def snapshot_rows(date, uni, daily, mf_day, backfilled, now_iso):
    """判定日 date の全ユニバース銘柄の記録行 (その日に日足がある銘柄のみ)。"""
    out = []
    for u in uni:
        bars = daily.get(u["code"]) or []
        idx = next((i for i, b in enumerate(bars) if b[0] == date), None)
        if idx is None or idx == 0:
            continue
        d, o, h, l, c, v = bars[idx]
        prev_c = bars[idx - 1][4]
        turnover = c * v
        hist = [b[4] * b[5] for b in bars[max(0, idx - MED_WINDOW):idx] if b[5] > 0]
        med = statistics.median(hist) if len(hist) >= MED_MIN else None
        surge = (turnover / med) if med else None
        flag = ""
        if surge is not None:
            flag = "candidate" if surge >= FLAG_SURGE else "near" if surge >= NEAR_SURGE else ""
        mf = mf_day.get(u["code"]) or ["", "", ""]
        out.append({
            "date": date, "code": u["code"], "name": u["name"], "bucket": u["bucket"], "group": u["group"],
            "close": fprice(c), "turnover": str(round(turnover)), "med20_turnover": str(round(med)) if med else "",
            "surge_1d": f"{surge:.3f}" if surge is not None else "",
            "ret_0": fret(c / prev_c - 1), "gap": fret(o / prev_c - 1), "flag": flag,
            "mf_surge": mf[0], "mf_z": mf[1], "mf_share_delta": mf[2],
            "universe_n": str(len(uni)), "backfilled": "1" if backfilled else "0", "recorded_at": now_iso,
            **{c_: "" for c_ in FILL_COLS},
        })
    return out


def fill_returns(rows, daily):
    """未記入の ret_h を、判定日より後の確定日足だけで記入する。戻り値: 記入した値の数。"""
    n = 0
    for key, r in rows.items():
        if all(r.get(f"ret_{h}") for h in HORIZONS):
            continue
        bars = daily.get(r["code"])
        if not bars:
            continue
        idx = next((i for i, b in enumerate(bars) if b[0] == r["date"]), None)
        if idx is None or idx + 1 >= len(bars):
            continue
        entry = bars[idx + 1]
        r["entry_date"], r["entry_open"] = entry[0], fprice(entry[1])
        for h in HORIZONS:
            if r.get(f"ret_{h}"):
                continue
            j = idx + h
            if j < len(bars):
                r[f"ret_{h}"] = fret(bars[j][4] / entry[1] - 1)
                n += 1
    # 超過リターン: 同じ判定日の記入済み銘柄の平均との差 (8割以上が記入済みの日だけ確定)
    by_date = {}
    for key, r in rows.items():
        by_date.setdefault(r["date"], []).append(r)
    for date, rs in by_date.items():
        for h in HORIZONS:
            vals = [float(x[f"ret_{h}"]) for x in rs if x.get(f"ret_{h}")]
            if not vals or len(vals) < 0.8 * len(rs):
                continue
            mean = sum(vals) / len(vals)
            for x in rs:
                if x.get(f"ret_{h}") and not x.get(f"ex_{h}"):
                    x[f"ex_{h}"] = fret(float(x[f"ret_{h}"]) - mean)
    return n


def summarize(rows):
    """flag別×期間別の成績 (記入済みの行のみ。backfilled行は除外)。"""
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "flag_surge": FLAG_SURGE, "near_surge": NEAR_SURGE, "groups": {}}
    dates = sorted({r["date"] for r in rows.values()})
    out["first_date"], out["last_date"], out["n_days"] = (dates[0] if dates else None,
                                                          dates[-1] if dates else None, len(dates))
    for flag in ("candidate", "near", ""):
        g = {}
        for h in HORIZONS:
            rs = [r for r in rows.values() if r["flag"] == flag and r["backfilled"] != "1" and r.get(f"ret_{h}")]
            ex = [float(r[f"ex_{h}"]) for r in rs if r.get(f"ex_{h}")]
            ret = [float(r[f"ret_{h}"]) for r in rs]
            g[f"h{h}"] = {"n": len(ret), "n_days": len({r["date"] for r in rs}),
                          "mean_ret": round(sum(ret) / len(ret), 5) if ret else None,
                          "mean_ex": round(sum(ex) / len(ex), 5) if ex else None,
                          "win_rate_ex": round(sum(1 for x in ex if x > 0) / len(ex), 3) if ex else None}
        out["groups"][flag or "other"] = g
    return out


def main():
    mkt = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if mkt not in MARKETS:
        print("使い方: python ledger.py jp|us"); sys.exit(2)
    cfg = MARKETS[mkt]
    d, tz = cfg["dir"], cfg["tz"]
    t0 = time.time()
    now = datetime.now(tz)
    # 当日の日足は引け後(final_hm)まで未確定とみなして使わない
    hh, mm = cfg["final_hm"]
    cutoff = now.strftime("%Y-%m-%d") if (now.hour, now.minute) < (hh, mm) else \
        (now + timedelta(days=1)).strftime("%Y-%m-%d")

    uni = load_universe(d)
    rows = read_ledger(d)
    codes = {u["code"] for u in uni} | {k[1] for k in rows}
    daily = {}
    for code in codes:
        if time.time() - t0 > DEADLINE_MIN * 60:
            print("⚠️ デッドライン超過 — 日足の読み込みを打ち切り (残りは次回)")
            break
        daily[code] = load_daily(os.path.join(d, f"{code}{cfg['suffix']}_1d.csv"), cfg["ts_col"], cutoff)

    # 判定日 = ユニバース銘柄の最終確定日の最頻値 (一部の売買停止銘柄に引きずられない)
    lasts = [daily[u["code"]][-1][0] for u in uni if daily.get(u["code"])]
    if not lasts:
        print("日足データが無い。記録をスキップ。"); return
    latest = max(set(lasts), key=lasts.count)
    calendar = sorted({b[0] for u in uni for b in (daily.get(u["code"]) or []) if b[0] <= latest})
    targets = [x for x in calendar[-(BACKFILL_DAYS + 1):]]
    recorded = {k[0] for k in rows}

    pending, mf_saved = update_mf_pending(d, cfg["mf_suffix"], cfg["mf_close_hm"])
    print(f"money_flow 引け時点スナップショット: "
          + (f"{mf_saved} を保存" if mf_saved else "今回は引け後の値ではないため保存なし")
          + f" (保持中 {sorted(pending)})")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    touched, new_n = set(), 0
    for date in targets:
        if date in recorded:
            continue
        backfilled = date != latest
        snap = snapshot_rows(date, uni, daily, pending.get(date, {}), backfilled, now_iso)
        for r in snap:
            rows[(r["date"], r["code"])] = r
        if snap:
            touched.add(date[:7]); new_n += len(snap)
            nc = sum(1 for r in snap if r["flag"] == "candidate")
            nn = sum(1 for r in snap if r["flag"] == "near")
            print(f"記録: {date} {len(snap)}銘柄 (候補{nc}・惜しくも候補外{nn})"
                  + (" ※取りこぼし日の補完(backfilled)" if backfilled else "")
                  + ("" if date in pending else " ※この日の引け時点のmoney_flowが未保存のためmf_*は空"))

    before = {k: dict(v) for k, v in rows.items() if not all(v.get(f"ret_{h}") for h in HORIZONS)}
    n_fill = fill_returns(rows, daily)
    for k, old in before.items():
        if rows[k] != old:
            touched.add(k[0][:7])

    if touched:
        write_ledger(d, rows, touched)
    summary = summarize(rows)
    if rows:
        os.makedirs(os.path.join(d, "ledger"), exist_ok=True)
        with open(os.path.join(d, "ledger", "ledger_summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
    c = summary["groups"].get("candidate", {})
    print(f"完了[{mkt}]: 新規記録{new_n}行・答え合わせ記入{n_fill}値・更新した月ファイル{sorted(touched)} "
          f"(判定日{latest}, 台帳{summary['n_days']}日分, 所要{time.time() - t0:.1f}秒)")
    for h in HORIZONS:
        g = c.get(f"h{h}") or {}
        if g.get("n"):
            print(f"  候補(candidate) +{h}日: n={g['n']} 平均{g['mean_ret']:+.2%} 超過{(g['mean_ex'] or 0):+.2%} "
                  f"超過勝率{(g['win_rate_ex'] or 0):.0%}")


if __name__ == "__main__":
    main()
