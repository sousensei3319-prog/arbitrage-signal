"""
JP Money Flow Screener (日本株 資金集中スクリーナー)

「どこに資金が集まりだしてるか」を、板情報を使わず **売買代金 (turnover = 終値×出来高)
の異常集中** で捉える。本リポジトリの smart_money_report が暗号資産でやっている
「今窓 vs 前窓」比較の株式版。

入力: data/jp_stocks/universe.csv (code,name,bucket) + data/jp_stocks/{code}_1m.csv
出力: data/jp_stocks/money_flow.csv (銘柄別スコア) と Discord/標準出力サマリー

各銘柄について:
  turnover(バー) = close × volume  [円]
  recent   = 直近 WINDOW_MIN 分の売買代金合計 (最新セッションの終盤)
  baseline = 履歴を WINDOW_MIN 分ブロックに区切った売買代金の中央値
  surge    = recent / baseline            (何倍に膨らんだか)
  z        = (recent - mean) / std        (異常度)
ユニバース横断:
  share_recent = 直近窓での売買代金シェア(%)
  share_base   = 履歴全体での売買代金シェア(%)
  share_delta  = share_recent - share_base  (資金が"向かってきた"量, ppt)
バケット別 (leader/core/hot) の recent/baseline で「どの層が熱いか」も集計。

設計:
  - 標準ライブラリのみ (matplotlibがあればチャートも出せるが本体はテキストで動く)
  - 板/歩み値のリアルタイムは無料枠外。売買代金の集中で"資金の向き先"を近似する
  - 収集(jp_stock_fetch.py)が更新するたびに走らせると、直近窓が前進して点灯が変わる

実行: python jp_money_flow.py
環境変数: WINDOW_MIN(既定30) / TOP_N(既定15) / DATA_DIR / UNIVERSE_FILE / INTERVAL
"""

import csv
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

JST = timezone(timedelta(hours=9))

DATA_DIR      = os.environ.get("DATA_DIR") or "data/jp_stocks"
UNIVERSE_FILE = os.environ.get("UNIVERSE_FILE") or os.path.join(DATA_DIR, "universe.csv")
INTERVAL      = os.environ.get("INTERVAL") or "1m"
WINDOW_MIN    = int(os.environ.get("WINDOW_MIN") or "30")   # 直近窓(分)
TOP_N         = int(os.environ.get("TOP_N") or "15")
OUT_CSV       = os.environ.get("MONEY_FLOW_CSV") or os.path.join(DATA_DIR, "money_flow.csv")
JSON_OUT      = os.environ.get("MONEY_FLOW_JSON") or os.path.join(DATA_DIR, "money_flow.json")

BUCKET_LABEL = {"leader": "けん引大型", "core": "主力", "hot": "話題・噂"}


def load_universe():
    meta = {}
    if os.path.exists(UNIVERSE_FILE):
        with open(UNIVERSE_FILE, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                code = (r.get("code") or "").strip()
                if not code:
                    continue
                sym = code if "." in code else code + ".T"
                meta[sym] = (r.get("name") or sym, r.get("bucket") or "core")
    return meta


def load_bars(sym):
    """(epoch, close, volume, turnover) のリストを返す。出来高0のバーは除外。"""
    path = os.path.join(DATA_DIR, f"{sym.replace('.', '_')}_{INTERVAL}.csv")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                v = float(r["volume"] or 0)
                c = float(r["close"])
            except (ValueError, KeyError, TypeError):
                continue
            if v <= 0:
                continue  # 寄り前/引け後の板寄せ相当バー
            rows.append((int(r["epoch"]), c, v, c * v))
    rows.sort(key=lambda x: x[0])
    return rows


def window_blocks(rows, win_sec):
    """時間で win_sec 幅の連続ブロックに区切り、各ブロックの売買代金合計を返す。"""
    if not rows:
        return []
    blocks = []
    start = rows[0][0]
    acc = 0.0
    for e, c, v, to in rows:
        if e - start >= win_sec:
            blocks.append(acc)
            start = e
            acc = 0.0
        acc += to
    if acc > 0:
        blocks.append(acc)
    return blocks


def analyze():
    meta = load_universe()
    if not meta:
        print(f"ユニバース {UNIVERSE_FILE} が無い/空。先に収集を実行すること。")
        sys.exit(0)

    win_sec = WINDOW_MIN * 60
    recs = []
    latest_ts = 0
    for sym, (name, bucket) in meta.items():
        rows = load_bars(sym)
        if len(rows) < 5:
            continue
        latest_ts = max(latest_ts, rows[-1][0])
        # 直近窓: 最終バーから遡って win_sec 内
        cut = rows[-1][0] - win_sec
        recent = sum(to for e, c, v, to in rows if e > cut)
        blocks = window_blocks(rows, win_sec)
        # baseline は直近ブロックを除いた分布
        base_blocks = blocks[:-1] if len(blocks) > 1 else blocks
        med = statistics.median(base_blocks) if base_blocks else 0.0
        mean = statistics.fmean(base_blocks) if base_blocks else 0.0
        sd = statistics.pstdev(base_blocks) if len(base_blocks) > 1 else 0.0
        surge = (recent / med) if med > 0 else 0.0
        z = ((recent - mean) / sd) if sd > 0 else 0.0
        total_to = sum(to for *_x, to in rows)
        first_c = rows[0][1]
        last_c = rows[-1][1]
        pct = (last_c / first_c - 1) * 100 if first_c else 0.0
        recs.append({
            "sym": sym, "name": name, "bucket": bucket,
            "recent": recent, "baseline_med": med, "surge": surge, "z": z,
            "total_to": total_to, "last": last_c, "pct": pct,
        })

    if not recs:
        print("有効データなし。")
        sys.exit(0)

    # ユニバース横断シェア
    tot_recent = sum(r["recent"] for r in recs) or 1.0
    tot_all = sum(r["total_to"] for r in recs) or 1.0
    for r in recs:
        r["share_recent"] = r["recent"] / tot_recent * 100
        r["share_base"] = r["total_to"] / tot_all * 100
        r["share_delta"] = r["share_recent"] - r["share_base"]

    # バケット別ヒート
    buckets = {}
    for r in recs:
        b = buckets.setdefault(r["bucket"], {"recent": 0.0, "total": 0.0, "n": 0})
        b["recent"] += r["recent"]; b["total"] += r["total_to"]; b["n"] += 1

    # 出力CSV
    recs_by_surge = sorted(recs, key=lambda r: -r["surge"])
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "bucket", "surge_ratio", "zscore",
                    "recent_turnover_yen", "baseline_median_yen",
                    "share_recent_pct", "share_base_pct", "share_delta_pp",
                    "last_close", "pct_window"])
        for r in recs_by_surge:
            w.writerow([r["sym"], r["name"], r["bucket"],
                        f'{r["surge"]:.3f}', f'{r["z"]:.2f}',
                        f'{r["recent"]:.0f}', f'{r["baseline_med"]:.0f}',
                        f'{r["share_recent"]:.3f}', f'{r["share_base"]:.3f}',
                        f'{r["share_delta"]:+.3f}',
                        f'{r["last"]:.1f}', f'{r["pct"]:+.2f}'])

    # ダッシュボード用JSON
    _dump_json(recs_by_surge, buckets, latest_ts)
    _print_report(recs_by_surge, buckets, latest_ts)
    return recs_by_surge, buckets


