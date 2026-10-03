"""話題枠(hot bucket)の週次自動入れ替え (hot_refresh.py)。

Yahoo Finance Japan の「出来高」「値上がり率」ランキング上位から、監視ユニバース外の
個別株(＝市場で今話題になっている銘柄)を検出して hot 枠に追加し、話題でなくなった
古い hot 銘柄を外す。TOPIX500(core)/日経225(leader)は絶対に外さない。追加/除外の
根拠は詳細ログに記録し、ダッシュボードにも供給する。

「話題」の操作的定義 (事実ベース):
  複数の市場ランキング(出来高・値上がり率)の上位に登場し、かつ現ユニバース外の個別株。
  SNS感情等の曖昧な指標は使わず、「実際に資金と注目が集まっている」を数字で捉える。

追加ルール:
  ランキング上位(各RANK_TOP位まで)の、universe外・普通株(ETF/投信/REIT等を除外)を hot に追加。

除外ルール (hot枠のみ・leader/core は対象外):
  - ABSENT_WEEKS(4)週連続で全ランキング圏外、かつ直近の売買代金が平常水準
    (money_flow.csv の surge_ratio < KEEP_SURGE(1.3)) の hot 銘柄を外す
  - ただし custom_groups.csv 掲載の恒久テーマ銘柄(半導体等)は保護し、絶対に外さない
  - 直近で集中(surge >= KEEP_SURGE)している銘柄は話題継続中とみなし残す

universe_refresh.py(月次)は既存 universe.csv の hot コードを温存する設計なので、
本スクリプトが universe.csv の hot 行を書き換えれば月次再構築とも自然に協調する。
新規追加銘柄の sector/group は空(=不明)のままとし、次回の月次 universe_refresh が
JPX33業種を機械付与する(週次でJPX一覧xlsを引くのは重いため)。

依存なし(標準ライブラリのみ)。実データ検証は GitHub Actions ランナー上でのみ可能
(サンドボックスからは Yahoo JP へ proxy403 で到達不可)。

2026-10-03 修正 (誤検出・取りこぼしの再発防止):
  - 銘柄の抽出を個別銘柄ページ(/quote/XXXX.T)へのリンクに限定。旧パターンはランキング
    最上部の広告/特集記事リンクを「出来高1位・値上がり率1位の銘柄」と誤認していた
    (2608・2609)。【】や先頭Newを含む名前も除外し、既にhot枠に入っている該当行は即時除外。
  - ページは取れたが銘柄を1件も解析できない場合はそのランキングを失敗扱いにする
    (成功扱いだと全hot銘柄が「圏外」と数えられ、HTML構造変更の数週後に一斉除外される)。
  - 圏外週カウントは同じ週に何度実行しても1回だけ数える (counted_week)。
  - 指数構成銘柄(index_members.csv)は話題枠から外しても leader/core 層へ戻すだけで、
    ユニバースからは消さない。
"""
import csv
import difflib
import json
import os
import re
import socket
import ssl
import sys
import unicodedata
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "jp_stocks")
UNIVERSE_FILE = os.path.join(DATA_DIR, "universe.csv")
CUSTOM_GROUPS_CSV = os.path.join(DATA_DIR, "custom_groups.csv")
HOT_STATE = os.path.join(DATA_DIR, "hot_state.json")
HOT_LOG = os.path.join(DATA_DIR, "hot_changes_log.csv")
HOT_LATEST = os.path.join(DATA_DIR, "hot_changes_latest.json")
MONEY_FLOW_CSV = os.path.join(DATA_DIR, "money_flow.csv")
# universe_refresh.py が書く指数構成銘柄一覧 (code,index_bucket)。話題枠から外す銘柄が
# 指数構成銘柄なら、ユニバースから消さずに leader/core へ戻すために使う
INDEX_MEMBERS_CSV = os.path.join(DATA_DIR, "index_members.csv")
# universe_refresh.py が書く東証上場の普通株の正式名一覧 (code,name)。広告の見分けに使う
LISTED_NAMES_CSV = os.path.join(DATA_DIR, "listed_names.csv")

