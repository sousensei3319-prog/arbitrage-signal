"""
Smart Money Tracker v1 (④スマートマネー追跡 — 点灯型シグナルBot)

「一番儲かってる人をパクる」の通知部分。
data/smart_money/leaderboard_top2000.csv (smart-money.yml が生成) の
上位 TRACK_N アドレスの現在perpポジションを毎回取得し、前回との差分から
「勝ち組が新しく入った銘柄」を検知する。

シグナル (点灯型・条件を満たした時だけDiscord通知):
  ★コンセンサス新規参入:
    前回スキャン以降に MIN_WALLETS 人以上が「同一銘柄・同方向」へ
    新規ポジションを建てた (新規 = 前回その銘柄のポジションが無い or 逆方向)。
    → 勝ち組が同時に群がる銘柄は情報優位/イベント起点の可能性。監視リスト入り候補。
    30日イベントスタディ: +24hで平均+1.4%・勝率60% (ベースライン±0.2%を明確に超過)

  ★VIPウォレット単独ムーブ:
    vip_addresses.csv (30日実現PnL上位の人間8人) は1人でも
    VIP_MIN_POS_USD 以上の新規/転換/クローズで即通知 (コンセンサス不要)。

  参考表示 (通知の下部に常設):
    スマートマネー合算ネットエクスポージャー上位 (いま何にどれだけ張っているか)

設計メモ:
  - clearinghouseState は weight 2 → TRACK_N=100 で 200/1200 per min。余裕。
  - 初回実行 (前回状態なし) は通知せず状態保存のみ (誤発火防止)。
  - クールダウン: 同一銘柄・同方向は COOLDOWN_H 時間再通知しない。
  - 発火履歴は smart_money_signals_log.csv に追記 (後でエッジ検証する素材)。
  - 状態は smart_money_state.json (GH Actionsがコミットバック)。

注意 (RULES.md準拠):
  - これは「候補の点灯」であって自動エントリー命令ではない。
  - 勝ち組の後追いは常に彼らより悪い価格。パクるのは銘柄選択であって執行ではない。

依存なし (Python標準ライブラリのみ)。
実行: python smart_money_tracker.py  (DISCORD_WEBHOOK_URL未設定ならDRY-RUN)
"""

import csv
import json
import os
import socket
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

# urlopenのtimeout引数が効かない経路 (DNS等) への保険。ジョブハング防止
socket.setdefaulttimeout(35)

JST = timezone(timedelta(hours=9))

# ============================================================
# Config (環境変数で調整可)
# ============================================================
# スマートマネー専用Webhookがあれば優先、なければ共通Webhook
DISCORD_WEBHOOK_URL = (os.environ.get("SMART_MONEY_WEBHOOK_URL")
                       or os.environ.get("DISCORD_WEBHOOK_URL", ""))
MENTION_EVERYONE    = os.environ.get("MENTION_EVERYONE", "0") == "1"

TRACK_N      = int(os.environ.get("TRACK_N") or "100")       # 追跡アドレス数
MIN_WALLETS  = int(os.environ.get("MIN_WALLETS") or "3")     # コンセンサス閾値(人)。500人収集・人間系163人で検証: 3人=1.5回/日が実用域
MIN_POS_USD  = float(os.environ.get("MIN_POS_USD") or "10000")  # ノイズ除外: この額未満の新規ポジは無視($)
COOLDOWN_H   = float(os.environ.get("COOLDOWN_H") or "12")   # 同一銘柄・同方向の再通知間隔(h)
ADDR_FILE    = os.environ.get("ADDR_FILE") or "data/smart_money/leaderboard_top2000.csv"
TRACKED_FILE = os.environ.get("TRACKED_FILE") or "data/smart_money/tracked_addresses.csv"
EXCLUDE_BOTS = os.environ.get("EXCLUDE_BOTS", "1") == "1"  # MM/HFT Botを追跡から除外
STATE_FILE   = os.environ.get("STATE_FILE") or "smart_money_state.json"
LOG_FILE     = os.environ.get("LOG_FILE") or "smart_money_signals_log.csv"

