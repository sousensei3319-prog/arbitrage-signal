"""
Smart Money Collector v1 (④スマートマネー追跡 — データ収集)

Twitterの実力者の言う「defillamaにのってるPJ全てを開いて一番儲かってる人
2000アドレスくらい収集してパクる」の自動化。2段構成:

  Stage1: 戦場選び (DefiLlama)
    どのチェーン/カテゴリ/プロトコルに資金・出来高・手数料が集まっているかを
    全プロトコル横断で取得 → 「どの戦場で戦うか」の材料。
    ※DefiLlamaはTVL/出来高/手数料の集計サイトで、個人トレーダーの
      アドレスやPnLランキングは載っていない。アドレス収集は Stage2 で行う。

  Stage2: 選球眼 (Hyperliquid)
    perp出来高で最大の戦場 Hyperliquid の公式リーダーボード(全ユーザーの
    日/週/月/全期間PnL)から上位2000アドレスを収集し、さらに上位N人の
    直近30日の全約定(fills)と現在ポジションを取得 → 「誰の何をパクるか」の材料。

出力 (すべて data/smart_money/ 以下, GH Actionsがコミットバック):
  defillama_protocols.csv.gz   全プロトコル (TVL/カテゴリ/チェーン/増減率)
  defillama_chains.csv         チェーン別TVL
  defillama_dexs.csv           DEX別 24h/7d/30d出来高
  defillama_perps.csv          perp DEX別出来高 (戦場サイズ比較)
  defillama_fees.csv           プロトコル別 手数料/収益
  leaderboard_top2000.csv      HLリーダーボード上位2000 (PnL/ROI/出来高 x 4期間)
  fills_topN.csv.gz            上位Nアドレスの直近30日の全約定
  positions_topN.csv           上位Nアドレスの現在perpポジション
  collect_meta.json            収集時刻/パラメータ/件数

データソース (全て公開API・認証不要):
  DefiLlama:   https://api.llama.fi (無料・オープン)
  Hyperliquid: https://stats-data.hyperliquid.xyz/Mainnet/leaderboard
               https://api.hyperliquid.xyz/info (weight制限 1200/min を尊重)

依存なし (Python標準ライブラリのみ)。
実行: python smart_money/collect_smart_money.py
"""

import csv
import gzip
import io
import json
import os
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

JST = timezone(timedelta(hours=9))

# ============================================================
# Config (環境変数で調整可)
# ============================================================
# 環境変数は空文字で渡ってくることがある(pushイベント時のinputs等)ため `or` で防御
TOP_N_ADDRESSES = int(os.environ.get("TOP_N_ADDRESSES") or "2000")   # リーダーボード保存数
FILLS_N         = int(os.environ.get("FILLS_N") or "200")            # fills取得アドレス数
FILLS_DAYS      = int(os.environ.get("FILLS_DAYS") or "30")          # fills遡り日数
RANK_WINDOW     = os.environ.get("RANK_WINDOW") or "month"           # day/week/month/allTime
MIN_ACCT_VALUE  = float(os.environ.get("MIN_ACCT_VALUE") or "10000") # fills対象の最低口座残高($)
OUT_DIR         = os.environ.get("OUT_DIR") or "data/smart_money"

LLAMA_BASE = "https://api.llama.fi"
HL_INFO    = "https://api.hyperliquid.xyz/info"
HL_LEADER  = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

# HL info APIのweight制限: 1200/min per IP。
# userFillsByTime=20, clearinghouseState=2 → 1アドレス22。
# 安全側に 1000/min ペースで叩く。
HL_WEIGHT_PER_MIN = 1000.0


def _get(url, timeout=60, retries=3):
    """GET + リトライ(指数バックオフ)"""
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "arb-signal/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if i == retries - 1:
                raise
            wait = 2 ** (i + 1)
            print(f"    retry {url[:60]} in {wait}s: {type(e).__name__} {str(e)[:60]}")
            time.sleep(wait)


