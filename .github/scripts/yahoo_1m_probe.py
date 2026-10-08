"""
Yahoo 1分足の調査 (一時的なもの・main には入れない)。

2026-10-09 の点検で、日本株の1分足が多くの日で 9:00〜9:04 の足を欠き、引け (15:30) の足の出来高が
いつも 0 だと分かった (1分足の合計は日足の出来高の6〜7割)。米国株も 16:00 の足の出来高がいつも 0 で、
9:30 の足の出来高が日によって 0 になる。原因が「Yahoo の応答に値が無い」のか「取得のしかた」なのかを、
生の応答で確かめる。発注・保存はしない (ログに要約を出すだけ)。
"""

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
JST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
TARGETS = [("7203.T", JST), ("9984.T", JST), ("1332.T", JST), ("6758.T", JST), ("8306.T", JST),
           ("AAPL", ET), ("NVDA", ET), ("F", ET), ("KO", ET)]


def fetch(ticker, rng, interval, prepost=False):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range={rng}&interval={interval}&includePrePost={'true' if prepost else 'false'}")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        res = json.loads(r.read().decode())["chart"]["result"][0]
    q = (res.get("indicators") or {}).get("quote", [{}])[0]
    return res.get("timestamp") or [], q, res.get("meta") or {}


def fmt(v):
    return "null" if v is None else (f"{v:.0f}" if isinstance(v, (int, float)) and v >= 100 else str(v))


def summarize(ticker, tz, rng, prepost=False):
    ts, q, meta = fetch(ticker, rng, "1m", prepost)
    days = {}
    for i, t in enumerate(ts):
        dt = datetime.fromtimestamp(t, tz=tz)
        o, c, v = (q.get("open") or [None])[i], (q.get("close") or [None])[i], (q.get("volume") or [None])[i]
        days.setdefault(dt.strftime("%Y-%m-%d"), []).append((dt.strftime("%H:%M:%S"), t % 60 == 0, o, c, v))
    tp = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start = datetime.fromtimestamp(tp["start"], tz=tz).strftime("%H:%M") if tp.get("start") else "?"
    end = datetime.fromtimestamp(tp["end"], tz=tz).strftime("%H:%M") if tp.get("end") else "?"
    print(f"--- {ticker} range={rng} prepost={prepost} 本数={len(ts)} 立会(meta)={start}-{end}")
    for day, bars in sorted(days.items()):
        nulls = sum(1 for b in bars if b[2] is None or b[3] is None)
        vol = sum(b[4] or 0 for b in bars)
        head = " ".join(f"{b[0][:5]}{'' if b[1] else '*'}:{fmt(b[4])}" for b in bars[:7])
        tail = " ".join(f"{b[0][:5]}{'' if b[1] else '*'}:{fmt(b[4])}" for b in bars[-3:])
        print(f"  {day} 足{len(bars)} 値なし{nulls} 出来高計{vol:.0f} | 最初 {head} | 最後 {tail}")


def daily(ticker, tz):
    ts, q, _ = fetch(ticker, "5d", "1d")
    out = []
    for i, t in enumerate(ts):
        out.append(f"{datetime.fromtimestamp(t, tz=tz).strftime('%m-%d')}:{fmt((q.get('volume') or [None])[i])}")
    print(f"--- {ticker} 日足の出来高 (5日) " + " ".join(out))


def main():
    now = datetime.now(timezone.utc)
    print(f"調査時刻 {now:%Y-%m-%d %H:%M} UTC = {now.astimezone(JST):%m-%d %H:%M} JST = {now.astimezone(ET):%m-%d %H:%M} ET")
    for ticker, tz in TARGETS:
        for rng in ("1d", "5d"):
            try:
                summarize(ticker, tz, rng)
            except Exception as e:  # 調査用なので1銘柄の失敗で止めない
                print(f"--- {ticker} range={rng} 取得失敗: {type(e).__name__}: {e}")
            time.sleep(0.6)
        try:
            daily(ticker, tz)
        except Exception as e:
            print(f"--- {ticker} 日足 取得失敗: {type(e).__name__}: {e}")
        time.sleep(0.6)
    # 寄りの足が null のままか、前後の時間帯の扱いで変わるかを1銘柄だけ見る
    try:
        summarize("7203.T", JST, "1d", prepost=True)
    except Exception as e:
        print(f"--- 7203.T prepost 取得失敗: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
