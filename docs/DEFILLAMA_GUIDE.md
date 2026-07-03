# DefiLlama 完全ガイド + スマートマネー追跡の方法論 (④教材)

> 元ネタ (Twitterの実力者のコメント):
> 「bot、というか仮想通貨の戦場選び、選球眼を養いたいなら defillama にのってる
> PJ全てを開いて一番儲かってる人2000アドレスくらい収集してパクればいいと思うよ。」

このコメントを分解すると、やるべきことは2つに分かれる。

| 工程 | 意味 | 使うデータ |
|---|---|---|
| **戦場選び** | どのチェーン/カテゴリ/プロトコルに資金・出来高・手数料が集まっているか | DefiLlama (このドキュメントの前半) |
| **選球眼** | その戦場で実際に勝っている人間を特定し、行動をパクる | 各プロトコルのリーダーボード/オンチェーンデータ (後半) |

**重要な前提**: DefiLlama自体には「儲かっている人のアドレス」は載っていない。
DefiLlamaはTVL・出来高・手数料の**集計サイト**であり、個人のPnLランキングは持たない。
実力者の言う「PJ全てを開いて」は「DefiLlamaで有望PJを見つけ→各PJの
リーダーボード/エクスプローラーを開いて」という意味に読むのが正しい。

---

## 1. DefiLlama の全機能マップ (戦場選びの道具箱)

