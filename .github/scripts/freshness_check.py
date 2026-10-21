"""データの鮮度点検 (標準ライブラリのみ)。古いものがあれば exit 1 (赤 → Discord に失敗通知)。

なぜ必要か (2026-10-04 の反証で判明): GitHub の予定実行は遅延・欠落することがあり、週次の
話題枠入れ替えが「動かなかった」週は赤にもならず誰も気づけない。毎営業日動く history ジョブの
最後で、週次ジョブと台帳の結果が新しいかを点検する。

点検すること (data/{jp,us}_stocks):
  - hot_changes_latest.json の週 (話題枠の週次入れ替え) が今週か先週か。先週のままなのは月曜だけ許す
    (入れ替えは月曜の朝に動く。火曜以降も先週なら今週分が動いていない)
  - ledger/ledger_summary.json の最終判定日が10日以内か (連休を考慮した余裕込み)
  - 1分足の収集が止まっていないか (2026-10-09 追加): 収集ジョブが毎回書き出す money_flow.json の
    最新足の日付が、仮想台帳の最終判定日より前なら止まっている。日本の収集は cron-job.org から
    トークンで起動するので、トークンの期限切れや cron-job.org の停止は「処理が起動しない」だけで
    赤にならない。引け後処理の1分足の追取りが同じ日の足を埋めるので、1分足のファイルだけでは気づけない
  - 補完・一部だけの判定日が増えていないか (2026-10-09 追加): 直近 BACKFILL_WINDOW 判定日のうち、
    取りこぼして補完した日 (backfilled=1) と一部の銘柄だけで記録された日 (partial_dates) が
    BACKFILL_MAX 日を超え、しかもそのうち1日が直近 NEW_BAD_DATES 判定日にある (= いま増えた) ときに赤。
    どちらも事前登録した評価の集計から外れるので、黙って増えると評価に使える日が減る。
    引け後処理が日付をまたいで動く (GitHub の予定実行の遅れ) のが主な原因。
    台帳を始めた直後の補完 (最初の本番の判定日より前) は数えない。
    増えた日だけ赤にするのは、超えた後に毎日赤が続くと見られなくなるため (2026-10-19 の米国: 3日で超えたまま
    10/14 以降は新しい補完が無いのに、旧版は窓から抜けるまで約4週間毎日赤になるところだった)
  - 赤の理由と対処は、Discord の失敗通知に載るよう RUNNER_TEMP/failure_notes.txt にも書く
    (notify_failure.py が読む)

使い方: python .github/scripts/freshness_check.py jp|us
"""
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MARKETS = {"jp": ("data/jp_stocks", timezone(timedelta(hours=9))),
           "us": ("data/us_stocks", ZoneInfo("America/New_York"))}
LEDGER_MAX_DAYS = 10
BACKFILL_WINDOW = 20
BACKFILL_MAX = 2
NEW_BAD_DATES = 2   # この数の最新の判定日に補完・一部だけの日があれば「いま増えた」とみなす


def week_index(s):
    """'2026-W40' → 通し番号 (週の差を数えるため)。"""
    y, w = s.split("-W")
    return date.fromisocalendar(int(y), int(w), 1).toordinal() // 7


def ledger_day_flags(d):
    """{判定日: 補完した日か} を台帳の月別CSVから読む (backfilled 列だけを見る)。"""
    out = {}
    ld = os.path.join(d, "ledger")
    if not os.path.isdir(ld):
        return out
    for fn in sorted(os.listdir(ld)):
        if not (fn.startswith("ledger_") and fn.endswith(".csv")):
            continue
        with open(os.path.join(ld, fn), newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                day = r.get("date") or ""
                if day:
                    out[day] = out.get(day, False) or (r.get("backfilled") == "1")
    return out


def check_backfills(flags, partial):
    """直近 BACKFILL_WINDOW 判定日の補完・一部だけの日 (台帳を始めた直後の補完は除く) と、
    そのうち直近 NEW_BAD_DATES 判定日にあるもの (いま増えた日) を返す。"""
    days = sorted(flags)
    start = next((x for x in days if not flags[x]), None)
    if start is None:
        return [], []
    recent = [x for x in days if x >= start][-BACKFILL_WINDOW:]
    bad = [x for x in recent if flags[x] or x in partial]
    newest = set(days[-NEW_BAD_DATES:])
    return bad, [x for x in bad if x in newest]


def write_notes(lines):
    """赤の理由を Discord の失敗通知 (notify_failure.py) に渡す。書けなくても点検の結果は変えない。"""
    path = os.path.join(os.environ.get("RUNNER_TEMP") or "", "failure_notes.txt") if os.environ.get("RUNNER_TEMP") else ""
    if not path or not lines:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(f"・{x}\n" for x in lines))
    except OSError:
        pass