def _dump_json(recs, buckets, latest_ts):
    import json
    tot_recent = sum(b["recent"] for b in buckets.values()) or 1.0
    payload = {
        "meta": {
            "window_min": WINDOW_MIN,
            "latest": datetime.fromtimestamp(latest_ts, JST).strftime("%Y-%m-%d %H:%M") if latest_ts else "-",
            "n": len(recs),
        },
        "rows": [{
            "code": r["sym"], "name": r["name"], "bucket": r["bucket"],
            "surge": round(r["surge"], 3), "z": round(r["z"], 2),
            "recent": round(r["recent"]), "share_recent": round(r["share_recent"], 3),
            "share_base": round(r["share_base"], 3), "share_delta": round(r["share_delta"], 3),
            "last": r["last"], "pct": round(r["pct"], 2),
        } for r in recs],
        "buckets": [{
            "bucket": name, "label": BUCKET_LABEL.get(name, name),
            "recent": round(b["recent"]), "n": b["n"],
            "share": round(b["recent"] / tot_recent * 100, 2),
        } for name, b in sorted(buckets.items(), key=lambda kv: -kv[1]["recent"])],
    }
    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


def _yen(v):
    if v >= 1e12: return f"{v/1e12:.2f}兆円"
    if v >= 1e8:  return f"{v/1e8:.1f}億円"
    if v >= 1e4:  return f"{v/1e4:.0f}万円"
    return f"{v:.0f}円"


def _print_report(recs, buckets, latest_ts):
    ts = datetime.fromtimestamp(latest_ts, JST).strftime("%Y-%m-%d %H:%M") if latest_ts else "-"
    print(f"\n=== 資金集中スクリーナー (直近{WINDOW_MIN}分 vs 履歴中央値) 最新バー {ts} JST ===")
    print(f"対象 {len(recs)}銘柄\n")
    print(f"{'順':>2} {'コード':<7}{'銘柄':<20}{'層':<10}{'集中度':>7}{'z':>7}{'直近売買代金':>12}{'Δシェア':>9}")
    for i, r in enumerate(recs[:TOP_N], 1):
        print(f"{i:>2} {r['sym']:<7}{r['name'][:9]:<11}{BUCKET_LABEL.get(r['bucket'],r['bucket']):<8}"
              f"{r['surge']:>6.2f}x{r['z']:>7.1f}{_yen(r['recent']):>12}{r['share_delta']:>+8.2f}p")
    print("\n--- バケット別ヒート (直近シェア) ---")
    tot_recent = sum(b["recent"] for b in buckets.values()) or 1.0
    for name, b in sorted(buckets.items(), key=lambda kv: -kv[1]["recent"]):
        print(f"  {BUCKET_LABEL.get(name,name):<10} 直近{_yen(b['recent']):>10}  "
              f"シェア{b['recent']/tot_recent*100:>5.1f}%  ({b['n']}銘柄)")


if __name__ == "__main__":
    analyze()
