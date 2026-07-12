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
"""
import csv
import json
import os
import re
import socket
import ssl
import sys
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

FIELDNAMES = ["code", "name", "bucket", "sector", "group"]

# 調整パラメータ (環境変数で上書き可)
RANK_TOP = int(os.environ.get("RANK_TOP") or "30")        # 各ランキングの採用上位数
ABSENT_WEEKS = int(os.environ.get("ABSENT_WEEKS") or "4")  # 連続圏外で除外する週数
KEEP_SURGE = float(os.environ.get("KEEP_SURGE") or "1.3")  # これ以上の集中度なら残す
FETCH_DEADLINE_MIN = float(os.environ.get("FETCH_DEADLINE_MIN") or "5")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
CODE_RE = re.compile(r"^[0-9][0-9A-Za-z]{3}$")

# 証券コード直後にリンクテキスト(銘柄名)が続くYahoo JPランキングのパターン
PAIR_RE = re.compile(r"/(\d[0-9A-Za-z]{3})/?[^>]*>\s*([^<\s][^<]{0,24})")

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


def parse_ranking(html, top=RANK_TOP):
    """ランキングHTMLから (code, name) を順位順・重複排除で最大top件返す。
    ETF等はnameで除外。ネットワーク非依存なのでローカルでも単体テスト可能。"""
    out, seen = [], set()
    for code, name in PAIR_RE.findall(html):
        name = name.strip()
        if not CODE_RE.match(code) or code in seen:
            continue
        if any(x in name for x in EXCLUDE_NAME):
            continue
        seen.add(code)
        out.append((code, name))
        if len(out) >= top:
            break
    return out


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
    for label, url in RANKINGS:
        if (datetime.now(timezone.utc) - t0).total_seconds() > FETCH_DEADLINE_MIN * 60:
            print("デッドライン超過、以降のランキング取得を打ち切り")
            break
        try:
            pairs = parse_ranking(fetch(url))
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, ssl.SSLError) as e:
            print(f"  {label}: 取得失敗 {type(e).__name__} (スキップ)")
            continue
        ok_sources += 1
        for rank, (code, name) in enumerate(pairs, 1):
            t = trending.setdefault(code, {"name": name, "sources": []})
            t["sources"].append(f"{label}{rank}位")
        print(f"  {label}: {len(pairs)}銘柄取得")

    if ok_sources == 0:
        print("全ランキング取得失敗。hot枠を変更せず終了(前週維持)。")
        return

    rows = load_universe_rows()
    protected = load_protected()
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
    surviving = []
    for r in rows:
        if r["bucket"] != "hot":
            surviving.append(r)
            continue
        code = r["code"]
        st = state.setdefault(code, {"absent": 0, "last_seen": week})
        if code in trend_codes:
            st["absent"] = 0; st["last_seen"] = week
        elif code not in {a["code"] for a in added}:
            st["absent"] = st.get("absent", 0) + 1

        s = surge.get(code, 0.0)
        if code in protected:
            surviving.append(r); kept.append((code, "恒久テーマ銘柄(保護)")); continue
        if st.get("absent", 0) >= ABSENT_WEEKS and s < KEEP_SURGE:
            removed.append({"code": code, "name": r["name"],
                            "reason": f"{st['absent']}週連続でランキング圏外・直近の集中度{s:.2f}x(<{KEEP_SURGE})のため除外"})
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
    json.dump({"week": week, "date": today, "added": added, "removed": removed,
               "hot_total": len(hot_now)},
              open(HOT_LATEST, "w", encoding="utf-8"), ensure_ascii=False)

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