FIELDNAMES = ["code", "name", "bucket", "sector", "group"]

# 調整パラメータ (環境変数で上書き可)
RANK_TOP = int(os.environ.get("RANK_TOP") or "30")        # 各ランキングの採用上位数
ABSENT_WEEKS = int(os.environ.get("ABSENT_WEEKS") or "4")  # 連続圏外で除外する週数
KEEP_SURGE = float(os.environ.get("KEEP_SURGE") or "1.3")  # これ以上の集中度なら残す
FETCH_DEADLINE_MIN = float(os.environ.get("FETCH_DEADLINE_MIN") or "5")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
CODE_RE = re.compile(r"^[0-9][0-9A-Za-z]{3}$")

# Yahoo JPの個別銘柄ページへのリンク (/quote/7203.T) 直後にリンクテキスト(銘柄名)が続くパターン。
# 【2026-10-03 修正】旧パターンは「/ + 英数4文字」で終わる任意のリンクを拾っていたため、
# ランキング最上部の広告・特集記事リンク (例: /…/2609 「【New】キオクシアや太陽誘電の売買は
# どう見極める」) を出来高1位・値上がり率1位の「銘柄」と誤認し、hot枠に追加していた
# (2026-08-10 の 2608、2026-09-21 の 2609)。個別銘柄ページ(/quote/)へのリンクだけに限定する。
PAIR_RE = re.compile(r"/quote/(\d[0-9A-Za-z]{3})(?:\.T)?/?[^>]*>\s*([^<\s][^<]{0,24})")
# 旧パターン: 除外したリンクをログに出して、判定が正しいかをランナーログで確認するためだけに使う
LEGACY_PAIR_RE = re.compile(r"/(\d[0-9A-Za-z]{3})/?[^>]*>\s*([^<\s][^<]{0,24})")
# 銘柄名として不自然なもの (広告・記事見出し)。東証の正式銘柄名に【】・読点・疑問符/感嘆符・
# 「提供」や先頭のNewは現れない。2026-10-03 のランナー実行で、銘柄ページ(/quote/5588.T)へ
# リンクする広告「SaaS過度懸念で売られた今、狙う成長株　提供:フ…」が出来高1位に入ったため追加
AD_NAME_RE = re.compile(r"[【】、？?！!]|提供|^\s*new\b", re.IGNORECASE)

RANKINGS = [
    ("出来高", "https://finance.yahoo.co.jp/stocks/ranking/volume?market=all&term=daily"),
    ("値上がり率", "https://finance.yahoo.co.jp/stocks/ranking/up?market=all&term=daily"),
]

# ETF/ETN/REIT/指数連動商品を名前で除外 (話題「個別株」だけを拾う)
EXCLUDE_NAME = ("ＥＴＦ", "ETF", "ＥＴＮ", "ETN", "投信", "上場", "ベア", "ブル",
                "レバレッジ", "インバース", "日経平均", "ＴＯＰＩＸ", "リート",
                "ＲＥＩＴ", "REIT", "指数", "連動")


def _now_week():
    d = datetime.now(JST)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}", d.strftime("%Y-%m-%d")


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or "utf-8"
        return raw.decode(enc, errors="replace")


def is_ad_name(name):
    return bool(AD_NAME_RE.search(name or ""))


def _norm_name(s):
    """比較用の正規化: 全角→半角(NFKC)・(株)/株式会社・空白・中黒を除去・小文字化。"""
    s = unicodedata.normalize("NFKC", s or "")
    for t in ("(株)", "株式会社", "(有)", "(同)"):
        s = s.replace(t, "")
    return re.sub(r"[\s・･.,]", "", s).lower()


def names_match(scraped, official):
    """ランキング上の名前が東証の正式名と同じ会社を指しているか (表記ゆれ・途中切れは許容)。
    広告文は銘柄ページへリンクしていても正式名とは似ても似つかないので弾ける。"""
    a, b = _norm_name(scraped), _norm_name(official)
    if not a or not b:
        return True   # 判定材料が無いときは通す (ad表記チェックは別途かかる)
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.5