def main():
    mkt = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if mkt not in MARKETS:
        print("使い方: python .github/scripts/freshness_check.py jp|us"); sys.exit(2)
    d, tz = MARKETS[mkt]
    now = datetime.now(tz)
    y, w, wd = now.isocalendar()
    problems = []

    try:
        latest = json.load(open(os.path.join(d, "hot_changes_latest.json"), encoding="utf-8"))
        gap = week_index(f"{y}-W{w:02d}") - week_index(latest["week"])
        if gap >= 2 or (gap == 1 and wd >= 2):
            problems.append(f"話題枠の週次入れ替えが {latest['week']} のまま (今週は {y}-W{w:02d})。"
                            "hot-refresh が動いていない/失敗している可能性。Actions で話題枠の入れ替えを手動実行する")
        print(f"話題枠の入れ替え: 最新 {latest['week']} ({latest.get('date')})")
    except (OSError, ValueError, KeyError) as e:
        problems.append(f"hot_changes_latest.json を読めない ({type(e).__name__})")

    p = os.path.join(d, "ledger", "ledger_summary.json")
    last, partial = None, set()
    if os.path.exists(p):
        try:
            summary = json.load(open(p, encoding="utf-8"))
            last = summary.get("last_date")
            partial = set(summary.get("partial_dates") or [])
            age = (now.date() - date.fromisoformat(last)).days if last else None
            print(f"仮想台帳: 最終判定日 {last} ({age}日前)")
            if age is None or age > LEDGER_MAX_DAYS:
                problems.append(f"仮想台帳の最終判定日が {last} のまま ({LEDGER_MAX_DAYS}日超)。"
                                "引け後処理 (history) が動いているか Actions で確かめる")
        except (OSError, ValueError) as e:
            problems.append(f"ledger_summary.json を読めない ({type(e).__name__})")
    else:
        print("仮想台帳: まだ記録なし (初回)")

    # 1分足の収集が止まっていないか (money_flow.json は収集ジョブだけが書く)
    if last:
        try:
            mf_latest = (json.load(open(os.path.join(d, "money_flow.json"), encoding="utf-8"))
                         .get("meta") or {}).get("latest") or ""
            print(f"1分足の収集: 最新の足 {mf_latest or '(なし)'}")
            if mf_latest[:10] < last:
                problems.append(f"1分足の収集が {mf_latest or '(記録なし)'} で止まっている (仮想台帳の最終判定日は {last})。"
                                "収集ジョブの起動 (日本は cron-job.org の5分ごとのジョブとトークンの期限、米国は予定実行) "
                                "と Yahoo の不調を確かめる")
        except (OSError, ValueError) as e:
            problems.append(f"money_flow.json を読めない ({type(e).__name__})")

    # 補完・一部だけの判定日が増えていないか
    try:
        bad, new_bad = check_backfills(ledger_day_flags(d), partial)
        print(f"補完・一部だけの判定日 (直近{BACKFILL_WINDOW}判定日): {len(bad)}日 {bad}"
              + (f" / いま増えた日 {new_bad}" if new_bad else ""))
        if len(bad) > BACKFILL_MAX and new_bad:
            problems.append(f"直近{BACKFILL_WINDOW}判定日のうち、補完した日・一部の銘柄だけの日が {len(bad)}日に増えた "
                            f"(今回 {', '.join(new_bad)})。この日は事前登録した評価に使えない。"
                            "引け後処理が遅れて動いた可能性 (GitHub の予定実行の遅れ・引け後処理の起動の失敗)。"
                            "1回なら対応不要。続くようなら Claude に伝える")
        elif len(bad) > BACKFILL_MAX:
            print(f"::warning::補完・一部だけの判定日が {len(bad)}日 (上限 {BACKFILL_MAX})。今回は増えていないので赤にしない")
    except (OSError, ValueError) as e:
        problems.append(f"仮想台帳の月別ファイルを読めない ({type(e).__name__})")

    for x in problems:
        print(f"::error::{x}")
    write_notes(problems)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
