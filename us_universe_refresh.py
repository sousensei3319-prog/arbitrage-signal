"""
米国株 監視ユニバース自動更新 (us_universe_refresh.py)

data/us_stocks/universe.csv (code,name,bucket,sector,group) を、S&P500 + Nasdaq-100 +
話題枠 (既存hotシード) から機械的に再構築する。手打ちでの銘柄追加は行わない設計。
日本株版 (universe_refresh.py) の忠実な移植 — 構造・優先順位・フォールバック方針は同じ。

データソース (Wikipedia、標準ライブラリのみ・html.parserでテーブル抽出):
  1. S&P500 = "List of S&P 500 companies"
     https://en.wikipedia.org/wiki/List_of_S%26P_500_companies
     constituentsテーブル (列: Symbol, Security, GICS Sector, GICS Sub-Industry)
  2. Nasdaq-100 = "Nasdaq-100"
     https://en.wikipedia.org/wiki/Nasdaq-100
     componentsテーブル (列: Ticker, Company, GICS Sector, GICS Sub-Industry)
  3. 話題枠(hot) = 既存 universe.csv の bucket=hot 行 (無条件温存)。

bucket割当の優先順位: hot(既存温存) > leader(Nasdaq-100) > core(S&P500の残り)。
既存銘柄の手書きname/sectorは上書きせず温存。新規銘柄のname/sectorはWikipediaの
値で機械的に埋める。

group列 (業種グループ集計用): GICS Sector を日本語11区分に固定マップして格納
(未知の英語名はそのまま格納・フォールバック)。sector列はGICS Sub-Industry (英語の
まま、詳細タグとして)。

custom_groups.csv (data/us_stocks/custom_groups.csv, code/custom_group/basis) も
本スクリプトが機械生成する。GICS Sub-Industryに"Semiconductor"を含む銘柄を「半導体」
として書き出す。JP版のcustom_groups.csvは手動キュレーションだが、こちらは完全に
機械管理 — 実行のたびに最新のSub-Industryから全件作り直す (手で追記しても次回実行で
上書きされる)。

全候補ソースが機械取得不可な場合は、取得できた範囲で構築し不足分をログに正直に出す
(例: S&P500取得失敗→leader+hotのみで構築を続行し、その旨を明示)。

このサンドボックス(Claude Code)からはWikipediaへproxy403で到達不可なため、
実URL・実テーブル構造の検証はGitHub Actionsランナー上でのみ可能 (JP版と同じ制約)。

設定 (環境変数、pushイベントでinputsが空文字になるケースに備えて `or` で既定値):
  UNIVERSE_FILE        既存/出力先ユニバースCSV (既定 data/us_stocks/universe.csv)
  FETCH_DEADLINE_MIN   全体デッドライン分・ハング防止 (既定 5)

実行: python us_universe_refresh.py
"""

import csv
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

socket.setdefaulttimeout(35)

UNIVERSE_FILE = os.environ.get("UNIVERSE_FILE") or "data/us_stocks/universe.csv"
DEADLINE_MIN = float(os.environ.get("FETCH_DEADLINE_MIN") or "5")
CUSTOM_GROUPS_CSV = os.path.join(os.path.dirname(UNIVERSE_FILE) or ".", "custom_groups.csv")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

# シンボルの緩い妥当性チェック (Wikipediaのドット表記 BRK.B 等を許容してから正規化する)
CODE_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
FIELDNAMES = ["code", "name", "bucket", "sector", "group"]

# GICS Sector(11区分) → 日本語。未知の英語名はそのまま格納 (取りこぼしを握りつぶさない)
SECTOR_JA = {
    "Information Technology": "情報技術",
    "Health Care": "ヘルスケア",
    "Financials": "金融",
    "Consumer Discretionary": "一般消費財",
    "Consumer Staples": "生活必需品",
    "Industrials": "資本財",
    "Energy": "エネルギー",
    "Materials": "素材",
    "Utilities": "公益事業",
    "Real Estate": "不動産",
    "Communication Services": "コミュニケーション・サービス",
}


def normalize_symbol(raw):
    """Wikipediaのシンボル表記を Yahoo Finance 形式に正規化する。
    ドット表記 (BRK.B, BF.B) はダッシュ (BRK-B, BF-B) に変換。
    妥当性チェックを通らない場合は None。"""
    sym = (raw or "").strip().upper()
    if not sym or not CODE_RE.match(sym):
        return None
    return sym.replace(".", "-")


