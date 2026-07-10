# CLAUDE.md — プロジェクトメモリ

## このリポジトリは何か

暗号資産の**シグナル通知ツール群** (実行Botではない)。GitHub Actionsで定期実行し、
条件を満たした時だけDiscordへ通知する「点灯型」が設計原則。
状態はJSONでコミットバック、発火履歴はCSVに蓄積して後検証する。

## コーディング規約 (既存コードに合わせること)

- 監視/通知スクリプトは**Python標準ライブラリのみ** (チャートを描くものだけ
  matplotlib+matplotlib-fontjaをworkflowでpip install。未導入でもテキスト通知で動く設計)
- コメント・ドキュメント・Discord通知は日本語
- 設定は環境変数 (`os.environ.get(X) or "default"` — pushイベントでinputsが
  空文字になるため `or` 必須)
- Webhook: `SMART_MONEY_WEBHOOK_URL` 優先、なければ `DISCORD_WEBHOOK_URL`。
  **通知には @everyone を付ける** (MENTION_EVERYONE='1')
- cron分は :00/:15/:30 を避けてオフセット (GitHub負荷ピークでドロップするため)
- コミットバックは `git add` を**1ファイルずつ** (複数まとめると未存在ファイルで
  全体がabortし、状態が永続化されないバグを踏んだ実績あり)
- workflowには concurrency を付けて直列化 (並走コミットバック競合でデータ喪失の実績あり)
- 外部APIを叩くスクリプトは `socket.setdefaulttimeout(35)` + 全体デッドライン
  (HL API応答待ちでジョブがハングした実績あり)

## ツール一覧 (①〜④)

| # | 何 | ファイル | workflow / 頻度 |
|---|---|---|---|
| ① | 取引所間価格スプレッド検知 | `screener.py` → 現在は `unified_signal.py`+`long_signal.py` | `scan.yml` 15分ごと |
| ② | FR極端モニター (キャリー/パニック/取引所間乖離) | `fr_extreme_monitor.py` | `fr-extreme.yml` 30分ごと |
| ④ | スマートマネー追跡 (下記詳細) | `smart_money/` ほか | 複数 |
| ⑤ | 日本株資金集中スクリーナー (下記詳細) | `jp_stock_fetch.py`+`jp_money_flow.py`+`dashboard/` | 複数 |

## ④ スマートマネー追跡 (2026-07-03 実装, PR #3)

元ネタ: 「defillamaにのってるPJ全てを開いて一番儲かってる人2000アドレス
くらい収集してパクればいい」→ 戦場選び→選球眼→手法解剖→シグナル化 の4段で実装。

### コンポーネント

| ファイル | 役割 | 実行 |
|---|---|---|
| `smart_money/collect_smart_money.py` | 収集器: DefiLlama全プロトコル + HLリーダーボード上位2000 + 上位1000人の30日約定/現在ポジション。約定間隔中央値<60秒でBot判定し `tracked_addresses.csv` 出力 | `smart-money.yml` **月1回手動** (Actionsから) |
| `smart_money_tracker.py` | シグナルBot: 🐋コンセンサス新規参入 (非Bot 3人以上・同一銘柄・同方向・$10k+, CD12h) + ⭐VIP単独ムーブ (PnL上位8人・$100k+・新規/転換/クローズ, CD6h)。7日1h足チャート添付 (ロング=緑↑矢印/ショート=赤↓矢印でentry描写) | `smart-money-tracker.yml` 毎時:17 |
| `smart_money_report.py` | 週次/デイリーレポート: 今回窓vs前回窓比較で急上昇▲/手仕舞い▼/主戦場/実現PnL。チャート3枚添付 (比較4枚組・時間帯ヒートマップ・現在ポジション)。全体増減%×増減銘柄数で「資金集中=ファンダ発生の可能性」を自動判定。DefiLlama新規上場 (直近14日・TVL$500k+) を検知する新戦場アラートも同梱 | 週次=`smart-money-report.yml` 月曜21:23 JST / 日次=`smart-money-daily.yml` 毎日21:07 JST |
| `smart_money/sm_filter.py` | 拒否権フィルター: SM合算ネットポジション$2M+に逆らう候補を unified_signal(ショート)/long_signal(ロング) から自動除外。state無し/6h超は素通し | 両シグナルに統合済み |
| `notebooks/smart_money_analysis.ipynb` | 教材ノート (24セル・グラフ13枚, 実行済み) | 手動 |
| `docs/DEFILLAMA_GUIDE.md` | DefiLlama全機能マップ + 方法論 + 罠 | - |
| `.github/workflows/sm-webhook-test.yml` | Webhook疎通テスト (ファイル更新pushまたはdispatchで発火) | 手動 |

### データ (data/smart_money/)