def _post_hl(body, timeout=30, retries=3):
    """HL info APIへPOST + リトライ"""
    data = json.dumps(body).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request(
                HL_INFO, data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "arb-signal/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == retries - 1:
                raise
            wait = 2 ** (i + 1)
            print(f"    HL retry {body.get('type')} in {wait}s: "
                  f"{type(e).__name__} {str(e)[:60]}")
            time.sleep(wait)


def _write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  wrote {path} ({len(rows)} rows)")


def _write_csv_gz(path, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(buf.getvalue())
    print(f"  wrote {path} ({len(rows)} rows, gz)")


# ============================================================
# Stage1: DefiLlama — 戦場の全体像
# ============================================================

def collect_defillama():
    counts = {}

    # --- 全プロトコル (TVL/カテゴリ/チェーン) ---
    print("[llama] /protocols ...")
    protos = json.loads(_get(f"{LLAMA_BASE}/protocols"))
    rows = []
    for p in protos:
        rows.append([
            p.get("name", ""), p.get("slug", ""), p.get("category", ""),
            ";".join(p.get("chains", []) or []),
            p.get("tvl") or 0,
            p.get("change_1d") if p.get("change_1d") is not None else "",
            p.get("change_7d") if p.get("change_7d") is not None else "",
            p.get("mcap") or "",
            p.get("listedAt") or "",
        ])
    rows.sort(key=lambda r: -(r[4] or 0))
    _write_csv_gz(f"{OUT_DIR}/defillama_protocols.csv.gz",
                  ["name", "slug", "category", "chains", "tvl_usd",
                   "change_1d_pct", "change_7d_pct", "mcap", "listed_at"], rows)
    counts["protocols"] = len(rows)

    # --- チェーン別TVL ---
    print("[llama] /v2/chains ...")
    chains = json.loads(_get(f"{LLAMA_BASE}/v2/chains"))
    rows = [[c.get("name", ""), c.get("tvl") or 0, c.get("tokenSymbol") or ""]
            for c in chains]
    rows.sort(key=lambda r: -(r[1] or 0))
    _write_csv(f"{OUT_DIR}/defillama_chains.csv",
               ["chain", "tvl_usd", "token"], rows)
    counts["chains"] = len(rows)

    # --- DEX / perp / 手数料 (dimensions API) ---
    for kind, fname in [("dexs", "defillama_dexs.csv"),
                        ("derivatives", "defillama_perps.csv"),
                        ("fees", "defillama_fees.csv")]:
        print(f"[llama] /overview/{kind} ...")
        url = (f"{LLAMA_BASE}/overview/{kind}"
               "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
        try:
            ov = json.loads(_get(url))
        except Exception as e:
            print(f"  skip {kind}: {type(e).__name__} {str(e)[:80]}")
            continue
        rows = []
        for p in ov.get("protocols", []):
            rows.append([
                p.get("name", ""), p.get("category", ""),
                ";".join(p.get("chains", []) or []),
                p.get("total24h") if p.get("total24h") is not None else "",
                p.get("total7d") if p.get("total7d") is not None else "",
                p.get("total30d") if p.get("total30d") is not None else "",
                p.get("change_1d") if p.get("change_1d") is not None else "",
                p.get("change_7d") if p.get("change_7d") is not None else "",
            ])
        rows.sort(key=lambda r: -(r[3] if isinstance(r[3], (int, float)) else 0))
        _write_csv(f"{OUT_DIR}/{fname}",
                   ["name", "category", "chains", "total_24h", "total_7d",
                    "total_30d", "change_1d_pct", "change_7d_pct"], rows)
        counts[kind] = len(rows)

    return counts


# ============================================================
# Stage2: Hyperliquid — リーダーボード + fills + ポジション
# ============================================================

WINDOWS = ["day", "week", "month", "allTime"]


def collect_leaderboard():
    """公式リーダーボード(全ユーザー) → 上位TOP_N_ADDRESSESをCSVに"""
    print("[HL] leaderboard (数十MBあるので時間がかかる) ...")
    raw = json.loads(_get(HL_LEADER, timeout=300))
    lb = raw.get("leaderboardRows", raw if isinstance(raw, list) else [])
    print(f"  leaderboard rows: {len(lb)}")

    parsed = []
    for row in lb:
        perf = {k: v for k, v in (row.get("windowPerformances") or [])}
        rec = {
            "address": row.get("ethAddress", ""),
            "display_name": row.get("displayName") or "",
            "account_value": float(row.get("accountValue") or 0),
        }
        for w in WINDOWS:
            p = perf.get(w) or {}
            rec[f"pnl_{w}"] = float(p.get("pnl") or 0)
            rec[f"roi_{w}"] = float(p.get("roi") or 0)
            rec[f"vlm_{w}"] = float(p.get("vlm") or 0)
        parsed.append(rec)

    # RANK_WINDOW のPnL降順で上位を採用 (「一番儲かってる人」の定義)
    parsed.sort(key=lambda r: -r[f"pnl_{RANK_WINDOW}"])
    top = parsed[:TOP_N_ADDRESSES]

    header = (["rank", "address", "display_name", "account_value"] +
              [f"{m}_{w}" for w in WINDOWS for m in ("pnl", "roi", "vlm")])
    rows = []
    for i, r in enumerate(top, 1):
        rows.append([i, r["address"], r["display_name"],
                     round(r["account_value"], 2)] +
                    [round(r[f"{m}_{w}"], 6) for w in WINDOWS
                     for m in ("pnl", "roi", "vlm")])
    _write_csv(f"{OUT_DIR}/leaderboard_top2000.csv", header, rows)
    return top, len(lb)


def collect_fills_and_positions(top):
    """上位FILLS_N人の直近FILLS_DAYS日の約定 + 現在ポジションを取得。
    weight制限を尊重してスリープを挟む。失敗アドレスはスキップして続行。"""
    targets = [r for r in top if r["account_value"] >= MIN_ACCT_VALUE][:FILLS_N]
    print(f"[HL] fills/positions for {len(targets)} addresses "
          f"(acct>=${MIN_ACCT_VALUE:,.0f}, ~{22*len(targets)/HL_WEIGHT_PER_MIN:.1f}min)")

    start_ms = int((time.time() - FILLS_DAYS * 86400) * 1000)
    sleep_per_addr = 22.0 / HL_WEIGHT_PER_MIN * 60.0  # weight22相当の待ち

    fill_rows, pos_rows = [], []
    ok = fail = 0
    for i, r in enumerate(targets, 1):
        a = r["address"]
        try:
            fills = _post_hl({"type": "userFillsByTime", "user": a,
                              "startTime": start_ms, "aggregateByTime": True})
            for f in fills or []:
                fill_rows.append([
                    a, f.get("time", ""), f.get("coin", ""),
                    f.get("side", ""),            # B=buy, A=sell
                    f.get("dir", ""),             # Open Long / Close Short 等
                    f.get("px", ""), f.get("sz", ""),
                    f.get("closedPnl", ""), f.get("fee", ""),
                    f.get("crossed", ""),         # True=taker
                ])
            st = _post_hl({"type": "clearinghouseState", "user": a})
            for ap in (st or {}).get("assetPositions", []):
                p = ap.get("position") or {}
                if not p.get("szi"):
                    continue
                lev = p.get("leverage") or {}
                pos_rows.append([
                    a, p.get("coin", ""), p.get("szi", ""),
                    p.get("entryPx", ""), p.get("positionValue", ""),
                    p.get("unrealizedPnl", ""), lev.get("value", ""),
                    lev.get("type", ""), p.get("liquidationPx", ""),
                ])
            ok += 1
        except Exception as e:
            fail += 1
            print(f"    [{i}/{len(targets)}] {a[:10]}.. fail: "
                  f"{type(e).__name__} {str(e)[:50]}")
        if i % 25 == 0:
            print(f"    [{i}/{len(targets)}] fills={len(fill_rows)} "
                  f"pos={len(pos_rows)} ok={ok} fail={fail}")
        time.sleep(sleep_per_addr)

    _write_csv_gz(f"{OUT_DIR}/fills_topN.csv.gz",
                  ["address", "time_ms", "coin", "side", "dir", "px", "sz",
                   "closed_pnl", "fee", "crossed"], fill_rows)
    _write_csv(f"{OUT_DIR}/positions_topN.csv",
               ["address", "coin", "szi", "entry_px", "position_value",
                "unrealized_pnl", "leverage", "leverage_type", "liq_px"],
               pos_rows)
    return {"fills_addresses_ok": ok, "fills_addresses_fail": fail,
            "fills": len(fill_rows), "positions": len(pos_rows)}


def collect_hl_meta():
    """perp銘柄一覧 + spot銘柄マッピング(@indexを人間可読名に変換する用)"""
    print("[HL] meta / spotMeta ...")
    meta = _post_hl({"type": "meta"})
    spot = _post_hl({"type": "spotMeta"})
    out = {
        "perp_universe": [u.get("name") for u in (meta or {}).get("universe", [])],
        "spot_pairs": {},
    }
    tokens = {i: t.get("name") for i, t in
              enumerate((spot or {}).get("tokens", []))}
    for u in (spot or {}).get("universe", []):
        # fills内では "@<index>" で現れる。"TOKEN/USDC" 形式へ
        idx = u.get("index")
        toks = u.get("tokens") or []
        if len(toks) == 2:
            name = f"{tokens.get(toks[0], toks[0])}/{tokens.get(toks[1], toks[1])}"
            out["spot_pairs"][f"@{idx}"] = name
    with open(f"{OUT_DIR}/hl_meta.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"  perp={len(out['perp_universe'])} spot_pairs={len(out['spot_pairs'])}")
    return {"perp_coins": len(out["perp_universe"]),
            "spot_pairs": len(out["spot_pairs"])}


# ============================================================
# main
# ============================================================

def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)
    meta = {
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "collected_at_jst": datetime.now(JST).isoformat(),
        "params": {"TOP_N_ADDRESSES": TOP_N_ADDRESSES, "FILLS_N": FILLS_N,
                   "FILLS_DAYS": FILLS_DAYS, "RANK_WINDOW": RANK_WINDOW,
                   "MIN_ACCT_VALUE": MIN_ACCT_VALUE},
        "counts": {},
    }

    try:
        meta["counts"].update(collect_defillama())
    except Exception as e:
        print(f"[llama] FAILED: {type(e).__name__} {str(e)[:100]}")
        meta["counts"]["defillama_error"] = str(e)[:200]

    try:
        meta["counts"].update(collect_hl_meta())
    except Exception as e:
        print(f"[HL meta] FAILED: {type(e).__name__} {str(e)[:100]}")

    top, total_lb = collect_leaderboard()
    meta["counts"]["leaderboard_total"] = total_lb
    meta["counts"]["leaderboard_saved"] = len(top)

    meta["counts"].update(collect_fills_and_positions(top))

    meta["elapsed_sec"] = round(time.time() - t0, 1)
    with open(f"{OUT_DIR}/collect_meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(f"DONE in {meta['elapsed_sec']}s: {json.dumps(meta['counts'])}")


if __name__ == "__main__":
    main()
