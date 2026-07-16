"""
US Money Flow Screener (米国株 資金集中スクリーナー)

「どこに資金が集まりだしてるか」を、板情報を使わず **売買代金 (turnover = 終値×出来高)
の異常集中** で捉える。jp_money_flow.py の忠実な移植 (JST→ET、円→ドル、.T補完削除の
差分のみ。window_stats() 等の集計ロジックは一切変更しない)。

入力: data/us_stocks/universe.csv (code,name,bucket,sector,group) + data/us_stocks/{code}_1m.csv
出力: data/us_stocks/money_flow.csv (銘柄別スコア) と Discord/標準出力サマリー

各銘柄について:
  turnover(バー) = close × volume  [ドル]
  recent   = 直近 WINDOW_MIN 分の売買代金合計 (最新セッションの終盤)
  baseline = 履歴を WINDOW_MIN 分ブロックに区切った売買代金の中央値
  surge    = recent / baseline            (何倍に膨らんだか)
  z        = (recent - mean) / std        (異常度)
ユニバース横断:
  share_recent = 直近窓での売買代金シェア(%)
  share_base   = 履歴全体での売買代金シェア(%)
  share_delta  = share_recent - share_base  (資金が"向かってきた"量, ppt)
バケット別 (leader=ナスダック100/core=S&P500主力/hot=話題・噂) の recent/baseline で
「どの層が熱いか」も集計。業種グループ別 (group=GICS 11セクターの日本語訳+独自区分)の
share_recent/share_base合算で「どの業種に資金が向いているか」も集計
(money_flow.jsonの"groups"に出力)。独自区分は data/us_stocks/custom_groups.csv
(code,custom_group,basis、us_universe_refresh.pyが機械生成) による上書きで、
GICS公式に無い切り口(例:「半導体」)を切り出す (ファイル無しなら従来どおり)。

設計:
  - 標準ライブラリのみ (matplotlibがあればチャートも出せるが本体はテキストで動く)
  - 板/歩み値のリアルタイムは無料枠外。売買代金の集中で"資金の向き先"を近似する
  - 収集(us_stock_fetch.py)が更新するたびに走らせると、直近窓が前進して点灯が変わる

実行: python us_money_flow.py
環境変数: WINDOW_MIN(既定30) / TOP_N(既定15) / DATA_DIR / UNIVERSE_FILE / INTERVAL
"""

import csv
import os
import statistics
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ET = ZoneInfo("America/New_York")

DATA_DIR      = os.environ.get("DATA_DIR") or "data/us_stocks"
UNIVERSE_FILE = os.environ.get("UNIVERSE_FILE") or os.path.join(DATA_DIR, "universe.csv")
INTERVAL      = os.environ.get("INTERVAL") or "1m"
WINDOW_MIN    = int(os.environ.get("WINDOW_MIN") or "30")   # 直近窓(分)
TOP_N         = int(os.environ.get("TOP_N") or "15")
OUT_CSV       = os.environ.get("MONEY_FLOW_CSV") or os.path.join(DATA_DIR, "money_flow.csv")
JSON_OUT      = os.environ.get("MONEY_FLOW_JSON") or os.path.join(DATA_DIR, "money_flow.json")

BUCKET_LABEL = {"leader": "ナスダック100", "core": "S&P500主力", "hot": "話題・噂"}

# 米国には JPX 空売り残高報告に相当する日次の公的個別開示が無い (下記_commentary末尾参照)。
# SUPPLY_CSVは将来的に何らかの需給データソースが用意された場合の受け皿として関数だけ残す
# (ファイル不存在なら load_short_positions() は空dictを返し、以下のロジックは素通りする)。
SUPPLY_CSV = os.environ.get("SUPPLY_DEMAND_CSV") or os.path.join(DATA_DIR, "supply_demand", "short_positions.csv")


