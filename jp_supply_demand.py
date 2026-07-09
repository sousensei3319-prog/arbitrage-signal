"""
JPX需給レイヤー: 空売り残高報告コレクター (jp_supply_demand.py)

JPXが毎営業日公表する「空売りの残高に関する情報」(金融商品取引法に基づく大口
空売り残高報告。発行済株式数の0.5%以上を保有する投資家の報告義務)から、
data/jp_stocks/universe.csv の監視銘柄に該当する行だけを抽出し、
data/jp_stocks/supply_demand/short_positions.csv に差分蓄積する。

背景:
  既存の資金集中スクリーナー(jp_money_flow.py)は売買代金(価格×出来高)のみで
  「誰が売買しているか」の裏付けが無い。本スクリプトは「空売り勢がどれだけ
  入っているか」を無料公開情報から調達し、需給面の事実データを追加する。

Phase1調査で判明した制約 (2026-07-09, Actionsランナー上の実データで確認済み):
  - 空売り残高報告: 発行済株式数の0.5%以上を報告した投資家のみが対象。
    0.5%未満の空売りは一切見えない(全体の空売り需給ではなく「大口」限定)
  - 週次遅行ではなく日次(公表は毎営業日17:00目処)だが、報告義務者側の
    集計・提出ラグがあるため計算年月日(calc_date)は公表日より数営業日前のことがある
  - 以下は同時に調査したが、JPX公式配信がPDFのみと判明したため不採用:
      - 空売り比率(市場全体の日次集計) … 個別銘柄ではなく市場全体の一本値でもある
      - 銘柄別信用取引週末残高(週次) … syumatsuYYYYMMDD00.pdf 固定
    日証金の貸借取引情報(taisyaku.jp)はJS動的レンダリング中心で一覧ページを
    stdlibで安定的にスクレイピングするのが困難と判断し、今回は見送り
    (個別銘柄detail頁は生HTMLにテーブルらしき断片はあったが、全銘柄横断の
    一覧取得手段が確立できていない。将来の拡張候補として保留)

設計:
  - 標準ライブラリのみを基本とし、Excel(.xls, 旧OLE2/BIFF形式)の解析にのみ
    xlrd を動的import。未導入(pip install xlrd されていない実行環境)では
    このソースをスキップし、他の処理(収集全体)は継続する
    (matplotlib同様「無くてもコア機能は動く」設計を踏襲。openpyxlではなく
    xlrdが必要なのはPhase1でファイルが旧BIFF形式と実証されたため)
  - 一覧ページ(index.html)のダウンロードリンクは日付ごとにハッシュ化された
    ディレクトリ名を含み予測できない(例: .../t13vrt000001iogo-att/
    20260701_Short_Positions.xls)。毎回一覧ページHTMLを取得し正規表現で
    その時点で公開されている全リンクを解決してから対象日を絞り込む
  - 投資家(報告者)単位の行をそのまま蓄積する(同一銘柄に複数投資家が報告する
    ケースがあるため)。銘柄別の集計(報告者数・合計比率)は利用側
    (jp_money_flow.py / dashboard)で行う
  - 既知の (disclosure_date, code, holder, calc_date) の組で重複排除

設定 (環境変数、pushイベントでinputsが空文字になるケースに備えて `or` で既定値):
  UNIVERSE_FILE        監視銘柄CSV (既定 data/jp_stocks/universe.csv)
  DATA_DIR              出力先ディレクトリ (既定 data/jp_stocks/supply_demand)
  OUT_CSV               出力ファイル名 (既定 short_positions.csv)
  FETCH_DEADLINE_MIN    全体デッドライン分・ハング防止 (既定 8)

実行: python jp_supply_demand.py
"""

import csv
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

# urlopenのtimeout引数が効かない経路(DNS等)への保険。ジョブハング防止
socket.setdefaulttimeout(35)

UNIVERSE_FILE = os.environ.get("UNIVERSE_FILE") or "data/jp_stocks/universe.csv"
DATA_DIR      = os.environ.get("DATA_DIR") or "data/jp_stocks/supply_demand"
OUT_CSV       = os.environ.get("OUT_CSV") or "short_positions.csv"
DEADLINE_MIN  = float(os.environ.get("FETCH_DEADLINE_MIN") or "8")

INDEX_URL = "https://www.jpx.co.jp/markets/public/short-selling/index.html"
BASE = "https://www.jpx.co.jp"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

FIELDNAMES = [
    "disclosure_date", "calc_date", "code", "name_ja", "name_en",
    "holder", "ratio_pct", "shares", "units",
    "prev_calc_date", "prev_ratio_pct", "notes",
]


