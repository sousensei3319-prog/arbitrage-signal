"""データの鮮度点検 (標準ライブラリのみ)。古いものがあれば exit 1 (赤 → Discord に失敗通知)。

なぜ必要か (2026-10-04 の反証で判明): GitHub の予定実行は遅延・欠落することがあり、週次の
話題枠入れ替えが「動かなかった」週は赤にもならず誰も気づけない。毎営業日動く history ジョブの
最後で、週次ジョブと台帳の結果が新しいかを点検する。

点検すること (data/{jp,us}_stocks):
  - hot_changes_latest.json の週 (話題枠の週次入れ替え) が今週か先週か。先週のままなのは月曜だけ許す
    (入れ替えは月曜の朝に動く。火曜以降も先週なら今週分が動いていない)
  - ledger/ledger_summary.json の最終判定日が10日以内か (連休を考慮した余裕込み)

使い方: python .github/scripts/freshness_check.py jp|us
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MARKETS = {"jp": ("data/jp_stocks", timezone(timedelta(hours=9))),
           "us": ("data/us_stocks", ZoneInfo("America/New_York"))}
LEDGER_MAX_DAYS = 10


def week_index(s):
    """'2026-W40' → 通し番号 (週の差を数えるため)。"""
    y, w = s.split("-W")
    return date.fromisocalendar(int(y), int(w), 1).toordinal() // 7


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
                            "hot-refresh が動いていない/失敗している可能性")
        print(f"話題枠の入れ替え: 最新 {latest['week']} ({latest.get('date')})")
    except (OSError, ValueError, KeyError) as e:
        problems.append(f"hot_changes_latest.json を読めない ({type(e).__name__})")

    p = os.path.join(d, "ledger", "ledger_summary.json")
    if os.path.exists(p):
        try:
            last = json.load(open(p, encoding="utf-8")).get("last_date")
            age = (now.date() - date.fromisoformat(last)).days if last else None
            print(f"仮想台帳: 最終判定日 {last} ({age}日前)")
            if age is None or age > LEDGER_MAX_DAYS:
                problems.append(f"仮想台帳の最終判定日が {last} のまま ({LEDGER_MAX_DAYS}日超)")
        except (OSError, ValueError) as e:
            problems.append(f"ledger_summary.json を読めない ({type(e).__name__})")
    else:
        print("仮想台帳: まだ記録なし (初回)")

    for x in problems:
        print(f"::error::{x}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
