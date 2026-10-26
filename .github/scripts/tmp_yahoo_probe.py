"""一時的な調査用・第2回 (PR を出す前に削除する)。標準ライブラリのみ。

第1回 (run 40411315557) で 9508・7240 は 2026-09-29 に上場廃止 (株式移転で持株会社の完全子会社)、
後継は 10-01 に新しいコードで上場と分かった。第2回は
  1. JPX の上場廃止・新規上場の表の生の HTML (freshness_check.py の読み取りが合っているか)
  2. freshness_check.jpx_delistings() をランナー上で実際に動かす
  3. 後継のコード (キューデンHD・NOK Group 641A) を Yahoo が1分足で返すか
  4. 後継が JPX の東証上場銘柄一覧 (data_j) に載っているか・規模区分・一覧の更新時期の書き方
  5. 米国 PSKY・WBD が Wikipedia の S&P500 の表でどうなっているか
"""
import json
import re
import socket
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

socket.setdefaulttimeout(35)
JST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
sys.path.insert(0, ".")
sys.path.insert(0, ".github/scripts")


def get(url, accept="text/html,application/json;q=0.9,*/*;q=0.8"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return None, repr(e).encode()
    finally:
        time.sleep(0.4)


def section(t):
    print(f"\n{'=' * 8} {t} {'=' * 8}", flush=True)


def raw_rows(url, keys, limit=6):
    st, body = get(url)
    html = body.decode("utf-8", "replace")
    trs = re.findall(r"(?is)<tr[^>]*>.*?</tr>", html)
    print(f"  {url}: HTTP {st} <tr>={len(trs)}")
    hits = [t for t in trs if any(k in unicodedata.normalize("NFKC", re.sub(r"<[^>]+>", "", t)) for k in keys)]
    for t in hits[:limit]:
        print("   RAW:", re.sub(r"\s+", " ", t)[:900])
    return html


def fmt(x):
    return datetime.fromtimestamp(x, JST).strftime("%Y-%m-%d %H:%M") if isinstance(x, (int, float)) else str(x)


def chart(sym, params):
    st, body = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?{params}",
                   accept="application/json")
    try:
        res = (json.loads(body).get("chart") or {}).get("result") or [None]
    except Exception:
        return f"HTTP {st} 解析不可"
    r0 = res[0] or {}
    t = r0.get("timestamp") or []
    q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    nn = [i for i, v in enumerate(q.get("close") or []) if v is not None and i < len(t)]
    vol = int(sum(v for v in (q.get("volume") or []) if v))
    m = r0.get("meta") or {}
    return (f"HTTP {st} n={len(t)} 値あり={len(nn)} {fmt(t[nn[0]]) if nn else '-'}〜{fmt(t[nn[-1]]) if nn else '-'} "
            f"出来高計={vol:,} | longName={m.get('longName')} firstTradeDate={fmt(m.get('firstTradeDate'))} "
            f"regularMarketTime={fmt(m.get('regularMarketTime'))} validRanges={m.get('validRanges')}")


def main():
    print(f"開始 {datetime.now(JST):%Y-%m-%d %H:%M} JST", flush=True)

    section("1. JPX の表の生の HTML")
    raw_rows("https://www.jpx.co.jp/listing/stocks/delisted/index.html", ["9508", "7240", "6486", "7946"])
    html_new = raw_rows("https://www.jpx.co.jp/listing/stocks/new/index.html", ["キューデン", "NOK Group", "九電"])

    section("2. freshness_check.jpx_delistings() をランナーで動かす")
    import freshness_check as fc
    res = fc.jpx_delistings({"9508", "7240", "6486", "7946", "7203"})
    print("  戻り値:", json.dumps(res, ensure_ascii=False))
    try:
        rows = fc.html_rows(fc.JPX_NEW_URL)
    except Exception as e:
        print(f"  新規上場の表を読めない: {e!r}")
        rows = []
    kyuden = [c for c in rows if any("キューデン" in unicodedata.normalize("NFKC", x) for x in c)]
    print("  新規上場の表でキューデンを含む行のセル:", kyuden[:3])
    nok = [c for c in rows if any("NOK Group" in unicodedata.normalize("NFKC", x) for x in c)]
    print("  新規上場の表で NOK Group を含む行のセル:", nok[:3])

    succ = {}
    for v in (res or {}).values():
        m = re.search(r"\b([0-9][0-9A-Z]{3})\b", v.get("successor") or "")
        if m:
            succ[m.group(1)] = v["successor"]
    for c in kyuden + nok:
        for x in c:
            if fc.CODE_RE.fullmatch(x):
                succ.setdefault(x, " ".join(c)[:60])
    print("  後継のコード:", succ)

    section("3. 後継のコードを Yahoo が返すか (比較: 旧コード)")
    for code in list(succ) + ["9508", "7240"]:
        for params in ("range=1d&interval=1m&includePrePost=false", "range=5d&interval=1m&includePrePost=false",
                       "range=2y&interval=1d&includePrePost=false"):
            print(f"  {code}.T {params.split('&')[0]} {params.split('&')[1]}: {chart(code + '.T', params)}")

    section("4. JPX の東証上場銘柄一覧 (data_j) と更新時期の書き方")
    import universe_refresh as ur
    st, body = get(ur.JPX_INDEX_URL)
    page = body.decode("utf-8", "replace")
    text = re.sub(r"\s+", " ", re.sub(r"(?s)<script.*?</script>|<[^>]+>", " ", page))
    for kw in ("data_j", "更新", "営業日", "月末", "掲載"):
        for m in list(re.finditer(re.escape(kw), text))[:3]:
            print(f"  [{kw}] …{text[max(0, m.start() - 120):m.end() + 120]}…")
    links = sorted(set(re.findall(r'href="([^"]*data_j\.xlsx?[^"]*)"', page, re.I)))
    if links and st == 200:
        raw = ur._fetch(ur.JPX_BASE + links[0] if not links[0].startswith("http") else links[0], timeout=25)
        rows_j, fmt_ = ur.read_sheet_rows(raw)
        print(f"  data_j: {links[0]} ({fmt_}, {len(rows_j)}行) 先頭: {rows_j[0][:4] if rows_j else None}")
        want = set(succ) | {"9508", "7240", "6486", "7946", "9502"}
        for r in rows_j:
            cells = [str(int(x)) if isinstance(x, float) and x == int(x) else str(x).strip() for x in r]
            if any(c in want for c in cells[:3]):
                print("   ", cells[:10])

    section("5. Wikipedia の S&P500 の表 (PSKY・WBD)")
    st, body = get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    w = body.decode("utf-8", "replace")
    print(f"  HTTP {st} {len(w)} bytes")
    for tr in re.findall(r"(?is)<tr[^>]*>.*?</tr>", w):
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", tr)).strip()
        if re.search(r"Paramount|Warner|PSKY|WBD", t):
            print("   ", t[:260])
    print("\n終了", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"途中で止まった: {e!r}")
