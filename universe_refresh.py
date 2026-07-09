"""
日本株 監視ユニバース自動更新 (universe_refresh.py) — 850銘柄化

data/jp_stocks/universe.csv (code,name,bucket,sector) を、TOPIX500 + 日経225 + 話題枠
(既存hot手動シード) から機械的に再構築する。手打ちでの銘柄追加は行わない設計。

データソース (2026-07-09 Phase1調査でActionsランナー上の実データにより確認済み。
サンドボックス(Claude Code)からは両サイトともproxy403で到達不可なため、
実URL・実列構成の検証はランナー上でのみ可能だった):

  1. TOPIX500 = JPX「東証上場銘柄一覧」(data_j.xls, 旧OLE2/BIFF形式・xlrd必須)
     一覧ページ https://www.jpx.co.jp/markets/statistics-equities/misc/01.html から
     data_j.xls への実リンク(ハッシュ化ディレクトリ名で予測不可)を都度解決する
     (jp_supply_demand.pyのxlsリンク解決と同じ設計)。
     列構成(0-indexed, 実データで確認済み): 0=日付 1=コード 2=銘柄名 3=市場・商品区分
     4=33業種コード 5=33業種区分 6=17業種コード 7=17業種区分 8=規模コード 9=規模区分
     TOPIX500 = 規模区分が {TOPIX Core30, TOPIX Large70, TOPIX Mid400} の行の合算
     (30+70+400=500という定義どおり。実データでは2026-07-09時点で31+68+394=493行で、
     500ちょうどではない — JPXの定期見直し途中の実態値であり、500に切り上げ/切り詰め
     する加工はしない。規模区分が"-"の行はETF/REIT/PRO Market等でTOPIX対象外のため除外)。
     このファイルは規模区分に関わらず全上場株式の33業種区分を持つため、
     sector(業種タグ)の機械採番ソースとしても使う。

  2. 日経225 = 日経公式構成銘柄ウエイトCSV
     https://indexes.nikkei.co.jp/nkave/archives/file/nikkei_stock_average_weight_jp.csv
     (cp932エンコード、列: 日付,コード,社名,業種,セクター,ウエート)。
     実データで226「行」あったが末尾1行は著作権表示の脚注文(コード列が数値でない)
     であり実データ行ではない → コード列が数字4桁+英字1桁以内の形式の行のみ採用し
     225銘柄に一致することを確認済み。

  3. 話題枠(hot) = 既存 universe.csv の bucket=hot 行(キオクシア等の手動シード)。
     「話題・これから話題の100社」を機械的に検出できる無料公式ソースは
     Phase1調査で見つからなかったため、既存の手動シードをそのまま温存する
     (PMへの既知の限界として報告する対象)。

bucket割当の優先順位 (PM決定): hot(既存手動シード) > leader(日経225) > core(TOPIX500の残り)。
既存46銘柄の手書きsector(詳細な業種タグ)は上書きせず温存。新規銘柄のsectorは
JPX 33業種区分から機械的に埋め、JPXデータに無い場合のみ日経の「業種」列で補完する。

全候補ソースが機械取得不可な場合は、取得できた範囲で構築し不足分をログに正直に出す
(例: TOPIX500取得失敗→leader+hotのみで構築を続行し、その旨を明示)。

設定 (環境変数、pushイベントでinputsが空文字になるケースに備えて `or` で既定値):
  UNIVERSE_FILE        既存/出力先ユニバースCSV (既定 data/jp_stocks/universe.csv)
  FETCH_DEADLINE_MIN   全体デッドライン分・ハング防止 (既定 5)

実行: python universe_refresh.py
"""

import csv
import io
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

socket.setdefaulttimeout(35)

UNIVERSE_FILE = os.environ.get("UNIVERSE_FILE") or "data/jp_stocks/universe.csv"
DEADLINE_MIN = float(os.environ.get("FETCH_DEADLINE_MIN") or "5")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

