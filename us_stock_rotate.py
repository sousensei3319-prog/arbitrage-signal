"""
米国株 1分足 ローリング窓+月次アーカイブ化 (us_stock_rotate.py)

jp_stock_rotate.py の忠実な移植。ライブファイル {code}_1m.csv を直近 ROLLING_DAYS
営業日分だけに切り詰め、溢れた古い分は月次gzipアーカイブ {code}_1m_YYYYMM.csv.gz へ
退避する (標準ライブラリのgzipモジュールのみ使用)。

設計:
  - 「営業日」はカレンダー日数ではなく、CSVに実際に存在する日付(timestamp_etの日付部分)の
    種類数で数える。休場日はそもそもバーが無いので自然に除外される。
  - アーカイブは月(YYYYMM)ごとに1ファイル。同じ月に複数回ローテーションしても
    gzipの追記(多重メンバー方式)でヘッダーは新規ファイル作成時のみ書く。
  - アーカイブはダッシュボード/スクリーナーからは読み込まない設計 (後検証用のみ)。
  - 1分足(*_1m.csv)のみが対象。日足/週足/月足はローテーション対象外。

注意 (JP版との差分): 米国ティッカーには ".T" のようなサフィックスが無い
(例: "AAPL_1m.csv")。JP版の LIVE_SUFFIX="_T_1m.csv" は「コード + . + interval」由来の
サフィックスだが、米国側は単に「コード + _ + interval」なので LIVE_SUFFIX="_1m.csv"。
glob パターン `*_1m.csv` は universe.csv や money_flow.csv 等 (拡張子が違う/末尾が
"_1m.csv"で終わらない) を誤爆しない。

設定 (環境変数、pushイベントでinputsが空文字になるケースに備えて `or` で既定値):
  DATA_DIR       対象ディレクトリ (既定 data/us_stocks)
  ROLLING_DAYS   ライブファイルに残す営業日数 (既定 7)

実行: python us_stock_rotate.py
"""

import csv
import glob
import gzip
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DATA_DIR = os.environ.get("DATA_DIR") or "data/us_stocks"
ROLLING_DAYS = int(os.environ.get("ROLLING_DAYS") or "7")
LIVE_SUFFIX = "_1m.csv"  # us_stock_fetch.py の出力ファイル名規則 (code + _ + INTERVAL + .csv)

HEADER = ["timestamp_et", "epoch", "ticker", "open", "high", "low", "close", "volume"]


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_archive(archive_path, rows):
    """rowsを月次gzipアーカイブへ追記 (新規作成時のみヘッダーを書く)。"""
    is_new = not os.path.exists(archive_path)
    with gzip.open(archive_path, "at", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(HEADER)
        for r in rows:
            w.writerow([r.get(h, "") for h in HEADER])


def rewrite_live(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow([r.get(h, "") for h in HEADER])


def rotate_one(path):
    rows = load_rows(path)
    if not rows:
        return 0, 0
    # epoch昇順であることを前提にしているが念のためソート
    rows.sort(key=lambda r: int(r["epoch"]))
    dates = sorted({r["timestamp_et"][:10] for r in rows if r.get("timestamp_et")})
    if len(dates) <= ROLLING_DAYS:
        return 0, len(dates)  # ローテーション不要 (窓内に収まっている)

    keep_dates = set(dates[-ROLLING_DAYS:])
    archive_rows = [r for r in rows if r["timestamp_et"][:10] not in keep_dates]
    keep_rows = [r for r in rows if r["timestamp_et"][:10] in keep_dates]

    # 月(YYYYMM)ごとにグループ化してアーカイブへ追記
    by_month = {}
    for r in archive_rows:
        ym = r["timestamp_et"][:7].replace("-", "")  # "YYYY-MM" -> "YYYYMM"
        by_month.setdefault(ym, []).append(r)

    base = path[: -len(LIVE_SUFFIX)]  # data/us_stocks/AAPL
    for ym, grp in by_month.items():
        archive_path = f"{base}{LIVE_SUFFIX[:-4]}_{ym}.csv.gz"  # {code}_1m_YYYYMM.csv.gz
        append_archive(archive_path, grp)

    rewrite_live(path, keep_rows)
    return len(archive_rows), len(keep_dates)


def main():
    pattern = os.path.join(DATA_DIR, f"*{LIVE_SUFFIX}")
    paths = sorted(glob.glob(pattern))
    if not paths:
        print(f"{pattern} に対象ファイルが無い。先に収集を実行すること。")
        return

    total_archived, n_rotated = 0, 0
    for path in paths:
        try:
            archived, kept_days = rotate_one(path)
        except (OSError, csv.Error, KeyError, ValueError) as e:
            print(f"{os.path.basename(path)}: 失敗 ({type(e).__name__}: {e})")
            continue
        if archived > 0:
            n_rotated += 1
            total_archived += archived
            print(f"{os.path.basename(path)}: {archived}行をアーカイブへ退避、"
                  f"ライブ側は直近{kept_days}営業日を保持")

    print(f"完了: {len(paths)}銘柄中{n_rotated}銘柄をローテーション "
          f"(合計{total_archived}行をアーカイブへ退避、ROLLING_DAYS={ROLLING_DAYS})")


if __name__ == "__main__":
    main()