VIP_FILE        = os.environ.get("VIP_FILE") or "data/smart_money/vip_addresses.csv"
VIP_MIN_POS_USD = float(os.environ.get("VIP_MIN_POS_USD") or "50000")  # VIPの通知下限($)
VIP_COOLDOWN_H  = float(os.environ.get("VIP_COOLDOWN_H") or "6")       # VIP同一銘柄の再通知間隔(h)
VIP_LOG_FILE    = os.environ.get("VIP_LOG_FILE") or "smart_money_vip_log.csv"

HL_INFO = "https://api.hyperliquid.xyz/info"
HL_EXPLORER = "https://app.hyperliquid.xyz/explorer/address"  # ウォレット詳細ページ
HL_LEADERBOARD_URL = "https://app.hyperliquid.xyz/leaderboard"


def _wallet_link(addr):
    """Discord embed用: ウォレットをHL公式Explorerへのリンクにする"""
    return f"[`{addr[:8]}..`]({HL_EXPLORER}/{addr})"


def _post_hl(body, timeout=30, retries=3):
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
            time.sleep(2 ** (i + 1))


def load_vips():
    """VIPリスト {address: 主戦場ラベル}。無ければ空。"""
    vips = {}
    if os.path.exists(VIP_FILE):
        with open(VIP_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                vips[row["address"]] = row.get("top_coins", "")
    return vips


def load_addresses():
    """追跡アドレスを読む。tracked_addresses.csv (Bot判定つき) を優先し、
    無ければリーダーボード上位から。どちらも smart-money.yml が生成する。"""
    if os.path.exists(TRACKED_FILE):
        addrs, skipped = [], 0
        with open(TRACKED_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if EXCLUDE_BOTS and row.get("is_bot") == "1":
                    skipped += 1
                    continue
                addrs.append(row["address"])
                if len(addrs) >= TRACK_N:
                    break
        print(f"追跡リスト: {TRACKED_FILE} から{len(addrs)}人 (Bot除外{skipped}人)")
        if addrs:
            return addrs
    if not os.path.exists(ADDR_FILE):
        print(f"アドレスファイルなし: {ADDR_FILE}")
        print("先に smart-money.yml (collect_smart_money.py) を実行すること。")
        sys.exit(0)
    addrs = []
    with open(ADDR_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            addrs.append(row["address"])
            if len(addrs) >= TRACK_N:
                break
    print(f"追跡リスト: {ADDR_FILE} 上位{len(addrs)}人 (Bot判定なし)")
    return addrs


def fetch_positions(addrs):
    """各アドレスの現在perpポジション {addr: {coin: {側/サイズ$/entry}}}"""
    out, fail = {}, 0
    for i, a in enumerate(addrs, 1):
        try:
            st = _post_hl({"type": "clearinghouseState", "user": a})
            pos = {}
            for ap in (st or {}).get("assetPositions", []):
                p = ap.get("position") or {}
                szi = float(p.get("szi") or 0)
                if szi == 0:
                    continue
                pos[p["coin"]] = {
                    "side": "L" if szi > 0 else "S",
                    "usd": abs(float(p.get("positionValue") or 0)),
                    "entry": float(p.get("entryPx") or 0),
                }
            out[a] = pos
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(addrs)}] {a[:10]}.. fail: {type(e).__name__}")
        time.sleep(0.15)  # weight 2/call → 実効 ~800/min で余裕を残す
    print(f"positions: ok={len(out)} fail={fail}")
    return out


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"positions": {}, "last_alerts": {}, "updated": ""}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def detect_consensus(prev_pos, cur_pos):
    """前回→今回の差分から「新規参入」を銘柄×方向で集計"""
    entries = {}  # (coin, side) -> [(addr, usd, entry_px)]
    for a, coins in cur_pos.items():
        prev = prev_pos.get(a, {})
        for coin, p in coins.items():
            if p["usd"] < MIN_POS_USD:
                continue
            was = prev.get(coin)
            is_new = was is None or was.get("side") != p["side"]
            if is_new:
                entries.setdefault((coin, p["side"]), []).append(
                    (a, p["usd"], p["entry"]))
    return {k: v for k, v in entries.items() if len(v) >= MIN_WALLETS}


def net_exposure(cur_pos, top=8):
    """全追跡ウォレットの銘柄別ネットエクスポージャー上位"""
    agg = {}
    for coins in cur_pos.values():
        for coin, p in coins.items():
            agg[coin] = agg.get(coin, 0.0) + (p["usd"] if p["side"] == "L" else -p["usd"])
    ranked = sorted(agg.items(), key=lambda kv: -abs(kv[1]))
    return ranked[:top]