def load_short_positions():
    """(米国向け・将来拡張用の受け皿) 日次の大口空売り開示データを銘柄別に集計する。

    現時点では対応する公的データソースが無いため SUPPLY_CSV は通常存在せず、
    その場合は空dictを返す(=呼び出し側は「データ無し」として扱い、捏造しない)。
    ファイル形式はJP版(jp_supply_demand.py出力)と同一を仮定した実装のみ残す。
    戻り値: {code: {"date": "YYYY-MM-DD", "n_holders": int, "total_ratio_pct": float}}
    """
    if not os.path.exists(SUPPLY_CSV):
        return {}
    latest_date = {}
    try:
        with open(SUPPLY_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                code, date = r.get("code"), r.get("disclosure_date")
                if not code or not date:
                    continue
                if date > latest_date.get(code, ""):
                    latest_date[code] = date
        agg = {}
        with open(SUPPLY_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                code = r.get("code")
                if not code or r.get("disclosure_date") != latest_date.get(code):
                    continue
                try:
                    ratio = float(r.get("ratio_pct") or 0)
                except (TypeError, ValueError):
                    ratio = 0.0
                a = agg.setdefault(code, {"date": latest_date[code], "n_holders": 0, "total_ratio_pct": 0.0})
                a["n_holders"] += 1
                a["total_ratio_pct"] += ratio
        return agg
    except (OSError, csv.Error):
        return {}


CUSTOM_GROUPS_CSV = os.environ.get("CUSTOM_GROUPS_CSV") or os.path.join(DATA_DIR, "custom_groups.csv")


def load_custom_groups():
    """独自区分の上書きマップ {code: custom_group} を返す。

    GICS公式11セクターに存在しない切り口(例:「半導体」— 公式では情報技術セクターの
    半導体・半導体製造装置サブ業種に含まれる)を、data/us_stocks/custom_groups.csv
    (code,custom_group,basis、us_universe_refresh.pyが機械生成) でグループ集計に
    反映するための仕組み。ファイルが無い/読めない場合は空dict(=GICSセクターのみ)。
    """
    if not os.path.exists(CUSTOM_GROUPS_CSV):
        return {}
    try:
        overrides = {}
        with open(CUSTOM_GROUPS_CSV, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                code = (r.get("code") or "").strip()
                grp = (r.get("custom_group") or "").strip()
                if code and grp:
                    overrides[code] = grp
        return overrides
    except (OSError, csv.Error):
        return {}


def _group_of(r, custom=None):
    """業種グループを取り出す共通ヘルパー。

    優先順位: 独自区分(custom_groups.csv、codeで引く) → group列(GICS 11セクター日本語訳) →
    sector列(group列が無い旧universe.csvの後方互換) → "不明"。
    """
    code = (r.get("code") or "").strip()
    if custom and code in custom:
        return custom[code]
    return (r.get("group") or "").strip() or (r.get("sector") or "").strip() or "不明"


def load_universe():
    meta = {}
    custom = load_custom_groups()
    if os.path.exists(UNIVERSE_FILE):
        with open(UNIVERSE_FILE, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                code = (r.get("code") or "").strip()
                if not code:
                    continue
                meta[code] = (r.get("name") or code, r.get("bucket") or "core",
                              (r.get("sector") or "").strip(), _group_of(r, custom))
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
                continue  # 寄り付き前/引け後の欠測バー相当
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


def window_stats(rows, win_sec):
    """(recent, prev, baseline_median) を返す汎用窓統計。

    analyze()内の単一WINDOW_MIN向けロジックを他モジュール(dashboard/build_us_dashboard.py)
    からも複数の窓幅(1分〜月足)で再利用できるよう関数化したもの。

    rows: [(epoch, close, volume, turnover), ...] epoch昇順ソート済み。
    win_sec: 窓幅(秒)。日足/週足/月足も日数×86400として同じロジックで扱える。
    戻り値: (recent, prev, baseline_median) — recent=直近窓合計, prev=その直前窓合計
    (勢いΔの算出用), baseline_median=履歴を窓幅ブロックに区切った中央値(直近ブロック除く)。
    """
    if not rows:
        return 0.0, 0.0, 0.0
    last_e = rows[-1][0]
    cut1 = last_e - win_sec
    cut2 = last_e - 2 * win_sec
    recent = sum(to for e, c, v, to in rows if e > cut1)
    prev = sum(to for e, c, v, to in rows if cut2 < e <= cut1)
    blocks = window_blocks(rows, win_sec)
    base_blocks = blocks[:-1] if len(blocks) > 1 else blocks
    med = statistics.median(base_blocks) if base_blocks else 0.0
    return recent, prev, med


def analyze():
    meta = load_universe()
    if not meta:
        print(f"ユニバース {UNIVERSE_FILE} が無い/空。先に収集を実行すること。")
        sys.exit(0)

    win_sec = WINDOW_MIN * 60
    recs = []
    latest_ts = 0
    for sym, (name, bucket, sector, group) in meta.items():
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
            "sym": sym, "name": name, "bucket": bucket, "sector": sector, "group": group,
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

    # 業種グループ別集計 (GICS 11セクター日本語訳、group列。無ければsectorにフォールバック)
    # 既に銘柄単位で計算済みのshare_recent/share_baseを合算するだけ (二重計算しない)。
    group_agg = {}
    for r in recs:
        g = group_agg.setdefault(r["group"], {"share_recent": 0.0, "share_base": 0.0, "n": 0})
        g["share_recent"] += r["share_recent"]; g["share_base"] += r["share_base"]; g["n"] += 1
    groups = sorted(
        [{"group": g, "share_pct": v["share_recent"], "base_share_pct": v["share_base"],
          "delta_pp": v["share_recent"] - v["share_base"], "n": v["n"]}
         for g, v in group_agg.items()],
        key=lambda x: -x["share_pct"])

    # 出力CSV
    recs_by_surge = sorted(recs, key=lambda r: -r["surge"])
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "name", "bucket", "sector", "group", "surge_ratio", "zscore",
                    "recent_turnover_usd", "baseline_median_usd",
                    "share_recent_pct", "share_base_pct", "share_delta_pp",
                    "last_close", "pct_window"])
        for r in recs_by_surge:
            w.writerow([r["sym"], r["name"], r["bucket"], r["sector"], r["group"],
                        f'{r["surge"]:.3f}', f'{r["z"]:.2f}',
                        f'{r["recent"]:.0f}', f'{r["baseline_med"]:.0f}',
                        f'{r["share_recent"]:.3f}', f'{r["share_base"]:.3f}',
                        f'{r["share_delta"]:+.3f}',
                        f'{r["last"]:.1f}', f'{r["pct"]:+.2f}'])

    # 自動分析コメント (事実ベース) + ダッシュボード用JSON
    commentary = _commentary(recs_by_surge, buckets, latest_ts, groups)
    _dump_json(recs_by_surge, buckets, latest_ts, commentary, groups)
    _print_report(recs_by_surge, buckets, latest_ts)
    print("\n--- 自動分析コメント ---")
    for line in commentary:
        print("  " + line)
    return recs_by_surge, buckets


def _session_bars(sym, latest_ts):
    """最新セッション(最新バーと同一ET日付)の (epoch,close,vol,turnover)。"""
    day = datetime.fromtimestamp(latest_ts, ET).strftime("%Y-%m-%d")
    return [row for row in load_bars(sym)
            if datetime.fromtimestamp(row[0], ET).strftime("%Y-%m-%d") == day]


def _peak_window(bars, win_sec=1800):
    """売買代金が最も集中した win_sec 窓を返す: (開始epoch, 窓内代金, 株価変化%)。"""
    if len(bars) < 2:
        return None
    best = None
    for i, (e0, c0, _v, _to) in enumerate(bars):
        s = 0.0; c_end = c0
        for e, c, _v2, to in bars[i:]:
            if e - e0 > win_sec:
                break
            s += to; c_end = c
        chg = (c_end / c0 - 1) * 100 if c0 else 0.0
        if best is None or s > best[1]:
            best = (e0, s, chg)
    return best


def _commentary(recs, buckets, latest_ts, groups=None):
    """算出可能な事実のみで日本語コメントを組み立てる (捏造しない)。"""
    lines = []
    if not recs or not latest_ts:
        return ["データ不足のため分析なし。"]
    hhmm = lambda e: datetime.fromtimestamp(e, ET).strftime("%H:%M")
    tot_recent = sum(b["recent"] for b in buckets.values()) or 1.0
    top_bucket = max(buckets.items(), key=lambda kv: kv[1]["recent"])
    bshare = top_bucket[1]["recent"] / tot_recent * 100
    total_recent = sum(r["recent"] for r in recs)
    lines.append(f"直近{WINDOW_MIN}分の売買代金は全体で {_usd(total_recent)}。"
                 f"最も資金が寄っているのは「{BUCKET_LABEL.get(top_bucket[0],top_bucket[0])}」層で全体の {bshare:.0f}%。")

    # 全体活況度 (直近窓の総代金 vs 履歴中央値の総和) — 過熱/閑散を倍率で事実描写
    total_base = sum(r["baseline_med"] for r in recs)
    if total_base > 0:
        activity = total_recent / total_base
        act_word = ("平常より活発" if activity >= 1.5
                    else "ほぼ平常並み" if activity >= 0.7 else "平常より閑散")
        lines.append(f"全体活況度: 直近{WINDOW_MIN}分の売買代金は平常時（履歴中央値）の "
                     f"{activity:.1f}倍（{act_word}）。")

    # 業種別シェア (GICS 11セクター、group列) — 上位3業種 + 変化幅(Δpp)が突出した業種
    if groups:
        top3 = groups[:3]
        rest = groups[3:]
        big_delta = max(rest, key=lambda g: abs(g["delta_pp"])) if rest else None
        parts = [f"{g['group']} {g['share_pct']:.1f}%（通常{g['base_share_pct']:.1f}%・{g['delta_pp']:+.1f}pp）"
                 for g in top3]
        if big_delta and abs(big_delta["delta_pp"]) >= 0.5:
            parts.append(f"{big_delta['group']} {big_delta['share_pct']:.1f}%"
                         f"（通常{big_delta['base_share_pct']:.1f}%・{big_delta['delta_pp']:+.1f}pp、変化幅で突出）")
        lines.append("業種別シェア: " + "、".join(parts) + "。")

        # 業種別の資金移動 (Δpp増加=流入 / 減少=流出) — 資金がどの業種へ動いたかの方向
        inflow_g = [g for g in sorted(groups, key=lambda g: -g["delta_pp"])[:2] if g["delta_pp"] > 0]
        outflow_g = [g for g in sorted(groups, key=lambda g: g["delta_pp"])[:2] if g["delta_pp"] < 0]
        mv = []
        if inflow_g:
            mv.append("流入 " + "・".join(f"{g['group']} {g['delta_pp']:+.1f}pp" for g in inflow_g))
        if outflow_g:
            mv.append("流出 " + "・".join(f"{g['group']} {g['delta_pp']:+.1f}pp" for g in outflow_g))
        if mv:
            lines.append("業種別の資金移動: " + " / ".join(mv) + "。")

    # 上位集中銘柄の時間帯と値動き
    strong = [r for r in recs if r["surge"] >= 1.5][:3]
    for r in strong:
        pk = _peak_window(_session_bars(r["sym"], latest_ts))
        when = f"{hhmm(pk[0])}頃から" if pk else "本セッションで"
        move = f"（同時間帯で株価 {pk[2]:+.1f}%）" if pk else ""
        lines.append(f"{r['name']}（{r['sector']}）: 集中度 {r['surge']:.1f}x。{when}売買代金が集中{move}。")

    # 業種クラスタ (同一業種で複数が平常比プラス)
    sect = {}
    for r in recs:
        if r["surge"] >= 1.3 and r["share_delta"] > 0:
            sect.setdefault(r["sector"], []).append(r["name"])
    clusters = sorted([(s, v) for s, v in sect.items() if len(v) >= 2], key=lambda x: -len(x[1]))
    if clusters:
        s, names = clusters[0]
        lines.append(f"業種では「{s}」に資金が広がっている（{len(names)}銘柄が平常比プラス: {'・'.join(names[:4])}）。")

    # 資金流入 (シェア拡大) 上位 — 銘柄名・シェア増・株価騰落を併記 (事実のみ)
    inflow_s = sorted([r for r in recs if r["share_delta"] >= 0.3], key=lambda r: -r["share_delta"])[:3]
    if inflow_s:
        lines.append("資金流入（シェア拡大）上位: " + " / ".join(
            f"{r['name']} +{r['share_delta']:.2f}pp（株価{r['pct']:+.1f}%）" for r in inflow_s) + "。")

    # 資金流出 (シェア縮小) 上位 — 資金が抜けている先も事実として提示
    outflow_s = sorted([r for r in recs if r["share_delta"] <= -0.3], key=lambda r: r["share_delta"])[:3]
    if outflow_s:
        lines.append("資金流出（シェア縮小）上位: " + " / ".join(
            f"{r['name']} {r['share_delta']:.2f}pp（株価{r['pct']:+.1f}%）" for r in outflow_s) + "。")

    # シェア急伸と株価の連動 — 出来高先行か株価連動かを件数で事実描写 (予測はしない)
    surging = [r for r in recs if r["share_delta"] >= 0.3]
    if surging:
        with_price = sum(1 for r in surging if r["pct"] >= 1.0)
        lines.append(f"シェア急伸（+0.3pp以上）は{len(surging)}銘柄。うち株価も+1%以上を伴うのは "
                     f"{with_price}銘柄、残り{len(surging) - with_price}銘柄は株価が追随せず出来高先行。")

    # (将来拡張用) 大口空売り開示データがある場合のみ事実を記載。現時点では通常データ無し。
    short_pos = load_short_positions()
    if short_pos:
        hits = [(r, short_pos[r["sym"]]) for r in recs if r["sym"] in short_pos]
        hits.sort(key=lambda x: -x[1]["total_ratio_pct"])
        for r, sp in hits[:3]:
            lines.append(f"空売り残高報告: {r['name']}に大口ショート {sp['n_holders']}件・"
                         f"合計 {sp['total_ratio_pct']:.2f}%（{sp['date']}公表）。")

    # 正直な限界 (捏造回避) — 米国向け
    lines.append("※米国には JPX 空売り残高報告に相当する日次の公的個別開示が無い"
                 "（FINRAの空売り残高は隔週集計のため本ダッシュボードの更新頻度とは合わず、"
                 "需給レイヤーとしては未収録）。値動きのきっかけ（ニュース等）も本データには"
                 "含まれず未検証。反転/急伸の断定は行わない。投資助言ではない。")
    return lines


def _dump_json(recs, buckets, latest_ts, commentary=None, groups=None):
    import json
    tot_recent = sum(b["recent"] for b in buckets.values()) or 1.0
    payload = {
        "meta": {
            "window_min": WINDOW_MIN,
            "latest": datetime.fromtimestamp(latest_ts, ET).strftime("%Y-%m-%d %H:%M") if latest_ts else "-",
            "n": len(recs),
        },
        "commentary": commentary or [],
        "rows": [{
            "code": r["sym"], "name": r["name"], "bucket": r["bucket"],
            "sector": r["sector"], "group": r["group"],
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
        "groups": [{
            "group": g["group"], "share_pct": round(g["share_pct"], 3),
            "base_share_pct": round(g["base_share_pct"], 3),
            "delta_pp": round(g["delta_pp"], 3), "n": g["n"],
        } for g in (groups or [])],
    }
    with open(JSON_OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


def _usd(v):
    if v >= 1e12: return f"{v/1e12:.2f}兆ドル"
    if v >= 1e8:  return f"{v/1e8:.1f}億ドル"
    if v >= 1e4:  return f"{v/1e4:.0f}万ドル"
    return f"{v:.0f}ドル"


def _print_report(recs, buckets, latest_ts):
    ts = datetime.fromtimestamp(latest_ts, ET).strftime("%Y-%m-%d %H:%M") if latest_ts else "-"
    print(f"\n=== 米国株 資金集中スクリーナー (直近{WINDOW_MIN}分 vs 履歴中央値) 最新バー {ts} ET ===")
    print(f"対象 {len(recs)}銘柄\n")
    print(f"{'順':>2} {'コード':<7}{'銘柄':<20}{'層':<10}{'集中度':>7}{'z':>7}{'直近売買代金':>12}{'Δシェア':>9}")
    for i, r in enumerate(recs[:TOP_N], 1):
        print(f"{i:>2} {r['sym']:<7}{r['name'][:9]:<11}{BUCKET_LABEL.get(r['bucket'],r['bucket']):<8}"
              f"{r['surge']:>6.2f}x{r['z']:>7.1f}{_usd(r['recent']):>12}{r['share_delta']:>+8.2f}p")
    print("\n--- バケット別ヒート (直近シェア) ---")
    tot_recent = sum(b["recent"] for b in buckets.values()) or 1.0
    for name, b in sorted(buckets.items(), key=lambda kv: -kv[1]["recent"]):
        print(f"  {BUCKET_LABEL.get(name,name):<10} 直近{_usd(b['recent']):>10}  "
              f"シェア{b['recent']/tot_recent*100:>5.1f}%  ({b['n']}銘柄)")


if __name__ == "__main__":
    analyze()
