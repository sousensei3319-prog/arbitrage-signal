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
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
MENTION_EVERYONE    = os.environ.get("MENTION_EVERYONE", "0") == "1"

TRACK_N      = int(os.environ.get("TRACK_N") or "100")       # 追跡アドレス数
MIN_WALLETS  = int(os.environ.get("MIN_WALLETS") or "3")     # コンセンサス閾値(人)
MIN_POS_USD  = float(os.environ.get("MIN_POS_USD") or "10000")  # ノイズ除外: この額未満の新規ポジは無視($)
COOLDOWN_H   = float(os.environ.get("COOLDOWN_H") or "12")   # 同一銘柄・同方向の再通知間隔(h)
ADDR_FILE    = os.environ.get("ADDR_FILE") or "data/smart_money/leaderboard_top2000.csv"
STATE_FILE   = os.environ.get("STATE_FILE") or "smart_money_state.json"
LOG_FILE     = os.environ.get("LOG_FILE") or "smart_money_signals_log.csv"

HL_INFO = "https://api.hyperliquid.xyz/info"


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


def load_addresses():
    """収集済みリーダーボードから上位TRACK_Nアドレスを読む"""
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


def discord_post(payload):
    if not DISCORD_WEBHOOK_URL:
        print("  [DRY-RUN] DISCORD_WEBHOOK_URL 未設定 — 通知内容:")
        print(json.dumps(payload, ensure_ascii=False, indent=1)[:3000])
        return
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "smart-money-tracker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except urllib.error.HTTPError as e:
        print(f"Discord error: {e.code} {e.read().decode()[:200]}")


def main():
    now = datetime.now(JST)
    print(f"Smart money tracker scan {now.strftime('%Y-%m-%d %H:%M JST')}")
    print(f"追跡: 上位{TRACK_N}人 / コンセンサス閾値: {MIN_WALLETS}人 / "
          f"最低ロット: ${MIN_POS_USD:,.0f}")

    addrs = load_addresses()
    cur_pos = fetch_positions(addrs)
    if not cur_pos:
        print("ポジション取得ゼロ — API障害の可能性。状態は変更しない。")
        return

    state = load_state()
    prev_pos = state.get("positions", {})
    first_run = not prev_pos

    signals = detect_consensus(prev_pos, cur_pos)

    # クールダウン適用
    now_ts = time.time()
    fire = {}
    for key, rows in signals.items():
        k = f"{key[0]}|{key[1]}"
        last = state.get("last_alerts", {}).get(k, 0)
        if now_ts - last >= COOLDOWN_H * 3600:
            fire[key] = rows
            state.setdefault("last_alerts", {})[k] = now_ts

    # 状態更新は必ず行う (次回の差分基準)
    state["positions"] = cur_pos
    state["updated"] = now.isoformat()
    save_state(state)

    if first_run:
        print(f"初回実行: 基準状態を保存 ({len(cur_pos)}ウォレット)。通知なし。")
        return
    if not fire:
        print(f"コンセンサス新規参入なし (検知{len(signals)}件はクールダウン中)。")
        return

    log_signals(now, fire)

    # Discord通知
    lines = []
    for (coin, side), rows in sorted(fire.items(),
                                     key=lambda kv: -sum(r[1] for r in kv[1])):
        total = sum(r[1] for r in rows)
        emoji = "🟢LONG" if side == "L" else "🔴SHORT"
        wallets = " ".join(f"`{r[0][:8]}`" for r in rows[:5])
        more = f" +{len(rows)-5}人" if len(rows) > 5 else ""
        lines.append(f"**{coin}** {emoji} — **{len(rows)}人** 合計 **${total:,.0f}**\n"
                     f"　└ {wallets}{more}")

    exp_lines = []
    for coin, usd in net_exposure(cur_pos):
        side = "🟢" if usd > 0 else "🔴"
        exp_lines.append(f"{side} {coin}: ${abs(usd):,.0f}")

    embed = {
        "title": "🐋 スマートマネー コンセンサス新規参入",
        "description": ("月間PnL上位ウォレットが同一銘柄・同方向に群がった:\n\n"
                        + "\n".join(lines))[:3900],
        "color": 0x1E90FF,
        "fields": [
            {"name": "📊 追跡ウォレット合算ネットポジション上位",
             "value": "\n".join(exp_lines)[:1000] or "-", "inline": False},
            {"name": "⚠️ 使い方",
             "value": ("これは**銘柄の監視リスト入り候補**であって追随命令ではない。\n"
                       "1. 彼らのentry価格より不利なら追わない (遅行データ)\n"
                       "2. RULES.mdのマクロ環境 (FGI/BTC方向) と整合してから\n"
                       "3. 発火履歴は smart_money_signals_log.csv で後検証"),
             "inline": False},
        ],
        "footer": {"text": f"Smart Money Tracker v1 | 追跡{len(cur_pos)}ウォレット | "
                           f"{now.strftime('%Y-%m-%d %H:%M JST')}"},
    }
    mention = "@everyone" if MENTION_EVERYONE else ""
    allowed = {"parse": ["everyone"]} if MENTION_EVERYONE else {"parse": []}
    discord_post({"content": mention, "embeds": [embed], "allowed_mentions": allowed})
    print(f"Discord通知送信: {len(fire)}シグナル。")


if __name__ == "__main__":
    main()