def log_signals(now, signals):
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time_jst", "coin", "side", "wallets", "total_usd",
                        "avg_entry", "addresses"])
        for (coin, side), rows in signals.items():
            total = sum(r[1] for r in rows)
            avg_e = sum(r[2] for r in rows) / len(rows)
            w.writerow([now.strftime("%Y-%m-%d %H:%M"), coin, side, len(rows),
                        round(total), round(avg_e, 6),
                        ";".join(r[0] for r in rows)])


def detect_vip_moves(prev_pos, cur_pos, vips):
    """VIPの単独ムーブ: 新規/転換(OPEN)とクローズ(CLOSE)を検知。
    返り値: [(addr, kind, coin, side, usd, entry_px)]"""
    moves = []
    for a in vips:
        cur = cur_pos.get(a)
        if cur is None:          # 取得失敗したVIPは判定しない (誤CLOSE防止)
            continue
        prev = prev_pos.get(a, {})
        for coin, p in cur.items():
            was = prev.get(coin)
            if p["usd"] >= VIP_MIN_POS_USD and (
                    was is None or was.get("side") != p["side"]):
                moves.append((a, "OPEN", coin, p["side"], p["usd"], p["entry"]))
        for coin, p in prev.items():
            if p.get("usd", 0) >= VIP_MIN_POS_USD and coin not in cur:
                moves.append((a, "CLOSE", coin, p["side"], p["usd"],
                              p.get("entry", 0)))
    return moves


def log_vip(now, moves):
    new_file = not os.path.exists(VIP_LOG_FILE)
    with open(VIP_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["time_jst", "address", "kind", "coin", "side",
                        "usd", "entry_px"])
        for a, kind, coin, side, usd, entry in moves:
            w.writerow([now.strftime("%Y-%m-%d %H:%M"), a, kind, coin, side,
                        round(usd), entry])


def render_chart(chart_specs, out_path="sm_chart.png"):
    """シグナル銘柄の7日1h足チャートを1枚に描く (最大4銘柄)。
    matplotlib未導入なら None (通知はテキストのみで成立する設計)。
    chart_specs: [(coin, タイトル文字列, entry_px or None, side "L"/"S"/None)]
    エントリー描写: ロング=緑の上向き矢印(下から上)、ショート=赤の下向き矢印(上から下)"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        try:
            import matplotlib_fontja  # noqa: F401  日本語フォント (無くても動く)
        except ImportError:
            pass
    except ImportError:
        print("  matplotlib未導入 — チャートなしで通知")
        return None

    specs = chart_specs[:4]
    candles = {}
    start = int((time.time() - 7 * 86400) * 1000)
    for spec in specs:
        coin = spec[0]
        if coin in candles:
            continue
        try:
            candles[coin] = _post_hl({"type": "candleSnapshot", "req": {
                "coin": coin, "interval": "1h",
                "startTime": start, "endTime": int(time.time() * 1000)}})
        except Exception as e:
            print(f"  candle {coin} fail: {type(e).__name__}")
            candles[coin] = None

    specs = [s for s in specs if candles.get(s[0])]
    if not specs:
        return None

    GREEN, RED = "#0ca30c", "#e34948"
    fig, axes = plt.subplots(len(specs), 1, figsize=(8, 2.6 * len(specs)),
                             squeeze=False, facecolor="#fcfcfb")
    from datetime import datetime as _dt
    for ax_row, (coin, title, entry, side) in zip(axes, specs):
        ax = ax_row[0]
        cs = candles[coin]
        ts = [_dt.fromtimestamp(c["t"] / 1000, tz=JST) for c in cs]
        close = [float(c["c"]) for c in cs]
        ax.plot(ts, close, color="#2a78d6", lw=1.6)
        if entry:
            col = GREEN if side == "L" else RED
            ax.axhline(entry, color=col, lw=1, ls="--")
            # エントリー矢印: ロング=下から上(緑) / ショート=上から下(赤)
            lo, hi = min(close + [entry]), max(close + [entry])
            span = (hi - lo) or entry * 0.01
            x_arrow = ts[int(len(ts) * 0.9)]
            d = span * 0.18
            tail = entry - d if side == "L" else entry + d
            ax.annotate("", xy=(x_arrow, entry), xytext=(x_arrow, tail),
                        arrowprops=dict(color=col, width=2.2, headwidth=9,
                                        headlength=7))
            label = "LONG entry" if side == "L" else "SHORT entry"
            ax.text(ts[0], entry, f" {label} {entry:g}", fontsize=8,
                    color=col, va="bottom" if side == "L" else "top")
            ax.set_ylim(lo - span * 0.25, hi + span * 0.25)
        ax.set_title(f"{coin} — {title} (7日 1h足, JST)", loc="left", fontsize=10)
        ax.grid(color="#e1e0d9", lw=0.6)
        ax.set_facecolor("#fcfcfb")
        ax.tick_params(axis="x", labelsize=7, rotation=20)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path


def discord_post(payload, image_path=None):
    """Webhook送信。image_pathがあればmultipartで添付し、
    embedからは attachment://<name> で参照できる。"""
    if not DISCORD_WEBHOOK_URL:
        print("  [DRY-RUN] DISCORD_WEBHOOK_URL 未設定 — 通知内容:")
        print(json.dumps(payload, ensure_ascii=False, indent=1)[:3000])
        return
    if image_path and os.path.exists(image_path):
        boundary = "----smartmoney" + str(int(time.time()))
        name = os.path.basename(image_path)
        with open(image_path, "rb") as f:
            img = f.read()
        parts = []
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="payload_json"\r\nContent-Type: application/json'
                     f"\r\n\r\n{json.dumps(payload)}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="files[0]"; filename="{name}"\r\n'
                     f"Content-Type: image/png\r\n\r\n".encode() + img + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        data = b"".join(parts)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "User-Agent": "smart-money-tracker/1.0"}
    else:
        data = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json",
                   "User-Agent": "smart-money-tracker/1.0"}
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=data, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")


