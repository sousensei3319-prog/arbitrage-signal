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
  - 指数の銘柄 (leader/core) の1分足が銘柄ごとに止まっていないか (2026-10-27 追加): 1分足の最後の日から
    最新の営業日までに STALE_1M_DAYS 営業日より多くたった銘柄を、日足が続いているか・止まっているかと一緒に
    ::warning:: で出す。STALE_MAX 銘柄までは警告だけ、超えて、しかも直近 NEW_STALE_DAYS 営業日に止まった銘柄がある
    (= いま増えた) ときに赤。2026-10 に 9508 九州電力・7240 ＮＯＫ (日本)・PSKY・WBD (米国) の1分足が3〜7週間
    止まっていたのに、取得は「取得0本」で成功扱い・money_flow の最新足は他の銘柄で新しいままなので、上の点検では
    気づけなかった。原因は上場廃止: 9508・7240 は 2026-09-29 に持株会社の完全子会社になり (株式移転)、後継の会社は
    新しいコードで 10-01 に上場した。Yahoo は廃止された銘柄の1分足を返さず、9508 は後継の日足を古いコードに
    つなぐので日足だけ続いて見えた。日本は止まっている銘柄があれば JPX の上場廃止銘柄一覧と新規上場の一覧を引いて、
    廃止日・理由・後継のコードを説明に載せる (JPX に届かなくても点検の結果は変えない)。
    営業日は指数の銘柄の日足の半数以上に出てくる日で数える (休場日の一覧は持たない)
  - 赤の理由と対処は、Discord の失敗通知に載るよう RUNNER_TEMP/failure_notes.txt にも書く
    (notify_failure.py が読む)

使い方: python .github/scripts/freshness_check.py jp|us
"""
import bisect
import csv
import json
import os
import re
import socket
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

socket.setdefaulttimeout(35)   # JPX の応答待ちで止まらないように (urlopen の timeout が効かない経路の保険)

MARKETS = {"jp": ("data/jp_stocks", timezone(timedelta(hours=9))),
           "us": ("data/us_stocks", ZoneInfo("America/New_York"))}
LEDGER_MAX_DAYS = 10
BACKFILL_WINDOW = 20
BACKFILL_MAX = 2
NEW_BAD_DATES = 2   # この数の最新の判定日に補完・一部だけの日があれば「いま増えた」とみなす
# 1分足の最後の日が最新の営業日よりこの営業日数より前なら「止まっている」(3営業日続けて1分足が無い)。
# 1日の欠けは売買停止・Yahoo の一時的な不調でもあり、翌日の引け後処理の5日分の取り直しで埋まる。
# 2 なら気づいた時点でまだ2営業日は取り直しの範囲に残る (収集側の不具合ならデータを失う前に直せる)
STALE_1M_DAYS = 2
# 止まっている指数の銘柄がこの数までは ::warning:: だけ。上場廃止 (買収・持株会社化) の銘柄は次の月次の銘柄リスト
# 更新まで残り、Yahoo 側の銘柄の不具合はこちらで直せないので、1〜2銘柄で毎日赤にすると赤が見られなくなる
STALE_MAX = 3
NEW_STALE_DAYS = 2   # この営業日数の内に「止まっている」になった銘柄を「いま増えた」とみなす
# 月次の銘柄リスト更新 (上場廃止の銘柄が外れる時): 日本は JPX の東証上場銘柄一覧、米国は Wikipedia の構成銘柄表
REFRESH_NOTE = {"jp": "次の月次の銘柄リスト更新 (毎月4日の朝・JPX の東証上場銘柄一覧から作り直す)",
                "us": "次の月次の銘柄リスト更新 (毎月5日の朝・Wikipedia の S&P500/Nasdaq-100 の表から作り直す)"}
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
JPX_DELISTED_URL = "https://www.jpx.co.jp/listing/stocks/delisted/index.html"
JPX_NEW_URL = "https://www.jpx.co.jp/listing/stocks/new/index.html"
CODE_RE = re.compile(r"[0-9][0-9A-Z]{3}")    # 東証のコード (285A のような英字入りも)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


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


def last_row_date(path):
    """CSV の最後の行の日付 (先頭の列の先頭10文字)。ファイルが無い・見出ししか無いなら None。
    1分足のファイルは大きいので末尾だけ読む (収集は時刻順に書き直すので最後の行が最後の足)"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        v = ln.split(",", 1)[0][:10]
        if DATE_RE.match(v):
            return v
    return None


def daily_dates(path):
    """日足の (日付の集合, 出来高のあった最後の日)。上場廃止の前は出来高0・値段そのままの足が数日付くことがある"""
    dates, last_traded = set(), None
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) < 8 or not DATE_RE.match(row[0][:10]):
                    continue
                dates.add(row[0][:10])
                try:
                    if float(row[7] or 0) > 0 and (last_traded is None or row[0][:10] > last_traded):
                        last_traded = row[0][:10]
                except ValueError:
                    pass
    except OSError:
        pass
    return dates, last_traded


