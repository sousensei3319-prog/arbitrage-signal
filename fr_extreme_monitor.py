"""
FR Extreme Monitor v1 (②FRアービ — 極端FR時のみ点灯する専用モニター)

fr_scanner v2 との違い:
  - 常時レポートではなく「極端値が出た時だけ」Discord通知 (点灯型)
  - 3クラスのシグナルを分離:
      A: 正FR極端   = 現物ロング + perpショートのデルタ中立キャリー候補
      B: 負FR極端   = ショート過密/パニック。BTC/ETH/SOLは確定エッジ
                      「FR PANIC → LONG」(FR<=-0.01% x2連続, 5年t=4.66) と照合
      C: 取引所間FR乖離 = perp-perp 両建て (現物不要・perp集中の資金と相性良)
  - シンボル衝突対策: クロス比較は「取引所間の価格一致 (±3%)」を必須ゲートに
    (①価格スプレッド検知で EDGE 1474% 等の同名別トークン罠が実証済みのため)
  - クールダウン状態を fr_monitor_state.json に永続化 (GH Actionsがコミットバック)
  - 発火履歴を fr_signals_log.csv に追記 → 将来「FR極端の持続性」検証の素材

データソース (全て公開API・認証不要):
  OKX:   /market/tickers + /public/funding-rate (個別・スレッド取得)
  Bybit: /v5/market/tickers + /v5/market/instruments-info (FR間隔)
  Gate:  /futures/usdt/tickers + /futures/usdt/contracts (FR間隔/次回時刻)
  MEXC:  /contract/funding_rate + /contract/ticker (価格/出来高)
  Bitget:/api/v2/mix/market/tickers + /contracts (FR間隔)

注意: 表示FRは「現行期(予測含む)」。決済前に縮むことがある。
      実弾は2〜3回の決済で継続を確認してから (RULES.md参照)。
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

JST = timezone(timedelta(hours=9))

# ============================================================
# Config (環境変数で調整可)
# ============================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MENTION_EVERYONE    = os.environ.get("MENTION_EVERYONE", "0") == "1"

MIN_APY_POS   = float(os.environ.get("MIN_APY_POS", "80"))    # A: 正FR極端の年率下限(%)
MIN_APY_NEG   = float(os.environ.get("MIN_APY_NEG", "200"))   # B: 負FR極端の年率下限(絶対値%)
MIN_APY_CROSS = float(os.environ.get("MIN_APY_CROSS", "150")) # C: 取引所間乖離の年率下限(%)
MIN_VOL       = float(os.environ.get("MIN_VOL", "2000000"))   # 24h出来高下限($) いずれかの取引所で
PRICE_AGREE   = float(os.environ.get("PRICE_AGREE_PCT", "3")) # クロス比較の価格一致ゲート(%)
COOLDOWN_H    = float(os.environ.get("COOLDOWN_H", "8"))      # 同一シグナルの再通知間隔(h)
ESCALATE_MULT = float(os.environ.get("ESCALATE_MULT", "1.5")) # クールダウン中でもAPYがこの倍率で再通知
NOTIONAL_USD  = float(os.environ.get("NOTIONAL_USD", "300"))  # 収益試算の1レッグ想定額($)
FEE_RT_PCT    = float(os.environ.get("FEE_RT_PCT", "0.24"))   # 往復手数料の想定(%)
MAX_PER_CLASS = int(os.environ.get("MAX_PER_CLASS", "5"))     # 各クラスの最大表示件数
STATE_FILE    = os.environ.get("STATE_FILE", "fr_monitor_state.json")
LOG_FILE      = os.environ.get("LOG_FILE", "fr_signals_log.csv")

EXCLUDE      = {"USDC","DAI","TUSD","FDUSD","USDD","USDE","BUSD","PYUSD"}
STOCK_TOKENS = {
    "AAPL","MSFT","GOOGL","GOOG","AMZN","META","NVDA","TSLA","AMD","INTC",
    "IBM","ORCL","DELL","MU","SNDK","ARM","CRM","ADBE","NFLX","QCOM","TXN",
    "AVGO","AMAT","LRCX","KLAC","MRVL","SMCI","HPE","HPQ","WDC","STX",
    "COIN","HOOD","MSTR","MARA","RIOT","HUT","CLSK","CIFR","BTBT",
    "COST","WMT","TGT","HD","LOW","NKE","SBUX","MCD",
    "JPM","BAC","GS","MS","WFC","C","BLK","V","MA","PYPL",
    "JNJ","PFE","MRNA","ABBV","LLY","UNH","CVS",
    "XOM","CVX","COP","SLB","BP","SHEL",
    "GE","CAT","BA","LMT","RTX","NOC","GD",
    "QQQ","SPY","IWM","DIA","GLD","SLV","USO","TLT","HYG",
    "SOXS","SOXX","FNGU","TQQQ","SQQQ",
    "DRAM","CRCL","H","BIDU","BABA","JD","PDD","NIO","XPEV","LI",
    "SKHYNIX","TSMC","SAMSUNG",   # "STOCK"を含まない株トークン (実弾点灯で発見)
}

OKX_BASE    = "https://www.okx.com/api/v5"
# Bybit本体はGH Actions(US IP)から403で拒否されるためミラーへフォールバック
BYBIT_HOSTS = ["https://api.bybit.com/v5", "https://api.bytick.com/v5"]
GATE_BASE   = "https://api.gateio.ws/api/v4"
MEXC_BASE   = "https://contract.mexc.com/api/v1"
BITGET_BASE = "https://api.bitget.com/api/v2"

# 確定エッジ FR PANIC → LONG の対象 (BTCのみ5年有意。ETH/SOLは参考表示)
# 注: BTCのPANIC水準(-0.01%/8h=年率-11%)はMIN_APY_NEGに届かないため、
#     この3銘柄はBクラス閾値と独立に照合する
FR_PANIC_COINS = {"BTC": "5年t=4.66で有意★確定エッジ★", "ETH": "BTCのみ有意(参考)", "SOL": "BTCのみ有意(参考)"}
FR_PANIC_THRESH = -0.0001  # -0.01%/8h


def excluded(coin):
    return coin in EXCLUDE or coin in STOCK_TOKENS or "STOCK" in coin


# ============================================================
# HTTP
# ============================================================
def fetch(url, timeout=20):
    req = urllib.request.Request(
        url, headers={"User-Agent": "fr-extreme-monitor/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def safe_float(x):
    try:
        f = float(x)
        return f if f == f else None  # NaN guard
    except (TypeError, ValueError):
        return None


# ============================================================
# 取引所別ローダー
# 戻り値: {coin: {"fr": float, "interval_h": float, "nft_ms": int|None,
#                "price": float|None, "vol": float|None}}
# ============================================================
def load_okx():
    tickers = fetch(f"{OKX_BASE}/market/tickers?instType=SWAP")["data"]
    base = {}
    for t in tickers:
        if not t["instId"].endswith("-USDT-SWAP"):
            continue
        coin = t["instId"].replace("-USDT-SWAP", "")
        if excluded(coin):
            continue
        last = safe_float(t.get("last")) or 0
        vol = (safe_float(t.get("volCcy24h")) or 0) * last
        if vol < MIN_VOL:
            continue  # OKXの個別FR取得はコスト高なので出来高ゲート先行
        base[coin] = {"instId": t["instId"], "price": last, "vol": vol}

    out = {}

    def one(coin):
        time.sleep(0.3)  # OKX public rate limit (20req/2s) を確実に下回るペース
        try:
            d = fetch(f"{OKX_BASE}/public/funding-rate?instId={base[coin]['instId']}")["data"]
            if not d:
                return
            fr = safe_float(d[0].get("fundingRate"))
            ft = safe_float(d[0].get("fundingTime"))      # 現行期の決済時刻(ms)
            nft = safe_float(d[0].get("nextFundingTime"))
            if fr is None:
                return
            ih = 8.0
            if ft and nft and nft > ft:
                ih = (nft - ft) / 3_600_000
            out[coin] = {"fr": fr, "interval_h": ih,
                         "nft_ms": int(ft) if ft else None,
                         "price": base[coin]["price"], "vol": base[coin]["vol"]}
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, list(base.keys())))
    return out


_bybit_host = [0]   # 直近成功ホストを記憶 (同一実行内の2回目以降の無駄打ち防止)


def _bybit_fetch(path):
    last_err = None
    for i in range(len(BYBIT_HOSTS)):
        idx = (_bybit_host[0] + i) % len(BYBIT_HOSTS)
        try:
            r = fetch(f"{BYBIT_HOSTS[idx]}{path}")
            _bybit_host[0] = idx
            return r
        except Exception as e:
            last_err = e
    raise last_err


def load_bybit():
    data = _bybit_fetch("/market/tickers?category=linear")["result"]["list"]
    # FR間隔はinstruments-infoから (極端時はミーム銘柄が4h/1h間隔になるため必須)
    intervals = {}
    try:
        info = _bybit_fetch("/market/instruments-info?category=linear&limit=1000")
        for it in info["result"]["list"]:
            m = safe_float(it.get("fundingInterval"))  # 分
            if m:
                intervals[it.get("symbol", "")] = m / 60.0
    except Exception:
        pass
    out = {}
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        coin = sym[:-4]
        if excluded(coin):
            continue
        fr = safe_float(t.get("fundingRate"))
        if fr is None:
            continue
        nft = safe_float(t.get("nextFundingTime"))
        out[coin] = {"fr": fr, "interval_h": intervals.get(sym, 8.0),
                     "nft_ms": int(nft) if nft else None,
                     "price": safe_float(t.get("lastPrice")),
                     "vol": safe_float(t.get("turnover24h"))}
    return out


def load_gate():
    # 注: limitパラメータは現在(0,100]制限。無指定なら全契約が返る
    contracts = fetch(f"{GATE_BASE}/futures/usdt/contracts")
    tickers = {}
    try:
        for t in fetch(f"{GATE_BASE}/futures/usdt/tickers"):
            tickers[t.get("contract", "")] = t
    except Exception:
        pass
    out = {}
    for c in contracts:
        name = c.get("name", "")
        if not name.endswith("_USDT"):
            continue
        coin = name[:-5]
        if excluded(coin):
            continue
        fr = safe_float(c.get("funding_rate"))
        if fr is None:
            continue
        ih = safe_float(c.get("funding_interval"))  # 秒
        ih = ih / 3600.0 if ih else 8.0
        nft_s = safe_float(c.get("funding_next_apply"))
        tk = tickers.get(name, {})
        vol = safe_float(tk.get("volume_24h_settle")) or safe_float(tk.get("volume_24h_quote"))
        out[coin] = {"fr": fr, "interval_h": ih,
                     "nft_ms": int(nft_s * 1000) if nft_s else None,
                     "price": safe_float(tk.get("last")) or safe_float(c.get("last_price")),
                     "vol": vol}
    return out


def load_mexc():
    d = fetch(f"{MEXC_BASE}/contract/funding_rate")
    if not d.get("success"):
        return {}
    tickers = {}
    try:
        td = fetch(f"{MEXC_BASE}/contract/ticker")
        if td.get("success"):
            for t in td.get("data", []):
                tickers[t.get("symbol", "")] = t
    except Exception:
        pass
    out = {}
    for t in d.get("data", []):
        sym = t.get("symbol", "")
        if not sym.endswith("_USDT"):
            continue
        coin = sym[:-5]
        if excluded(coin):
            continue
        fr = safe_float(t.get("fundingRate"))
        if fr is None:
            continue
        tk = tickers.get(sym, {})
        nft = safe_float(t.get("nextSettleTime"))
        out[coin] = {"fr": fr,
                     "interval_h": safe_float(t.get("collectCycle")) or 8.0,
                     "nft_ms": int(nft) if nft else None,
                     "price": safe_float(tk.get("lastPrice")),
                     "vol": safe_float(tk.get("amount24"))}
    return out


def load_bitget():
    data = fetch(f"{BITGET_BASE}/mix/market/tickers?productType=USDT-FUTURES")["data"]
    intervals = {}
    try:
        for c in fetch(f"{BITGET_BASE}/mix/market/contracts?productType=USDT-FUTURES")["data"]:
            h = safe_float(c.get("fundInterval"))
            if h:
                intervals[c.get("symbol", "")] = h
    except Exception:
        pass
    out = {}
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        coin = sym[:-4]
        if excluded(coin):
            continue
        fr = safe_float(t.get("fundingRate"))
        if fr is None:
            continue
        out[coin] = {"fr": fr, "interval_h": intervals.get(sym, 8.0),
                     "nft_ms": None,
                     "price": safe_float(t.get("lastPr")),
                     "vol": safe_float(t.get("usdtVolume"))}
    return out


# ============================================================
# 計算
# ============================================================
def calc_apy(fr, interval_h):
    if fr is None or not interval_h or interval_h <= 0:
        return None
    return fr * (24.0 / interval_h) * 365 * 100


def minutes_until(ms_ts):
    if not ms_ts:
        return None
    diff = (ms_ts - datetime.now(timezone.utc).timestamp() * 1000) / 60000
    return max(0, int(diff))


def fmt_vol(v):
    if v is None: return "n/a"
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.2f}M"
    if v >= 1e3: return f"${v/1e3:.1f}K"
    return f"${v:.0f}"


def breakeven_str(fr_abs, interval_h):
    """往復手数料を回収するのに必要な決済回数と時間。"""
    if not fr_abs or fr_abs <= 0:
        return "n/a"
    cycles = (FEE_RT_PCT / 100.0) / fr_abs
    hours = cycles * interval_h
    return f"{cycles:.1f}回決済({hours:.0f}h)で手数料回収"


def daily_usd(fr, interval_h, notional):
    if fr is None or not interval_h:
        return 0.0
    return notional * fr * (24.0 / interval_h)


# ============================================================
# State (クールダウン) / ログ
# ============================================================
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    # 7日より古いエントリは捨てる
    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    state = {k: v for k, v in state.items() if v.get("ts", 0) > cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def should_notify(state, key, apy_abs):
    now = datetime.now(timezone.utc).timestamp()
    prev = state.get(key)
    if prev is None:
        return True
    if now - prev.get("ts", 0) > COOLDOWN_H * 3600:
        return True
    if apy_abs >= prev.get("apy", 1e9) * ESCALATE_MULT:
        return True  # クールダウン中でも明確な悪化/拡大は再通知
    return False


def mark_notified(state, key, apy_abs):
    state[key] = {"ts": datetime.now(timezone.utc).timestamp(), "apy": apy_abs}


def log_signal(scan_time, klass, coin, detail):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_jst", "class", "coin", "apy", "fr", "interval_h",
                        "exchange", "cross_apy_spread", "vol_usd", "notified"])
        w.writerow([scan_time.strftime("%Y-%m-%d %H:%M"), klass, coin,
                    f"{detail.get('apy', 0):.1f}", f"{detail.get('fr', 0):.6f}",
                    detail.get("interval_h", ""), detail.get("ex", ""),
                    f"{detail.get('cross_apy', 0):.1f}" if detail.get("cross_apy") else "",
                    f"{detail.get('vol', 0):.0f}" if detail.get("vol") else "",
                    "1" if detail.get("notified") else "0"])


# ============================================================
# Discord
# ============================================================
def discord_post(payload):
    if not DISCORD_WEBHOOK_URL:
        print("  [DRY-RUN] DISCORD_WEBHOOK_URL 未設定 — 通知内容:")
        print(json.dumps(payload, ensure_ascii=False, indent=1)[:3000])
        return
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "fr-extreme-monitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")


# ============================================================
# Main
# ============================================================
def main():
    scan_time = datetime.now(JST)
    print(f"FR extreme monitor scan {scan_time.strftime('%Y-%m-%d %H:%M JST')}")
    print(f"閾値: A正FR>={MIN_APY_POS}% / B負FR<=-{MIN_APY_NEG}% / C乖離>={MIN_APY_CROSS}% (全て年率換算)")

    sources = {"OKX": load_okx, "Bybit": load_bybit, "Gate": load_gate,
               "MEXC": load_mexc, "Bitget": load_bitget}
    fr_data, ex_status = {}, {}
    for name, fn in sources.items():
        try:
            d = fn()
            fr_data[name] = d
            ex_status[name] = True
            print(f"  {name}: {len(d)} pairs")
        except Exception as e:
            fr_data[name] = {}
            ex_status[name] = False
            print(f"  {name}: FAILED ({type(e).__name__}: {str(e)[:60]})")

    alive = [n for n, ok in ex_status.items() if ok]
    if not alive:
        print("全取引所取得失敗。終了。")
        return

    # ---- 銘柄ユニバース: いずれかの取引所で出来高 >= MIN_VOL ----
    all_coins = set()
    for ex in alive:
        all_coins |= set(fr_data[ex].keys())
    universe = []
    for coin in all_coins:
        vols = [fr_data[ex][coin].get("vol") for ex in alive if coin in fr_data[ex]]
        vols = [v for v in vols if v]
        if vols and max(vols) >= MIN_VOL:
            universe.append(coin)
    print(f"  ユニバース: {len(universe)}銘柄 (出来高>={fmt_vol(MIN_VOL)} @いずれかの取引所)")

    state = load_state()
    sig_a, sig_b, sig_c = [], [], []

    for coin in universe:
        by_ex = {ex: fr_data[ex][coin] for ex in alive if coin in fr_data[ex]}
        apys = {}
        for ex, q in by_ex.items():
            apy = calc_apy(q["fr"], q["interval_h"])
            if apy is not None:
                apys[ex] = apy
        if not apys:
            continue

        max_ex = max(apys, key=apys.get)
        min_ex = min(apys, key=apys.get)
        best_vol = max((q.get("vol") or 0) for q in by_ex.values())

        def venue_vol_ok(ex, floor):
            # 実際に取引するvenueの出来高ゲート (出来高不明のvenueは通す)
            v = by_ex[ex].get("vol")
            return v is None or v >= floor

        # --- A: 正FR極端 (デルタ中立キャリー候補) ---
        if apys[max_ex] >= MIN_APY_POS and venue_vol_ok(max_ex, MIN_VOL):
            q = by_ex[max_ex]
            sig_a.append({
                "coin": coin, "ex": max_ex, "fr": q["fr"], "apy": apys[max_ex],
                "interval_h": q["interval_h"], "nft_min": minutes_until(q.get("nft_ms")),
                "vol": best_vol, "all": apys,
            })

        # --- B: 負FR極端 (パニック / FR PANICエッジ照合) ---
        # BTC/ETH/SOLはPANIC水準(-0.01%/8h換算)に達したら閾値と無関係に点灯
        q_min = by_ex[min_ex]
        fr_8h = q_min["fr"] * (8.0 / q_min["interval_h"]) if q_min["interval_h"] else q_min["fr"]
        panic_hit = coin in FR_PANIC_COINS and fr_8h <= FR_PANIC_THRESH
        if apys[min_ex] <= -MIN_APY_NEG or panic_hit:
            sig_b.append({
                "coin": coin, "ex": min_ex, "fr": q_min["fr"], "apy": apys[min_ex],
                "interval_h": q_min["interval_h"], "nft_min": minutes_until(q_min.get("nft_ms")),
                "vol": best_vol, "all": apys,
                "edge": FR_PANIC_COINS.get(coin) if panic_hit else None,
            })

        # --- C: 取引所間FR乖離 (perp-perp両建て候補・価格一致ゲート必須) ---
        if len(apys) >= 2:
            spread = apys[max_ex] - apys[min_ex]
            if (spread >= MIN_APY_CROSS
                    and venue_vol_ok(max_ex, MIN_VOL / 2)
                    and venue_vol_ok(min_ex, MIN_VOL / 2)):
                p_hi = by_ex[max_ex].get("price")
                p_lo = by_ex[min_ex].get("price")
                # 同名別トークン罠の排除: 両venueの価格が一致しない場合は捨てる
                if p_hi and p_lo and abs(p_hi - p_lo) / max(p_hi, p_lo) * 100 <= PRICE_AGREE:
                    fr_hi = by_ex[max_ex]["fr"]
                    sig_c.append({
                        "coin": coin, "ex": max_ex, "ex_lo": min_ex,
                        "fr": fr_hi, "apy": apys[max_ex], "apy_lo": apys[min_ex],
                        "cross_apy": spread, "interval_h": by_ex[max_ex]["interval_h"],
                        "nft_min": minutes_until(by_ex[max_ex].get("nft_ms")),
                        "vol": best_vol, "all": apys,
                    })

    sig_a.sort(key=lambda s: s["apy"], reverse=True)
    # B: 確定エッジ(FR PANIC)該当を最優先 — ミーム極端値に押し出されないように
    sig_b.sort(key=lambda s: (0 if s.get("edge") else 1, s["apy"]))
    sig_c.sort(key=lambda s: s["cross_apy"], reverse=True)

    # ---- クールダウン判定 + ログ ----
    # 通知枠はMAX_PER_CLASS。クールダウン中の上位銘柄は飛ばして下位を繰り上げ、
    # 「記録したのに表示されない」銘柄を作らない (markするのは通知する分だけ)
    def apply_cooldown(sigs, klass, score_fn):
        notify = []
        for s in sigs:
            if len(notify) >= MAX_PER_CLASS:
                break
            key = f"{klass}:{s['coin']}"
            if should_notify(state, key, score_fn(s)):
                s["notified"] = True
                mark_notified(state, key, score_fn(s))
                notify.append(s)
        for s in sigs[:MAX_PER_CLASS * 3]:
            log_signal(scan_time, klass, s["coin"], s)
        return notify

    notify_a = apply_cooldown(sig_a, "A", lambda s: abs(s["apy"]))
    notify_b = apply_cooldown(sig_b, "B", lambda s: abs(s["apy"]))
    notify_c = apply_cooldown(sig_c, "C", lambda s: s["cross_apy"])
    save_state(state)

    print(f"\n検出: A={len(sig_a)} B={len(sig_b)} C={len(sig_c)} / "
          f"通知(クールダウン後): A={len(notify_a)} B={len(notify_b)} C={len(notify_c)}")
    for tag, sigs in (("A", sig_a), ("B", sig_b), ("C", sig_c)):
        for s in sigs[:5]:
            extra = f" 乖離{s['cross_apy']:.0f}%" if tag == "C" else ""
            print(f"  [{tag}] {s['coin']}: APY {s['apy']:+.1f}% @ {s['ex']}{extra}"
                  f" vol {fmt_vol(s.get('vol'))}{' *notify*' if s.get('notified') else ''}")

    if not (notify_a or notify_b or notify_c):
        print("点灯なし (通知スキップ)。")
        return

    # ---- Discord embed ----
    def line_common(s):
        nft = f" 次回決済{s['nft_min']}分後" if s.get("nft_min") is not None else ""
        ivl = f"{s['interval_h']:.0f}h毎" if s.get("interval_h") else ""
        others = " / ".join(f"{ex}{a:+.0f}%" for ex, a in sorted(s["all"].items(), key=lambda x: -x[1]))
        return nft, ivl, others

    parts = []
    if notify_a:
        lines = []
        for s in notify_a[:MAX_PER_CLASS]:
            nft, ivl, others = line_common(s)
            dly = daily_usd(s["fr"], s["interval_h"], NOTIONAL_USD)
            lines.append(
                f"**{s['coin']}** APY **{s['apy']:+.0f}%** @ {s['ex']} ({s['fr']*100:+.4f}%/{ivl}){nft}\n"
                f"　→ 現物L+perpS ${NOTIONAL_USD:.0f}/leg で約**${dly:.2f}/日** | {breakeven_str(abs(s['fr']), s['interval_h'])}\n"
                f"　全venue: {others} | 出来高{fmt_vol(s.get('vol'))}")
        parts.append("__**A: 正FR極端 (デルタ中立キャリー候補)**__\n" + "\n".join(lines))
    if notify_b:
        lines = []
        for s in notify_b[:MAX_PER_CLASS]:
            nft, ivl, others = line_common(s)
            edge = f"\n　🚨 **確定エッジFR PANIC圏: {s['edge']}** → edge_hunter/paper botを確認" if s.get("edge") else ""
            lines.append(
                f"**{s['coin']}** APY **{s['apy']:+.0f}%** @ {s['ex']} ({s['fr']*100:+.4f}%/{ivl}){nft}\n"
                f"　ショート過密/パニック。踏み上げ燃料あり{edge}\n"
                f"　全venue: {others} | 出来高{fmt_vol(s.get('vol'))}")
        parts.append("__**B: 負FR極端 (ショート過密/パニック)**__\n" + "\n".join(lines))
    if notify_c:
        lines = []
        for s in notify_c[:MAX_PER_CLASS]:
            nft, ivl, others = line_common(s)
            dly = NOTIONAL_USD * (s["cross_apy"] / 100) / 365
            lines.append(
                f"**{s['coin']}** 乖離 **{s['cross_apy']:.0f}%/年** ({s['ex']}{s['apy']:+.0f}% ↔ {s['ex_lo']}{s['apy_lo']:+.0f}%)\n"
                f"　→ {s['ex']}でS + {s['ex_lo']}でL (perp-perp) ${NOTIONAL_USD:.0f}/leg で約**${dly:.2f}/日**{nft}\n"
                f"　全venue: {others} | 出来高{fmt_vol(s.get('vol'))} | 価格一致±{PRICE_AGREE}%確認済")
        parts.append("__**C: 取引所間FR乖離 (perp-perp両建て候補)**__\n" + "\n".join(lines))

    embed = {
        "title": "🔥 FR極端モニター 点灯",
        "description": "\n\n".join(parts)[:3900],
        "color": 0xFF4500,
        "fields": [
            {"name": "集計情報",
             "value": (f"スキャン時刻: {scan_time.strftime('%Y-%m-%d %H:%M JST')} (現行期FRの時点値)\n"
                       f"検出: A={len(sig_a)} / B={len(sig_b)} / C={len(sig_c)}件"
                       f" (通知はクールダウン{COOLDOWN_H:.0f}h適用後)\n"
                       f"取引所: {' | '.join(('✅ ' if ex_status[n] else '❌ ') + n for n in sources)}"),
             "inline": False},
            {"name": "⚠️ 入る前の3チェック",
             "value": ("1. **継続性**: 表示FRは現行期。決済2〜3回の継続を見てから\n"
                       "2. **手数料**: 往復" + f"{FEE_RT_PCT:.2f}%" + "想定。損益分岐表示を確認\n"
                       "3. **10万円の現実**: 両建ては各レッグ証拠金が薄くなる。"
                       "レバ2x以下・1銘柄集中禁止 (RULES.md)"),
             "inline": False},
        ],
        "footer": {"text": "FR Extreme Monitor v1 | ②FRアービ点灯型 | 履歴はfr_signals_log.csvに蓄積中"},
    }
    mention = "@everyone" if MENTION_EVERYONE else ""
    allowed = {"parse": ["everyone"]} if MENTION_EVERYONE else {"parse": []}
    discord_post({"content": mention, "embeds": [embed], "allowed_mentions": allowed})
    print("Discord通知送信。")


if __name__ == "__main__":
    main()
