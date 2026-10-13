"""
戦略Aの検証に使えるデータが、GitHub Actions のランナーから取れるかを調べる (一時的なもの・main には入れない)。
発注・保存はしない。各データ源について、届くか・どこまで過去にさかのぼれるか・1回の応答の件数と時間をログに出す。
"""
import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

socket.setdefaulttimeout(30)
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def get(url, data=None, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read().decode()
            return r.status, body, time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, (e.read().decode(errors="replace")[:200] if e.fp else ""), time.time() - t0
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}", time.time() - t0


def ts(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def report(name, status, body, sec, parse):
    try:
        info = parse(json.loads(body)) if status == 200 else body[:160]
    except Exception as e:
        info = f"解析失敗 {type(e).__name__}: {body[:120]}"
    print(f"[{name}] HTTP {status} {sec:.1f}秒 | {info}")


def main():
    print("調査時刻", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    # OKX: 日足 (古い方へさかのぼる)
    after = ""
    oldest = None; n = 0
    for _ in range(12):
        url = "https://www.okx.com/api/v5/market/history-candles?instId=ETH-USDT-SWAP&bar=1Dutc&limit=100" + (f"&after={after}" if after else "")
        st, body, sec = get(url)
        if st != 200:
            print("[OKX 日足] HTTP", st, body[:120]); break
        d = json.loads(body).get("data") or []
        if not d:
            break
        n += len(d); oldest = d[-1][0]; after = d[-1][0]
        time.sleep(0.15)
    print(f"[OKX 日足 ETH-USDT-SWAP] {n}本 最古 {ts(oldest) if oldest else '-'}")
    st, body, sec = get("https://www.okx.com/api/v5/public/funding-rate-history?instId=ETH-USDT-SWAP&limit=100")
    report("OKX FR履歴 (最新100)", st, body, sec, lambda j: f"{len(j['data'])}件 最古 {ts(j['data'][-1]['fundingTime'])} 最新 {ts(j['data'][0]['fundingTime'])}")
    st, body, sec = get("https://www.okx.com/api/v5/public/funding-rate-history?instId=ETH-USDT-SWAP&limit=100&after=1730000000000")
    report("OKX FR履歴 (2024-10 より前)", st, body, sec, lambda j: f"{len(j['data'])}件" + (f" 最古 {ts(j['data'][-1]['fundingTime'])}" if j['data'] else ""))
    for period in ("1D", "1H"):
        st, body, sec = get(f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=ETH&period={period}")
        report(f"OKX L/S {period}", st, body, sec, lambda j: f"{len(j['data'])}件 最古 {ts(j['data'][-1][0])} 最新 {ts(j['data'][0][0])}")
    st, body, sec = get("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio-contract?instId=ETH-USDT-SWAP&period=1D&limit=100")
    report("OKX L/S (contract) 1D", st, body, sec, lambda j: f"{len(j['data'])}件" + (f" 最古 {ts(j['data'][-1][0])}" if j['data'] else ""))
    st, body, sec = get("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio-contract?instId=ETH-USDT-SWAP&period=1D&limit=100&end=1720000000000")
    report("OKX L/S (contract) 1D 2024-07 以前", st, body, sec, lambda j: f"{len(j['data'])}件" + (f" 最古 {ts(j['data'][-1][0])}" if j['data'] else ""))
    st, body, sec = get("https://www.okx.com/api/v5/public/instruments?instType=SWAP")
    report("OKX 銘柄一覧 SWAP", st, body, sec, lambda j: f"{sum(1 for x in j['data'] if x['instId'].endswith('-USDT-SWAP'))} USDT無期限")
    # Hyperliquid: FR 履歴 (1時間ごと)
    for start in ("2023-06-01", "2024-06-01"):
        ms = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        st, body, sec = get("https://api.hyperliquid.xyz/info", data=json.dumps({"type": "fundingHistory", "coin": "ETH", "startTime": ms}).encode(),
                            headers={"Content-Type": "application/json"})
        report(f"HL FR履歴 ETH {start}〜", st, body, sec, lambda j: f"{len(j)}件 {ts(j[0]['time'])}〜{ts(j[-1]['time'])} 例 {j[0]['fundingRate']}")
    st, body, sec = get("https://api.hyperliquid.xyz/info", data=json.dumps({"type": "meta"}).encode(), headers={"Content-Type": "application/json"})
    report("HL meta", st, body, sec, lambda j: f"{len(j['universe'])}銘柄 (うち上場廃止 {sum(1 for u in j['universe'] if u.get('isDelisted'))})")
    # Kraken Futures: FR 履歴 (1回で全期間)
    st, body, sec = get("https://futures.kraken.com/derivatives/api/v4/historicalfundingrates?symbol=PF_ETHUSD")
    report("Kraken Futures FR履歴 PF_ETHUSD", st, body, sec, lambda j: f"{len(j['rates'])}件 {j['rates'][0]['timestamp']}〜{j['rates'][-1]['timestamp']}")
    st, body, sec = get("https://futures.kraken.com/derivatives/api/v3/instruments")
    report("Kraken Futures 銘柄一覧", st, body, sec, lambda j: f"{sum(1 for x in j['instruments'] if str(x.get('symbol','')).startswith('PF_'))} PF_ 無期限")
    # 参考: 届かないと分かっている所 (変わっていないかの確認)
    for name, url in (("Bybit FR", "https://api.bybit.com/v5/market/funding/history?category=linear&symbol=ETHUSDT&limit=200"),
                      ("Binance FR", "https://fapi.binance.com/fapi/v1/fundingRate?symbol=ETHUSDT&limit=1000"),
                      ("FGI", "https://api.alternative.me/fng/?limit=2")):
        st, body, sec = get(url)
        print(f"[{name}] HTTP {st} {sec:.1f}秒 {body[:100] if st != 200 else '取得可'}")


if __name__ == "__main__":
    main()