def check_1m_stale(d, mkt):
    """指数の銘柄 (leader/core) のうち、1分足の最後の日から最新の営業日までに STALE_1M_DAYS 営業日より多くたった銘柄を返す。
    戻り値 (最新の営業日, [{code, name, last_1m, lag_1m, last_1d, lag_1d, last_traded, listed}, ...])。
    営業日 = 指数の銘柄の日足の半数以上に出てくる日。lag = その日より後の営業日の数 (日付が無ければ全営業日の数)"""
    suffix = "_T" if mkt == "jp" else ""
    with open(os.path.join(d, "universe.csv"), newline="", encoding="utf-8") as f:
        members = [r for r in csv.DictReader(f) if (r.get("bucket") or "") in ("leader", "core")]
    listed = None   # 日本だけ: 前回の月次更新の時点で JPX の一覧に載っていた普通株
    p = os.path.join(d, "listed_names.csv")
    if mkt == "jp" and os.path.exists(p):
        with open(p, newline="", encoding="utf-8") as f:
            listed = {(r.get("code") or "").strip() for r in csv.DictReader(f)}
    per, seen = [], Counter()
    for r in members:
        code = (r.get("code") or "").strip()
        dates, last_traded = daily_dates(os.path.join(d, f"{code}{suffix}_1d.csv"))
        seen.update(dates)
        per.append((code, (r.get("name") or "").strip(), max(dates) if dates else None, last_traded,
                    last_row_date(os.path.join(d, f"{code}{suffix}_1m.csv"))))
    cal = sorted(x for x, n in seen.items() if n * 2 >= len(members))
    if not cal:
        raise ValueError("指数の銘柄の日足から営業日を作れない")

    def lag(x):
        return len(cal) - bisect.bisect_right(cal, x) if x else len(cal)

    out = []
    for code, name, last_1d, last_traded, last_1m in per:
        if lag(last_1m) > STALE_1M_DAYS:
            out.append({"code": code, "name": name, "last_1m": last_1m, "lag_1m": lag(last_1m),
                        "last_1d": last_1d, "lag_1d": lag(last_1d), "last_traded": last_traded,
                        "listed": None if listed is None else code in listed})
    return cal[-1], out