def _fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class _TableParser(HTMLParser):
    """WikipediaのHTMLからtable要素を抽出する最小限のパーサ (標準ライブラリのみ)。

    【重要】トップレベルのtableだけを見る旧実装では、Nasdaq-100ページの構成銘柄
    テーブルを取りこぼした (2026-07-16 ランナー上のプローブで実測: レイアウト用
    ラッパー等の内側にネストしており、トップレベル11個の中に存在しなかった)。
    そこでスタック方式で**全深さのtable**をそれぞれ独立に収集する。セルのテキストは
    最も内側の開いているtableにのみ割り当てるため、ネストしても混ざらない。
    ヘッダー照合(_find_table)で目的のテーブルだけを選ぶので、ノイズ増は無害。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []   # 完了した全table (ネスト含む、閉じた順)
        self._stack = []   # 開いているtable: {"rows": [...], "row": None, "cell": None}

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append({"rows": [], "row": None, "cell": None})
        elif self._stack:
            t = self._stack[-1]
            if tag == "tr":
                t["row"] = []
            elif tag in ("td", "th"):
                t["cell"] = []

    def handle_endtag(self, tag):
        if tag == "table":
            if self._stack:
                self.tables.append(self._stack.pop()["rows"])
        elif self._stack:
            t = self._stack[-1]
            if tag == "tr":
                if t["row"] is not None:
                    t["rows"].append(t["row"])
                t["row"] = None
            elif tag in ("td", "th"):
                if t["cell"] is not None and t["row"] is not None:
                    t["row"].append("".join(t["cell"]).strip())
                t["cell"] = None

    def handle_data(self, data):
        if self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"].append(data)


def _find_table(tables, required_headers):
    """ヘッダー行に required_headers (小文字部分一致) を全て含む最初のtableを返す。"""
    for t in tables:
        if not t or not t[0]:
            continue
        header = [c.strip().lower() for c in t[0]]
        if all(any(req in h for h in header) for req in required_headers):
            return t
    return None


def _col_index(header, name):
    """列名の完全一致(大小無視)優先、無ければ部分一致で列indexを返す。無ければNone。"""
    name_l = name.lower()
    for i, h in enumerate(header):
        if h.strip().lower() == name_l:
            return i
    for i, h in enumerate(header):
        if name_l in h.strip().lower():
            return i
    return None


def fetch_sp500():
    """S&P500構成銘柄を取得し (codes, sub_industry_map, group_map, name_map) を返す。
    取得/解析失敗時は (set(), {}, {}, {}) — 呼び出し側でフォールバックする。"""
    try:
        html = _fetch(SP500_URL, timeout=25).decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"S&P500一覧の取得失敗: {type(e).__name__}: {e}。coreソースをスキップ。")
        return set(), {}, {}, {}

    try:
        parser = _TableParser()
        parser.feed(html)
        table = _find_table(parser.tables, ["symbol", "security", "gics sector"])
        if not table or len(table) < 2:
            print("S&P500一覧: constituentsテーブルが見つからなかった (構造変更の可能性)。coreソースをスキップ。")
            return set(), {}, {}, {}
        header = table[0]
        i_sym = _col_index(header, "Symbol")
        i_name = _col_index(header, "Security")
        i_sector = _col_index(header, "GICS Sector")
        i_sub = _col_index(header, "GICS Sub-Industry")
        if i_sym is None or i_sector is None or i_sub is None:
            print("S&P500一覧: 列構成が想定と異なる (Symbol/GICS Sector/GICS Sub-Industry)。coreソースをスキップ。")
            return set(), {}, {}, {}

        codes, sub_map, group_map, name_map = set(), {}, {}, {}
        for row in table[1:]:
            if len(row) <= max(i_sym, i_sector, i_sub):
                continue
            code = normalize_symbol(row[i_sym])
            if not code:
                continue
            codes.add(code)
            sub = row[i_sub].strip()
            if sub:
                sub_map[code] = sub
            sector = row[i_sector].strip()
            if sector:
                group_map[code] = SECTOR_JA.get(sector, sector)
            if i_name is not None and i_name < len(row) and row[i_name].strip():
                name_map[code] = row[i_name].strip()
        if len(codes) < 400:
            print(f"⚠️ S&P500一覧の解析結果が{len(codes)}銘柄と少なすぎる"
                  "(テーブル構造が変わった可能性)。念のためこのまま使用するが要確認。")
        return codes, sub_map, group_map, name_map
    except Exception as e:
        print(f"S&P500一覧の解析失敗: {type(e).__name__}: {e}。coreソースをスキップ。")
        return set(), {}, {}, {}


def fetch_nasdaq100():
    """Nasdaq-100構成銘柄を取得し (codes, sub_industry_map, group_map, name_map) を返す。
    取得/解析失敗時は (set(), {}, {}, {})。"""
    try:
        html = _fetch(NASDAQ100_URL, timeout=25).decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"Nasdaq-100一覧の取得失敗: {type(e).__name__}: {e}。leaderソースをスキップ。")
        return set(), {}, {}, {}

    try:
        parser = _TableParser()
        parser.feed(html)
        # 列名は歴史的に "Ticker" だが "Symbol" へ変わる可能性に備えて両方試す
        table = (_find_table(parser.tables, ["ticker", "gics sector"])
                 or _find_table(parser.tables, ["symbol", "gics sector"]))
        if not table or len(table) < 2:
            print("Nasdaq-100一覧: componentsテーブルが見つからなかった (構造変更の可能性)。leaderソースをスキップ。")
            return set(), {}, {}, {}
        header = table[0]
        i_sym = _col_index(header, "Ticker")
        if i_sym is None:
            i_sym = _col_index(header, "Symbol")
        i_name = _col_index(header, "Company")
        i_sector = _col_index(header, "GICS Sector")
        i_sub = _col_index(header, "GICS Sub-Industry")
        if i_sym is None or i_sector is None:
            print("Nasdaq-100一覧: 列構成が想定と異なる (Ticker/GICS Sector)。leaderソースをスキップ。")
            return set(), {}, {}, {}

        codes, sub_map, group_map, name_map = set(), {}, {}, {}
        for row in table[1:]:
            if len(row) <= i_sym or len(row) <= i_sector:
                continue
            code = normalize_symbol(row[i_sym])
            if not code:
                continue
            codes.add(code)
            if i_sub is not None and i_sub < len(row) and row[i_sub].strip():
                sub_map[code] = row[i_sub].strip()
            sector = row[i_sector].strip()
            if sector:
                group_map[code] = SECTOR_JA.get(sector, sector)
            if i_name is not None and i_name < len(row) and row[i_name].strip():
                name_map[code] = row[i_name].strip()
        if len(codes) < 80:
            print(f"⚠️ Nasdaq-100一覧の解析結果が{len(codes)}銘柄と少なすぎる"
                  "(テーブル構造が変わった可能性)。念のためこのまま使用するが要確認。")
        return codes, sub_map, group_map, name_map
    except Exception as e:
        print(f"Nasdaq-100一覧の解析失敗: {type(e).__name__}: {e}。leaderソースをスキップ。")
        return set(), {}, {}, {}


def load_existing():
    """既存universe.csvを読み、(hot_codes, existing_row) を返す。
    existing_rowは全既存銘柄のname/sector/group(旧ファイルにgroup列が無ければ空文字)
    を保持する辞書 (温存用)。"""
    hot_codes, existing = set(), {}
    if not os.path.exists(UNIVERSE_FILE):
        return hot_codes, existing
    with open(UNIVERSE_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            code = (r.get("code") or "").strip()
            if not code:
                continue
            existing[code] = {"name": r.get("name") or code, "sector": r.get("sector") or "",
                               "group": r.get("group") or ""}
            if (r.get("bucket") or "").strip() == "hot":
                hot_codes.add(code)
    return hot_codes, existing


def build_universe(deadline):
    hot_codes, existing = load_existing()
    if not hot_codes and not existing:
        print(f"既存 {UNIVERSE_FILE} が無い/空。話題枠(hot)は0件として続行する。")

    sp500_codes, sp_sub, sp_group, sp_name = set(), {}, {}, {}
    nq_codes, nq_sub, nq_group, nq_name = set(), {}, {}, {}
    if time.time() < deadline:
        sp500_codes, sp_sub, sp_group, sp_name = fetch_sp500()
    if time.time() < deadline:
        nq_codes, nq_sub, nq_group, nq_name = fetch_nasdaq100()

    if not sp500_codes:
        print("⚠️ S&P500ソースが空 (取得/解析失敗)。core層はhot/leaderのみになる — PM判断が必要。")
    if not nq_codes:
        print("⚠️ Nasdaq-100ソースが空 (取得/解析失敗)。leader層は既存hotのみになる — PM判断が必要。")

    leader_codes = nq_codes
    all_codes = hot_codes | leader_codes | sp500_codes
    dropped = (set(existing) - hot_codes) - leader_codes - sp500_codes
    if dropped:
        print(f"注意: 既存銘柄のうち{len(dropped)}件がS&P500/Nasdaq-100/hotのいずれにも該当せず"
              f"新ユニバースから除外される (データファイルは削除しない): {sorted(dropped)}")

    # Sub-Industry(sector列用) は S&P500優先、無ければNasdaq-100で補完
    sub_map = dict(nq_sub)
    sub_map.update(sp_sub)

    rows = []
    for code in all_codes:
        if code in hot_codes:
            bucket = "hot"
        elif code in leader_codes:
            bucket = "leader"
        else:
            bucket = "core"

        if code in existing:
            name = existing[code]["name"]
            sector = existing[code]["sector"]
        else:
            name = sp_name.get(code) or nq_name.get(code) or code
            sector = sub_map.get(code, "")

        # group = GICS Sector(日本語11区分)。取得失敗時のみ既存group(温存)、それも無ければ空文字。
        group = sp_group.get(code) or nq_group.get(code) or existing.get(code, {}).get("group") or ""

        rows.append({"code": code, "name": name, "bucket": bucket, "sector": sector, "group": group})

    order = {"leader": 0, "core": 1, "hot": 2}
    rows.sort(key=lambda r: (order.get(r["bucket"], 9), r["code"]))
    return rows, sub_map, {
        "sp500_n": len(sp500_codes), "leader_n": len(leader_codes), "hot_n": len(hot_codes),
        "dropped_n": len(dropped),
    }


def write_custom_groups(rows, sub_map):
    """custom_groups.csv (code,custom_group,basis) を機械生成する。
    GICS Sub-Industryに"Semiconductor"を含む銘柄を「半導体」として書き出す。

    【機械管理ファイル】JP版のcustom_groups.csvは手動キュレーションだが、こちらは
    実行のたびに全件作り直す完全自動生成ファイル。手で追記・編集しても次回の
    us_universe_refresh.py実行で上書きされる点に注意。
    """
    universe_codes = {r["code"] for r in rows}
    entries = []
    for code, sub in sub_map.items():
        if code in universe_codes and "Semiconductor" in sub:
            entries.append({"code": code, "custom_group": "半導体", "basis": sub})
    entries.sort(key=lambda e: e["code"])

    os.makedirs(os.path.dirname(CUSTOM_GROUPS_CSV) or ".", exist_ok=True)
    with open(CUSTOM_GROUPS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["code", "custom_group", "basis"])
        w.writeheader()
        w.writerows(entries)
    return len(entries)


def main():
    deadline = time.time() + DEADLINE_MIN * 60
    rows, sub_map, stats = build_universe(deadline)
    if not rows:
        print("ユニバースが空になったため書き込みを中止する (既存ファイルを保持)。")
        sys.exit(1)

    os.makedirs(os.path.dirname(UNIVERSE_FILE) or ".", exist_ok=True)
    with open(UNIVERSE_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(rows)

    n_custom = write_custom_groups(rows, sub_map)

    bucket_n = {}
    for r in rows:
        bucket_n[r["bucket"]] = bucket_n.get(r["bucket"], 0) + 1
    print(f"完了: {UNIVERSE_FILE} を{len(rows)}銘柄で再構築 "
          f"(leader={bucket_n.get('leader', 0)}, core={bucket_n.get('core', 0)}, "
          f"hot={bucket_n.get('hot', 0)}; ソース内訳: S&P500候補={stats['sp500_n']}, "
          f"Nasdaq-100={stats['leader_n']}, hot既存シード={stats['hot_n']}, "
          f"除外={stats['dropped_n']}); custom_groups.csv(半導体)={n_custom}銘柄")


if __name__ == "__main__":
    main()