JPX_INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_BASE = "https://www.jpx.co.jp"
NIKKEI225_CSV_URL = "https://indexes.nikkei.co.jp/nkave/archives/file/nikkei_stock_average_weight_jp.csv"

TOPIX500_SIZES = {"TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"}
CODE_RE = re.compile(r"^[0-9]{4}[0-9A-Za-z]?$")  # 4桁数字 or 英数混在(例 285A)
FIELDNAMES = ["code", "name", "bucket", "sector"]


def _fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_jpx_listed():
    """JPX東証上場銘柄一覧を取得し (topix500_codes, jpx_sector, jpx_name) を返す。
    取得/解析に失敗した場合は (set(), {}, {}) を返し、呼び出し側でフォールバックする。"""
    try:
        import xlrd
    except ImportError:
        print("xlrd未導入 (workflowでのみ `pip install xlrd` する設計)。TOPIX500ソースをスキップ。")
        return set(), {}, {}

    try:
        html = _fetch(JPX_INDEX_URL, timeout=20).decode("utf-8", errors="replace")
        links = sorted(set(re.findall(r'href="([^"]*data_j\.xlsx?[^"]*)"', html, re.IGNORECASE)))
        if not links:
            print("JPX東証上場銘柄一覧: data_j.xls相当のリンクが見つからなかった。TOPIX500ソースをスキップ。")
            return set(), {}, {}
        href = links[0]
        url = href if href.startswith("http") else JPX_BASE + href
        raw = _fetch(url, timeout=25)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"JPX東証上場銘柄一覧の取得失敗: {type(e).__name__}: {e}。TOPIX500ソースをスキップ。")
        return set(), {}, {}

    try:
        wb = xlrd.open_workbook(file_contents=raw)
        ws = wb.sheet_by_index(0)
        header = ws.row_values(0)
        code_col = header.index("コード")
        name_col = header.index("銘柄名")
        sector_col = header.index("33業種区分")
        size_col = header.index("規模区分")

        topix500, sector_map, name_map = set(), {}, {}
        for i in range(1, ws.nrows):
            row = ws.row_values(i)
            if len(row) <= max(code_col, name_col, sector_col, size_col):
                continue
            raw_code = row[code_col]
            code = (str(int(raw_code)) if isinstance(raw_code, float) and raw_code == int(raw_code)
                    else str(raw_code).strip())
            if not CODE_RE.match(code):
                continue
            sector = str(row[sector_col]).strip()
            if sector and sector != "-":
                sector_map[code] = sector
                name_map[code] = str(row[name_col]).strip()
            size = str(row[size_col]).strip()
            if size in TOPIX500_SIZES:
                topix500.add(code)
        return topix500, sector_map, name_map
    except Exception as e:
        print(f"JPX東証上場銘柄一覧の解析失敗: {type(e).__name__}: {e}。TOPIX500ソースをスキップ。")
        return set(), {}, {}


def fetch_nikkei225():
    """日経225公式ウエイトCSVを取得し (leader_codes, nikkei_sector, nikkei_name) を返す。
    取得/解析に失敗した場合は (set(), {}, {}) を返す。"""
    try:
        raw = _fetch(NIKKEI225_CSV_URL, timeout=25)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"日経225ウエイトCSVの取得失敗: {type(e).__name__}: {e}。leaderソースをスキップ。")
        return set(), {}, {}

    try:
        text = raw.decode("cp932")
        reader = csv.DictReader(io.StringIO(text))
        codes, sector_map, name_map = set(), {}, {}
        for r in reader:
            code = (r.get("コード") or "").strip()
            if not CODE_RE.match(code):
                continue  # 末尾の著作権表示など非データ行を除外
            codes.add(code)
            name_map[code] = (r.get("社名") or "").strip()
            sector = (r.get("業種") or "").strip()
            if sector:
                sector_map[code] = sector
        if len(codes) < 200:
            print(f"⚠️ 日経225ウエイトCSVの解析結果が{len(codes)}銘柄と少なすぎる"
                  "(列構成が変わった可能性)。念のためこのまま使用するが要確認。")
        return codes, sector_map, name_map
    except (UnicodeDecodeError, csv.Error) as e:
        print(f"日経225ウエイトCSVの解析失敗: {type(e).__name__}: {e}。leaderソースをスキップ。")
        return set(), {}, {}


