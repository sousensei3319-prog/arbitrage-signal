"""
日本株 1分足コレクター (実験プロトタイプ)

Yahoo Finance の非公式チャートAPI (v8/finance/chart) を標準ライブラリのみで叩き、
指定銘柄の1分足を data/jp_stocks/ 以下にCSVで蓄積する。

背景・設計:
  - このAPIの1分足は直近5〜7日分しか返らない (Yahoo側の制限)。本スクリプトを
    GitHub Actions等で定期実行し、毎回 RANGE 分を取得 → 既知タイムスタンプと
    重複しない行だけ追記することで、実行間隔を超えた連続履歴を自前で積み上げる。
  - 東証の休場日・昼休み中に実行しても新規バーが単に無いだけ
    (重複排除が休場日カレンダー代わりになるので祝日リストは持たない)。
  - query1/query2 の2ホストへフォールバック (Yahoo側が片方だけ不調な場合がある)。
  - 非公式・無認証のエンドポイントのため仕様変更や一時ブロックのリスクがある。
    本番のシグナル化に使う前に、数日〜数週間動かして安定性を見ること。

設定 (環境変数、pushイベントで空文字になるケースに備えて `or` で既定値にフォールバック):
  JP_TICKERS          Yahoo Finance形式のカンマ区切り (既定: 7203.T,6758.T,9984.T)
  RANGE                取得レンジ (既定 5d)
  INTERVAL             足種 (既定 1m)
  DATA_DIR             CSV出力先 (既定 data/jp_stocks)
  FETCH_DEADLINE_MIN   全体デッドライン分・ハング防止 (既定 5)

実行:
  python jp_stock_fetch.py
  JP_TICKERS="7203.T,6758.T" python jp_stock_fetch.py
"""

import csv
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# urlopenのtimeout引数が効かない経路 (DNS等) への保険。ジョブハング防止
socket.setdefaulttimeout(35)

JST = timezone(timedelta(hours=9))

# ============================================================
# Config (環境変数で調整可)
# ============================================================
DEFAULT_TICKERS = "7203.T,6758.T,9984.T"  # トヨタ/ソニーG/ソフトバンクG (サンプル)
TICKERS = [t.strip() for t in
           (os.environ.get("JP_TICKERS") or DEFAULT_TICKERS).split(",") if t.strip()]
RANGE        = os.environ.get("RANGE") or "5d"
INTERVAL     = os.environ.get("INTERVAL") or "1m"
DATA_DIR     = os.environ.get("DATA_DIR") or "data/jp_stocks"
DEADLINE_MIN = float(os.environ.get("FETCH_DEADLINE_MIN") or "5")

CHART_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def fetch_chart(ticker, timeout=20):
    """chart APIから (timestamp[], quote dict) を返す。両ホスト失敗で例外送出。"""
    last_err = None
    for host in CHART_HOSTS:
        url = (f"https://{host}/v8/finance/chart/{ticker}"
               f"?range={RANGE}&interval={INTERVAL}&includePrePost=false")
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            result = (data.get("chart") or {}).get("result") or []
            if not result:
                err = (data.get("chart") or {}).get("error")
                raise ValueError(f"no result (error={err})")
            r0 = result[0]
            timestamps = r0.get("timestamp") or []
            quote = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
            return timestamps, quote
        except Exception as e:
            last_err = e
            continue
    raise last_err


def existing_epochs(path):
    """CSVに既に入っているepoch秒の集合 (重複追記防止 = 休場日カレンダー代わり)。"""
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {int(row["epoch"]) for row in csv.DictReader(f) if row.get("epoch")}


def append_bars(path, ticker, timestamps, quote, known):
    opens  = quote.get("open") or []
    highs  = quote.get("high") or []
    lows   = quote.get("low") or []
    closes = quote.get("close") or []
    vols   = quote.get("volume") or []

    new_rows = []
    for i, ts in enumerate(timestamps):
        if ts in known:
            continue
        o = opens[i] if i < len(opens) else None
        h = highs[i] if i < len(highs) else None
        l = lows[i] if i < len(lows) else None
        c = closes[i] if i < len(closes) else None
        if None in (o, h, l, c):
            continue  # 板寄せ前後などの欠測バーはスキップ
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        jst = datetime.fromtimestamp(ts, tz=JST).strftime("%Y-%m-%d %H:%M:%S")
        new_rows.append((jst, ts, ticker, o, h, l, c, v))
        known.add(ts)

    if not new_rows:
        return 0
    new_rows.sort(key=lambda row: row[1])
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["timestamp_jst", "epoch", "ticker",
                        "open", "high", "low", "close", "volume"])
        w.writerows(new_rows)
    return len(new_rows)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    deadline = time.time() + DEADLINE_MIN * 60
    total_new, fail = 0, 0

    for i, ticker in enumerate(TICKERS, 1):
        if time.time() > deadline:
            print(f"⚠️ デッドライン({DEADLINE_MIN:.0f}分)超過 — "
                  f"{i - 1}/{len(TICKERS)}銘柄で打ち切り")
            break
        safe_name = ticker.replace(".", "_")
        path = os.path.join(DATA_DIR, f"{safe_name}_{INTERVAL}.csv")
        try:
            timestamps, quote = fetch_chart(ticker)
            known = existing_epochs(path)
            n = append_bars(path, ticker, timestamps, quote, known)
            total_new += n
            print(f"[{i}/{len(TICKERS)}] {ticker}: 取得{len(timestamps)}本 / 新規{n}本追記")
        except (urllib.error.URLError, urllib.error.HTTPError,
                ValueError, KeyError, TimeoutError) as e:
            fail += 1
            print(f"[{i}/{len(TICKERS)}] {ticker}: 取得失敗 ({type(e).__name__}: {e})")
        time.sleep(1)  # 連続リクエストでのブロック回避

    print(f"完了: 新規{total_new}本を{DATA_DIR}に保存 (失敗{fail}銘柄)")


if __name__ == "__main__":
    main()