DefiLlama (https://defillama.com) は DeFi 最大の無料データアグリゲーター。
主要セクションと「戦場選び」での使い方:

### 1-1. TVL系 (資金がどこに置かれているか)

| セクション | URL | 戦場選びでの意味 |
|---|---|---|
| Protocols (全プロトコルTVLランキング) | `/` | 資金量の絶対値。**6,000以上のPJ**が全チェーン横断で並ぶ |
| Chains (チェーン別TVL) | `/chains` | どのチェーンが主戦場か。ETH/Solana/Base/Arbitrum/HyperEVM… |
| カテゴリ別 | `/protocols/{category}` | Lending / DEX / Liquid Staking / Perp… 業種ごとの规模 |
| Compare | `/compare` | チェーン同士・プロトコル同士の成長比較 |
| Recent (新規上場PJ) | `/recent` | **新しく載ったPJ = 早期に触ると報われやすい戦場候補** |
| Forks | `/forks` | フォーク系の乱立状況 (レッドオーシャン判定) |
| Oracles / Treasuries / Entities | 各 | 裏方インフラ・運営資金の健全性 |

### 1-2. 出来高・手数料系 (カネが「動いている」場所) ★最重要

TVLは「置かれている」だけの資金も含む。トレーダーの戦場選びでは
**出来高 (volume) と手数料 (fees/revenue)** の方が本質的。

| セクション | URL | 意味 |
|---|---|---|
| DEXs (現物DEX出来高) | `/dexs` | 現物の主戦場。Uniswap/PancakeSwap/Raydium/Orca… |
| Perps (無期限先物出来高) | `/perps` | **perpの主戦場。Hyperliquidが圧倒的シェア** |
| Fees & Revenue | `/fees` | 実際にユーザーが金を払っているPJ = 本物の需要 |
| Options / Aggregators / Bridge Volume | 各 | 派生戦場 |
| Stablecoins | `/stablecoins` | ステーブル発行量 = チェーンへの資金流入の先行指標 |

**読み方のコツ**: 「手数料が伸びている × TVLがまだ小さい」= 資本効率が高く
インセンティブ(エアドロ等)が出やすい若い戦場。逆に「TVL巨大 × 手数料薄い」は
枯れた戦場。

### 1-3. イベント・カタリスト系 (いつ動くか)

| セクション | URL | 使い方 |
|---|---|---|
| Unlocks (トークンアンロック) | `/unlocks` | 供給ショックのカレンダー。ショート/回避の材料 |
| Raises (資金調達) | `/raises` | VCがどこに張っているか |
| Hacks | `/hacks` | 事故履歴。触ってはいけないPJの照合 |
| ETFs | `/etfs` | 機関フロー (RULES.mdのマクロスイングCと接続) |
| Narrative Tracker | `/narrative-tracker` | ナラティブ別のパフォーマンス比較 |
| Yields | `/yields` | 2万以上のプール利回り。キャリー戦略の土台 |

### 1-4. API (このリポジトリが使うもの)

全部無料・認証不要 (https://api-docs.defillama.com):

```
GET https://api.llama.fi/protocols            # 全プロトコル + TVL + カテゴリ
GET https://api.llama.fi/v2/chains            # チェーン別TVL
GET https://api.llama.fi/overview/dexs        # DEX出来高ランキング (24h/7d/30d)
GET https://api.llama.fi/overview/derivatives # perp出来高ランキング
GET https://api.llama.fi/overview/fees        # 手数料/収益ランキング
GET https://api.llama.fi/protocol/{slug}      # 個別PJのTVL時系列
GET https://coins.llama.fi/prices/current/... # トークン価格
GET https://stablecoins.llama.fi/stablecoins  # ステーブル供給
```

→ `smart_money/collect_smart_money.py` が protocols/chains/dexs/derivatives/fees
を取得して `data/smart_money/` に保存する。

---

## 2. 「一番儲かってる人2000アドレス」をどう集めるか (選球眼)

### 2-1. 収集源の現実的な選択肢

| 収集源 | アドレス数 | PnLの信頼性 | コスト | 備考 |
|---|---|---|---|---|
| **Hyperliquid 公式リーダーボード** | 全ユーザー(数十万) | ◎ 取引所公式のPnL | 無料 | **本命。1ソースで2000どころか全員取れる** |
| GMX/他perp DEXのリーダーボード | 数千 | ○ | 無料(subgraph) | 戦場としてはHLより小さい |
| Dune Analytics | 任意 | △ 自作クエリ次第 | 無料枠あり | DEX現物の勝ち組抽出に最強だがSQL必要 |
| Nansen / Arkham | ラベル付き | ○ | 有料 | 「Smart Money」ラベルを買う選択肢 |
| Birdeye / GeckoTerminal | トークン別上位 | △ | 無料枠 | Solanaミームの勝ち組追跡向け |
| ブロックエクスプローラー直 | 任意 | × 自力計算 | 無料 | 上級者向け |

**このリポジトリの選択**: Hyperliquid 一本に絞る。理由:
1. DefiLlamaのperp出来高で圧倒的1位 = 実力者の言う「一番カネが動く戦場」そのもの
2. 公式API (`stats-data.hyperliquid.xyz/Mainnet/leaderboard`) が全ユーザーの
   日/週/月/全期間の PnL・ROI・出来高 を無料で返す — **「儲かってる人」の定義が公式数値で済む**
3. `api.hyperliquid.xyz/info` でアドレスごとの全約定(fills)・現在ポジションまで取れる
   → 「どの銘柄を・いつ・どこで売買したか」が完全に再構成できる
4. 全て認証不要・無料。オンチェーン公開データなので合法かつ规約違反もない

### 2-2. パイプライン (実装済み)

```
smart_money/collect_smart_money.py  (GitHub Actions: smart-money.yml)
  Stage1: DefiLlama → 戦場の地図 (5ファイル)
  Stage2: HLリーダーボード → 月次PnL上位2000アドレス
  Stage3: 上位200人 × 直近30日 → 全約定 + 現在ポジション
        ↓ data/smart_money/*.csv(.gz) にコミットバック
notebooks/smart_money_analysis.ipynb  → 分析・グラフ (教材)
smart_money_tracker.py                → 定期監視・Discord通知 (シグナルBot)
```

### 2-3. 「パクる」の解像度を上げる — 何を見るか

儲かっている人の約定履歴から抽出できる情報と、その使い道:

1. **銘柄選択 (what)** — 上位勢の売買が集中している銘柄リスト。
   彼らは板の厚さ・ボラ・手数料を織り込んだ上で銘柄を選んでいる。
   「勝ち組の出来高上位銘柄」は、そのまま自分の監視リストの土台になる。
2. **タイミング (when)** — 時間帯ヒートマップ (UTC/JST)。
   米国時間に偏るのか、指標発表時か、24時間張り付きBotか。
   人間の裁量トレーダーとBotは時間分布で見分けられる。
3. **サイズと方向 (how)** — 1回あたりのロット、買い/売り比率、
   Open/Close の方向 (`dir` フィールドで新規ロング/ショート/決済が分かる)。
4. **保有時間** — 同一銘柄のOpen→Closeの間隔。スキャル/デイ/スイングの分類。
5. **勝ちの源泉** — `closedPnl` を銘柄別に合計すると「その人がどの銘柄で
   稼いだか」が直接出る。出来高が多い銘柄と稼いだ銘柄は往々にして違う。
6. **今のポジション (合意形成)** — 上位N人の現在ポジションを合算すると
   「スマートマネーの総意」が出る。急な偏りの変化はシグナルになる。

### 2-4. 罠と限界 (正直な注意)

- **リーダーボードの生存バイアス**: 月間PnL上位は「今月たまたま勝った
  ハイレバ勢」を含む。allTime PnL・ROI・出来高を併用してフィルタすること。
- **Botのコピーは無意味**: HFT/マーケットメイクBot (約定数が異常に多い・
  往復が秒単位) の銘柄選択はパクれても執行はパクれない。約定間隔で除外する。
- **Vault/サブアカウント**: displayName付きはVaultの可能性。中身は他人の金。
- **遅行性**: fillsは事後データ。コピートレードは常に「彼らより悪い価格」で
  入ることになる。**パクるのは個別トレードではなく銘柄選択と戦場選び**、
  これが元ツイートの本旨。
- **規模の非対称**: 口座$10Mの人の1%リスクと自分の$200では最適解が違う。
  小資本はむしろ彼らが「入り始めた」小型銘柄の初動に妙味がある。

---

## 3. このデータから作れるシグナル (実装との対応)

| シグナル | ロジック | 実装 |
|---|---|---|
| **コンセンサス新規参入** | 上位N人のうちK人以上が同一銘柄・同方向に新規ポジション | `smart_money_tracker.py` (実装済み) |
| コンセンサス転換 | 銘柄の合算ネットエクスポージャーの符号反転 | tracker拡張候補 |
| 新戦場アラート | DefiLlama日次スナップショットでperp/DEX出来高の急伸検知 | 収集を定期化すれば可能 |
| 勝ち組L/S乖離 | 勝ち組と負け組のポジション方向が割れた時、勝ち組に従う | 既存 `hypertracker.py` が同思想 (HT API) |
| リスト更新 | 月次でリーダーボード再取得→追跡アドレスを入れ替え | `smart-money.yml` を月1で手動実行 |

既存のRULES.md体系との位置づけ: FGI/FR/ベーシスが「市場の歪み」を見るのに対し、
スマートマネー追跡は「勝者の行動」を見る。**直交する情報源**なので、
両方が同じ方向を向いた時だけ張る、という使い方が理にかなう。