def load_existing():
    """既存universe.csvを読み、(hot_codes, existing_row) を返す。
    existing_rowは全既存銘柄のname/sectorを保持する辞書 (温存用)。"""
    hot_codes, existing = set(), {}
    if not os.path.exists(UNIVERSE_FILE):
        return hot_codes, existing
    with open(UNIVERSE_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            code = (r.get("code") or "").strip()
            if not code:
                continue
            existing[code] = {"name": r.get("name") or code, "sector": r.get("sector") or ""}
            if (r.get("bucket") or "").strip() == "hot":
                hot_codes.add(code)
    return hot_codes, existing


def build_universe(deadline):
    hot_codes, existing = load_existing()
    if not hot_codes and not existing:
        print(f"既存 {UNIVERSE_FILE} が無い/空。話題枠(hot)は0件として続行する。")

    topix500, jpx_sector, jpx_name = ({}, {}, {})
    leader_codes, nikkei_sector, nikkei_name = (set(), {}, {})
    if time.time() < deadline:
        topix500, jpx_sector, jpx_name = fetch_jpx_listed()
    if time.time() < deadline:
        leader_codes, nikkei_sector, nikkei_name = fetch_nikkei225()

    if not topix500:
        print("⚠️ TOPIX500ソースが空 (取得/解析失敗)。core層はhot/leaderのみになる — PM判断が必要。")
    if not leader_codes:
        print("⚠️ 日経225ソースが空 (取得/解析失敗)。leader層は既存hotのみになる — PM判断が必要。")

    all_codes = hot_codes | leader_codes | set(topix500)
    dropped = (set(existing) - hot_codes) - leader_codes - set(topix500)
    if dropped:
        print(f"注意: 既存銘柄のうち{len(dropped)}件がTOPIX500/日経225/hotのいずれにも該当せず"
              f"新ユニバースから除外される (データファイルは削除しない): {sorted(dropped)}")

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
            name = nikkei_name.get(code) or jpx_name.get(code) or code
            sector = jpx_sector.get(code) or nikkei_sector.get(code) or ""

        rows.append({"code": code, "name": name, "bucket": bucket, "sector": sector})

    order = {"leader": 0, "core": 1, "hot": 2}
    rows.sort(key=lambda r: (order.get(r["bucket"], 9), r["code"]))
    return rows, {
        "topix500_n": len(topix500), "leader_n": len(leader_codes), "hot_n": len(hot_codes),
        "dropped_n": len(dropped),
    }


def main():
    deadline = time.time() + DEADLINE_MIN * 60
    rows, stats = build_universe(deadline)
    if not rows:
        print("ユニバースが空になったため書き込みを中止する (既存ファイルを保持)。")
        sys.exit(1)

    os.makedirs(os.path.dirname(UNIVERSE_FILE) or ".", exist_ok=True)
    with open(UNIVERSE_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(rows)

    bucket_n = {}
    for r in rows:
        bucket_n[r["bucket"]] = bucket_n.get(r["bucket"], 0) + 1
    print(f"完了: {UNIVERSE_FILE} を{len(rows)}銘柄で再構築 "
          f"(leader={bucket_n.get('leader', 0)}, core={bucket_n.get('core', 0)}, "
          f"hot={bucket_n.get('hot', 0)}; ソース内訳: TOPIX500候補={stats['topix500_n']}, "
          f"日経225={stats['leader_n']}, hot手動シード={stats['hot_n']}, "
          f"除外={stats['dropped_n']})")


if __name__ == "__main__":
    main()
