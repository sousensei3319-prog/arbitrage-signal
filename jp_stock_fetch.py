"""
日本株 1分足コレクター (実験プロトタイプ)

Yahoo Finance の非公式チャートAPI (v8/finance/chart) を標準ライブラリのみで叩き、
指定銘柄の1分足を data/jp_stocks/ 以下にCSVで蓄積する。

背景・設計:
  - このAPIの1分足は直近5〜7日分しか返らない (Yahoo側の制限)。本スクリプトを
    GitHub Actions等で定期実行し、毎回 RANGE 分を取得 → 既存CSVへ「同じ足は最新の
    取得値で上書き・新しい足は追加」(upsert) することで、実行間隔を超えた連続履歴を
    自前で積み上げる (2026-10-03 に「新規だけ追記」方式から変更 — 理由は merge_bars())。
  - 東証の休場日・昼休み中に実行しても新規バーが単に無いだけ
    (同じ足の重複が起きないので休場日カレンダー代わりになり、祝日リストは持たない)。
  - query1/query2 の2ホストへフォールバック (Yahoo側が片方だけ不調な場合がある)。
  - 非公式・無認証のエンドポイントのため仕様変更や一時ブロックのリスクがある。
    本番のシグナル化に使う前に、数日〜数週間動かして安定性を見ること。

設定 (環境変数、pushイベントで空文字になるケースに備えて `or` で既定値にフォールバック):
  JP_TICKERS          Yahoo Finance形式のカンマ区切り (既定: 7203.T,6758.T,9984.T)
  RANGE                取得レンジ (既定 5d)
  INTERVAL             足種 (既定 1m)
  DATA_DIR             CSV出力先 (既定 data/jp_stocks)
  FETCH_DEADLINE_MIN   全体デッドライン分・ハング防止 (既定 5)
  SLEEP_SEC            銘柄間の基本スリープ秒・連続リクエストブロック回避 (既定 0.4)

850銘柄化時のレート制限対策 (2026-07 追加):
  - 429(Too Many Requests)を検知したら、以降の銘柄で段階的にスリープを延長する
    (適応的バックオフ)。延長は実行全体を通じて維持し、失敗が続くほど伸ばす
    (上限あり)。1銘柄の429ごとにリトライはしない — 次回実行(30分後)の
    再取得(upsert)で自然に埋まる設計を踏襲し、ジョブ全体の遅延を避ける

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
# 監視ユニバース: code列を持つCSV (name/bucket列は任意)。多数銘柄を扱う本命の入口。
UNIVERSE_FILE = os.environ.get("UNIVERSE_FILE") or "data/jp_stocks/universe.csv"
RANGE        = os.environ.get("RANGE") or "5d"
INTERVAL     = os.environ.get("INTERVAL") or "1m"
DATA_DIR     = os.environ.get("DATA_DIR") or "data/jp_stocks"
DEADLINE_MIN = float(os.environ.get("FETCH_DEADLINE_MIN") or "5")
SLEEP_SEC    = float(os.environ.get("SLEEP_SEC") or "0.4")
BACKOFF_STEP = 2.0    # 429を検知するたびに増やすスリープ秒
BACKOFF_MAX  = 8.0    # 銘柄間スリープに上乗せする上限秒

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


HEADER = ["timestamp_jst", "epoch", "ticker", "open", "high", "low", "close", "volume"]


def bar_key(epoch, interval):
    """同じ足を指す同一性キー。1d は取引日(JST日付) — Yahooは当日足を「引け時刻」の
    仮タイムスタンプで返し、翌日以降は「寄り時刻」の正式タイムスタンプで返すため、
    epochで重複判定すると同じ日が2行になる (2026-09-24 に日足361銘柄で実測)。
    それ以外 (1m/1wk/1mo) は epoch そのもの。"""
    if interval == "1d":
        return datetime.fromtimestamp(epoch, tz=JST).strftime("%Y-%m-%d")
    return epoch


def fetched_rows(ticker, timestamps, quote, interval):
    """APIの応答を [key, [timestamp_jst, epoch, ticker, o, h, l, c, v]] のリストに整える。

    除外するもの:
      - OHLCのどれかが欠測のバー (板寄せ前後など)
      - 1m: 分頭に揃っていないタイムスタンプの点 = Yahooが末尾に付ける「現在値ティック」
        (出来高0・OHLC同値)。毎回の取得で1行ずつ溜まり、JPでは全行の約13%を占めていた
      - 1wk/1mo: 末尾の現在値点 (正式な週足/月足は現地0時の時刻で返るが、末尾に
        「その日の日足」相当の点が付き、毎日1行ずつ溜まっていた)。応答内で最も多い
        時刻と違う時刻の点を除く (新規上場銘柄で正式足が別時刻になるケースにも追随)
    """
    opens  = quote.get("open") or []
    highs  = quote.get("high") or []
    lows   = quote.get("low") or []
    closes = quote.get("close") or []
    vols   = quote.get("volume") or []

    tods = [datetime.fromtimestamp(ts, tz=JST).strftime("%H:%M:%S") if ts is not None else None
            for ts in timestamps]
    mode_tod = None
    valid = [t for t in tods if t is not None]
    if interval in ("1wk", "1mo") and len(valid) >= 5:
        top = max(set(valid), key=valid.count)
        if valid.count(top) >= 0.8 * len(valid):
            mode_tod = top

    out = []
    for i, ts in enumerate(timestamps):
        if ts is None:
            continue
        if interval == "1m" and ts % 60 != 0:
            continue
        if mode_tod is not None and tods[i] != mode_tod:
            continue
        o = opens[i] if i < len(opens) else None
        h = highs[i] if i < len(highs) else None
        l = lows[i] if i < len(lows) else None
        c = closes[i] if i < len(closes) else None
        if None in (o, h, l, c):
            continue  # 板寄せ前後などの欠測バーはスキップ
        v = vols[i] if i < len(vols) and vols[i] is not None else 0
        jst = datetime.fromtimestamp(ts, tz=JST).strftime("%Y-%m-%d %H:%M:%S")
        # 文字列化は旧append方式(csv.writerにfloat/intを渡す=str())と同じ表記にする
        out.append((bar_key(ts, interval), [jst, str(ts), ticker, str(o), str(h), str(l), str(c), str(v)]))
    return out


def merge_bars(path, ticker, timestamps, quote, interval):
    """取得結果を既存CSVへ「同じ足は最新の取得値で上書き(upsert)・新しい足は追加」で反映し、
    epoch昇順で書き直す。戻り値 (新規本数, 更新本数, 除去したゴミ行数)。

    旧方式 (既知epochは捨てて新規だけ追記) の問題 — 2026-10-03 に実データで確認:
      - 取得時点で未確定だった足 (取得の約1〜2%で最新の1分足が途中の値のまま返る) が
        二度と直らない
      - 日足は同じ日が「引け時刻の仮足」と「寄り時刻の正式足」の2行になる
      - 週足/月足は期間途中に取った正式足が凍結し (例: 祝日明けの週足が出来高0のまま)、
        末尾の現在値点が毎日1行ずつ溜まる
    既存行も読み込み時に同じ規則で掃除する (1mの現在値ティック・1dの同日重複は後勝ち)。
    """
    existing = {}
    removed = 0
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) < 8 or not row[1].lstrip("-").isdigit():
                    removed += 1
                    continue
                ep = int(row[1])
                if interval == "1m" and ep % 60 != 0:
                    removed += 1  # 過去に溜まった現在値ティック (出来高0) を掃除
                    continue
                k = bar_key(ep, interval)
                if k in existing:
                    removed += 1  # 同じ足の重複 (後から追記された方を残す)
                existing[k] = row[:8]

    n_new = n_upd = 0
    for k, row in fetched_rows(ticker, timestamps, quote, interval):
        old = existing.get(k)
        if old is None:
            n_new += 1
        elif old != row:
            n_upd += 1
        else:
            continue
        existing[k] = row

    if n_new or n_upd or removed or not os.path.exists(path):
        rows = sorted(existing.values(), key=lambda r: int(r[1]))
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)  # 既定の改行(CRLF)は既存ファイル・jp_stock_rotate.pyと同じ
            w.writerow(HEADER)
            w.writerows(rows)
        os.replace(tmp, path)
    return n_new, n_upd, removed


def load_tickers():
    """取得対象を解決: JP_TICKERS(env) > UNIVERSE_FILE(code列) > DEFAULT_TICKERS。
    コードは '7203' でも '7203.T' でも可 (どちらの入口でも .T を自動補完)。"""
    env = os.environ.get("JP_TICKERS")
    if env:
        # '7203' のように .T 無しで渡されても補完する (hot-refresh.yml の新規銘柄の初期取得は
        # universe.csv のコードをそのまま渡すため、補完が無いと全銘柄 404 で失敗していた)
        return [t if "." in t else t + ".T" for t in (x.strip() for x in env.split(",")) if t]
    if os.path.exists(UNIVERSE_FILE):
        out = []
        with open(UNIVERSE_FILE, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                code = (r.get("code") or "").strip()
                if not code:
                    continue
                out.append(code if "." in code else code + ".T")
        if out:
            print(f"ユニバース {UNIVERSE_FILE} から {len(out)}銘柄")
            return out
    return [t.strip() for t in DEFAULT_TICKERS.split(",") if t.strip()]


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    tickers = load_tickers()
    deadline = time.time() + DEADLINE_MIN * 60
    total_new, total_upd, total_rm, fail, n_429 = 0, 0, 0, 0, 0
    backoff = 0.0  # 429を検知するたびに伸びる追加スリープ (実行全体で維持・上限あり)
    t0 = time.time()

    for i, ticker in enumerate(tickers, 1):
        if time.time() > deadline:
            print(f"⚠️ デッドライン({DEADLINE_MIN:.0f}分)超過 — "
                  f"{i - 1}/{len(tickers)}銘柄で打ち切り")
            break
        safe_name = ticker.replace(".", "_")
        path = os.path.join(DATA_DIR, f"{safe_name}_{INTERVAL}.csv")
        try:
            timestamps, quote = fetch_chart(ticker)
            n, u, rm = merge_bars(path, ticker, timestamps, quote, INTERVAL)
            total_new += n; total_upd += u; total_rm += rm
            print(f"[{i}/{len(tickers)}] {ticker}: 取得{len(timestamps)}本 / 新規{n}本・更新{u}本"
                  + (f"・不要行除去{rm}本" if rm else ""))
        except urllib.error.HTTPError as e:
            fail += 1
            if e.code == 429:
                n_429 += 1
                backoff = min(backoff + BACKOFF_STEP, BACKOFF_MAX)
                print(f"[{i}/{len(tickers)}] {ticker}: 429 rate limited — "
                      f"以降のスリープを+{backoff:.1f}sに延長 (リトライはせず次回実行で回収)")
            else:
                print(f"[{i}/{len(tickers)}] {ticker}: 取得失敗 (HTTPError: {e})")
        except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as e:
            fail += 1
            print(f"[{i}/{len(tickers)}] {ticker}: 取得失敗 ({type(e).__name__}: {e})")
        time.sleep(SLEEP_SEC + backoff)  # 連続リクエストでのブロック回避 (429検知時は延長)

    elapsed = time.time() - t0
    print(f"完了: 新規{total_new}本・更新{total_upd}本・不要行除去{total_rm}本を{DATA_DIR}に反映 "
          f"(INTERVAL={INTERVAL}, 失敗{fail}銘柄, うち429={n_429}件, "
          f"所要{elapsed:.0f}秒, 対象{len(tickers)}銘柄)")


if __name__ == "__main__":
    main()
