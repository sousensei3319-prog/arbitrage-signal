"""話題株ランキング取得源の偵察スクリプト (一時的・検証後に削除)。

日本株の「話題(値上がり率・出来高・売買代金)」ランキングを、GitHub Actionsランナーから
安定してスクレイピングできるサイトを見極めるための偵察。サンドボックス(Claude Code)からは
日本の金融サイトへproxy403で到達できないため、ランナー上の実行結果でのみ判断できる。

各候補URLに対して: HTTPステータス / バイト長 / 4文字証券コード様トークンの検出数 /
抽出できた (コード,名前) サンプルを表示する。標準ライブラリのみ。
"""
import re
import ssl
import sys
import urllib.request
import urllib.error

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# 証券コード: 4文字・先頭数字 (285A等の新コード体系に対応)
CODE_RE = re.compile(r'\b([0-9][0-9A-Za-z]{3})\b')

CANDIDATES = [
    ("Yahoo JP 売買代金", "https://finance.yahoo.co.jp/stocks/ranking/tradingValue?market=all&term=daily"),
    ("Yahoo JP 出来高",   "https://finance.yahoo.co.jp/stocks/ranking/volume?market=all&term=daily"),
    ("Yahoo JP 値上がり率", "https://finance.yahoo.co.jp/stocks/ranking/up?market=all&term=daily"),
    ("kabutan 値上がり率", "https://kabutan.jp/warning/?mode=2_1"),
    ("kabutan 出来高",    "https://kabutan.jp/warning/?mode=3_1"),
    ("kabutan 売買代金",  "https://kabutan.jp/warning/?mode=4_1"),
    ("minkabu 人気",      "https://minkabu.jp/stock/ranking/pv"),
    ("minkabu 値上がり",   "https://minkabu.jp/stock/ranking/rise"),
]


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or "utf-8"
        return r.status, raw.decode(enc, errors="replace")


def main():
    out = []
    for label, url in CANDIDATES:
        out.append(f"\n===== {label} =====\n{url}")
        try:
            status, html = fetch(url)
        except urllib.error.HTTPError as e:
            out.append(f"  HTTPError {e.code}")
            continue
        except Exception as e:  # noqa: BLE001  偵察なので全例外を握って次へ
            out.append(f"  ERROR {type(e).__name__}: {e}")
            continue
        codes = CODE_RE.findall(html)
        # 4桁数字コード(証券コードらしいもの)だけ集計
        stock_like = [c for c in codes if c[0].isdigit()]
        uniq = []
        seen = set()
        for c in stock_like:
            if c not in seen:
                seen.add(c); uniq.append(c)
        out.append(f"  status={status} bytes={len(html)} コード様トークン={len(stock_like)} ユニーク={len(uniq)}")
        out.append(f"  先頭ユニークコード20: {uniq[:20]}")
        # コードの直後にリンクや名前が続くパターンを軽く探る
        m = re.findall(r'/(\d[0-9A-Za-z]{3})/?[^>]*>\s*([^<\s][^<]{0,20})', html)
        if m:
            out.append(f"  (code,近傍文字列)サンプル: {m[:8]}")
    text = "\n".join(out)
    print(text)
    with open("probe_result.txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")


if __name__ == "__main__":
    main()
