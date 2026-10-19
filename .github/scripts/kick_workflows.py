"""引け後処理 (history)・米国の収集・週次の話題枠の入れ替えを決まった時刻に起動する (標準ライブラリのみ)。

なぜ必要か (2026-10-13〜19 の実測): GitHub の予定実行は毎回数時間遅れる。
  - 日本の引け後処理は 16:33 JST 予定が 21:38〜翌0:12 JST に動き、日付をまたいだ 10/13 は仮想台帳がその日を
    記録できなかった (後で補完されるが、補完した日は事前登録した評価から外れる)
  - 米国の収集は 10/19 (月) の最初の run が 15:24 ET (場が開いて6時間後)。米国の話題枠の入れ替えはその前の
    13:27 ET に動き、金曜の引け後の集中度で除外を判定していた。日本の話題枠も寄り前 (07:11 JST) の予定が 09:13 JST
    (場中) に動いていた
  - cron-job.org から起動する案 (SKILL.md (3b)) は、日本株の収集のジョブしか入っていない
日本株の収集 (jp-stock.yml) は cron-job.org から5分ごとに確実に起動されているので、その最後でこのスクリプトを
呼び、下の時間帯に「まだ起動されていなければ」起動する。予定実行・cron-job.org・手動で既に起動されていれば
何もしない (二重に動かさない)。予定実行はそのまま予備として残る (遅れて2回目が動いても、台帳は同じ日を
記録し直さず、1分足は落ち着いた足を上書きしないので害は無い)。

  jp-history: 平日 (JST) 16:35〜23:59 に、その日 16:00 JST 以降に作られた jp-stock-history.yml の run が無ければ起動
  us-history: 平日 (ET) 16:35〜23:59 に、その日 16:00 ET 以降に作られた us-stock-history.yml の run が無ければ起動
              (米国の日足はその日の夜に銘柄ごとに出るので、この時刻の台帳は前の営業日を記録する = 従来どおり)
  us-collect: 平日 (ET) 9:40〜16:40 に、直近 14 分以内に作られた us-stock.yml の run が無ければ起動 (約15分ごと)
  jp-hot:     月曜 (JST) 7:15〜8:45 に、その日 7:00 JST 以降に作られた hot-refresh.yml の run が無ければ起動
  us-hot:     月曜 (ET) 7:15〜9:15 に、その日 6:00 ET 以降に作られた us-hot-refresh.yml の run が無ければ起動
              (話題枠の入れ替えは「今週済みなら何もしない」ので、重なっても1回分しか動かない)
  祝日は見ない (休場日に動いても新しい足・新しい判定日が無いだけで害は無い。予定実行も同じ)

使い方: GITHUB_TOKEN / GITHUB_REPOSITORY を渡して
        python .github/scripts/kick_workflows.py                 (上の表どおりに起動)
        python .github/scripts/kick_workflows.py --dispatch pages.yml   (指定の workflow を main で1回起動)
        (workflow の permissions に actions: write が要る。起動できなくても exit 0 = 呼び出し元を赤にしない)
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

JST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
API = "https://api.github.com"

# since: その日のこの時刻 (現地) 以降に作られた run があれば起動済み / recent_min: 直近この分数以内に作られた run があれば起動済み
# days: 曜日 (月=0)。省略は平日
RULES = [
    {"name": "jp-history", "wf": "jp-stock-history.yml", "tz": JST, "start": (16, 35), "end": (23, 59), "since": (16, 0)},
    {"name": "us-history", "wf": "us-stock-history.yml", "tz": ET, "start": (16, 35), "end": (23, 59), "since": (16, 0)},
    {"name": "us-collect", "wf": "us-stock.yml", "tz": ET, "start": (9, 40), "end": (16, 40), "recent_min": 14},
    {"name": "jp-hot", "wf": "hot-refresh.yml", "tz": JST, "days": (0,), "start": (7, 15), "end": (8, 45), "since": (7, 0)},
    {"name": "us-hot", "wf": "us-hot-refresh.yml", "tz": ET, "days": (0,), "start": (7, 15), "end": (9, 15), "since": (6, 0)},
]


def in_window(rule, now_utc):
    """決まった曜日 (省略は平日) の start〜end (現地・両端を含む) なら現地時刻を、外なら None を返す。"""
    now = now_utc.astimezone(rule["tz"])
    if now.weekday() not in rule.get("days", range(5)) or not (rule["start"] <= (now.hour, now.minute) <= rule["end"]):
        return None
    return now


def started_after(runs, after_utc):
    """runs (GitHub API の workflow_runs) に after_utc 以降に作られたものがあるか。"""
    for r in runs:
        try:
            created = datetime.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if created >= after_utc:
            return True
    return False


def threshold(rule, now_local):
    """この時刻より後に作られた run があれば「起動済み」とみなす (UTC)。"""
    if "recent_min" in rule:
        return (now_local - timedelta(minutes=rule["recent_min"])).astimezone(timezone.utc)
    h, m = rule["since"]
    return now_local.replace(hour=h, minute=m, second=0, microsecond=0).astimezone(timezone.utc)


def api(method, path, token, body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "kick-workflows"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read().decode()
        return r.status, (json.loads(data) if data else None)


def dispatch(wf, token, repo):
    st, _ = api("POST", f"/repos/{repo}/actions/workflows/{wf}/dispatches", token, {"ref": "main"})
    return st


def main():
    token, repo = os.environ.get("GITHUB_TOKEN") or "", os.environ.get("GITHUB_REPOSITORY") or ""
    if not token or not repo:
        print("::warning::GITHUB_TOKEN / GITHUB_REPOSITORY が無いので起動しない"); return
    if len(sys.argv) >= 3 and sys.argv[1] == "--dispatch":
        try:
            print(f"{sys.argv[2]} を起動した (HTTP {dispatch(sys.argv[2], token, repo)})")
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"::warning::{sys.argv[2]} を起動できなかった ({type(e).__name__}: {e})")
        return
    now_utc = datetime.now(timezone.utc)
    for rule in RULES:
        now = in_window(rule, now_utc)
        if now is None:
            continue
        try:
            _, data = api("GET", f"/repos/{repo}/actions/workflows/{rule['wf']}/runs?branch=main&per_page=20", token)
            after = threshold(rule, now)
            if started_after((data or {}).get("workflow_runs") or [], after):
                print(f"{rule['name']}: 起動済み (現地 {after.astimezone(rule['tz']):%H:%M} 以降に run あり)")
                continue
            print(f"{rule['name']}: {rule['wf']} を起動した (HTTP {dispatch(rule['wf'], token, repo)}・現地 {now:%m-%d %H:%M})")
        except (urllib.error.URLError, OSError, ValueError) as e:
            # 起動できなくても収集は止めない。続けば鮮度点検 (補完の日の増加・収集の停止) が赤にする
            print(f"::warning::{rule['name']}: 起動できなかった ({type(e).__name__}: {e})")


if __name__ == "__main__":
    main()