def html_rows(url):
    """JPX の一覧ページの表を、行ごとのセルの文字のリストで返す (標準ライブラリだけで読む・取れなければ例外)"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    rows = []
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", html):
        cells = [re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>|&nbsp;|&#160;", " ", c)).strip()
                 for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", tr)]
        if any(cells):
            rows.append(cells)
    return rows


def _company_key(s):
    """会社名を突き合わせる形に (全角・半角をそろえ、(株)・株式会社・空白を除く)"""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\(株\)|株式会社|\s|\*", "", s)


def jpx_delistings(codes):
    """止まっている日本の銘柄を JPX の上場廃止銘柄一覧で引き、完全子会社化なら新規上場の一覧で後継の会社も引く。
    戻り値 {code: {"date": "2026/09/29", "reason": "…の完全子会社化（株式移転）", "successor": "NOK Group（株） 641A・2026/10/01 上場"}}。
    一覧を読めない時は None (点検の結果は変えず、説明が「可能性」のままになるだけ)。一覧に無い銘柄は戻り値に入らない"""
    try:
        rows = html_rows(JPX_DELISTED_URL)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"JPX の上場廃止銘柄一覧を読めない ({type(e).__name__}) — 止まっている銘柄の説明は「可能性」のまま")
        return None
    out = {}
    for cells in rows:
        code = next((c for c in cells if c in codes), None)
        day = next((c for c in cells if re.fullmatch(r"\d{4}/\d{2}/\d{2}", c)), None)
        if code and day and code not in out:
            out[code] = {"date": day, "reason": cells[-1], "successor": ""}
    parents = {c: v["reason"].split("の完全子会社化")[0] for c, v in out.items() if "の完全子会社化" in v["reason"]}
    if parents:
        try:
            listings = html_rows(JPX_NEW_URL)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(f"JPX の新規上場の一覧を読めない ({type(e).__name__}) — 後継の会社のコードは載せない")
            listings = []
        for c, parent in parents.items():
            key = _company_key(parent)
            for cells in listings:
                name = next((x for x in cells if key and _company_key(x) == key), None)
                new_code = next((x for x in cells if CODE_RE.fullmatch(x)), None)
                if name and new_code:
                    day = re.search(r"\d{4}/\d{2}/\d{2}", " ".join(cells))
                    out[c]["successor"] = f"{name} {new_code}" + (f"・{day.group(0)} 上場" if day else "")
                    break
    return out


def describe_stale(x, mkt, jpx, today):
    """止まっている銘柄1件の説明 (ログの ::warning:: と Discord の通知に使う)。
    jpx: jpx_delistings() の戻り値 (日本で引けた時だけ。None なら JPX の情報なし)"""
    s = f"{x['code']} {x['name']}: 1分足が {x['last_1m'] or '(1本も無い)'} で止まっている ({x['lag_1m']}営業日)。"
    daily_on = x["lag_1d"] <= STALE_1M_DAYS
    if daily_on:
        s += f"日足は {x['last_1d']} まで続いている。"
    else:
        s += f"日足も {x['last_1d'] or '(無い)'} で止まっている"
        s += (f" (出来高のあった最後の日は {x['last_traded']})。" if x["last_traded"] and x["last_traded"] != x["last_1d"]
              else "。")
    info = (jpx or {}).get(x["code"]) if mkt == "jp" and jpx is not None else None
    if info:
        when = info["date"].replace("/", "-")
        if when > today:
            return s + (f"JPX の上場廃止銘柄一覧: {info['date']} に上場廃止の予定 ({info['reason']})。"
                        f"上場廃止の後の{REFRESH_NOTE[mkt]}で自動で外れる (データのファイルは残る)。対応は不要")
        s += f"JPX の上場廃止銘柄一覧: {info['date']} に上場廃止 ({info['reason']})"
        s += f"。後継は {info['successor']}。" if info["successor"] else "。"
        if daily_on and x["last_1d"] and x["last_1d"] > when:
            s += "上場廃止の後も日足が続くのは、Yahoo が後継の会社の日足を古いコードにつないでいるため (1分足は付かない) とみられる。"
        return s + (f"上場廃止の銘柄は{REFRESH_NOTE[mkt]}で、JPX の一覧から消えていれば自動で外れる (データのファイルは残る)。"
                    "後継の会社は JPX の一覧 (月末の時点・翌月の初めの数営業日に更新) に載った後の月次更新で入る。それまで対応は不要")
    if mkt == "jp" and jpx is not None:
        s += "JPX の上場廃止銘柄一覧には無い (上場は続いている)。"
        if daily_on:
            return s + ("売買は続いているのに Yahoo が1分足を返さない (取得0本) = Yahoo 側の銘柄の不具合で、こちらでは直せない。"
                        "この銘柄はダッシュボードの分〜時間の窓と🚨の判定に入らない。何週間も続くなら Claude に伝える")
        return s + "売買停止・Yahoo 側の不具合の可能性。何週間も続くなら Claude に伝える"
    if mkt == "jp" and x["listed"] is not None:
        s += ("前回の月次更新の時点の JPX の一覧には" + ("載っていた" if x["listed"] else "無かった (上場廃止済み)")
              + " (JPX の上場廃止銘柄一覧は今回読めなかった)。")
    if daily_on:
        return s + ("売買は続いているように見えるのに Yahoo が1分足を返さない (取得0本)。上場廃止・合併の後に Yahoo が後継の会社の"
                    "日足を古いコードにつないでいる (2026-10 の 9508 九州電力) か、Yahoo 側の銘柄の不具合。"
                    "この銘柄はダッシュボードの分〜時間の窓と🚨の判定に入らない。上場廃止なら"
                    f"{REFRESH_NOTE[mkt]}で自動で外れる")
    return s + (f"上場廃止・買収・売買停止の可能性。上場廃止なら{REFRESH_NOTE[mkt]}で自動で外れる (データのファイルは残る)。"
                "それまで対応は不要")


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
    problems, details = [], []   # details: 赤の理由の後ろに付ける銘柄ごとの説明 (Discord の通知に載る)

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

    # 指数の銘柄の1分足が銘柄ごとに止まっていないか (取得は「取得0本」で成功扱いになり、上の点検では見えない)
    try:
        ref, stale = check_1m_stale(d, mkt)
        new = [x for x in stale if x["lag_1m"] <= STALE_1M_DAYS + NEW_STALE_DAYS]
        print(f"1分足が止まっている指数の銘柄 (最新の営業日 {ref}・{STALE_1M_DAYS}営業日より多く前で止まった銘柄): {len(stale)}銘柄"
              + (f" / いま増えた {len(new)}銘柄" if new else ""))
        jpx = jpx_delistings({x["code"] for x in stale}) if stale and mkt == "jp" else None
        today = now.date().isoformat()
        for x in stale:
            print(f"::warning::{describe_stale(x, mkt, jpx, today)}")
        if len(stale) > STALE_MAX and new:
            problems.append(f"1分足が止まっている指数の銘柄が {len(stale)}銘柄に増えた (今回 {len(new)}銘柄: "
                            + "、".join(f"{x['code']} {x['name']}" for x in new[:8]) + (" ほか" if len(new) > 8 else "")
                            + ")。何銘柄も同時なら Yahoo の不調・収集の打ち切り (締め切り) を疑い、Actions の収集のログで"
                            "「取得0本」「デッドライン」を確かめる。1銘柄ずつの事情はログの警告 (::warning::) に出ている")
            details += [describe_stale(x, mkt, jpx, today) for x in new[:3]]
        elif len(stale) > STALE_MAX:
            print(f"::warning::1分足が止まっている指数の銘柄が {len(stale)}銘柄 (上限 {STALE_MAX})。今回は増えていないので赤にしない")
    except (OSError, ValueError) as e:
        problems.append(f"1分足の点検に使う銘柄リスト・日足を読めない ({type(e).__name__})")

    for x in problems:
        print(f"::error::{x}")
    write_notes(problems + details if problems else [])
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
