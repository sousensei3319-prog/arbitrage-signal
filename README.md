# Cross-Exchange Arbitrage Signal v1（①価格スプレッド検知）

複数取引所の同一銘柄の価格「歪み（スプレッド）」を検知し、Discord に通知する**シグナルツール**。
実行ボットではない（retail のレイテンシでは同時約定ボットは勝てない、という調査結論に基づく設計）。
まず「取れるスプレッドが本当に発生しているか」を可視化・記録するのが目的。

## パイプライン

1. **収集** — OKX / Bybit / Gate / MEXC / KuCoin / Bitget の現物 USDT ペアの best bid/ask を一括取得。
   （Binance.com は GitHub Actions(US) でジオブロックのため除外）
2. **生スプレッド** — 2取引所以上に存在する共通シンボルで `(最高bid − 最安ask)/最安ask` を計算。
3. **罠フィルタ** — `MAX_SPREAD`(既定25%)超は同名別トークン/上場停止/枯れ板としてカット。
4. **★板厚ゲート★** — 上位候補だけ両脚の板を取得し、`TARGET_NOTIONAL`($)を約定したときの
   スリッページ込み実効スプレッドを計算。板が薄くて約定不可なら「取れない罠」として除外。
5. **通知** — 生き残りを Discord に集計時刻つきで送信。

## 設定（環境変数）

| 変数 | 既定 | 意味 |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | (空=DRY-RUN) | Discord Webhook。未設定なら送信せずログのみ |
| `MENTION_EVERYONE` | `0` | `1` で @everyone |
| `RAW_SPREAD_MIN` | `0.4` | 生スプレッドの一次足切り(%) |
| `NET_SPREAD_MIN` | `0.5` | 板厚込み実効スプレッドの点灯閾値(%) |
| `MAX_SPREAD` | `25` | これ超は罠としてカット(%) |
| `TARGET_NOTIONAL` | `200` | 板厚検証で想定する片道ロット($) |
| `MIN_VOLUME_24H` | `1000000` | 24h出来高フィルタ($) |
| `MAX_SIGNALS` | `12` | Discord 送信最大件数 |

## ローカル実行

```bash
python screener.py            # DRY-RUN（送信せずログのみ）
# 本番:
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." python screener.py
```

依存なし（Python 標準ライブラリのみ）。

## GitHub Actions

`.github/workflows/scan.yml` が5分ごとに実行。
リポジトリ Secrets に `DISCORD_WEBHOOK_URL` を登録するだけ。

## v1 の既知の限界（正直な注意）

- **シンボル衝突**: 25%以下でも、同名別トークンによる偽スプレッドが残りうる
  （例: "AI" "EDGE" など汎用ティッカー）。完全除去には将来トークン識別
  （コントラクトアドレス/名称照合）が必要。中〜高スプレッドは人間が要確認。
- **スナップショット誤差**: 表示値は取得時点の板。約定時には縮小している可能性が高い。
- **送金型は不可**: 着金10〜30分の間に歪みが消える。両取引所に事前資金を置いた
  同時約定でのみ現実的。これは「稼ぐ手段」ではなく「監視インフラの土台」。

## ② FR極端モニター（`fr_extreme_monitor.py`） — 2026-06-11 稼働開始

ロードマップ②の実装。常時キャリーは小資本では薄いため、**極端FR時のみ点灯する通知型**。

- **A: 正FR極端**（年率≥80%）— 現物L+perpSデルタ中立キャリー候補。$300/legの日次収益と手数料損益分岐を表示
- **B: 負FR極端**（年率≤-200%）— ショート過密/パニック。BTC/ETH/SOLは確定エッジ
  「FR PANIC→LONG」（-0.01%/8h、5年t=4.66）を閾値と独立に照合し最優先表示
- **C: 取引所間FR乖離**（年率差≥150%）— perp-perp両建て候補。
  **価格一致±3%ゲート**で同名別トークン罠（①のEDGE 1474%教訓）を排除
- 5取引所: OKX/Bybit/Gate/MEXC/Bitget。FR間隔（1h/4h/8h）を取引所APIから取得し年率を正確化
- クールダウン8h（`fr_monitor_state.json`をActionsがコミットバック）、
  発火履歴は`fr_signals_log.csv`に蓄積 → 将来の「FR極端の持続性」検証素材
- workflow: `.github/workflows/fr-extreme.yml`（30分ごと）。点灯時のみDiscord通知
- ⚠️ 表示FRは現行期の時点値。**実弾は決済2〜3回の継続確認後**（RULES.md）

## ④ スマートマネー追跡（`smart_money/` + `smart_money_tracker.py`） — 2026-07-03 追加

「defillamaにのってるPJ全てを開いて一番儲かってる人2000アドレスくらい収集してパクる」の実装。
**戦場選び (DefiLlama) → 選球眼 (HLリーダーボード上位2000) → 手法解剖 → シグナル化** の4段。

- **収集**: `smart-money.yml` を手動実行（月1目安）→ DefiLlama全プロトコル +
  リーダーボード上位2000 + 上位1000人の30日約定/現在ポジションを `data/smart_money/` にコミット
- **教材/分析**: `notebooks/smart_money_analysis.ipynb`（実行済みグラフ付き）と
  `docs/DEFILLAMA_GUIDE.md`。主な発見: 月間PnL上位の9割超はMM/HFT Bot（執行はパクれない）、
  PnLは上位100人に6割集中、代金上位と稼ぎ頭の銘柄は別物
- **通知Bot**: `smart_money_tracker.py`（`smart-money-tracker.yml`, 1hごと）が
  非Bot上位100人のポジション差分から「コンセンサス新規参入（3人以上・同一銘柄・同方向）」と
  「⭐VIP（実現PnL上位8人, `vip_addresses.csv`）の単独大口ムーブ($50k+)」を
  **7日足チャート付き**でDiscord通知。30日検証: +24h平均+1.4%・勝率60%。
  履歴は `smart_money_signals_log.csv` / `smart_money_vip_log.csv`
- **週次/デイリーレポート**: `smart_money_report.py`（週次=月曜21:23 JST /
  デイリー=毎日21:07 JST）が今回窓vs前回窓の**比較**で「急上昇▲/手仕舞い▼」「主戦場」
  「実現PnL」をチャート3枚（比較4枚組・時間帯ヒートマップ・現在ポジション）付き配信。
  全体増減と増減銘柄数から資金集中（ファンダ発生の兆候）を自動判定
- **新戦場アラート**: 同レポート内でDefiLlama新規上場（直近14日・TVL$500k+）も検知し
  「🆕新戦場アラート」欄に表示。`data/smart_money/new_protocols_{weekly,daily}.csv` に記録
- **拒否権フィルター**: `smart_money/sm_filter.py` — unified_signal/long_signal が
  スマートマネーの合算ネットポジション($2M+)に逆らう候補を自動除外
- Webhook: Secrets の `SMART_MONEY_WEBHOOK_URL`（未設定なら `DISCORD_WEBHOOK_URL`）
- ⚠️ シグナルは監視リスト入り候補。追随は常に彼らより悪い価格（RULES.md ④の運用ルール参照）

## 次の段階（ロードマップ）

- **③全部入りダッシュボード** — 価格/FR/ベーシス/板厚を統合し「方向シグナル」を出す