def load_listed_names():
    """{code: 正式名}。ファイルが無い(月次更新が未実行)場合は空 = 名前照合はスキップ。"""
    out = {}
    if os.path.exists(LISTED_NAMES_CSV):
        with open(LISTED_NAMES_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                c = (r.get("code") or "").strip()
                if c:
                    out[c] = (r.get("name") or "").strip()
    return out


def parse_ranking(html, top=RANK_TOP, listed=None, rejected=None):
    """ランキングHTMLから (code, name) を順位順・重複排除で最大top件返す。
    個別銘柄ページ(/quote/)へのリンクのみ採用し、ETF等・広告見出し風の名前・東証の正式名と
    食い違う名前 (listed={code: 正式名} が与えられた時) は除外する。名前は正式名に置き換える。
    除外した (code, name, 理由) は rejected リストへ追記。ネットワーク非依存で単体テスト可能。"""
    listed = listed or {}
    out, seen = [], set()
    for code, name in PAIR_RE.findall(html):
        name = name.strip()
        if not CODE_RE.match(code) or code in seen:
            continue
        if any(x in name for x in EXCLUDE_NAME):
            continue
        reason = None
        if is_ad_name(name):
            reason = "広告/記事見出し風の名前"
        elif code in listed and not names_match(name, listed[code]):
            reason = f"東証の正式名「{listed[code]}」と一致しない (広告の可能性)"
        if reason:
            if rejected is not None and all(r[0] != code for r in rejected):
                rejected.append((code, name, reason))
            continue
        seen.add(code)
        out.append((code, listed.get(code) or name))
        if len(out) >= top:
            break
    return out


def rejected_links(html):
    """旧パターンなら銘柄として拾っていたが、今回は除外したリンク (code, name) の先頭10件。
    = 個別銘柄ページ(/quote/)以外へのリンク、または広告見出し風の名前。ランナーログ確認用。"""
    quote_ok = {c for c, n in PAIR_RE.findall(html) if not is_ad_name(n.strip())}
    out, seen = [], set()
    for code, name in LEGACY_PAIR_RE.findall(html):
        if code in quote_ok or code in seen or not CODE_RE.match(code):
            continue
        seen.add(code)
        out.append((code, name.strip()))
    return out[:10]


def load_universe_rows():
    rows = []
    with open(UNIVERSE_FILE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({k: (r.get(k) or "") for k in FIELDNAMES})
    return rows


def load_protected():
    """恒久テーマ銘柄(custom_groups.csv 掲載 = 半導体等)は自動除外しない。"""
    protected = set()
    if os.path.exists(CUSTOM_GROUPS_CSV):
        with open(CUSTOM_GROUPS_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                c = (r.get("code") or "").strip()
                if c:
                    protected.add(c)
    return protected


def load_index_members():
    """{code: "leader"/"core"}。ファイルが無い(月次更新が未実行)場合は空 = 従来どおり削除。"""
    out = {}
    if os.path.exists(INDEX_MEMBERS_CSV):
        with open(INDEX_MEMBERS_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                c, b = (r.get("code") or "").strip(), (r.get("index_bucket") or "").strip()
                if c and b in ("leader", "core"):
                    out[c] = b
    return out


def load_surge():
    """money_flow.csv から {code: surge_ratio} を読む(除外判定の集中度チェック用)。"""
    surge = {}
    if os.path.exists(MONEY_FLOW_CSV):
        with open(MONEY_FLOW_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                c = (r.get("code") or "").split(".")[0]
                try:
                    surge[c] = float(r.get("surge_ratio") or 0)
                except ValueError:
                    pass
    return surge


def load_state():
    if os.path.exists(HOT_STATE):
        try:
            return json.load(open(HOT_STATE, encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def main():
    t0 = datetime.now(timezone.utc)
    socket.setdefaulttimeout(35)
    week, today = _now_week()

    # 1) ランキング取得 (取れたものだけ使う。全滅ならhot枠を維持して終了)
    trending = {}  # code -> {"name","sources":[...]}
    ok_sources = 0
    listed = load_listed_names()
    print(f"東証の正式名一覧: {len(listed)}銘柄" + ("" if listed else " (未生成のため名前照合はスキップ)"))
    for label, url in RANKINGS:
        if (datetime.now(timezone.utc) - t0).total_seconds() > FETCH_DEADLINE_MIN * 60:
            print("デッドライン超過、以降のランキング取得を打ち切り")
            break
        try:
            html = fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, ssl.SSLError) as e:
            print(f"  {label}: 取得失敗 {type(e).__name__} (スキップ)")
            continue
        rejected = []
        pairs = parse_ranking(html, listed=listed, rejected=rejected)
        for c, n, why in rejected:
            print(f"  {label}: 除外 {c} 「{n}」 — {why}")
        rej = rejected_links(html)
        if rej:
            print(f"  {label}: 銘柄ページ以外へのリンクとして除外(先頭{len(rej)}件): "
                  + " / ".join(f"{c} {n}" for c, n in rej))
        if not pairs:
            # ページは取れたが銘柄を1件も解析できない = HTML構造の変更。成功扱いにすると
            # 全hot銘柄が「圏外」と数えられ、数週後に一斉除外されてしまうため失敗扱いにする。
            print(f"  {label}: ⚠️ 銘柄を1件も解析できなかった (HTML構造変更の可能性) — このランキングは失敗扱い")
            continue
        ok_sources += 1
        for rank, (code, name) in enumerate(pairs, 1):
            t = trending.setdefault(code, {"name": name, "sources": []})
            t["sources"].append(f"{label}{rank}位")
        print(f"  {label}: {len(pairs)}銘柄取得 (上位: "
              + "・".join(f"{c} {n}" for c, n in pairs[:3]) + ")")

    if ok_sources == 0:
        print("全ランキング取得失敗。hot枠を変更せず終了(前週維持)。")
        return

    rows = load_universe_rows()
    protected = load_protected()
    index_members = load_index_members()
    surge = load_surge()
    state = load_state()

    existing_codes = {r["code"] for r in rows}
    hot_rows = [r for r in rows if r["bucket"] == "hot"]
    hot_codes = {r["code"] for r in hot_rows}
    # leader/core のコードは絶対に触らない
    non_hot_codes = existing_codes - hot_codes

    added, removed, kept = [], [], []

    # 2) 追加: ランキング上位の universe外・普通株を hot に (上限なし)
    for code, info in trending.items():
        if code in existing_codes:
            # 既存hotが再登場 → 圏外カウントをリセット
            if code in hot_codes:
                state.setdefault(code, {})["absent"] = 0
                state[code]["last_seen"] = week
            continue
        # 新規追加
        rows.append({"code": code, "name": info["name"], "bucket": "hot",
                     "sector": "", "group": ""})  # sector/groupは次回月次でJPX付与
        existing_codes.add(code); hot_codes.add(code)
        state[code] = {"absent": 0, "last_seen": week, "added": today,
                       "source": "・".join(info["sources"][:3])}
        added.append({"code": code, "name": info["name"],
                      "reason": f"{'・'.join(info['sources'][:3])} にランクイン(ユニバース外の話題株として新規採用)"})

    # 3) 圏外カウント更新 & 除外判定 (hot枠のみ・保護銘柄と集中中は残す)
    trend_codes = set(trending)
    added_codes = {a["code"] for a in added}
    surviving = []
    for r in rows:
        if r["bucket"] != "hot":
            surviving.append(r)
            continue
        code = r["code"]
        # 過去に広告・記事リンクを誤検出して入った行 (例: 2609「【New】…」) は即時除外
        if is_ad_name(r["name"]):
            removed.append({"code": code, "name": r["name"],
                            "reason": "ランキング上の広告/記事リンクを銘柄と誤認して追加された行のため除外"})
            state.pop(code, None)
            continue
        st = state.setdefault(code, {"absent": 0, "last_seen": week})
        if code in trend_codes:
            st["absent"] = 0; st["last_seen"] = week
        elif code not in added_codes and st.get("counted_week") != week:
            # 同じ週に再実行(手動dispatch・push失敗後の再実行等)しても二重に数えない
            st["absent"] = st.get("absent", 0) + 1
            st["counted_week"] = week

        s = surge.get(code, 0.0)
        if code in protected:
            surviving.append(r); kept.append((code, "恒久テーマ銘柄(保護)")); continue
        if st.get("absent", 0) >= ABSENT_WEEKS and s < KEEP_SURGE:
            reason = f"{st['absent']}週連続でランキング圏外・直近の集中度{s:.2f}x(<{KEEP_SURGE})のため除外"
            if code in index_members:
                # 指数構成銘柄はユニバースから消さず、本来の層(leader/core)へ戻して監視を継続
                r["bucket"] = index_members[code]
                surviving.append(r)
                reason += f" (指数構成銘柄のため {index_members[code]} 層として監視は継続)"
            removed.append({"code": code, "name": r["name"], "reason": reason})
            state.pop(code, None)
        else:
            surviving.append(r)
            if st.get("absent", 0) > 0:
                kept.append((code, f"圏外{st['absent']}週目だが集中度{s:.2f}x等で継続"))

    # 4) 書き出し (universe.csv は leader>core>hot 並びを維持)
    order = {"leader": 0, "core": 1, "hot": 2}
    surviving.sort(key=lambda r: (order.get(r["bucket"], 9), r["code"]))
    with open(UNIVERSE_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader(); w.writerows(surviving)

    json.dump(state, open(HOT_STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=0)

    # 変更ログ(追記) + 最新サマリー(ダッシュボード用)
    new_log = not os.path.exists(HOT_LOG)
    with open(HOT_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_log:
            w.writerow(["date", "week", "action", "code", "name", "reason"])
        for a in added:
            w.writerow([today, week, "add", a["code"], a["name"], a["reason"]])
        for rm in removed:
            w.writerow([today, week, "remove", rm["code"], rm["name"], rm["reason"]])

    hot_now = [r for r in surviving if r["bucket"] == "hot"]
    # 同じ週の再実行 (手動dispatch等) で「今週の入れ替え」欄が空で上書きされないよう、
    # 同週の既存サマリーがあれば追加/除外を合算する
    # (同じ週に「追加→除外」された銘柄は差し引きゼロなので両方から外す。新規銘柄の初期取得も行わない)
    latest_added, latest_removed = added, removed
    try:
        prev = json.load(open(HOT_LATEST, encoding="utf-8"))
        if prev.get("week") == week:
            a_codes, r_codes = {a["code"] for a in added}, {x["code"] for x in removed}
            prev_a = [a for a in prev.get("added", []) if a.get("code") not in a_codes]
            prev_r = [x for x in prev.get("removed", []) if x.get("code") not in r_codes]
            undone = {a.get("code") for a in prev_a} & r_codes      # 今週追加→今回除外
            redone = {x.get("code") for x in prev_r} & a_codes      # 今週除外→今回再追加
            latest_added = [a for a in prev_a + added if a.get("code") not in undone | redone]
            latest_removed = [x for x in prev_r + removed if x.get("code") not in undone | redone]
    except (OSError, ValueError):
        pass
    json.dump({"week": week, "date": today, "added": latest_added, "removed": latest_removed,
               "hot_total": len(hot_now)},
              open(HOT_LATEST, "w", encoding="utf-8"), ensure_ascii=False)
    # 今回の実行で追加した銘柄だけを workflow に渡す (初期履歴の取得とコミット対象用)。
    # 週の合算(latest)を使うと、同じ週の再実行で既に収集中の銘柄を取り直し、収集ジョブと衝突する
    out = os.environ.get("HOT_ADDED_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(",".join(a["code"] for a in added))

    print(f"\n=== 話題枠 週次入れ替え {week} ({today}) ===")
    print(f"取得ランキング: {ok_sources}/{len(RANKINGS)} / hot枠: {len(hot_now)}銘柄")
    print(f"追加 {len(added)}件:")
    for a in added:
        print(f"  + {a['code']} {a['name']} — {a['reason']}")
    print(f"除外 {len(removed)}件:")
    for rm in removed:
        print(f"  - {rm['code']} {rm['name']} — {rm['reason']}")


if __name__ == "__main__":
    main()