- `leaderboard_top2000.csv` 月間PnL上位2000 (全40,074ユーザーから)
- `tracked_addresses.csv` 上位500 + Bot判定 (Bot337/人間163)
- `vip_addresses.csv` 30日実現PnL上位の人間8人
- `fills_topN.csv.gz` 500人×30日の全約定 (427,990件/$14.1B)
- `positions_topN.csv` / `hl_meta.json` / `defillama_*.csv` / `attention_screen.csv` (週次) / `attention_daily.csv` (日次)
- 状態: `smart_money_state.json` / 履歴: `smart_money_signals_log.csv`, `smart_money_vip_log.csv`

### 実データで検証済みの知見 (2026-07-03, 30日窓)

- HL=perp戦場の6割超。PnLは上位100人に6割集中
- **月間PnL上位500人中337人はMM/HFT Bot** (2026-07-03収集時点) → 追跡は人間系のみ
  (Botの執行はパクれない)。2026-07-04にFILLS_N=1000へ拡大したので次回収集後に再確認
- コンセンサスのイベントスタディ: **+24hで平均+1.4%・勝率60%** (n=51, ベースライン±0.2%)
- 閾値は追跡母数に比例して変わる: 人間163人ではBot除外×3人=1.5回/日が実用域
  (×2人=5.5回/日で過多)。**収集のたびにノート§5のスイープで再確認**
- 「売買代金上位」≠「稼ぎ頭」。パクるなら実現PnL上位を見る
- **勝ち組人間の戦場は xyz: (HL上の株式/商品perp) へ移行中** (GOLD/BRENTOIL/半導体株)

### 運用

- 全自動: tracker毎時 / デイリー21:07 / 週次(月)21:23 — 通知が無い=大きな動きが無い
- **月1回だけ手動**: Actions → Smart Money Collect (追跡リスト/VIP入れ替え, 約16分)
- 閾値調整はworkflowのenvのみ (MIN_WALLETS, MIN_POS_USD, VIP_MIN_POS_USD, MIN_RISE_USD)
- 運用ルール (通知が来た時の手順・禁止事項) は RULES.md ④節

### ハマりどころ (再発防止)

1. フィーチャーブランチのみのworkflowは workflow_dispatch 未登録 → push トリガーで代用
2. GitHub上のpushイベントでは `inputs.*` が空文字 → `int('')` で落ちる
3. secretsはリポジトリ管理者しか登録できない。**public repoにWebhook URLを
   コミットするとDiscordのsecret scanningで自動無効化される** — 絶対にコミットしない
4. HLリーダーボードは `stats-data.hyperliquid.xyz/Mainnet/leaderboard` (数十MB)。
   info APIはweight 1200/min (userFillsByTime=20, clearinghouseState=2)
5. matplotlibのフォントに絵文字なし → チャートタイトルはテキストのみ

## ⑤ 日本株資金集中スクリーナー (2026-07-05〜 実装, PR #6 / 需給レイヤーPR #8 / 500銘柄化)

暗号資産とは別軸。日本株**約500銘柄 (TOPIX500+日経225+話題枠, leader/core/hot)** の
1分足〜月足をYahoo Finance非公式APIから収集し、売買代金の異常集中を検知して
ダッシュボードとしてGitHub Pagesに常時公開する。
JPX公式の空売り残高報告(大口0.5%以上・日次)を需給の裏付けとして追加済み (下記コンポーネント表)。

### コンポーネント

