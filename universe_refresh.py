"""
日本株 監視ユニバース自動更新 (universe_refresh.py) — 850銘柄化

data/jp_stocks/universe.csv (code,name,bucket,sector,group) を、TOPIX500 + 日経225 + 話題枠
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

group列 (JPX正式33業種区分、業種グループ集計用): sectorとは別に、全銘柄へ
JPX「東証上場銘柄一覧」の33業種区分(jpx_sector)をそのまま機械付与する。
sectorは初期46銘柄が手書きの詳細タグ(「総合商社」「電線・光ファイバ」等、全68種類)
でグループ集計に使えないため、正規の33業種で揃えたgroup列を別途持たせる。
フォールバック順: jpx_sector → 既存universe.csvのgroup(温存) → ""(空、取得失敗時)。

【安全装置 (2026-10-03 追加・fail-closed)】
2026-09-03の月次実行で、JPXが data_j を旧xls(BIFF)から xlsx 形式へ切り替えていたため
xlrd が「Excel xlsx file; not supported」で解析失敗 → TOPIX500ソースが空のまま
「取得できた範囲で構築」して既存core 271銘柄をユニバースから落とし、runはsuccess
(緑)で終わっていた (1分足の収集が約1か月止まり、Yahooの制限で埋め戻し不可)。
再発防止として以下に変更した:
  1. 形式を先頭バイトで判定し、xlsx(zip) は標準ライブラリ(zipfile+ElementTree)で、
     旧xls(OLE2/BIFF) は従来どおり xlrd で読む (xlsxのためにopenpyxlは追加しない)。
  2. ソースの件数が下限未満 (TOPIX500 < MIN_TOPIX500, 日経225 < MIN_NIKKEI225) または
     既存leader/coreからの除外が多すぎる (> MAX_DROP) 場合は universe.csv を一切
     書き換えず exit 2 で終了する (runが赤くなりGitHubから通知が届く)。
     「取得できた範囲で構築」は廃止 — 前回の正常なユニバースを保持する方が安全。
     定期見直し等で本当に大量入替が起きた月は、workflow_dispatch の max_drop 入力で
     MAX_DROP を一時的に引き上げて再実行する。
  3. 指数構成銘柄の一覧 index_members.csv (code,index_bucket=leader/core) を
     universe.csv と同時に書き出す。hot枠の銘柄が指数構成銘柄でもある場合 (手動シードの
     半導体株や、障害中に話題枠として入ったTOPIX500銘柄) に、hot_refresh.py が圏外除外する
     際「ユニバースから消す」のではなく「leader/coreへ戻す」ために使う (bucket優先順位の
     PM決定 hot>leader>core はそのまま)。

設定 (環境変数、pushイベントでinputsが空文字になるケースに備えて `or` で既定値):
  UNIVERSE_FILE        既存/出力先ユニバースCSV (既定 data/jp_stocks/universe.csv)
  FETCH_DEADLINE_MIN   全体デッドライン分・ハング防止 (既定 5)
  MIN_TOPIX500         TOPIX500ソースの最低件数 (既定 450。実測 493 @2026-07-09)
  MIN_NIKKEI225        日経225ソースの最低件数 (既定 200)
  MAX_DROP             既存leader/coreから一度に外してよい最大件数 (既定 100)

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
import xml.etree.ElementTree as ET
import zipfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

socket.setdefaulttimeout(35)

UNIVERSE_FILE = os.environ.get("UNIVERSE_FILE") or "data/jp_stocks/universe.csv"
DEADLINE_MIN = float(os.environ.get("FETCH_DEADLINE_MIN") or "5")
MIN_TOPIX500 = int(os.environ.get("MIN_TOPIX500") or "450")
MIN_NIKKEI225 = int(os.environ.get("MIN_NIKKEI225") or "200")
MAX_DROP = int(os.environ.get("MAX_DROP") or "100")
# 指数構成銘柄の一覧 (code,index_bucket)。universe.csv と同じディレクトリに置く
INDEX_MEMBERS_FILE = os.path.join(os.path.dirname(UNIVERSE_FILE) or ".", "index_members.csv")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

JPX_INDEX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_BASE = "https://www.jpx.co.jp"
NIKKEI225_CSV_URL = "https://indexes.nikkei.co.jp/nkave/archives/file/nikkei_stock_average_weight_jp.csv"

TOPIX500_SIZES = {"TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"}
# 東証の証券コードは常に4文字: 先頭1桁は数字、残り3文字は数字または英字
# (2024年の新コード体系で末尾が英字になる銘柄が増えている。例 285A・130A等)。
# 旧仮定「4桁数字+任意の英数字1文字」だと新コードを1文字取りこぼしていたため修正。
CODE_RE = re.compile(r"^[0-9][0-9A-Za-z]{3}$")
FIELDNAMES = ["code", "name", "bucket", "sector", "group"]


def _fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


ZIP_MAGIC = b"PK\x03\x04"                            # xlsx (Office Open XML = zip)
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"      # 旧xls (OLE2/BIFF)
_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _col_index(cell_ref):
    """セル参照 "C12" → 0始まりの列番号 2。"""
    n = 0
    for ch in cell_ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _rich_text(el):
    """<si>/<is> 要素の文字列。直下の<t>、または直下の<r>の<t>だけを使い、
    日本語Excelのふりがな(<rPh>)は値に含めない (含めると「トヨタ自動車トヨタジドウシャ」になる)。"""
    t = el.find(_NS_MAIN + "t")
    if t is not None:
        return t.text or ""
    return "".join((r.findtext(_NS_MAIN + "t") or "") for r in el.findall(_NS_MAIN + "r"))


def _xlsx_shared_strings(zf):
    """共有文字列表 (sharedStrings.xml の <si> を順に文字列化)。"""
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    return [_rich_text(si) for si in ET.fromstring(zf.read("xl/sharedStrings.xml")).iter(_NS_MAIN + "si")]


def _xlsx_first_sheet_path(zf):
    """workbook.xml の先頭シート → rels で実ファイルパスを解決。失敗時は sheet1.xml 相当。"""
    try:
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        first = wb.find(f"{_NS_MAIN}sheets/{_NS_MAIN}sheet")
        rid = first.get(_NS_REL + "id")
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rels.iter(_NS_PKG_REL + "Relationship"):
            if rel.get("Id") == rid:
                target = rel.get("Target") or ""
                path = target.lstrip("/") if target.startswith("/") else "xl/" + target
                if path in zf.namelist():
                    return path
    except (KeyError, AttributeError, ET.ParseError):
        pass
    sheets = sorted(n for n in zf.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    if not sheets:
        raise ValueError("xlsx内にワークシートが見つからない")
    return sheets[0]


def _xlsx_rows(raw):
    """xlsxの先頭シートを標準ライブラリ(zipfile+ElementTree)だけで読み、行ごとのセル値
    リストを返す。数値はfloat (xlrdと同じ型)、文字列はstr、空セルは""。"""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    shared = _xlsx_shared_strings(zf)
    root = ET.fromstring(zf.read(_xlsx_first_sheet_path(zf)))
    rows = []
    for row in root.iter(_NS_MAIN + "row"):
        vals = {}
        for pos, c in enumerate(row.findall(_NS_MAIN + "c")):
            ref = c.get("r")
            idx = _col_index(ref) if ref else pos
            typ = c.get("t") or "n"
            if typ == "inlineStr":
                is_ = c.find(_NS_MAIN + "is")
                val = _rich_text(is_) if is_ is not None else ""
            else:
                v = c.findtext(_NS_MAIN + "v")
                if v is None:
                    val = ""
                elif typ == "s":
                    val = shared[int(v)]
                elif typ == "n":
                    try:
                        val = float(v)
                    except ValueError:
                        val = v
                else:  # str(数式の文字列結果) / b(真偽) / e(エラー) は文字列のまま
                    val = v
            vals[idx] = val
        rows.append([vals.get(i, "") for i in range(max(vals) + 1)] if vals else [])
    return rows


def read_sheet_rows(raw):
    """Excelファイル(bytes)の先頭シートを (行リスト, 形式名) で返す。形式は拡張子でなく
    先頭バイトで判定する (JPXはURL/拡張子を変えずに中身の形式を変えることがある)。"""
    if raw[:4] == ZIP_MAGIC:
        return _xlsx_rows(raw), "xlsx"
    if raw[:8] == OLE2_MAGIC:
        try:
            import xlrd
        except ImportError:
            raise RuntimeError("旧xls(BIFF)形式だが xlrd 未導入 (workflowの `pip install xlrd` を確認)")
        ws = xlrd.open_workbook(file_contents=raw).sheet_by_index(0)
        return [ws.row_values(i) for i in range(ws.nrows)], "xls"
    raise ValueError(f"Excelでない応答 (先頭バイト={raw[:16]!r}) — エラーページ/HTMLの可能性")


def _norm_header(v):
    return re.sub(r"[\s　]", "", str(v))


def parse_jpx_rows(rows):
    """東証上場銘柄一覧の行リストから (topix500_codes, jpx_sector, jpx_name) を返す。
    ヘッダー行は「コード」「規模区分」を含む最初の行 (列の並び替え・空白混入に耐える)。
    ネットワーク非依存の純関数 (合成xls/xlsxでローカル単体テスト可能)。"""
    hdr_i, header = None, None
    for i, r in enumerate(rows[:20]):
        names = [_norm_header(v) for v in r]
        if "コード" in names and "規模区分" in names:
            hdr_i, header = i, names
            break
    if header is None:
        raise ValueError("ヘッダー行(コード/規模区分)が見つからない — 列構成が変わった可能性")
    code_col = header.index("コード")
    name_col = header.index("銘柄名")
    sector_col = header.index("33業種区分")
    size_col = header.index("規模区分")

    topix500, sector_map, name_map = set(), {}, {}
    for row in rows[hdr_i + 1:]:
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


def fetch_jpx_listed():
    """JPX東証上場銘柄一覧を取得し (topix500_codes, jpx_sector, jpx_name) を返す。
    取得/解析に失敗した場合は (set(), {}, {}) を返す (→ main()の件数ガードで書き込み中止)。"""
    try:
        html = _fetch(JPX_INDEX_URL, timeout=20).decode("utf-8", errors="replace")
        links = sorted(set(re.findall(r'href="([^"]*data_j\.xlsx?[^"]*)"', html, re.IGNORECASE)))
        if not links:
            print("JPX東証上場銘柄一覧: data_j.xls/xlsx相当のリンクが見つからなかった。TOPIX500ソースをスキップ。")
            return set(), {}, {}
        href = links[0]
        url = href if href.startswith("http") else JPX_BASE + href
        raw = _fetch(url, timeout=25)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"JPX東証上場銘柄一覧の取得失敗: {type(e).__name__}: {e}。TOPIX500ソースをスキップ。")
        return set(), {}, {}

    try:
        rows, fmt = read_sheet_rows(raw)
        topix500, sector_map, name_map = parse_jpx_rows(rows)
        print(f"JPX東証上場銘柄一覧: {url.rsplit('/', 1)[-1]} を{fmt}形式として解析 "
              f"({len(rows)}行, 業種付き{len(sector_map)}銘柄, TOPIX500={len(topix500)})")
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
        if len(codes) < MIN_NIKKEI225:
            print(f"⚠️ 日経225ウエイトCSVの解析結果が{len(codes)}銘柄と少なすぎる"
                  "(列構成が変わった可能性)。main()の件数ガードで書き込みを中止する。")
        return codes, sector_map, name_map
    except (UnicodeDecodeError, csv.Error) as e:
        print(f"日経225ウエイトCSVの解析失敗: {type(e).__name__}: {e}。leaderソースをスキップ。")
        return set(), {}, {}


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

    topix500, jpx_sector, jpx_name = ({}, {}, {})
    leader_codes, nikkei_sector, nikkei_name = (set(), {}, {})
    if time.time() < deadline:
        topix500, jpx_sector, jpx_name = fetch_jpx_listed()
    if time.time() < deadline:
        leader_codes, nikkei_sector, nikkei_name = fetch_nikkei225()

    if not topix500:
        print("⚠️ TOPIX500ソースが空 (取得/解析失敗)。")
    if not leader_codes:
        print("⚠️ 日経225ソースが空 (取得/解析失敗)。")

    all_codes = hot_codes | leader_codes | set(topix500)
    dropped = (set(existing) - hot_codes) - leader_codes - set(topix500)
    if dropped:
        print(f"注意: 既存銘柄のうち{len(dropped)}件がTOPIX500/日経225/hotのいずれにも該当せず"
              f"新ユニバースから除外される (データファイルは削除しない): {sorted(dropped)}")
    hot_in_index = hot_codes & (leader_codes | set(topix500))
    if hot_in_index:
        print(f"話題枠のうち指数構成銘柄 {len(hot_in_index)}件 (話題枠から外れても leader/core として"
              f"監視を継続する — index_members.csv 経由): {sorted(hot_in_index)}")

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

        # group = 正規JPX33業種区分。全銘柄に対しjpx_sectorを最優先で機械付与し、
        # 取得失敗時のみ既存group(温存)、それも無ければ空文字にフォールバックする。
        group = jpx_sector.get(code) or existing.get(code, {}).get("group") or ""

        rows.append({"code": code, "name": name, "bucket": bucket, "sector": sector, "group": group})

    order = {"leader": 0, "core": 1, "hot": 2}
    rows.sort(key=lambda r: (order.get(r["bucket"], 9), r["code"]))
    return rows, {
        "topix500_n": len(topix500), "leader_n": len(leader_codes), "hot_n": len(hot_codes),
        "dropped_n": len(dropped), "hot_in_index_n": len(hot_in_index),
        # 指数構成銘柄 → 本来のbucket (hot_refresh.py が話題枠から外す時の戻り先)
        "index_members": {c: ("leader" if c in leader_codes else "core")
                          for c in (leader_codes | set(topix500))},
    }


def guard_problems(stats):
    """書き込みを中止すべき異常のリスト (空なら正常)。fail-closed の判定本体。"""
    problems = []
    if stats["topix500_n"] < MIN_TOPIX500:
        problems.append(f"TOPIX500ソースが{stats['topix500_n']}銘柄 (下限{MIN_TOPIX500}未満 — 取得/形式変更の失敗)")
    if stats["leader_n"] < MIN_NIKKEI225:
        problems.append(f"日経225ソースが{stats['leader_n']}銘柄 (下限{MIN_NIKKEI225}未満 — 取得/形式変更の失敗)")
    if stats["dropped_n"] > MAX_DROP:
        problems.append(f"既存leader/coreから{stats['dropped_n']}銘柄を一度に除外しようとした "
                        f"(上限{MAX_DROP}超 — 定期見直しで本当に大量入替なら max_drop を引き上げて再実行)")
    return problems


def main():
    deadline = time.time() + DEADLINE_MIN * 60
    rows, stats = build_universe(deadline)
    if not rows:
        print("ユニバースが空になったため書き込みを中止する (既存ファイルを保持)。")
        sys.exit(1)
    problems = guard_problems(stats)
    if problems:
        print("❌ 安全装置: 以下の異常のため universe.csv を書き換えずに終了する (前回の正常な内容を保持):")
        for p in problems:
            print(f"   - {p}")
        sys.exit(2)

    os.makedirs(os.path.dirname(UNIVERSE_FILE) or ".", exist_ok=True)
    with open(UNIVERSE_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(rows)
    with open(INDEX_MEMBERS_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "index_bucket"])
        for code, b in sorted(stats["index_members"].items()):
            w.writerow([code, b])

    bucket_n = {}
    for r in rows:
        bucket_n[r["bucket"]] = bucket_n.get(r["bucket"], 0) + 1
    print(f"完了: {UNIVERSE_FILE} を{len(rows)}銘柄で再構築 "
          f"(leader={bucket_n.get('leader', 0)}, core={bucket_n.get('core', 0)}, "
          f"hot={bucket_n.get('hot', 0)}; ソース内訳: TOPIX500候補={stats['topix500_n']}, "
          f"日経225={stats['leader_n']}, hot={stats['hot_n']}(うち指数構成銘柄{stats['hot_in_index_n']}), "
          f"除外={stats['dropped_n']})")


if __name__ == "__main__":
    main()