def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_universe_codes():
    codes = {}
    if not os.path.exists(UNIVERSE_FILE):
        return codes
    with open(UNIVERSE_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = (row.get("code") or "").strip()
            if code:
                codes[code] = row.get("name") or code
    return codes


def find_xls_links(deadline):
    """一覧ページから当日分含む直近の *_Short_Positions.xls リンクを列挙する。
    ディレクトリ名がハッシュ化され予測できないため、毎回HTMLから解決する。"""
    if time.time() > deadline:
        return []
    html = _fetch(INDEX_URL, timeout=20).decode("utf-8", errors="replace")
    links = sorted(set(re.findall(
        r'href="([^"]*short-selling[^"]*-att/(\d{8})_Short_Positions\.xls)"',
        html, re.IGNORECASE)))
    out = []
    for href, ymd in links:
        url = href if href.startswith("http") else BASE + href
        out.append((ymd, url))
    return out


def existing_keys(path):
    """既知の (disclosure_date, code, holder, calc_date) 集合 (重複追記防止)。"""
    keys = set()
    if not os.path.exists(path):
        return keys
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            keys.add((r.get("disclosure_date"), r.get("code"),
                      r.get("holder"), r.get("calc_date")))
    return keys


def _xldate(serial, datemode, xlrd_mod):
    try:
        t = xlrd_mod.xldate_as_tuple(float(serial), datemode)
        return f"{t[0]:04d}-{t[1]:02d}-{t[2]:02d}"
    except Exception:
        return ""


def parse_workbook(raw_bytes, disclosure_date, universe_codes, xlrd_mod):
    """xlsバイト列 → universe銘柄に該当する行のdictリスト。
    列構成(0-indexed, Phase1でActionsランナー上の実データにより確認済み):
      1=計算年月日 2=銘柄コード 3=銘柄名(日本語) 4=銘柄名(英語) 5=商号・名称・氏名
      10=空売り残高割合 11=空売り残高数量 12=空売り残高売買単位数
      13=直近計算年月日 14=直近空売り残高割合 15=備考
    データ行はヘッダー(「銘柄コード」を含む行)の次行から始まる。
    """
    wb = xlrd_mod.open_workbook(file_contents=raw_bytes)
    ws = wb.sheet_by_index(0)
    datemode = wb.datemode

    header_i = None
    for i in range(min(20, ws.nrows)):
        rv = ws.row_values(i)
        if any("コード" in str(x) for x in rv):
            header_i = i
            break
    if header_i is None:
        raise ValueError("ヘッダー行(銘柄コードを含む行)が見つからない")

    rows = []
    for i in range(header_i + 1, ws.nrows):
        rv = ws.row_values(i)
        if len(rv) < 16:
            continue
        raw_code = rv[2]
        if raw_code in ("", None):
            continue
        code = str(int(raw_code)) if isinstance(raw_code, float) and raw_code == int(raw_code) else str(raw_code).strip()
        if code not in universe_codes:
            continue
        calc_date = _xldate(rv[1], datemode, xlrd_mod) if isinstance(rv[1], float) else str(rv[1])
        prev_calc_date = _xldate(rv[13], datemode, xlrd_mod) if isinstance(rv[13], float) else str(rv[13])
        try:
            ratio_pct = round(float(rv[10]) * 100, 4) if rv[10] not in ("", None) else ""
        except (TypeError, ValueError):
            ratio_pct = ""
        try:
            prev_ratio_pct = round(float(rv[14]) * 100, 4) if rv[14] not in ("", None) else ""
        except (TypeError, ValueError):
            prev_ratio_pct = ""
        rows.append({
            "disclosure_date": disclosure_date,
            "calc_date": calc_date,
            "code": code,
            "name_ja": str(rv[3]).strip(),
            "name_en": str(rv[4]).strip(),
            "holder": str(rv[5]).strip(),
            "ratio_pct": ratio_pct,
            "shares": rv[11] if rv[11] not in ("", None) else "",
            "units": rv[12] if rv[12] not in ("", None) else "",
            "prev_calc_date": prev_calc_date,
            "prev_ratio_pct": prev_ratio_pct,
            "notes": str(rv[15]).strip(),
        })
    return rows


def main():
    deadline = time.time() + DEADLINE_MIN * 60
    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, OUT_CSV)

    universe_codes = load_universe_codes()
    if not universe_codes:
        print(f"ユニバース {UNIVERSE_FILE} が無い/空。先に収集を実行すること。")
        return

    try:
        import xlrd
    except ImportError:
        print("xlrd未導入 (workflowでのみ `pip install xlrd` する設計)。"
              "空売り残高報告の収集をスキップ (コア機能は継続)。")
        return

    try:
        links = find_xls_links(deadline)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"一覧ページ取得失敗: {type(e).__name__}: {e}")
        return
    if not links:
        print("一覧ページに空売り残高報告のxlsリンクが見つからなかった。")
        return

    known = existing_keys(out_path)
    known_dates = {k[0] for k in known}
    total_new, processed_days, fail = 0, 0, 0

    for ymd, url in links:
        if time.time() > deadline:
            print(f"⚠️ デッドライン({DEADLINE_MIN:.0f}分)超過 — 残り{len(links) - processed_days}日分は打ち切り")
            break
        disclosure_date = f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}"
        if disclosure_date in known_dates:
            continue  # 既に取り込み済みの公表日はスキップ (再ダウンロード不要)
        try:
            raw = _fetch(url, timeout=25)
            rows = parse_workbook(raw, disclosure_date, universe_codes, xlrd)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError, ValueError) as e:
            fail += 1
            print(f"{disclosure_date}: 取得/解析失敗 ({type(e).__name__}: {e})")
            continue
        new_rows = [r for r in rows
                    if (r["disclosure_date"], r["code"], r["holder"], r["calc_date"]) not in known]
        if new_rows:
            is_new = not os.path.exists(out_path)
            with open(out_path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDNAMES)
                if is_new:
                    w.writeheader()
                w.writerows(new_rows)
            for r in new_rows:
                known.add((r["disclosure_date"], r["code"], r["holder"], r["calc_date"]))
        processed_days += 1
        total_new += len(new_rows)
        print(f"{disclosure_date}: universe該当 {len(rows)}件 / 新規追記 {len(new_rows)}件")

    print(f"完了: {processed_days}日分処理, 新規{total_new}行を{out_path}に保存 (失敗{fail}日分)")


if __name__ == "__main__":
    main()