| ファイル | 役割 | workflow / 頻度 |
|---|---|---|
| `universe_refresh.py` | 監視ユニバースの機械構築: JPX「東証上場銘柄一覧」(data_j.xls, 規模区分Core30+Large70+Mid400=TOPIX500, 旧BIFF形式でxlrd必須)+日経公式構成銘柄ウエイトCSV(cp932)から `universe.csv` を再構築。bucket優先順位 hot(手動シード温存)>leader(日経225)>core(TOPIX500残り)。既存銘柄の手書きsectorは温存、新規はJPX33業種区分で機械付与 | `universe-refresh.yml` 月1回(毎月3日 21:41 UTC) |
| `jp_stock_fetch.py` | Yahoo Finance非公式チャートAPI(v8/finance/chart, query1→query2フォールバック)から1分足を取得し `data/jp_stocks/{code}_T_1m.csv` に重複排除で差分追記。取得対象は `data/jp_stocks/universe.csv`(code,name,bucket,sector 約500銘柄)。通常巡回はRANGE=1d軽量化(497銘柄実測231秒・429ゼロ)、日またぎ欠損は引け後のRANGE=5d追取りで回収。429検知で適応的バックオフ | `jp-stock.yml` 東証立会時間の平日30分間隔(毎時23分・53分) / `jp-stock-history.yml` 平日引け後1回(日足2y/週足5y/月足max/1分足5d追取り) |
| `jp_stock_rotate.py` | ストレージ肥大化対策: 1分足ライブファイルを直近ROLLING_DAYS(7)営業日に切り詰め、溢れた分を月次gzipアーカイブ `{code}_T_1m_YYYYMM.csv.gz` へ退避(多重メンバーgzip追記)。アーカイブは後検証用でスクリーナー/ダッシュボードは読まない | `jp-stock-history.yml` の最終ステップ |
| `jp_money_flow.py` | 売買代金(終値×出来高)の異常集中スクリーナー。直近窓vs履歴中央値でsurge/z/share_deltaを算出し `data/jp_stocks/money_flow.{csv,json}` を出力。json内commentaryは事実ベースの自動分析文。497銘柄で1秒未満。`window_stats()`はdashboardの窓統計事前計算からも再利用。業種グループ集計はJPX33業種+独自区分 — `data/jp_stocks/custom_groups.csv`(code,custom_group,basis)の上書きで「半導体」等の公式に無い切り口を切り出せる(手順はSKILL.md (4b)節) | `jp-stock.yml`/`pages.yml` に統合済み |
| `dashboard/template.html` + `dashboard/build_dashboard.py` | **遅延読み込み型**ダッシュボード: `site/index.html`(自動分析コメント・急騰アラート・集計窓1分〜月足のランキング統計をPython側で事前計算して埋め込み・検索可能セレクタ・空売り残バッジ・初期選択銘柄のチャートのみ同梱)+銘柄別チャートJSON `site/data/{code}.json` を生成。銘柄選択時にfetchで遅延取得(同一オリジン) | `pages.yml` の1ステップ |
| — | `site/` (index.html + data/*.json) をGitHub Pagesにデプロイ | `pages.yml`(`jp-stock.yml`完了ごとにworkflow_run発火、schedule 06:37 UTCバックアップ、workflow_dispatch可) |
| `jp_supply_demand.py` | JPX需給レイヤー: 「空売りの残高に関する情報」(発行済株式数0.5%以上の大口報告、日次、旧xls形式)から一覧ページを都度スクレイピングしてuniverse該当分だけ抽出し `data/jp_stocks/supply_demand/short_positions.csv` に投資家単位で差分蓄積。`jp_money_flow.py`のcommentaryと`dashboard`のバッジに供給。xlrd未導入時は自動スキップ(コア機能は継続) | `jp-supply-demand.yml` 平日18:07 JST(空売り残高公表17:00の後) |

公開URL: **https://sousensei3319-prog.github.io/arbitrage-signal/**

### 運用

- 全自動 (収集→スクリーナー→ダッシュボード→Pagesデプロイの一気通貫)。手動操作は基本不要
- 詳細な運用手順(データ鮮度確認・手動収集・Pages再デプロイと障害切り分け・銘柄追加/削除・
  閾値調整・ダッシュボード改修時の検証)は **`.claude/skills/jp-stock-ops/SKILL.md`** 参照

### ハマりどころ (再発防止・詳細はSKILL.md)

1. サンドボックス(Claude Code)からはYahoo/github.ioがproxy403で到達不可。実データ検証は
   GitHub Actionsランナー上の実行結果(run conclusion・ログ)でのみ判断可能
2. Yahoo月足APIは新規上場銘柄(285A等)に日足相当のデータを返すバグがある → 週足/月足は
   Yahooの1wk/1moを直接信用せず、自前で日足から集約する設計
3. エポック時刻基準で複数銘柄の窓を揃えると銘柄間の最終バーずれで偽の急騰(集中度)が
   生まれる → 日付・インデックス基準で揃える
4. 日足ファイルの当日バーは寄り直後取得で未確定値になる → 当日分は1分足から再構成
5. 1分足はYahoo側の直近5〜7日制限があるため、定期実行+重複排除で実行間隔を超える
   連続履歴を自前で積み上げる設計。数日止まると欠損は埋め戻せない
6. JPX空売り残高報告のExcelは旧OLE2/BIFF形式(.xls)で配信され、openpyxlでは開けない
   (xlrdが必要、2026-07-09にActionsランナー上の実データで確認)。一覧ページのDLリンクは
   日付ごとにハッシュ化されたディレクトリ名を含み予測できないため、毎回一覧HTMLをスクレイピング
   してリンクを解決する設計。信用取引週末残高・空売り比率(市場全体)はJPX公式配信がPDFのみと
   判明したため不採用(実装しない判断が正しい判断のケース)
7. JPX「東証上場銘柄一覧」(data_j.xls)も同じ旧BIFF形式でxlrd必須。TOPIX500は規模区分
   Core30+Large70+Mid400の合算だが、定期見直しの端境期には500ちょうどにならない
   (2026-07-09実測: 31+68+394=493)。500に無理に合わせる加工はしない
8. 東証の証券コードは4文字で先頭1桁のみ数字保証(285A等の新コード体系)。「4桁数字」の
   正規表現だと取りこぼす — `^[0-9][0-9A-Za-z]{3}$` を使う
9. 日経225公式ウエイトCSVはcp932エンコードで、末尾に著作権表示の脚注行が混じる
   (コード列の形式チェックで除外する)
10. templateとbuilderでファイル名規則を合わせる: 銘柄別JSONは `7203.T`→`7203_T.json`。
    またtemplateには `<meta charset="utf-8">` が必須 (ローカルhttp.server検証で
    charset無しだと文字化けする。Pagesはヘッダーで補うため顕在化しない)
