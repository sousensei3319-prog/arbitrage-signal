"""
スマートマネー拒否権フィルター (⑤既存シグナルとの統合)

smart_money_tracker.py が毎時保存する smart_money_state.json (追跡ウォレットの
現在ポジション) から銘柄別ネットエクスポージャーを計算し、既存シグナルに
「勝ち組の総意に真っ向から逆らう取引を落とす」拒否権を与える。

  - unified_signal.py (ショート): スマートマネーが大きくネットロングの銘柄は見送り
  - long_signal.py    (ロング) : スマートマネーが大きくネットショートの銘柄は見送り

設計方針:
  - 攻め(銘柄追加)ではなく守り(候補削除)にのみ使う。閾値は保守的に$2M。
  - stateが無い/6時間より古い場合はフィルター無効 (空dictを返し、何も落とさない)。
    mainマージ前やtracker障害時に既存シグナルの挙動を一切変えないため。
  - 銘柄名はHL表記に合わせて素直に照合。HLの1000倍系 (kPEPE等) は k接頭辞も試す。

依存なし (標準ライブラリのみ)。
"""

import json
import os
from datetime import datetime, timedelta, timezone

STATE_FILE = os.environ.get("SM_STATE_FILE") or "smart_money_state.json"
VETO_USD   = float(os.environ.get("SM_VETO_USD") or "2000000")  # 拒否権の発動閾値($)
MAX_AGE_H  = float(os.environ.get("SM_MAX_AGE_H") or "6")       # stateの鮮度上限(h)


def load_net_exposure(state_file=None):
    """{coin: ネットポジション$ (＋=ロング)} を返す。無効時は {}。"""
    path = state_file or STATE_FILE
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        updated = datetime.fromisoformat(state.get("updated", ""))
        if datetime.now(timezone.utc) - updated > timedelta(hours=MAX_AGE_H):
            print(f"  [SM filter] stateが{MAX_AGE_H:.0f}h超に古い ({state.get('updated')}) — 無効")
            return {}
        agg = {}
        for coins in state.get("positions", {}).values():
            for coin, p in coins.items():
                usd = float(p.get("usd") or 0)
                agg[coin] = agg.get(coin, 0.0) + (usd if p.get("side") == "L" else -usd)
        return agg
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"  [SM filter] 読込失敗: {type(e).__name__} — 無効")
        return {}


def _lookup(exposure, coin):
    """HL表記との照合 (BTC / kPEPE のような1000倍接頭辞も試す)"""
    for key in (coin, f"k{coin}"):
        if key in exposure:
            return exposure[key]
    return None


def veto(coin, direction, exposure, threshold=None):
    """direction='SHORT'|'LONG'。拒否すべきなら (True, 理由文字列)。"""
    th = threshold if threshold is not None else VETO_USD
    net = _lookup(exposure, coin)
    if net is None:
        return False, ""
    if direction == "SHORT" and net >= th:
        return True, f"スマートマネーが${net/1e6:.1f}Mネットロング"
    if direction == "LONG" and net <= -th:
        return True, f"スマートマネーが${abs(net)/1e6:.1f}Mネットショート"
    return False, ""
