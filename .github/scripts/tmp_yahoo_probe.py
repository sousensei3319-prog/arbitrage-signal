"""一時的な調査用 (PR を出す前に削除する)。標準ライブラリのみ。

1分足が取れない銘柄 (日本 9508/7240/7946・米国 PSKY/WBD) について、Yahoo の別の取り方
(ホスト・期間・足の長さ・パラメータ・spark・検索・認証付き quote) と、Yahoo!ファイナンス日本版・
JPX の上場廃止/整理銘柄の一覧を、同じ業種の正常な銘柄と比べて出力する。
サンドボックスからは Yahoo/JPX に届かない (proxy 403) ので、Actions のランナーで動かす。
"""
import http.cookiejar
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

socket.setdefaulttimeout(35)
JST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DEADLINE = time.time() + 14 * 60
H1, H2 = "query1.finance.yahoo.com", "query2.finance.yahoo.com"


def get(url, opener=None, accept="application/json,text/html;q=0.9,*/*;q=0.8", timeout=25):
    """(status, body bytes, final url)。例外は status=None で返す (調査を止めない)。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        f = opener.open(req, timeout=timeout) if opener else urllib.request.urlopen(req, timeout=timeout)
        with f as r:
            return r.status, r.read(), r.geturl()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body, url
    except Exception as e:
        return None, repr(e).encode(), url
    finally:
        time.sleep(0.35)


def fmt_ts(x, tz):
    return datetime.fromtimestamp(x, tz).strftime("%Y-%m-%d %H:%M") if isinstance(x, (int, float)) else str(x)


def chart(host, sym, params, tz, opener=None):
    url = f"https://{host}/v8/finance/chart/{urllib.parse.quote(sym)}?{params}"
    st, body, _ = get(url, opener=opener)
    out = {"st": st}
    try:
        d = json.loads(body)
    except Exception:
        out["err"] = body[:100]
        return out
    c = d.get("chart") or {}
    if c.get("error"):
        out["err"] = c["error"]
    res = (c.get("result") or [None])[0]
    if not res:
        return out
    t = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = q.get("close") or []
    vols = q.get("volume") or []
    nn = [i for i, v in enumerate(closes) if v is not None and i < len(t)]
    out.update(n=len(t), nn=len(nn),
               first=fmt_ts(t[0], tz) if t else "-",
               last=fmt_ts(t[nn[-1]], tz) if nn else "-",
               vol=int(sum(v for v in vols if v)),
               meta=res.get("meta") or {})
    return out


META_KEYS = ("symbol", "exchangeName", "fullExchangeName", "instrumentType", "currency", "firstTradeDate",
             "regularMarketTime", "regularMarketPrice", "regularMarketVolume", "chartPreviousClose",
             "previousClose", "dataGranularity", "range", "validRanges", "hasPrePostMarketData",
             "longName", "shortName", "exchangeTimezoneName", "priceHint")


def meta_line(m, tz):
    parts = []
    for k in META_KEYS:
        if k not in m:
            continue
        v = m[k]
        if k in ("firstTradeDate", "regularMarketTime"):
            v = fmt_ts(v, tz)
        parts.append(f"{k}={v}")
    ctp = (m.get("currentTradingPeriod") or {}).get("regular") or {}
    if ctp:
        parts.append(f"regular={fmt_ts(ctp.get('start'), tz)}〜{fmt_ts(ctp.get('end'), tz)}")
    return " ".join(parts)


def row(label, r):
    if "n" in r:
        s = f"n={r['n']:<5} 値あり={r['nn']:<5} {r['first']} 〜 {r['last']}  出来高計={r['vol']:,}"
    else:
        s = f"結果なし err={r.get('err')}"
    print(f"  {label:<38} HTTP {r['st']}  {s}")


def section(t):
    print(f"\n{'=' * 8} {t} {'=' * 8}", flush=True)


def probe_chart(sym, tz, full, opener=None, crumb=""):
    now = int(time.time())
    p7 = f"period1={now - 7 * 86400}&period2={now}"
    variants = [
        ("q1 1m range=1d (今の取り方)", H1, "range=1d&interval=1m&includePrePost=false"),
        ("q2 1m range=1d", H2, "range=1d&interval=1m&includePrePost=false"),
        ("q1 1m range=5d (引け後の取り直し)", H1, "range=5d&interval=1m&includePrePost=false"),
        ("q2 1m range=5d", H2, "range=5d&interval=1m&includePrePost=false"),
        ("q1 1d range=2y", H1, "range=2y&interval=1d&includePrePost=false"),
        ("q1 1wk range=5y", H1, "range=5y&interval=1wk&includePrePost=false"),
        ("q1 1mo range=max", H1, "range=max&interval=1mo&includePrePost=false"),
    ]
    if full:
        variants += [
            ("q1 1m range=1d (2回目)", H1, "range=1d&interval=1m&includePrePost=false"),
            ("q2 1m range=1d (2回目)", H2, "range=1d&interval=1m&includePrePost=false"),
            ("q1 1m period1/2 (7日)", H1, f"{p7}&interval=1m&includePrePost=false"),
            ("q2 1m period1/2 (7日)", H2, f"{p7}&interval=1m&includePrePost=false"),
            ("q1 1m range=1d includePrePost=true", H1, "range=1d&interval=1m&includePrePost=true"),
            ("q1 1m range=7d", H1, "range=7d&interval=1m"),
            ("q1 2m range=5d", H1, "range=5d&interval=2m"),
            ("q1 5m range=5d", H1, "range=5d&interval=5m"),
            ("q1 15m range=1mo", H1, "range=1mo&interval=15m"),
            ("q1 30m range=1mo", H1, "range=1mo&interval=30m"),
            ("q1 60m range=1mo", H1, "range=1mo&interval=60m"),
            ("q1 90m range=1mo", H1, "range=1mo&interval=90m"),
            ("q1 1d range=5d", H1, "range=5d&interval=1d"),
            ("q2 1d range=2y", H2, "range=2y&interval=1d"),
            ("q1 1d range=max", H1, "range=max&interval=1d"),
            ("q1 1d period1=0", H1, f"period1=0&period2={now}&interval=1d"),
            ("q1 1m ja-JP/JP", H1, "range=1d&interval=1m&lang=ja-JP&region=JP"),
            ("q1 1m サイトと同じ引数", H1,
             "range=1d&interval=1m&includePrePost=true&events=div%7Csplit%7Cearn&lang=en-US&region=US&source=cosaic"),
            ("q2 1m yfinance と同じ引数", H2,
             "range=5d&interval=1m&includePrePost=False&events=div%2Csplits%2CcapitalGains"),
        ]
        if opener is not None and crumb:
            variants += [("q2 1m range=1d cookie+crumb", H2,
                          f"range=1d&interval=1m&crumb={urllib.parse.quote(crumb)}")]
    first_meta = {}
    for label, host, params in variants:
        if time.time() > DEADLINE:
            print("  (締め切り)")
            break
        r = chart(host, sym, params, tz, opener=opener if "crumb" in label else None)
        row(label, r)
        if r.get("meta") and label.split(" (")[0] in ("q1 1m range=1d", "q1 1d range=2y", "q1 1mo range=max"):
            first_meta[label] = r["meta"]
    for label, m in first_meta.items():
        print(f"  meta[{label}]: {meta_line(m, tz)}")


def spark(symbols, tz):
    for host in (H1, H2):
        for ver in ("v7", "v8"):
            url = f"https://{host}/{ver}/finance/spark?symbols={','.join(symbols)}&range=1d&interval=1m"
            st, body, _ = get(url)
            try:
                d = json.loads(body)
            except Exception:
                print(f"  {host} {ver}: HTTP {st} 解析不可 {body[:80]!r}")
                continue
            res = []
            if isinstance(d.get("spark"), dict):
                for x in d["spark"].get("result") or []:
                    resp = (x.get("response") or [{}])[0]
                    res.append(f"{x.get('symbol')}: n={len(resp.get('timestamp') or [])}")
            else:
                for k, v in d.items():
                    if isinstance(v, dict):
                        res.append(f"{k}: n={len(v.get('timestamp') or [])}")
            print(f"  {host} {ver}: HTTP {st} " + (" / ".join(res) or str(d)[:160]))


def search(q):
    url = (f"https://{H2}/v1/finance/search?q={urllib.parse.quote(q)}"
           "&quotesCount=8&newsCount=0&listsCount=0&enableFuzzyQuery=false")
    st, body, _ = get(url)
    try:
        quotes = json.loads(body).get("quotes") or []
    except Exception:
        print(f"  search {q!r}: HTTP {st} 解析不可")
        return
    print(f"  search {q!r}: HTTP {st} {len(quotes)}件")
    for x in quotes:
        print("     ", {k: x.get(k) for k in ("symbol", "exchange", "exchDisp", "quoteType", "typeDisp",
                                               "shortname", "longname", "isYahooFinance")})


def crumb_session():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    for u in ("https://fc.yahoo.com/", "https://finance.yahoo.com/"):
        st, _, _ = get(u, opener=op, accept="text/html")
        print(f"  cookie取得 {u}: HTTP {st} cookie数={len(cj)}")
        if len(cj):
            break
    st, body, _ = get(f"https://{H1}/v1/test/getcrumb", opener=op, accept="text/plain")
    crumb = body.decode("utf-8", "replace").strip() if st == 200 else ""
    print(f"  getcrumb: HTTP {st} crumb={'あり' if crumb else 'なし'} ({body[:40]!r})")
    return op, crumb


QUOTE_KEYS = ("symbol", "quoteType", "marketState", "exchange", "fullExchangeName", "quoteSourceName",
              "exchangeDataDelayedBy", "sourceInterval", "regularMarketTime", "regularMarketPrice",
              "regularMarketVolume", "tradeable", "longName", "shortName", "firstTradeDateMilliseconds",
              "messageBoardId", "averageDailyVolume10Day")


def quote(op, crumb, symbols, tz):
    if not crumb:
        print("  crumb が無いので quote は飛ばす")
        return
    url = f"https://{H1}/v7/finance/quote?symbols={','.join(symbols)}&crumb={urllib.parse.quote(crumb)}"
    st, body, _ = get(url, opener=op)
    try:
        res = (json.loads(body).get("quoteResponse") or {}).get("result") or []
    except Exception:
        print(f"  quote: HTTP {st} 解析不可 {body[:100]!r}")
        return
    print(f"  quote: HTTP {st} {len(res)}件")
    for x in res:
        d = {k: x.get(k) for k in QUOTE_KEYS if k in x}
        if "regularMarketTime" in d:
            d["regularMarketTime"] = fmt_ts(d["regularMarketTime"], tz)
        print("     ", d)
    for s in symbols[:3]:
        url = (f"https://{H1}/v10/finance/quoteSummary/{urllib.parse.quote(s)}"
               f"?modules=quoteType,price&crumb={urllib.parse.quote(crumb)}")
        st, body, _ = get(url, opener=op)
        try:
            r0 = ((json.loads(body).get("quoteSummary") or {}).get("result") or [{}])[0]
        except Exception:
            print(f"  quoteSummary {s}: HTTP {st} 解析不可")
            continue
        qt, pr = r0.get("quoteType") or {}, r0.get("price") or {}
        print(f"  quoteSummary {s}: HTTP {st} quoteType=" +
              str({k: qt.get(k) for k in ("exchange", "quoteType", "shortName", "longName",
                                          "firstTradeDateEpochUtc", "timeZoneFullName", "messageBoardId")}) +
              " price=" + str({k: pr.get(k) for k in ("quoteSourceName", "regularMarketSource",
                                                      "exchangeDataDelayedBy", "marketState")}))


def text_rows(html):
    rows = re.split(r"(?i)<tr[\s>]", html)
    out = []
    for r in rows:
        t = re.sub(r"<[^>]+>", " ", r)
        t = re.sub(r"&nbsp;|&#160;", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            out.append(t)
    return out


def jpx_pages(keys):
    pages = [
        ("上場廃止銘柄一覧", "https://www.jpx.co.jp/listing/stocks/delisted/index.html"),
        ("監理銘柄・整理銘柄", "https://www.jpx.co.jp/listing/market-alerts/supervision/index.html"),
        ("新規上場", "https://www.jpx.co.jp/listing/stocks/new/index.html"),
    ]
    for name, url in pages:
        st, body, fin = get(url, accept="text/html")
        html = body.decode("utf-8", "replace")
        rows = text_rows(html)
        hits = [r for r in rows if any(k in r for k in keys)]
        print(f"  {name}: HTTP {st} 行数={len(rows)} 該当={len(hits)} ({fin})")
        for r in hits[:8]:
            print("     ", r[:220])
        if st == 200 and rows:
            print("      先頭の行:", " | ".join(x[:80] for x in rows[1:4]))


def yahoo_jp(codes):
    for c in codes:
        st, body, fin = get(f"https://finance.yahoo.co.jp/quote/{c}.T", accept="text/html")
        html = body.decode("utf-8", "replace")
        title = re.search(r"<title>(.*?)</title>", html, re.S)
        marks = [m for m in ("上場廃止", "整理銘柄", "監理銘柄", "売買停止", "株式併合", "公開買付", "TOB",
                             "持株会社", "商号変更", "取引停止", "ページが見つかりません")
                 if m in html]
        print(f"  {c}.T: HTTP {st} {fin}\n      title={title.group(1).strip()[:90] if title else None} 目印={marks}")
        m = re.search(r"__PRELOADED_STATE__\s*=\s*", html)
        if not m:
            continue
        try:
            state, _ = json.JSONDecoder().raw_decode(html[m.end():])
        except Exception as e:
            print(f"      PRELOADED_STATE 解析不可 ({type(e).__name__})")
            continue
        print("      state のキー:", list(state)[:25])
        found = []

        def walk(o, path, depth):
            if len(found) >= 45 or depth > 5:
                return
            if isinstance(o, dict):
                for k, v in o.items():
                    p = f"{path}.{k}" if path else k
                    if isinstance(v, (dict, list)):
                        walk(v, p, depth + 1)
                    elif re.search(r"(?i)(time|date|price|status|delist|halt|stop|market|name|code)", k):
                        found.append(f"{p}={str(v)[:50]}")
            elif isinstance(o, list) and o:
                walk(o[0], path + "[0]", depth + 1)

        for k in state:
            if re.search(r"(?i)(price|board|stock|quote|detail|chart|timeseries)", k):
                walk(state[k], k, 0)
        for f in found:
            print("       ", f)
        urls = sorted(set(re.findall(r"https?://[a-zA-Z0-9.-]*yahoo[^\"'\s<>]*(?:chart|history|timeseries)[^\"'\s<>]*",
                                     html)))[:12]
        for u in urls:
            print("      url:", u[:160])


def jpx_listed(codes):
    sys.path.insert(0, ".")
    try:
        import universe_refresh as ur
    except Exception as e:
        print(f"  universe_refresh を読めない: {e}")
        return
    st, body, _ = get(ur.JPX_INDEX_URL, accept="text/html")
    html = body.decode("utf-8", "replace")
    links = sorted(set(re.findall(r'href="([^"]*data_j\.xlsx?[^"]*)"', html, re.I)))
    dates = sorted(set(re.findall(r"\d{4}年\d{1,2}月(?:\d{1,2}日|末)", html)))[:8]
    print(f"  JPX 一覧ページ: HTTP {st} data_j のリンク={links} 日付らしき文字={dates}")
    topix, sector, names = ur.fetch_jpx_listed()
    for c in codes:
        print(f"  {c}: 一覧に{'ある' if c in names else '無い'} 名前={names.get(c)} 業種={sector.get(c)} TOPIX500={'○' if c in topix else '×'}")


def main():
    jp_bad, jp_ok = ["9508.T", "7240.T", "7946.T"], ["9502.T", "9503.T", "7203.T"]
    us_bad, us_ok = ["PSKY", "WBD"], ["AAPL", "DIS"]
    print(f"開始 {datetime.now(JST):%Y-%m-%d %H:%M} JST / {datetime.now(ET):%Y-%m-%d %H:%M} ET", flush=True)

    section("認証付きの取り方 (cookie + crumb) の準備")
    op, crumb = crumb_session()

    for s in jp_bad:
        section(f"日本 {s} (止まっている銘柄・全パターン)")
        probe_chart(s, JST, True, op, crumb)
    for s in jp_ok:
        section(f"日本 {s} (比較用の正常な銘柄)")
        probe_chart(s, JST, False)
    for s in us_bad:
        section(f"米国 {s} (止まっている銘柄)")
        probe_chart(s, ET, True, op, crumb)
    for s in us_ok:
        section(f"米国 {s} (比較用)")
        probe_chart(s, ET, False)

    section("spark (複数銘柄の簡易チャート)")
    spark(jp_bad[:2] + ["9502.T"], JST)
    spark(us_bad + ["AAPL"], ET)

    section("検索 (別の銘柄コードに移っていないか)")
    for q in ("9508", "9508.T", "Kyushu Electric", "九州電力", "7240", "7240.T", "NOK Corporation",
              "7946", "PSKY", "Paramount Skydance", "WBD", "Warner Bros. Discovery"):
        search(q)

    section("quote / quoteSummary (cookie + crumb)")
    quote(op, crumb, jp_bad + ["9502.T"], JST)
    quote(op, crumb, us_bad + ["AAPL"], ET)

    section("Yahoo!ファイナンス日本版の銘柄ページ")
    yahoo_jp(["9508", "7240", "7946", "9502"])

    section("JPX の一覧 (上場廃止・監理/整理・新規上場)")
    jpx_pages(["7240", "9508", "7946", "ＮＯＫ", "NOK", "光陽社", "九州電力", "九電"])

    section("JPX 東証上場銘柄一覧 (data_j) に載っているか")
    jpx_listed(["7240", "9508", "7946", "9502"])
    print("\n終了", flush=True)


if __name__ == "__main__":
    main()
