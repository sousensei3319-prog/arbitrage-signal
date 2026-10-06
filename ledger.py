"""
仮想の約定記録 (ledger.py) — 日米株スクリーナーの「その日の判定」を毎日保存し、
後日の値動きで自動的に答え合わせする台帳。発注はしない (記録と集計だけ)。

なぜ必要か (2026-10-03 のML実現性検証で判明したこと):
  - 今のデータで「資金集中シグナルは儲かるか」を検証すると、ユニバース(監視銘柄)が
    「後から見て話題になった銘柄」を含むため結果が良く見える (後知恵バイアス)。
  - 判定に使った入力は後から変わる (Yahooの訂正・分割調整・ユニバース入替)。
  → 「その日・その時点で何をどう判定したか」を毎日そのまま残し、+1/+5/+20営業日の
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
  hot_added = 話題枠(hot)の銘柄が最後に追加された日 (hot_changes_log.csv)。
             判定日の当日以降に追加された話題株はその判定日の記録に入れない・集計しない
             (場中に「その日の値上がり」で選ばれた銘柄が、選ばれた理由そのものである同じ日や
              前営業日の記録に混ざるのを防ぐ。寄り前に追加された銘柄も追加日の分は入れない —
              追加の時刻は記録していないため、安全側に倒す。2026-10-06 修正、下の「事前登録」参照)

答え合わせ (後日自動記入):
  入口 = 記録した時刻 (recorded_at) より後に来る最初の寄り付き (日本 9:00 / 米国 9:30 現地)。
         ただし判定日から entry_lag 営業日 (日本1・米国2) より前にはしない。
         日本は通常「判定日の翌営業日の寄り」。米国は日足が翌日にしか届かず記録が翌日夕方に
         なるため「判定日の2営業日後の寄り」になる (記録より前の値段では実際に買えないため)。
         米国でも予定実行が遅れて当日の日足が届いた後に記録すると翌営業日の寄りになり、
         日によって入口の遅れが混ざっていたので、2026-10-06 から最低の遅れを固定した。
  ret_h = (入口の日から数えてh営業日目の終値) ÷ (入口の始値) − 1   … h = 1, 5, 20
          (手数料・スリッページ・配当は含まない。配当落ちの日は高配当株が不利に見える点に注意)
  ex_h  = ret_h − (同じ判定日の記録銘柄の ret_h の中央値)       … 地合いの影響を除いた超過分
  exb_h = ret_h − (同じ判定日・同じ区分(bucket)の ret_h の中央値) … 区分差(配当・規模)も除いた超過分
          中央値を使うのは、分割の段差など1銘柄の異常値で全銘柄の超過分が歪まないようにするため。
          ex は記入済みが8割以上になった日、または判定日から h+10 営業日たった日に確定する
          (ユニバースから外れた銘柄が多い日でも永久に未確定にならないように)
  split_date = 入口〜出口の間に株式分割のような段差 (前日比が 1/2・1/3・1/4・1/5・1/10 や
          その逆数の±4%以内) があった日。その期間の ret は記入しない (Yahooの日足は分割が
          未調整のことがある。例: 7946 は2026-03-02に 3,735円→746円)
  直近 LEDGER_RECALC_SESSIONS (30) 営業日の判定日の答え合わせは毎回計算し直す
  (Yahooが後から分割調整・訂正した値を反映する)。それより古い行は確定値として触らない。
  判定時点の列 (surge_1d・flag・mf_* など) は一度記録したら書き換えない。

保存先: data/{jp,us}_stocks/ledger/ledger_YYYY-MM.csv (判定日の月ごと) と
        ledger_summary.json (flag別の件数・平均。backfilled行・分割疑いの行は除外)。
        1行約250バイト × 日本約620行・米国約810行/日 → 月に日本約3MB・米国約4MB。
同じ判定日を何度実行しても行は増えない (最初の記録を正とする = その時点の値を保つ)。
ジョブが落ちて記録できなかった日は、直近 LEDGER_BACKFILL_DAYS 営業日以内なら翌回に
日足だけで補完し backfilled=1 を付ける (mf_* は空。集計から除外)。

評価の事前登録 (2026-10-04 固定。結果を見てから変えない):
  主要評価項目: 日本・flag=candidate・backfilled=0・分割疑いなし の ex_5。
    判定日ごとに候補銘柄の ex_5 を平均 → その日次系列の平均。標準誤差は5日保有の重なりを
    考慮した Newey-West (ラグ4)。
  評価する時点: ex_5 が確定した判定日が120日たまった時点で1回だけ (記録開始から約6か月)。
  合格: 平均 ≥ +0.5% かつ t ≥ 2.0 (120日で標準誤差は約0.22%の見込み = これより小さい効果は
    この台帳では判定できない。指数構成銘柄の過去2年の日足では同じ定義で +0.18% 程度)。
  副次 (探索扱い・結論には使わない): hot枠のみ / near / mf_* を使った判定 / ex_1・ex_20 /
    exb_* / 米国 (米国は同日の候補どうしの連動が強く、判定に必要な日数が日本の数倍〜十数倍)。
  評価日までは有意かどうかを見て判断しない (件数・欠測などの健全性の確認だけ)。
  修正 (2026-10-06。成績は見ずに、運用の点検で見つけた記録の仕方の問題だけを直した):
    - 話題枠に判定日の当日以降に追加された銘柄は記録・集計しない (旧: 判定日より後だけ)。
      米国 10/05 は、遅れた予定実行が場中に追加した25銘柄 (うち20が候補) が同じ日の記録に入っていた
    - 入口を判定日から日本1・米国2営業日より前にしない (米国で入口の遅れが混ざっていた)。
      日本は通常どおり記録していれば変わらない (主要評価項目への影響なし)

実行: python ledger.py jp   /   python ledger.py us
      (各 history workflow の最後に実行。日足が確定した引け後に走らせる前提)
      python ledger.py jp --pending-codes
      (答え合わせ待ちなのにユニバースから外れた銘柄のコードをカンマ区切りで出力する。
       workflow がこの銘柄の日足だけ取得し続ける = 外れた銘柄が集計から消える生存バイアスを防ぐ)
設定 (環境変数、pushイベントで空文字になるケースに備えて `or` で既定値):
  LEDGER_FLAG_SURGE (2.0) / LEDGER_NEAR_SURGE (1.5) / LEDGER_HORIZONS ("1,5,20")
  LEDGER_BACKFILL_DAYS (3) / LEDGER_DEADLINE_MIN (5) / LEDGER_RECALC_SESSIONS (30)
  LEDGER_NOW (試験専用: 現在時刻をISO形式で上書き。過去時点の再現テストに使う。本番では設定しない)
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
RECALC_SESSIONS = int(os.environ.get("LEDGER_RECALC_SESSIONS") or "30")
MED_WINDOW = 20     # 平常時の売買代金 = 直前20営業日の中央値
MED_MIN = 10        # 中央値の計算に必要な最低営業日数 (未満なら surge は空)
EX_FILL_SHARE = 0.8  # 同じ判定日の記録銘柄のうち、この割合以上に ret が入ったら ex を確定
EX_WAIT = 10        # または判定日から h+EX_WAIT 営業日たったら、入っている分で ex を確定
EX_MIN_ROWS = 20    # ex の基準 (中央値) に必要な最低銘柄数
EXB_MIN_ROWS = 5    # exb (区分内の中央値) に必要な最低銘柄数
PENDING_SESSIONS = 45   # この営業日数より古い判定日の未記入は追いかけない (--pending-codes)
SPLIT_RATIOS = (2, 3, 4, 5, 10)
SPLIT_TOL = 0.04

MARKETS = {
    # 日足が確定したとみなす現地時刻 (引け後の余裕込み): JP 15:30引け→16:00 / US 16:00引け→16:30
    # mf_close_hm: money_flow の最新バー時刻がこれ以降なら「引け時点の値」とみなして保存する
    # open_hm: 寄り付きの現地時刻 (入口 = 記録時刻より後の最初の寄り)
    # entry_lag: 入口を判定日から何営業日後より前にしないか (通常の記録時刻での入口に合わせて固定)
    "jp": {"dir": "data/jp_stocks", "tz": timezone(timedelta(hours=9)), "ts_col": "timestamp_jst",
           "suffix": "_T", "mf_suffix": ".T", "final_hm": (16, 0), "mf_close_hm": "15:20", "open_hm": (9, 0),
           "entry_lag": 1},
    "us": {"dir": "data/us_stocks", "tz": ZoneInfo("America/New_York"), "ts_col": "timestamp_et",
           "suffix": "", "mf_suffix": "", "final_hm": (16, 30), "mf_close_hm": "15:50", "open_hm": (9, 30),
           "entry_lag": 2},
}

SNAP_COLS = ["date", "code", "name", "bucket", "group", "hot_added", "close", "turnover", "med20_turnover",
             "surge_1d", "ret_0", "gap", "flag", "mf_surge", "mf_z", "mf_share_delta",
             "universe_n", "backfilled", "recorded_at"]
FILL_COLS = (["entry_date", "entry_open"] + [f"ret_{h}" for h in HORIZONS] + [f"ex_{h}" for h in HORIZONS]
             + [f"exb_{h}" for h in HORIZONS] + ["split_date"])
COLS = SNAP_COLS + FILL_COLS

PREREG = {
    "registered": "2026-10-04",
    "primary": "日本・flag=candidate・backfilled=0・分割疑いなし の ex_5 (同日の記録銘柄の中央値との差)。"
               "判定日ごとに平均 → 日次系列の平均。標準誤差は Newey-West (ラグ4)",
    "evaluate_at": "ex_5 が確定した判定日が120日たまった時点で1回だけ",
    "success": "平均 ≥ +0.5% かつ t ≥ 2.0",
    "secondary": "hot枠のみ / near / mf_* を使った判定 / ex_1・ex_20 / exb_* / 米国 は探索扱い (結論に使わない)",
    "no_peeking": "評価日までは有意かどうかで判断しない (件数・欠測の健全性の確認のみ)",
    "amendments": ["2026-10-06 (成績は見ずに記録の仕方だけ修正): 話題枠に判定日の当日以降に追加された銘柄は"
                   "記録・集計しない / 入口を判定日から日本1・米国2営業日より前にしない"],
}


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


def load_hot_added(d):
    """{code: 話題枠に最後に追加された日 (YYYY-MM-DD)}。ログが無ければ空。"""
    out = {}
    p = os.path.join(d, "hot_changes_log.csv")
    if not os.path.exists(p):
        return out
    with open(p, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("action") or "") == "add":
                c, dt = (r.get("code") or "").strip(), (r.get("date") or "")[:10]
                if c and dt and dt >= out.get(c, ""):
                    out[c] = dt
    return out


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
    """全月の台帳 → {(date, code): row(dict)}。古い列構成のファイルも読める (無い列は空)。"""
    out = {}
    ld = os.path.join(d, "ledger")
    if not os.path.isdir(ld):
        return out
    for fn in sorted(os.listdir(ld)):
        if fn.startswith("ledger_") and fn.endswith(".csv"):
            with open(os.path.join(ld, fn), newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    out[(r["date"], r["code"])] = {c: r.get(c) or "" for c in COLS}
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


def _late_hot(r):
    """判定日の当日以降に話題枠へ追加された銘柄の行か (記録しない・集計しない対象)。
    2026-10-06 より前の記録 (米国 10/05 など) に残っている分を集計と超過分の基準から外すのに使う。"""
    return bool(r.get("hot_added")) and r["hot_added"] >= r["date"]


def snapshot_rows(date, uni, daily, mf_day, backfilled, now_iso, hot_added=None):
    """判定日 date の全ユニバース銘柄の記録行 (その日に日足がある銘柄のみ)。
    判定日の当日以降に話題枠へ追加された銘柄は入れない (その日の動きで選ばれた可能性があり、
    追加の時刻は記録していないため、寄り前の追加も含めて追加日の分は入れない)。"""
    hot_added = hot_added or {}
    out = []
    for u in uni:
        added = hot_added.get(u["code"], "") if u["bucket"] == "hot" else ""
        if added and added >= date:
            continue
        bars = daily.get(u["code"]) or []
        idx = next((i for i in range(len(bars) - 1, -1, -1) if bars[i][0] == date), None)
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
            "hot_added": added,
            "close": fprice(c), "turnover": str(round(turnover)), "med20_turnover": str(round(med)) if med else "",
            "surge_1d": f"{surge:.3f}" if surge is not None else "",
            "ret_0": fret(c / prev_c - 1), "gap": fret(o / prev_c - 1), "flag": flag,
            "mf_surge": mf[0], "mf_z": mf[1], "mf_share_delta": mf[2],
            "universe_n": str(len(uni)), "backfilled": "1" if backfilled else "0", "recorded_at": now_iso,
            **{c_: "" for c_ in FILL_COLS},
        })
    return out


def _split_like(ratio):
    """前日比が株式分割/併合の比率 (1/2・1/3・1/4・1/5・1/10 とその逆数) の±4%以内か。"""
    if ratio <= 0:
        return False
    return any(abs(ratio * n - 1) < SPLIT_TOL or abs(ratio / n - 1) < SPLIT_TOL for n in SPLIT_RATIOS)


def entry_index(bars, idx, recorded_at, cfg):
    """判定日 bars[idx] の記録時刻より後に来る最初の寄り付きの添字 (無ければ None)。
    ただし判定日から cfg["entry_lag"] 営業日より前にはしない (記録の時刻で入口の遅れが変わらないように)。"""
    try:
        rec = datetime.fromisoformat(recorded_at) if recorded_at else None
    except ValueError:
        rec = None
    hh, mm = cfg["open_hm"]
    for j in range(idx + max(1, cfg.get("entry_lag", 1)), len(bars)):
        if rec is None:
            return j
        y, mo, dd = (int(x) for x in bars[j][0].split("-"))
        if datetime(y, mo, dd, hh, mm, tzinfo=cfg["tz"]) > rec:
            return j
    return None


def fill_returns(rows, daily, calendar, cfg):
    """答え合わせ (入口・ret・ex・exb・split_date) を記入する。戻り値: 新たに記入した ret の数。

    直近 RECALC_SESSIONS 営業日の判定日の行と、まだ記入が終わっていない行は毎回計算し直す
    (Yahooの後からの分割調整・訂正を反映)。それより古く記入済みの行は触らない。"""
    active = set(calendar[-RECALC_SESSIONS:])
    pos = {d: i for i, d in enumerate(calendar)}
    idx_of = {}   # code → {date: 日足の添字}
    recalc_dates = set()
    n_new = 0
    for key, r in rows.items():
        done = all(r.get(f"ret_{h}") or r.get("split_date") for h in HORIZONS)
        if done and r["date"] not in active:
            continue
        bars = daily.get(r["code"])
        if not bars:
            continue
        if r["code"] not in idx_of:
            idx_of[r["code"]] = {b[0]: i for i, b in enumerate(bars)}
        idx = idx_of[r["code"]].get(r["date"])
        if idx is None:
            continue
        before = {c: r.get(c, "") for c in FILL_COLS}
        for c in FILL_COLS:
            r[c] = ""
        j0 = entry_index(bars, idx, r.get("recorded_at"), cfg)
        if j0 is not None:
            entry = bars[j0]
            r["entry_date"], r["entry_open"] = entry[0], fprice(entry[1])
            for h in HORIZONS:
                j = j0 + h - 1
                if j >= len(bars):
                    continue
                # 入口〜出口の日またぎに分割のような段差があれば、その期間の ret は記入しない
                jump = next((bars[k][0] for k in range(j0 + 1, j + 1)
                             if _split_like(bars[k][1] / bars[k - 1][4]) or _split_like(bars[k][4] / bars[k - 1][4])),
                            None)
                if jump:
                    r["split_date"] = r["split_date"] or jump
                    continue
                r[f"ret_{h}"] = fret(bars[j][4] / entry[1] - 1)
                if not before.get(f"ret_{h}"):
                    n_new += 1
        recalc_dates.add(r["date"])

    # 超過リターン: 同じ判定日の記録銘柄の中央値との差 (ex) と、同じ区分の中央値との差 (exb)
    by_date = {}
    for key, r in rows.items():
        if r["date"] in recalc_dates and not _late_hot(r):
            by_date.setdefault(r["date"], []).append(r)
    last_pos = len(calendar) - 1
    for date, rs in by_date.items():
        age = last_pos - pos[date] if date in pos else 0
        for h in HORIZONS:
            vals = [float(x[f"ret_{h}"]) for x in rs if x.get(f"ret_{h}")]
            final = len(vals) >= EX_MIN_ROWS and (len(vals) >= EX_FILL_SHARE * len(rs) or age >= h + EX_WAIT)
            for x in rs:
                x[f"ex_{h}"] = x[f"exb_{h}"] = ""
            if not final:
                continue
            base = statistics.median(vals)
            by_b = {}
            for x in rs:
                if x.get(f"ret_{h}"):
                    by_b.setdefault(x["bucket"], []).append(float(x[f"ret_{h}"]))
            for x in rs:
                if not x.get(f"ret_{h}"):
                    continue
                v = float(x[f"ret_{h}"])
                x[f"ex_{h}"] = fret(v - base)
                bv = by_b.get(x["bucket"]) or []
                if len(bv) >= EXB_MIN_ROWS:
                    x[f"exb_{h}"] = fret(v - statistics.median(bv))
    return n_new


def _nw_t(series, lag=4):
    """日次系列の平均と Newey-West 標準誤差による t 値 (系列が短ければ None)。"""
    n = len(series)
    if n < 10:
        return None, None
    m = sum(series) / n
    e = [x - m for x in series]
    s = sum(x * x for x in e) / n
    for k in range(1, min(lag, n - 1) + 1):
        w = 1 - k / (lag + 1)
        s += 2 * w * sum(e[i] * e[i - k] for i in range(k, n)) / n
    se = (s / n) ** 0.5 if s > 0 else None
    return m, (m / se if se else None)


def summarize(rows, mkt):
    """flag別×期間別の成績 (ret が記入済みの行のみ。backfilled行・分割疑いの行・当日以降に話題枠へ
    追加された銘柄の行は除外) と、
    事前登録した主要評価項目の進み具合。評価日前の数字は判断に使わない。"""
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "flag_surge": FLAG_SURGE, "near_surge": NEAR_SURGE, "prereg": PREREG, "groups": {},
           "note": "入口=記録時刻より後の最初の寄り (判定日から日本1・米国2営業日より前にはしない)。"
                   "ex=同日の記録銘柄の中央値との差。判定日の当日以降に話題枠へ追加された銘柄は除外。"
                   "評価日前の数字は参考 (判断に使わない)"}
    dates = sorted({r["date"] for r in rows.values()})
    out["first_date"], out["last_date"], out["n_days"] = (dates[0] if dates else None,
                                                          dates[-1] if dates else None, len(dates))
    ok = [r for r in rows.values() if r["backfilled"] != "1" and not r.get("split_date") and not _late_hot(r)]
    for flag in ("candidate", "near", ""):
        g = {}
        for h in HORIZONS:
            rs = [r for r in ok if r["flag"] == flag and r.get(f"ret_{h}")]
            ex = [float(r[f"ex_{h}"]) for r in rs if r.get(f"ex_{h}")]
            ret = [float(r[f"ret_{h}"]) for r in rs]
            g[f"h{h}"] = {"n": len(ret), "n_days": len({r["date"] for r in rs}),
                          "mean_ret": round(sum(ret) / len(ret), 5) if ret else None,
                          "mean_ex": round(sum(ex) / len(ex), 5) if ex else None,
                          "n_ex": len(ex),
                          "win_rate_ex": round(sum(1 for x in ex if x > 0) / len(ex), 3) if ex else None}
        out["groups"][flag or "other"] = g
    if mkt == "jp" and 5 in HORIZONS:
        daymeans = {}
        for r in ok:
            if r["flag"] == "candidate" and r.get("ex_5"):
                daymeans.setdefault(r["date"], []).append(float(r["ex_5"]))
        series = [sum(v) / len(v) for _, v in sorted(daymeans.items())]
        out["primary_progress"] = {"n_days": len(series), "target_days": 120,
                                   "evaluable": len(series) >= 120}
        if len(series) >= 120:
            m, t = _nw_t(series)
            out["primary_progress"].update({"mean_ex5": round(m, 5) if m is not None else None,
                                            "t_newey_west": round(t, 2) if t is not None else None})
    return out


def pending_codes(d, uni):
    """答え合わせ待ち (ret が未記入・分割疑いなし・直近 PENDING_SESSIONS 判定日以内) なのに
    ユニバースから外れた銘柄のコード一覧。"""
    rows = read_ledger(d)
    dates = sorted({k[0] for k in rows})[-PENDING_SESSIONS:]
    recent = set(dates)
    in_uni = {u["code"] for u in uni}
    out = set()
    for (date, code), r in rows.items():
        if date in recent and code not in in_uni and not r.get("split_date") \
                and not all(r.get(f"ret_{h}") for h in HORIZONS):
            out.add(code)
    return sorted(out)


def _now_utc():
    """現在時刻 (UTC)。試験では LEDGER_NOW で過去時点を再現する。"""
    s = os.environ.get("LEDGER_NOW")
    return datetime.fromisoformat(s).astimezone(timezone.utc) if s else datetime.now(timezone.utc)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mkt = (args[0] if args else "").lower()
    if mkt not in MARKETS:
        print("使い方: python ledger.py jp|us [--pending-codes]"); sys.exit(2)
    cfg = MARKETS[mkt]
    d, tz = cfg["dir"], cfg["tz"]
    uni = load_universe(d)
    if "--pending-codes" in sys.argv:
        print(",".join(pending_codes(d, uni)))
        return

    t0 = time.time()
    now = _now_utc().astimezone(tz)
    # 当日の日足は引け後(final_hm)まで未確定とみなして使わない
    hh, mm = cfg["final_hm"]
    cutoff = now.strftime("%Y-%m-%d") if (now.hour, now.minute) < (hh, mm) else \
        (now + timedelta(days=1)).strftime("%Y-%m-%d")

    rows = read_ledger(d)
    hot_added = load_hot_added(d)
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
    now_iso = _now_utc().isoformat(timespec="seconds")
    touched, new_n = set(), 0
    for date in targets:
        if date in recorded:
            continue
        backfilled = date != latest
        snap = snapshot_rows(date, uni, daily, pending.get(date, {}), backfilled, now_iso, hot_added)
        for r in snap:
            rows[(r["date"], r["code"])] = r
        if snap:
            touched.add(date[:7]); new_n += len(snap)
            nc = sum(1 for r in snap if r["flag"] == "candidate")
            nn = sum(1 for r in snap if r["flag"] == "near")
            late = sum(1 for u in uni if u["bucket"] == "hot" and hot_added.get(u["code"], "") >= date)
            print(f"記録: {date} {len(snap)}銘柄 (候補{nc}・惜しくも候補外{nn})"
                  + (f" ※判定日の当日以降に話題枠へ追加された{late}銘柄は除外" if late else "")
                  + (" ※取りこぼし日の補完(backfilled)" if backfilled else "")
                  + ("" if date in pending else " ※この日の引け時点のmoney_flowが未保存のためmf_*は空"))

    # 答え合わせで書き換わりうる行 (直近の判定日・未記入) だけ控えておき、変わった月だけ書き直す
    # (同じ判定日の ex は全銘柄まとめて決まるので、その日の行をまとめて控える)
    track = set(calendar[-RECALC_SESSIONS:]) | {v["date"] for v in rows.values()
                                                if not all(v.get(f"ret_{h}") or v.get("split_date") for h in HORIZONS)}
    before = {k: dict(v) for k, v in rows.items() if v["date"] in track}
    n_fill = fill_returns(rows, daily, calendar, cfg)
    for k, old in before.items():
        if rows[k] != old:
            touched.add(k[0][:7])
    n_split = sum(1 for r in rows.values() if r.get("split_date"))

    if touched:
        write_ledger(d, rows, touched)
    summary = summarize(rows, mkt)
    if rows:
        os.makedirs(os.path.join(d, "ledger"), exist_ok=True)
        with open(os.path.join(d, "ledger", "ledger_summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
    c = summary["groups"].get("candidate", {})
    print(f"完了[{mkt}]: 新規記録{new_n}行・答え合わせ記入{n_fill}値・分割疑い{n_split}行・"
          f"更新した月ファイル{sorted(touched)} (判定日{latest}, 台帳{summary['n_days']}日分, 所要{time.time() - t0:.1f}秒)")
    for h in HORIZONS:
        g = c.get(f"h{h}") or {}
        if g.get("n"):
            ex = (f"超過{g['mean_ex']:+.2%} 超過勝率{g['win_rate_ex']:.0%} (n={g['n_ex']})"
                  if g.get("mean_ex") is not None else "超過は未確定 (同日の記入が揃うまで)")
            print(f"  候補(candidate) +{h}日: n={g['n']} 平均{g['mean_ret']:+.2%} {ex}  ※評価日前の参考値")
    if mkt == "jp" and summary.get("primary_progress"):
        p = summary["primary_progress"]
        print(f"  主要評価項目(事前登録) の進み具合: {p['n_days']}/{p['target_days']}判定日")


if __name__ == "__main__":
    main()
