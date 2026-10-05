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
| ⑥ | 米国株資金集中スクリーナー (⑤の米国版・下記詳細) | `us_*.py`+`dashboard/build_us_dashboard.py` | 複数 |
| ⑦ | 検証基盤: 仮想の約定記録 / 戦略Cの答え合わせ (下記詳細) | `ledger.py` / `strategy_c_check.py` | history workflow内 / 手動 |

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
- シグナル種別の説明は RULES.md ④節

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
| `universe_refresh.py` | 監視ユニバースの機械構築: JPX「東証上場銘柄一覧」(data_j.xls, 規模区分Core30+Large70+Mid400=TOPIX500, 2026-09からxlsx形式。先頭バイトで判定し xlsx=標準ライブラリ / 旧xls=xlrd)+日経公式構成銘柄ウエイトCSV(cp932)から `universe.csv` を再構築。bucket優先順位 hot(既存hot枠温存)>leader(日経225)>core(TOPIX500残り)。**安全装置**: TOPIX500<450・日経225<200・既存leader/coreの除外>100 のどれかで書き込まず exit 2(赤)。大量入替の月は dispatch入力 max_drop で上限を上げて再実行。指数構成銘柄一覧 `index_members.csv` と上場普通株の正式名一覧 `listed_names.csv` も出力(hot_refreshが指数銘柄を消さずleader/coreへ戻す・広告文を見分けるのに使う)。既存銘柄の手書きsectorは温存、新規はJPX33業種区分で機械付与(hot_refreshが追加したsector空の話題株にもここでJPX33業種を付与) | `universe-refresh.yml` 月1回(毎月3日 21:41 UTC) |
| `hot_refresh.py` | 話題枠(hot)の**週次自動入れ替え**: Yahoo Finance JPの出来高・値上がり率ランキング上位(各RANK_TOP位)からユニバース外の個別株(ETF/投信/REIT除外)を hot に追加し、ABSENT_WEEKS(4)週連続で全ランキング圏外かつ直近集中度<KEEP_SURGE(1.3)の古いhot銘柄を除外。**leader/core(TOPIX500/日経225)は絶対に外さない**、custom_groups掲載の恒久テーマ銘柄(半導体等)は保護。追加/除外の根拠は `hot_changes_log.csv`(履歴)/`hot_changes_latest.json`(ダッシュボード「今週の話題枠入れ替え」欄)/`hot_state.json`(圏外週カウント)に記録。Yahoo JP全滅時は前週hot枠を維持したまま赤(exit 2)で知らせる(2026-10-04〜)。上限なし(枠が増えるほど収集が重くなる点だけ注意)。2026-10-03修正: 抽出を `/quote/XXXX.T` リンクに限定(広告記事を1位と誤認していた)・【】/先頭New名の除外と既存誤登録行の即時除外・解析0件は失敗扱い・同週再実行で圏外を二重カウントしない・指数銘柄はユニバースから消さない・所有ファイルだけをコミット(money_flowはコミットしない)・ランキング上の名前を東証の正式名(`listed_names.csv`)と照合して広告文を除外。2026-10-04: 正式名どおりの名前は広告表記チェック対象外(9720「ホテル、ニューグランド」の誤除外)、ETF等の除外語は普通株一覧に無いコードだけに適用(本物17銘柄を弾いていた)、一覧に無い新規上場は社名の形((株)/株式会社)のみ採用。2026-10-05: GitHubの予定実行とmainへのpushは「予備」(今週の入れ替えが済んでいれば何もしない — 予定実行が場中に遅れて動き当日の値上がり株を足していたため)。決まった時刻の起動は cron-job.org → workflow_dispatch | `hot-refresh.yml` 週1回(日曜22:11 UTC=月曜07:11 JST 寄り前) |
| `jp_stock_fetch.py` | Yahoo Finance非公式チャートAPI(v8/finance/chart, query1→query2フォールバック)から1分足を取得し `data/jp_stocks/{code}_T_1m.csv` に **upsert(同じ足は最新の取得値で上書き・新しい足は追加)** で反映(2026-10-03に「新規だけ追記」から変更: 1mの現在値ティック(分頭でない出来高0の行)は保存しない、1dは取引日をキーにして同日重複を防ぐ、1wk/1moは末尾の現在値点を入れない)。取得対象は `data/jp_stocks/universe.csv`(code,name,bucket,sector 約500銘柄)。通常巡回はRANGE=1d軽量化(497銘柄実測231秒・429ゼロ)、日またぎ欠損は引け後のRANGE=5d追取りで回収。429検知で適応的バックオフ | `jp-stock.yml` 東証立会時間の平日30分間隔(毎時23分・53分) / `jp-stock-history.yml` 平日引け後1回(日足2y/週足5y/月足max/1分足5d追取り) |
| `jp_stock_rotate.py` | ストレージ肥大化対策: 1分足ライブファイルを直近ROLLING_DAYS(7)営業日に切り詰め、溢れた分を月次gzipアーカイブ `{code}_T_1m_YYYYMM.csv.gz` へ退避(多重メンバーgzip追記)。アーカイブは後検証用でスクリーナー/ダッシュボードは読まない | `jp-stock-history.yml` の最終ステップ |
| `jp_money_flow.py` | 売買代金(終値×出来高)の異常集中スクリーナー。Yahoo 1分足の「累計出来高入り」異常足(出来高≥その日の累計×0.9・値動き<0.5%・その日20本目以降)は窓統計から除外(2026-10-04〜、ハマりどころ16)。直近窓vs履歴中央値でsurge/z/share_deltaを算出し `data/jp_stocks/money_flow.{csv,json}` を出力。json内commentaryは事実ベースの自動分析文。497銘柄で1秒未満。`window_stats()`はdashboardの窓統計事前計算からも再利用。業種グループ集計はJPX33業種+独自区分 — `data/jp_stocks/custom_groups.csv`(code,custom_group,basis)の上書きで「半導体」等の公式に無い切り口を切り出せる(手順はSKILL.md (4b)節) | `jp-stock.yml`/`pages.yml` に統合済み |
| `dashboard/template.html` + `dashboard/help_template.html` + `dashboard/build_dashboard.py` | **遅延読み込み型**ダッシュボード: `site/index.html`(自動分析コメント・急騰アラート・集計窓1分〜月足のランキング統計をPython側で事前計算して埋め込み・検索可能セレクタ・空売り残バッジ・初期選択銘柄のチャートのみ同梱)+銘柄別チャートJSON `site/data/{code}.json` を生成。銘柄選択時にfetchで遅延取得(同一オリジン)。使い方ガイド `site/help.html` も同時生成(help_template.htmlに銘柄数/最新時刻を差し込む静的ページ、ヘッダーとフッターからリンク) | `pages.yml` の1ステップ |
| — | `site/` (index.html + help.html + data/*.json) をGitHub Pagesにデプロイ | `pages.yml`(`jp-stock.yml`完了ごとにworkflow_run発火、schedule 06:37 UTCバックアップ、workflow_dispatch可) |
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
11. JPXは data_j を2026-09に旧xls→xlsxへ予告なく変更し、月次更新が解析失敗→「取れた範囲で
    構築」でcore 271銘柄を落とした(runは緑)。外部ソースの形式は拡張子でなく先頭バイトで判定し、
    件数が不自然なら**書き込まずに赤で止める(fail-closed)**。この種の更新処理で「取れた範囲で続行」はしない
12. Yahoo chart APIの実態 (2026-10-03 実データで確認): 日米とも約15分遅延で、進行中の1分は
    「分頭でない時刻・出来高0」の現在値ティックとして末尾に付く(未確定の1分足が返るのは取得の1〜2%)。
    日足は当日分を引け時刻の仮タイムスタンプで返し翌日以降は寄り時刻で返す(→取引日キーで扱う)。
    米国の日足は当日分が翌日の取得まで出てこない。週足/月足の末尾には日足相当の点が付く
13. コミットバックの競合: 5分ごとの収集(cron-job.orgから24時間dispatch)と週次/月次ジョブが
    同じファイルを触ると rebase が衝突し、旧方式は「push failed (race) - next run」で**緑のまま結果が消えた**
    (2026-09-28の話題枠入替)。週次/月次/検証ジョブは `.github/scripts/commit_back.sh`(1ファイルずつadd・
    push競合は再試行・失敗は赤)を使い、自分の所有ファイルだけをコミットする
14. Yahoo JPランキングページ最上部の広告/特集記事リンク(例「【New】キオクシアや…」)は旧正規表現で
    「出来高1位の銘柄」に見えた。さらに**銘柄ページ(/quote/5588.T)へリンクする広告文**
    (「SaaS過度懸念で売られた今、狙う成長株　提供:…」)もある(2026-10-03ランナーで確認)。
    リンク先(/quote/)・広告表記(【】、？！提供・先頭New)・**東証の正式名との照合**
    (`listed_names.csv`、universe_refreshが毎月出力)の3段で判定し、採用時は正式名に置き換える
15. hot-refresh.yml の新規銘柄の初期履歴取得は `.T` 無しのコードを JP_TICKERS に渡していたため
    **全銘柄404で毎週失敗していた**(`|| true` で緑)。jp_stock_fetch.py 側で `.T` を補完するよう修正
16. Yahoo 1分足に「1分の出来高=その日の累計」が値動きなしで入る異常足がある(2026-08に集中、日本2,062件・
    米国1,767件、複数銘柄が同時刻)。1分足合計が公式の日出来高を超える=実在しない出来高で、30分集中度を
    中央値8.2倍に跳ね上げ85%が偽の点灯。upsertでは直らない(Yahoo側の値)ので窓統計で除外する。
    日本は単独でも除外(大口は立会外で1分足に載らない)、**米国は±1分に3銘柄以上同時の時だけ**
    (米国は単独のブロック取引が本物として載る)
17. 「赤」は見られなければ緑と同じ: universe/hot/history の日米6本は main で失敗すると Discord に通知
    (`.github/scripts/notify_failure.py`)。予定実行が黙って欠けた週は history 最後の
    `.github/scripts/freshness_check.py` が拾う。マージ・大量書き直しは週末に(平日場中は収集と衝突)
18. 収集ジョブの1回の所要時間が起動間隔より長いと、待機していた run はトリガー時点の古いコミットから始まり、
    前の run と同じ1分足ファイルを書いて rebase が衝突 → 結果が捨てられる(緑の「push failed (race)」)。
    621銘柄で約6分 > cron-job.org の5分間隔になり**1回おきに捨てられていた**(2026-10-05)。
    jp-stock.yml は開始時に最新コミットへ同期する(直列化済みなので前の run の保存を必ず含む)

## ⑥ 米国株資金集中スクリーナー (2026-07-16 実装)

⑤の忠実な米国版。ファイル/データ/workflowは `us_` プレフィックスで完全並行
(**JP系ファイルとは相互不干渉** — 共有するのは `pages.yml` と GitHub Pages サイトのみ)。
公開URL: https://sousensei3319-prog.github.io/arbitrage-signal/**us/** (JPと相互リンク)。

### ⑤との差分だけ記す (それ以外は⑤と同一設計)

| 項目 | JP (⑤) | US (⑥) |
|---|---|---|
| ユニバース | TOPIX500+日経225+hot (JPX xls+日経CSV) | S&P500+Nasdaq-100+hot (**Wikipedia** 構成銘柄表, html.parserのみ・xlrd不要) |
| 業種group | JPX33業種 | **GICS 11セクター日本語訳** (sector列=GICS Sub-Industry英語) |
| custom_groups | 手動キュレーション | **機械生成** (Sub-Industryに"Semiconductor"を含む→「半導体」。手編集しても次回refresh実行で上書き) |
| タイムゾーン | JST固定+9h | **ET** (`zoneinfo`/Python・`Intl.DateTimeFormat`/JS — DSTがあるため固定オフセット禁止) |
| CSV時刻列 | timestamp_jst | **timestamp_et** |
| シンボル | `.T`自動補完 | 補完なし。Wikipediaのドット表記は**ダッシュへ正規化** (BRK.B→BRK-B, Yahoo形式) |
| hot入替の源 | Yahoo JPランキングHTML | Yahoo **predefined screener JSON** (most_actives/day_gainers, quoteType==EQUITYでETF除外)。全滅時は前週維持 |
| 需給レイヤー | JPX空売り残高報告 | **無し** (FINRAは隔週集計で日次相当が存在しない。ダッシュボードにもその旨明記・捏造しない) |
| 収集時間帯 | 場中 0-6 UTC | 場中 **13-21 UTC** (EDT 13:30-20:00 / EST 14:30-21:00 の両方をカバー) |
| stale注記 | ストップ高/安の可能性 | 売買停止・データ遅延の可能性 (米国に値幅制限ストップ高は無い) |
| 1分足の異常足除外 | 1銘柄でも除外 | **±1分に3銘柄以上同時の時だけ** (単独の大口ブロック取引は本物のため残す) |

### workflow対応 (すべてJP版の構造踏襲・concurrencyあり・push トリガーはブランチ検証用)

- `us-stock.yml` 場中30分毎 (`9,39 13-20` + `9 21` UTC 平日) — 収集+スクリーナー+コミットバック
- `us-stock-history.yml` 引け後 (`23 21` UTC 平日) — 日足2y/週足5y/月足max/1m 5d追取り+ローテーション
- `us-universe-refresh.yml` 月1回 (`41 20 4` UTC) — universe.csv+custom_groups.csv再構築
- `us-hot-refresh.yml` 月曜 `11 11` UTC (NY寄り前) — 話題枠週次入替+新規銘柄の初期履歴取得
- `pages.yml` に米国ビルド2ステップ (`|| echo` ガードでUS側失敗がJP公開を止めない) と
  米国場中スケジュール (`7,17,27,37,47,57 13-20` + `7,37 21` UTC) を追加済み

### 運用メモ

- 米国市場はJSTの夜間 (夏 22:30-5:00 / 冬 23:30-6:00)。「日中にUSダッシュボードが
  動かない」のは正常 (前日引けデータを表示)
- 障害切り分け・手動実行・閾値調整は `.claude/skills/jp-stock-ops/SKILL.md` の手順が
  `jp`→`us` の読み替えでそのまま使える

## ⑦ 検証基盤 (2026-10-03 実装)

ML実現性検証(2026-10)で「今のデータでは後知恵バイアス(後から話題になった銘柄がユニバースに
入っている)と標本不足で、シグナルの良し悪しを判定できない」と分かったため、判定時点の記録を
毎日残す仕組みと、RULES.md 戦略Cの答え合わせを用意した。どちらも発注しない・APIキー不要。

| ファイル | 役割 | 実行 |
|---|---|---|
| `ledger.py` | 仮想の約定記録。引け後にユニバース全銘柄の判定(日次の売買代金集中度 surge_1d=当日÷直前20日中央値、flag=candidate(≥2.0倍)/near(≥1.5倍)、引け時点のmoney_flow値)を `data/{jp,us}_stocks/ledger/ledger_YYYY-MM.csv` に記録し、後日 ret_1/5/20 (入口=**記録時刻より後の最初の寄り**で買いh日目の引け)・ex_h(同日の**中央値**との差)・exb_h(同じ区分の中央値との差)を自動記入。判定日より後に話題枠へ追加された銘柄は記録しない(hot_added)。分割のような段差を跨ぐ期間は記入しない(split_date)。直近30営業日の答え合わせは毎回計算し直す(Yahooの後からの調整を反映)。ユニバースから外れた答え合わせ待ちの銘柄は `--pending-codes` で日足を取り続ける。同じ日は再実行しても増えない(最初の記録が正)。取りこぼした日は3営業日まで backfilled=1 付きで補完し集計から除外。**評価方法を事前登録済み**(主要=日本candidateのex_5・120判定日で1回だけ・平均≥+0.5%かつt≥2。docstring/summaryのprereg)。月に日本約3MB・米国約4MB | `jp-stock-history.yml`/`us-stock-history.yml` の最後: 台帳は収集データと**別に再試行付きで保存**し、失敗は赤(Discord通知) |
| `strategy_c_check.py` | 戦略Cの答え合わせ (結果は RULES.md C節と long_signal.py の通知文に反映済み)。FGI(alternative.me)+BTC/ETH日足(OKX、Coinbaseで照合)で 固定積立/恐怖で倍額/RULES.md戦略C(≤30買い・≥70で25%利確)/一括 を全期間+3区間で比較。**合否基準はスクリプト冒頭に結果を見る前に固定**。結果は `data/strategy_c/result.json`。事前登録外の追加検証 `post_hoc` (同じ予算での比較・予測力の無い価格で基準を満たす割合) も併記 — 判定1「恐怖で倍額=採用」は事前基準どおりだが、同じ予算では固定積立に負け(BTC 4.94倍 vs 5.12倍)、予測力ゼロでも87.5%が基準を満たす甘い基準だった | `strategy-c-check.yml` 手動のみ |

- 米国は日足の当日分が翌日にしか届かないため、ledgerの判定日は米国だけ1営業日遅れて記録され、
  入口は判定日の2営業日後の寄りになる (記録より前の値段では買えないため)。money_flowは5分ごとに
  上書きされるので、引け後の値を `ledger/mf_pending.json` に日付別に保存してから使う
- 数か月たまるまで成績は判断しない (1日分の候補はほぼ同じ地合いを共有するため、件数より日数が効く)。
  必要日数の目安 (2026-10-04 反証の試算): 日本の+5日超過+1%なら約50日・+0.5%なら約190日、
  米国は候補どうしの連動が強く数倍〜十数倍。指数構成銘柄の過去2年では同じ定義で+0.18%程度
- 価格は配当調整なし: 配当落ちの日 (日本は3月・9月末) は高配当の大型株が不利に見える → exb (区分内) も見る

