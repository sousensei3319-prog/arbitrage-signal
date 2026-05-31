"""
Cross-Exchange Arbitrage SIGNAL Screener v1 (①価格スプレッド検知)

これは「実行ボット」ではなく「歪み(価格スプレッド)を検知してDiscordに通知する
シグナルツール」です。retailのレイテンシでは同時約定の実行ボットは勝てないため
(調査結論)、まずは"取れるスプレッドが本当に発生しているか"を可視化・記録します。

毎回の実行でやること:
  1. 複数取引所(OKX/Bybit/Gate/MEXC/KuCoin/Bitget)の現物 USDT ペアの
     best bid / ask を一括取得。Binance.com は GH Actions(US) でジオブロックのため除外。
  2. 2取引所以上に存在する共通シンボルで、生スプレッド%を計算:
        buy @ 最安ask の取引所, sell @ 最高bid の取引所
  3. 手数料(往復taker)を引いた net スプレッド% でフィルタ。
  4. ★板厚ゲート★ 上位候補だけ両脚の板を取得し、想定ロット($)を約定したときの
     スリッページ込みの「実際に取れるスプレッド%」を計算。取れない罠を除外。
  5. 生き残りを Discord に日本語通知(@everyone + 表)。

CLAUDE.md ルール: 出力には必ず集計期間(スキャン時刻)を明記する。
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Windows コンソール(cp932)で絵文字/日本語ログがクラッシュしないよう UTF-8 に固定
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

JST = timezone(timedelta(hours=9))

# ============================================================
# Config (env で上書き可)
# ============================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MENTION_EVERYONE = os.environ.get("MENTION_EVERYONE", "0") == "1"

# 生スプレッドの一次足切り (%). これ未満は板厚計算もしない (ノイズ)
RAW_SPREAD_MIN = float(os.environ.get("RAW_SPREAD_MIN", "0.4"))
# 板厚込みの最終点灯閾値 (%). 調査結論: 0.4〜0.7%以上ないと手数料に消える
NET_SPREAD_MIN = float(os.environ.get("NET_SPREAD_MIN", "0.5"))
# 上限スプレッド (%). これを超える鞘は「同名別トークン(シンボル衝突)」「上場停止」
# 「枯れ板の古い気配」のほぼ確実な罠。現実のCEX間鞘は通常5%未満なので25%で足切り。
MAX_SPREAD = float(os.environ.get("MAX_SPREAD", "25"))
# 板厚計算で想定する片道ロット (USDT). "本当にこの額を約定できるか" を検証
TARGET_NOTIONAL = float(os.environ.get("TARGET_NOTIONAL", "200"))
# 24h出来高フィルタ (USDT). 罠銘柄(スカスカ板)の一次除外
MIN_VOLUME_24H = float(os.environ.get("MIN_VOLUME_24H", "1000000"))
# Discord に送る最大件数
MAX_SIGNALS = int(os.environ.get("MAX_SIGNALS", "12"))
# 安定 / ステーブル系は除外 (鞘がほぼ手数料に消える & 罠が多い)
EXCLUDE_BASES = {"USDC", "DAI", "TUSD", "FDUSD", "USDD", "EUR", "USDE", "BUSD", "PYUSD"}


# ============================================================
# HTTP
# ============================================================
def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "arb-signal/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def safe_float(x, default=None):
    try:
        v = float(x)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# ============================================================
# Exchange adapters
#   各取引所:
#     name      : 表示名
#     taker_fee : 片道 taker 手数料 (率). 往復は ×2 で使う
#     book()    : {base: {"bid": float, "ask": float, "vol": usd24h}} を返す
#     depth(base): {"bids":[[p,sz],...], "asks":[[p,sz],...]} or None
#   ジオブロック等で失敗したら例外を投げ、main側で「その取引所だけスキップ」。
# ============================================================
class Exchange:
    name = "base"
    taker_fee = 0.001  # 0.1% デフォルト

    def book(self):
        raise NotImplementedError

    def depth(self, base):
        raise NotImplementedError

    @staticmethod
    def _norm(base):
        return base.upper()


class OKX(Exchange):
    name = "OKX"
    taker_fee = 0.001
    API = "https://www.okx.com/api/v5"

    def book(self):
        d = fetch_json(f"{self.API}/market/tickers?instType=SPOT")["data"]
        out = {}
        for t in d:
            inst = t["instId"]
            if not inst.endswith("-USDT"):
                continue
            base = inst[:-5]
            bid = safe_float(t.get("bidPx")); ask = safe_float(t.get("askPx"))
            if not bid or not ask:
                continue
            last = safe_float(t.get("last"), 0) or 0
            vol = (safe_float(t.get("volCcy24h"), 0) or 0)  # 既に通貨(USDT)建てに近い
            out[base] = {"bid": bid, "ask": ask, "vol": vol if vol else last}
        return out

    def depth(self, base):
        d = fetch_json(f"{self.API}/market/books?instId={base}-USDT&sz=50")["data"]
        if not d:
            return None
        b = d[0]
        return {"bids": [[float(x[0]), float(x[1])] for x in b["bids"]],
                "asks": [[float(x[0]), float(x[1])] for x in b["asks"]]}


class Bybit(Exchange):
    name = "Bybit"
    taker_fee = 0.001
    API = "https://api.bybit.com/v5"

    def book(self):
        d = fetch_json(f"{self.API}/market/tickers?category=spot")["result"]["list"]
        out = {}
        for t in d:
            sym = t["symbol"]
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            bid = safe_float(t.get("bid1Price")); ask = safe_float(t.get("ask1Price"))
            if not bid or not ask:
                continue
            turn = safe_float(t.get("turnover24h"), 0) or 0
            out[base] = {"bid": bid, "ask": ask, "vol": turn}
        return out

    def depth(self, base):
        d = fetch_json(f"{self.API}/market/orderbook?category=spot&symbol={base}USDT&limit=50")["result"]
        if not d or not d.get("a"):
            return None
        return {"bids": [[float(x[0]), float(x[1])] for x in d["b"]],
                "asks": [[float(x[0]), float(x[1])] for x in d["a"]]}


class Gate(Exchange):
    name = "Gate"
    taker_fee = 0.001
    API = "https://api.gateio.ws/api/v4"

    def book(self):
        d = fetch_json(f"{self.API}/spot/tickers")
        out = {}
        for t in d:
            pair = t.get("currency_pair", "")
            if not pair.endswith("_USDT"):
                continue
            base = pair[:-5]
            bid = safe_float(t.get("highest_bid")); ask = safe_float(t.get("lowest_ask"))
            if not bid or not ask:
                continue
            vol = safe_float(t.get("quote_volume"), 0) or 0
            out[base] = {"bid": bid, "ask": ask, "vol": vol}
        return out

    def depth(self, base):
        d = fetch_json(f"{self.API}/spot/order_book?currency_pair={base}_USDT&limit=50")
        if not d or not d.get("asks"):
            return None
        return {"bids": [[float(x[0]), float(x[1])] for x in d["bids"]],
                "asks": [[float(x[0]), float(x[1])] for x in d["asks"]]}


class MEXC(Exchange):
    name = "MEXC"
    taker_fee = 0.0005  # MEXC現物takerは低め
    API = "https://api.mexc.com/api/v3"

    def book(self):
        book = fetch_json(f"{self.API}/ticker/bookTicker")
        vol_map = {}
        try:
            for t in fetch_json(f"{self.API}/ticker/24hr"):
                vol_map[t["symbol"]] = safe_float(t.get("quoteVolume"), 0) or 0
        except Exception:
            pass
        out = {}
        for t in book:
            sym = t["symbol"]
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            bid = safe_float(t.get("bidPrice")); ask = safe_float(t.get("askPrice"))
            if not bid or not ask:
                continue
            out[base] = {"bid": bid, "ask": ask, "vol": vol_map.get(sym, 0)}
        return out

    def depth(self, base):
        d = fetch_json(f"{self.API}/depth?symbol={base}USDT&limit=50")
        if not d or not d.get("asks"):
            return None
        return {"bids": [[float(x[0]), float(x[1])] for x in d["bids"]],
                "asks": [[float(x[0]), float(x[1])] for x in d["asks"]]}


class KuCoin(Exchange):
    name = "KuCoin"
    taker_fee = 0.001
    API = "https://api.kucoin.com/api/v1"

    def book(self):
        d = fetch_json(f"{self.API}/market/allTickers")["data"]["ticker"]
        out = {}
        for t in d:
            sym = t.get("symbol", "")
            if not sym.endswith("-USDT"):
                continue
            base = sym[:-5]
            bid = safe_float(t.get("buy")); ask = safe_float(t.get("sell"))
            if not bid or not ask:
                continue
            vol = safe_float(t.get("volValue"), 0) or 0
            out[base] = {"bid": bid, "ask": ask, "vol": vol}
        return out

    def depth(self, base):
        d = fetch_json(f"{self.API}/market/orderbook/level2_20?symbol={base}-USDT")["data"]
        if not d or not d.get("asks"):
            return None
        return {"bids": [[float(x[0]), float(x[1])] for x in d["bids"]],
                "asks": [[float(x[0]), float(x[1])] for x in d["asks"]]}


class Bitget(Exchange):
    name = "Bitget"
    taker_fee = 0.001
    API = "https://api.bitget.com/api/v2"

    def book(self):
        d = fetch_json(f"{self.API}/spot/market/tickers")["data"]
        out = {}
        for t in d:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            bid = safe_float(t.get("bidPr")); ask = safe_float(t.get("askPr"))
            if not bid or not ask:
                continue
            vol = safe_float(t.get("usdtVolume"), 0) or 0
            out[base] = {"bid": bid, "ask": ask, "vol": vol}
        return out

    def depth(self, base):
        d = fetch_json(f"{self.API}/spot/market/orderbook?symbol={base}USDT&limit=50")["data"]
        if not d or not d.get("asks"):
            return None
        return {"bids": [[float(x[0]), float(x[1])] for x in d["bids"]],
                "asks": [[float(x[0]), float(x[1])] for x in d["asks"]]}


EXCHANGES = [OKX(), Bybit(), Gate(), MEXC(), KuCoin(), Bitget()]


# ============================================================
# 板厚スリッページ計算 (★最重要ゲート★)
#   target_usdt を約定したときの「平均約定価格」を板から計算する。
#   買い側は asks を食い上げ、売り側は bids を食い下げる。
#   板が薄くて目標額を満たせなければ None を返す = 「取れない」。
# ============================================================
def realized_avg_price(levels, target_usdt):
    """levels を target_usdt 分消化したときの平均約定価格を返す。
    届かなければ None。"""
    remaining = target_usdt
    total_qty = 0.0
    total_cost = 0.0
    for price, size in levels:
        level_usd = price * size
        take = min(remaining, level_usd)
        qty = take / price
        total_qty += qty
        total_cost += qty * price
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-6 or total_qty <= 0:
        return None
    return total_cost / total_qty


def realistic_spread(buy_book, sell_book, target_usdt, buy_fee, sell_fee):
    """買い取引所の asks と売り取引所の bids から、想定ロット約定後の
    手数料込み実効スプレッド% を返す。取れなければ None。"""
    if not buy_book or not sell_book:
        return None
    avg_buy = realized_avg_price(buy_book["asks"], target_usdt)
    avg_sell = realized_avg_price(sell_book["bids"], target_usdt)
    if avg_buy is None or avg_sell is None:
        return None
    # 手数料: 買いで buy_fee, 売りで sell_fee を払う
    gross = (avg_sell - avg_buy) / avg_buy * 100
    fee_pct = (buy_fee + sell_fee) * 100
    return gross - fee_pct


# ============================================================
# Discord (既存スクリーナーと同じ multipart 方式)
# ============================================================
def discord_post(payload_json):
    if not DISCORD_WEBHOOK_URL:
        print("  [DRY-RUN] DISCORD_WEBHOOK_URL 未設定 — 送信スキップ")
        return
    data = json.dumps(payload_json).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "arb-signal/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")


def fmt_price(p):
    if p >= 100: return f"${p:,.2f}"
    if p >= 1:   return f"${p:,.4f}"
    if p >= 0.01: return f"${p:.5f}"
    return f"${p:.8f}"


def build_embed(sigs, scan_time, alive_names):
    now_jst = scan_time.strftime("%Y-%m-%d %H:%M JST")
    lines = []
    for s in sigs:
        lines.append(
            f"**{s['base']}/USDT**  実効 **{s['net']:.2f}%**  (生 {s['raw']:.2f}%)\n"
            f"　買 {s['buy_ex']} {fmt_price(s['avg_buy'])} → 売 {s['sell_ex']} {fmt_price(s['avg_sell'])}  "
            f"｜${int(TARGET_NOTIONAL)}約定可"
        )
    desc = "\n".join(lines) if lines else "条件を満たす歪みなし。"

    embed = {
        "title": "🔀 クロス取引所スプレッド検知シグナル",
        "description": desc,
        "color": 0x00B0FF,
        "fields": [
            {"name": "集計条件",
             "value": (f"スキャン時刻: {now_jst}\n"
                       f"対象取引所: {', '.join(alive_names)}\n"
                       f"想定ロット: ${int(TARGET_NOTIONAL)} / 点灯閾値(実効): {NET_SPREAD_MIN}%\n"
                       f"24h出来高フィルタ: ${int(MIN_VOLUME_24H):,}"),
             "inline": False},
            {"name": "⚠️ 注意",
             "value": ("これは実行ボットではなくシグナルです。送金型は着金中に歪みが消えます。"
                       "両取引所に事前資金を置いた同時約定でのみ現実的。"
                       "数値はスナップショットで、約定時には縮小している可能性大。"),
             "inline": False},
        ],
        "footer": {"text": "Cross-Exchange Arb Signal v1"},
    }
    return embed


# ============================================================
# Main
# ============================================================
def main():
    scan_time = datetime.now(JST)
    print(f"Arb scan start {scan_time.strftime('%Y-%m-%d %H:%M JST')}")

    # ---- Stage 0: 各取引所の板ティッカーを収集 (失敗した取引所はスキップ) ----
    books = {}       # name -> {base: {bid,ask,vol}}
    fee_map = {}      # name -> taker_fee
    ex_map = {}       # name -> Exchange instance
    for ex in EXCHANGES:
        try:
            b = ex.book()
            books[ex.name] = b
            fee_map[ex.name] = ex.taker_fee
            ex_map[ex.name] = ex
            print(f"  {ex.name}: {len(b)} USDT pairs (fee {ex.taker_fee*100:.2f}%)")
        except Exception as e:
            print(f"  {ex.name}: FAILED ({type(e).__name__}: {str(e)[:80]}) — skip")

    alive = list(books.keys())
    if len(alive) < 2:
        print("生存取引所が2未満。スプレッド計算不可。終了。")
        return

    # ---- Stage 1: 共通シンボルで生スプレッドを計算 ----
    all_bases = set()
    for b in books.values():
        all_bases |= set(b.keys())

    prelim = []
    for base in all_bases:
        if base in EXCLUDE_BASES:
            continue
        quotes = []  # (name, bid, ask, vol)
        for name in alive:
            q = books[name].get(base)
            if q:
                quotes.append((name, q["bid"], q["ask"], q["vol"]))
        if len(quotes) < 2:
            continue
        # 出来高フィルタ: 関与する取引所のどれかが最低出来高を超えること
        if max(q[3] for q in quotes) < MIN_VOLUME_24H:
            continue
        # 最安ask で買い、最高bid で売る
        buy = min(quotes, key=lambda x: x[2])   # min ask
        sell = max(quotes, key=lambda x: x[1])  # max bid
        if buy[0] == sell[0]:
            continue
        raw = (sell[1] - buy[2]) / buy[2] * 100
        # 上限超え = 同名別トークン/上場停止/枯れ板の罠 → 除外
        if raw > MAX_SPREAD:
            continue
        # ざっくり手数料を引いた一次足切り
        approx_fee = (fee_map[buy[0]] + fee_map[sell[0]]) * 100
        if raw - approx_fee < RAW_SPREAD_MIN:
            continue
        prelim.append({"base": base, "buy_ex": buy[0], "sell_ex": sell[0],
                       "raw": raw, "buy_ask": buy[2], "sell_bid": sell[1]})

    prelim.sort(key=lambda x: x["raw"], reverse=True)
    print(f"Stage-1 生スプレッド候補: {len(prelim)} (上位{min(len(prelim),30)}件を板厚検証)")

    # ---- Stage 2: ★板厚ゲート★ 上位だけ板を取得し実効スプレッドを計算 ----
    signals = []
    for p in prelim[:30]:
        base = p["base"]
        try:
            buy_book = ex_map[p["buy_ex"]].depth(base)
            sell_book = ex_map[p["sell_ex"]].depth(base)
        except Exception as e:
            print(f"  {base}: depth取得失敗 ({str(e)[:50]}) — skip")
            continue
        net = realistic_spread(buy_book, sell_book, TARGET_NOTIONAL,
                               fee_map[p["buy_ex"]], fee_map[p["sell_ex"]])
        if net is None:
            print(f"  {base}: 板が薄く ${int(TARGET_NOTIONAL)} 約定不可 — 罠として除外")
            continue
        if net < NET_SPREAD_MIN:
            print(f"  {base}: 実効 {net:.2f}% < 閾値 {NET_SPREAD_MIN}% — 除外")
            continue
        avg_buy = realized_avg_price(buy_book["asks"], TARGET_NOTIONAL)
        avg_sell = realized_avg_price(sell_book["bids"], TARGET_NOTIONAL)
        signals.append({**p, "net": net, "avg_buy": avg_buy, "avg_sell": avg_sell})
        print(f"  ✅ {base}: 実効 {net:.2f}% (生 {p['raw']:.2f}%) "
              f"買 {p['buy_ex']} → 売 {p['sell_ex']}")

    signals.sort(key=lambda x: x["net"], reverse=True)
    signals = signals[:MAX_SIGNALS]

    if not signals:
        print("板厚を通過した歪みなし。市場は効率的(または出来高不足)。")
        return

    embed = build_embed(signals, scan_time, alive)
    mention = "@everyone" if MENTION_EVERYONE else ""
    allowed = {"parse": ["everyone"]} if MENTION_EVERYONE else {"parse": []}
    discord_post({"content": mention, "embeds": [embed], "allowed_mentions": allowed})
    print(f"Sent {len(signals)} arb signal(s).")


if __name__ == "__main__":
    main()