def main():
    now = datetime.now(JST)
    print(f"Smart money tracker scan {now.strftime('%Y-%m-%d %H:%M JST')}")
    print(f"追跡: 上位{TRACK_N}人 / コンセンサス閾値: {MIN_WALLETS}人 / "
          f"最低ロット: ${MIN_POS_USD:,.0f}")

    vips = load_vips()
    addrs = load_addresses()
    # VIPは追跡人数の枠に関係なく必ず監視する
    addrs = list(dict.fromkeys(list(vips) + addrs))
    cur_pos = fetch_positions(addrs)
    if not cur_pos:
        print("ポジション取得ゼロ — API障害の可能性。状態は変更しない。")
        return

    state = load_state()
    prev_pos = state.get("positions", {})
    first_run = not prev_pos

    signals = detect_consensus(prev_pos, cur_pos)
    vip_moves = [] if first_run else detect_vip_moves(prev_pos, cur_pos, vips)

    # クールダウン適用
    now_ts = time.time()
    alerts = state.setdefault("last_alerts", {})
    fire = {}
    for key, rows in signals.items():
        k = f"{key[0]}|{key[1]}"
        if now_ts - alerts.get(k, 0) >= COOLDOWN_H * 3600:
            fire[key] = rows
            alerts[k] = now_ts
    vip_fire = []
    for m in vip_moves:
        a, kind, coin, side, usd, entry = m
        k = f"vip|{a[:10]}|{coin}|{kind}|{side}"
        if now_ts - alerts.get(k, 0) >= VIP_COOLDOWN_H * 3600:
            vip_fire.append(m)
            alerts[k] = now_ts

    # 状態更新は必ず行う (次回の差分基準)
    state["positions"] = cur_pos
    state["updated"] = now.isoformat()
    save_state(state)

    if first_run:
        print(f"初回実行: 基準状態を保存 ({len(cur_pos)}ウォレット)。通知なし。")
        return
    if not fire and not vip_fire:
        print(f"シグナルなし (コンセンサス検知{len(signals)}件/VIP検知{len(vip_moves)}件は"
              "クールダウン中または閾値未満)。")
        return

    if fire:
        log_signals(now, fire)
    if vip_fire:
        log_vip(now, vip_fire)

    # ---- Discord通知の組み立て ----
    lines = []
    chart_specs = []  # (coin, タイトル, entry)
    for (coin, side), rows in sorted(fire.items(),
                                     key=lambda kv: -sum(r[1] for r in kv[1])):
        total = sum(r[1] for r in rows)
        avg_e = sum(r[2] for r in rows) / len(rows)
        emoji = "🟢LONG" if side == "L" else "🔴SHORT"
        wallets = " ".join(_wallet_link(r[0]) for r in rows[:5])
        more = f" +{len(rows)-5}人" if len(rows) > 5 else ""
        lines.append(f"**{coin}** {emoji} — **{len(rows)}人** 合計 **${total:,.0f}**\n"
                     f"　└ {wallets}{more}")
        # チャートタイトルは絵文字なし (matplotlibのフォントに無いため)
        chart_specs.append((coin, f"{len(rows)}人 {'LONG' if side == 'L' else 'SHORT'}",
                            avg_e, side))

    vip_lines = []
    for a, kind, coin, side, usd, entry in sorted(vip_fire, key=lambda m: -m[4]):
        emoji = "🟢L" if side == "L" else "🔴S"
        act = "🆕新規/転換" if kind == "OPEN" else "❎クローズ"
        vip_lines.append(f"{_wallet_link(a)} {act} **{coin}** {emoji} ${usd:,.0f}"
                         + (f" @ {entry:g}" if entry else ""))
        if kind == "OPEN":
            chart_specs.append((coin, f"VIP {a[:8]} "
                                f"{'LONG' if side == 'L' else 'SHORT'}",
                                entry, side))

    exp_lines = []
    for coin, usd in net_exposure(cur_pos):
        side = "🟢" if usd > 0 else "🔴"
        exp_lines.append(f"{side} {coin}: ${abs(usd):,.0f}")

    title = ("🐋 スマートマネー コンセンサス新規参入" if fire
             else "⭐ VIPウォレット ムーブ")
    desc_parts = []
    if lines:
        desc_parts.append("月間PnL上位ウォレットが同一銘柄・同方向に群がった:\n\n"
                          + "\n".join(lines))
    if vip_lines:
        desc_parts.append("__**⭐ VIP (30日実現PnL上位8人) の単独ムーブ**__\n"
                          + "\n".join(vip_lines))
    embed = {
        "title": title,
        "description": "\n\n".join(desc_parts)[:3900],
        "color": 0x1E90FF if fire else 0xFFD700,
        "fields": [
            {"name": "📊 追跡ウォレット合算ネットポジション上位",
             "value": "\n".join(exp_lines)[:1000] or "-", "inline": False},
            {"name": "⚠️ 使い方",
             "value": ("これは**銘柄の監視リスト入り候補**であって追随命令ではない。\n"
                       "1. 彼らのentry価格より不利なら追わない (遅行データ)\n"
                       "2. RULES.mdのマクロ環境 (FGI/BTC方向) と整合してから\n"
                       "3. 検証: コンセンサスは+24hで平均+1.4%/勝率60% (30日, n=51)"),
             "inline": False},
            {"name": "🔎 ソース元",
             "value": (f"データ: [Hyperliquid 公式リーダーボード]({HL_LEADERBOARD_URL}) の"
                       "公開API (認証不要)。ウォレット名をタップすると"
                       f" [HL Explorer]({HL_EXPLORER}) で実際の取引履歴を確認できる\n"
                       "収集/判定ロジック: `smart_money/collect_smart_money.py` + "
                       "`smart_money_tracker.py` (GitHub Actions)"),
             "inline": False},
        ],
        "footer": {"text": f"Smart Money Tracker v2 | 追跡{len(cur_pos)}ウォレット "
                           f"(VIP{len(vips)}) | {now.strftime('%Y-%m-%d %H:%M JST')}"},
    }

    # 重複銘柄を除いて最大4銘柄のチャートを添付
    seen, uniq_specs = set(), []
    for s in chart_specs:
        if s[0] not in seen:
            seen.add(s[0])
            uniq_specs.append(s)
    chart = render_chart(uniq_specs)
    if chart:
        embed["image"] = {"url": f"attachment://{os.path.basename(chart)}"}

    mention = "@everyone" if MENTION_EVERYONE else ""
    allowed = {"parse": ["everyone"]} if MENTION_EVERYONE else {"parse": []}
    discord_post({"content": mention, "embeds": [embed],
                  "allowed_mentions": allowed}, image_path=chart)
    print(f"Discord通知送信: コンセンサス{len(fire)}件 / VIP{len(vip_fire)}件"
          f"{' (チャート付き)' if chart else ''}。")


if __name__ == "__main__":
    main()
