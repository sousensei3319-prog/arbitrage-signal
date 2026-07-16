---
name: jp-stock-ops
description: 日本株資金集中スクリーナーの運用・点検・修理・拡張ランブック。データが古い/Pagesが落ちた/銘柄追加/閾値調整/障害対応のときに使う
---

# 日本株 資金集中スクリーナー 運用ランブック

このドキュメントだけを読めば、下位モデルでも本システムの点検・修理・拡張ができることを
目標にしている。暗号資産系ツール(①②④)とは完全に別系統・別データなので、
**触ってよいのは `jp_*` / `us_*` / `universe_refresh.py` / `dashboard/` /
`data/jp_stocks/` / `data/us_stocks/` / `jp-stock*.yml` / `us-*.yml` /
`jp-supply-demand.yml` / `universe-refresh.yml` / `pages.yml` のみ**。
`screener.py` `fr_*` `smart_money*` 等の暗号資産系ファイルには一切手を触れないこと。

> **米国株版 (⑥) について**: 2026-07-16 に本システムの忠実な米国版を `us_` プレフィックスで
> 並設した (S&P500+Nasdaq-100、GICS 11セクター、ET表示、公開は同サイトの `/us/`)。
> 本ランブックの点検・手動収集・Pages切り分け・閾値調整の手順は `jp`→`us`
> (`jp-stock.yml`→`us-stock.yml`、`data/jp_stocks/`→`data/us_stocks/`、
> `7203_T_1m.csv`→`AAPL_1m.csv`) と読み替えればそのまま適用できる。JPとの設計差分は
> CLAUDE.md ⑥節を参照 (需給レイヤー無し・custom_groupsは機械生成・時刻列はtimestamp_et)。

## 1. システム全体図

```
JPX東証上場銘柄一覧(data_j.xls) + 日経225公式ウエイトCSV
        │ universe_refresh.py (月1回・機械構築, hot手動シードは温存)
        ▼
data/jp_stocks/universe.csv (約500銘柄: TOPIX500+日経225+話題枠, code,name,bucket,sector)
        │
        ▼
jp_stock_fetch.py ──(Yahoo Finance非公式 v8/finance/chart API)──┐
   通常巡回=RANGE 1d軽量 / 引け後=日足2y・週足5y・月足max・1m 5d追取り │
        │ 差分追記 (epoch重複排除) + 429適応バックオフ            │
        ▼                                                      │
data/jp_stocks/{code}_T_{interval}.csv                         │
        │  └─ 1分足は jp_stock_rotate.py が直近7営業日に切り詰め、 │
        │     溢れは {code}_T_1m_YYYYMM.csv.gz へ月次アーカイブ    │
        ▼                                                      │
jp_money_flow.py (売買代金の異常集中スクリーナー)               │
        │ 直近窓 vs 履歴中央値 → surge/z/share_delta            │
        ▼                                                      │
data/jp_stocks/money_flow.{csv,json}  ← commentary(自動分析文, 事実ベース) 同梱
        │                                                      │
        │   JPX空売り残高報告(日次・大口0.5%以上)                │
        │        │                                             │
        │        ▼                                             │
        │   jp_supply_demand.py ──(一覧HTMLスクレイピング→xls)   │
        │        │ universe該当分のみ・投資家単位で差分蓄積      │
        │        ▼                                             │
        │   data/jp_stocks/supply_demand/short_positions.csv    │
        │        │ (jp_money_flow.pyのcommentaryにも合流)       │
        ▼        ▼                                             │
dashboard/build_dashboard.py + dashboard/template.html         │
        │ 窓統計(1分〜月足)をPython側で事前計算 →                │
        │   site/index.html (スカラー統計+初期銘柄チャートのみ)   │
        │   site/data/{code}.json (銘柄別チャート, 選択時にfetch) │
        ▼                                                      │
site/ (.gitignore済み・コミットしない)                          │
        │                                                      │
        ▼                                                      │
GitHub Pages (actions/upload-pages-artifact + deploy-pages)    │
        │                                                      │
        ▼                                                      │
https://sousensei3319-prog.github.io/arbitrage-signal/  ◄──────┘
```

### ファイル→workflow対応

| ファイル | 役割 | workflow | 頻度 |
|---|---|---|---|
| `universe_refresh.py` | 監視ユニバースを機械構築: JPX「東証上場銘柄一覧」(規模区分Core30+Large70+Mid400=TOPIX500, xlrd必須)+日経225公式ウエイトCSV(cp932)から `universe.csv` を再構築しコミットバック。bucket優先順位 hot(温存)>leader(日経225)>core(残り)。既存銘柄の手書きsectorは温存 | `universe-refresh.yml` | 月1回 `41 21 3 * *` (毎月3日 21:41 UTC) + dispatch |
| `jp_stock_fetch.py` | Yahoo Finance非公式チャートAPI(v8/finance/chart, query1→query2フォールバック)から1分足(既定)を取得し `data/jp_stocks/{code}_T_1m.csv` に重複排除で差分追記。通常巡回は `RANGE=1d`(当日分のみ・497銘柄実測231秒/429ゼロ)。429検知で銘柄間スリープを段階延長(適応バックオフ、`SLEEP_SEC`+最大8秒) | `jp-stock.yml` | 東証立会時間の平日 `23,53 0-6 * * 1-5` (JST 9:00-15:00相当、毎時23分・53分の30分間隔) |
| `jp_stock_fetch.py` (履歴用) | 同スクリプトをINTERVAL/RANGE違いで4回呼び日足2年/週足5年/月足max/1分足5d(日またぎ欠損の追取り)を蓄積 | `jp-stock-history.yml` | 引け後の平日 `33 7 * * 1-5` (07:33 UTC=16:33 JST) |
| `jp_stock_rotate.py` | 1分足ライブファイルを直近 `ROLLING_DAYS`(既定7)営業日に切り詰め、溢れた分を月次gzipアーカイブ `{code}_T_1m_YYYYMM.csv.gz` へ退避(標準ライブラリgzipの多重メンバー追記)。アーカイブは後検証用でスクリーナー/ダッシュボードは読み込まない | `jp-stock-history.yml` の最終ステップ | 同上 |
| `jp_money_flow.py` | 売買代金(終値×出来高)の異常集中スクリーナー。直近窓(既定30分)vs履歴中央値でsurge/z/share_deltaを算出し `data/jp_stocks/money_flow.{csv,json}` を出力。json内commentaryは事実ベースの自動分析文 | `jp-stock.yml` 内の1ステップ、および `pages.yml` の再計算ステップ | jp-stock.ymlに同期 / pages.yml実行毎 |
| `dashboard/build_dashboard.py` + `dashboard/template.html` | 収集済みCSV(1m/1d) + universe.csv + money_flow.json + short_positions.csv を読み、集計窓(1分〜月足)ごとのランキング統計をPython側で事前計算(`jp_money_flow.window_stats()`を再利用)して、テンプレートの `__MFR__`/`__COMMENTARY__`/`__SUPPLY__`/`__INITIAL__` を埋めた `site/index.html` と、銘柄別チャートJSON `site/data/{code}.json`(銘柄選択時にブラウザがfetchで遅延取得)を生成。約500銘柄×1.7秒 | `pages.yml` の1ステップ | 同上 |
| — | 生成された `site/` (index.html + data/*.json) を GitHub Pages にデプロイ | `pages.yml` | `jp-stock.yml` 完了ごとに `workflow_run` で発火(収集→即再デプロイで「開けば最新」を実現) + `37 6 * * 1-5` バックアップ + `workflow_dispatch` |
| `jp_supply_demand.py` | JPX公式「空売りの残高に関する情報」(発行済株式数0.5%以上の大口投資家のみ・日次公表・旧xls形式)の一覧ページを都度スクレイピングし、universe.csv該当銘柄の投資家単位レコードだけを `data/jp_stocks/supply_demand/short_positions.csv` に差分蓄積。xlrd未導入環境では自動スキップ(コア機能=収集全体は継続) | `jp-supply-demand.yml` | 平日 `7 9 * * 1-5` (JST 18:07、空売り残高公表目処17:00の後) |

公開URL: **https://sousensei3319-prog.github.io/arbitrage-signal/**

## 2. 定型オペレーション手順

### (1) データ鮮度の確認方法

```bash
git pull origin main
tail -3 data/jp_stocks/7203_T_1m.csv   # 最終バーの timestamp_jst 列を見る
```

- 平日 9:00-15:30 JST の間は数分〜30分程度の遅れで最新バーが伸びているはず(収集は毎時23分・53分の30分間隔)。
- 15:30 JST 前後のバー(出来高0の板寄せ相当)まで来ていれば当日の1分足収集は完了。
- 日足/週足/月足は `{code}_T_1d.csv` 等の末尾を同様に確認。引け後(16:33 JST以降)に前日分まで
  伸びていれば正常。当日分は日足ファイルには**まだ**入らない設計(§4-3参照、1分足から再構成される)。
- 空売り残高報告は `tail -3 data/jp_stocks/supply_demand/short_positions.csv` で
  `disclosure_date` 列を確認。18:07 JST以降の平日実行後、当日または前営業日の日付まで
  伸びていれば正常。**銘柄が1件も出てこないのは異常ではない**(0.5%以上を報告した
  投資家がいた銘柄・日だけがそもそも存在するため、universe約500銘柄のうち該当が
  0件の日も普通にある)。ファイル自体が長期間更新されない場合のみ異常を疑う
- **このサンドボックス(Claude Code)からは Yahoo Finance にも github.io にも JPXにも
  到達できない(プロキシ403)**。実データの疎通確認は必ず GitHub Actions ランナー上の
  実行結果(ジョブログ・コミット・run の conclusion)で行うこと。WebFetch/curlでの直接確認は不可。

### (2) 手動収集の回し方

mcp__github__actions_run_trigger を使う(`gh` CLIではない、下記参照)。

```
mcp__github__actions_run_trigger(
  method="run_workflow", owner="sousensei3319-prog", repo="arbitrage-signal",
  workflow_id="jp-stock.yml", ref="main")
```

- `jp-stock-history.yml` も同様に `workflow_id` を差し替えるだけ。
- 実行後は `mcp__github__actions_list(method="list_workflow_runs", resource_id="jp-stock.yml")`
  で最新runを確認し、`mcp__github__get_job_logs(run_id=..., failed_only=true, return_content=true)`
  で失敗有無を見る。
- **actions_list の結果は巨大(数十万文字)になりファイルに退避されることが多い。
  `jq` で `id, run_number, status, conclusion, created_at` 等の必要フィールドだけ抽出すること。**

### (3) Pages再デプロイと失敗時の切り分け

```
mcp__github__actions_run_trigger(
  method="run_workflow", owner="sousensei3319-prog", repo="arbitrage-signal",
  workflow_id="pages.yml", ref="main")
```

失敗時の切り分け:

| 症状 | 原因 | 対処 |
|---|---|---|
| ジョブが `runner_id: 0` のまま `cancelled` で終わる、ログZIPが404 | ランナー割当の一時的失敗(GitHub側インフラ) | 一時エラーとして`workflow_dispatch`で再実行。2〜3分待って`conclusion`を確認。3回まで再試行 |
| `Deployment failed, try again later` (deploy-pages step) | Pages側の一時エラー | 同上、再実行で解消することが多い |
| `Resource not accessible by integration` (configure-pages/deploy-pages step) | リポジトリの Settings → Pages が "GitHub Actions" ソースになっていない(初回有効化がされていない) | コードでは直せない。リポジトリ管理者に Settings → Pages → Source を "GitHub Actions" にしてもらう案内をする |
| `build_dashboard.py` や `jp_money_flow.py` で Python例外 | コード起因(データ形式変化・想定外の欠損など) | ログの Traceback を読み、**推測で直さず**根本原因を特定してから修正。ローカルで同じスクリプトを再現実行して検証してからPRに含める |
| 収集(jp-stock.yml)は成功しているのにPagesが更新されない | `workflow_run` トリガーの対象workflow名(`"JP Stock 1m Collector"`)とpages.yml内の名前が不一致、または収集run自体がconclusion=failure | pages.yml の `on.workflow_run.workflows` と jp-stock.yml の `name:` が一致しているか確認。収集runのconclusionも確認 |

run一覧・ジョブ詳細・ログ取得は全て `mcp__github__actions_list` / `mcp__github__actions_get` /
`mcp__github__get_job_logs` を使う(`gh` CLI ではない)。事前に ToolSearch で
`select:mcp__github__actions_list,mcp__github__get_job_logs,mcp__github__actions_run_trigger` 等を
ロードすること。

### (3b) GitHubスケジューラ遅延と外部cron (cron-job.org)

**症状**: mainの `chore: JP stock 1m data` コミットが場中(平日9:00〜15:30 JST)に
30分ごとに増えない / jp-stock.yml の schedule 起点のランが1日2〜3回しか無い
(実測: 2026-07-10は場中 04:28 と 08:12 UTC の2回のみ)。原因はGitHub Actionsの
スケジュール間引き(負荷状況による仕様)で、**コードでは直せない**。RANGE=1d設計の
ため当日データの欠損は起きないが、ダッシュボードの更新頻度が落ちる。

**恒久対策 (2026-07-11導入)**: 外部cron **cron-job.org (ユーザーアカウント)** から
workflow_dispatch API を直接叩いて場中30分ごとの実行を保証する。

- スケジュール: 平日 9:23〜15:53 JST (Asia/Tokyo) の毎時 23分・53分
- リクエスト: `POST https://api.github.com/repos/sousensei3319-prog/arbitrage-signal/actions/workflows/jp-stock.yml/dispatches`
  body `{"ref":"main"}`
- ヘッダ: `Authorization: Bearer <fine-grained PAT>` (権限は Actions: Read and write のみ・
  対象リポジトリ限定・期限365日) + `Accept: application/vnd.github+json`
- **PATは2027-07頃に失効するので再発行が必要**。失効時の症状: cron-job.org側が
  401を返し、event=workflow_dispatch のランが止まる(データは schedule 分だけに戻る)。
  対処: ユーザーがPATを再発行し cron-job.org のジョブ設定を更新する
- **PATをリポジトリにコミットするのは厳禁**(public repoのため。Discord Webhookと
  同じ扱い — secret scanningの対象になる前に絶対に入れない)
- GitHub側の schedule トリガーは**保険として残置**。外部cronと二重発火しても
  concurrency直列化 + epoch重複排除で無害(データは重複しない)

**点検方法**: `mcp__github__actions_list(method="list_workflow_runs",
resource_id="jp-stock.yml", workflow_runs_filter={"event":"workflow_dispatch"})` で
event=workflow_dispatch のランが場中30分ごと(毎時23分・53分)に並んでいるか確認する。
並んでいなければ cron-job.org 側の停止/401(PAT失効)を疑う。

### (4) 銘柄の追加/削除

universe.csv は `universe_refresh.py` が月1回機械的に再構築する(TOPIX500+日経225+hot)。
**TOPIX500/日経225由来の銘柄を手で追加/削除してはいけない**(次回リフレッシュで上書きされる)。

- **話題枠(hot)の追加/削除だけが手動操作**: `data/jp_stocks/universe.csv` の該当行を
  編集する(`code,name,bucket,sector` の4列、bucket=`hot`)。universe_refresh.py は
  bucket=hot の行を無条件に温存するので、リフレッシュで消えることはない
- `code`: 証券コード(`.T`無しでも可、スクリプト側が自動補完)。4文字の新コード体系
  (285A等)にも対応
- 追加すると次回の `jp-stock.yml` 実行で自動的に収集対象になる(コード変更不要)。
  過去分の1分足は無い(Yahoo側5〜7日制限のため、追加後から蓄積が始まる)。
  日足/週足/月足は次回 `jp-stock-history.yml` で `RANGE=2y/5y/max` により
  さかのぼって一括取得される
- 構成銘柄の入替を待たず即時反映したいときは Actions → JP Stock Universe Refresh を
  workflow_dispatch で手動実行
- TOPIX500/日経225から外れた銘柄はリフレッシュ時にuniverseから自動で消えるが、
  既存の `{code}_T_*.csv` は残る(データは消さなくてよい)

### (4b) 独自区分 (custom_groups.csv) の追加/削除

業種グループ集計(バー/ツリーマップ/数値一覧/自動分析コメントの「業種別シェア」)は
JPX公式33業種(universe.csvのgroup列)を基本とするが、公式に無い切り口は
**`data/jp_stocks/custom_groups.csv`** (列: `code,custom_group,basis`) の上書きで
独立グループとして切り出せる(2026-07-11に「半導体」13銘柄で導入)。

- **1行追加/削除するだけで反映される**(コード変更不要。jp_money_flow.py と
  dashboard/build_dashboard.py が同じ `load_custom_groups()` で読む)。
  ファイルを削除すれば従来どおりJPX33業種のみに戻る(後方互換)
- universe.csv とは**別ファイル**なので universe_refresh.py の月次リビルドで消えない。
  universe_refresh.py はこのファイルに触れないこと
- `basis` 列は選定根拠のメモ(集計には未使用。csvにコメント行を入れられないため列で持つ)
- **選定基準 (PM決定・主力事業ベース)**: 「半導体デバイス/製造装置/ウェハ材料が主力事業」
  の銘柄のみ入れる。売上の一部が半導体でも主力が別の銘柄は入れない
- **境界例 (現在は除外中・ユーザー要望があれば追加可)**: 信越化学(多角化学)、
  HOYA(医療主力)、イビデン(パッケージ基板)、富士電機(重電主力)
- 注意: custom_groupの銘柄はJPX公式グループ(例: 電気機器)から抜けて数字が移動する。
  ダッシュボードのツールチップには「独自区分(JPX公式33業種外)」の注記が自動で付く
  (ビルド時にmeta.custom_groupsへ区分名を埋め込む方式のためテンプレ変更不要)

### (4c) 話題枠 (hot) の週次自動入れ替え (hot_refresh.py)

話題枠(bucket=hot)は **毎週月曜の寄り前に自動で入れ替わる** (2026-07-12導入)。
手動でのhot銘柄追加は基本不要になった。

- **データ源**: Yahoo Finance JP の**出来高**・**値上がり率**ランキング
  (`finance.yahoo.co.jp/stocks/ranking/volume|up?market=all&term=daily`)。
  2026-07-12にランナー上の実データで疎通確認済み。kabutan(405)・minkabu(403)・
  Yahoo売買代金(400)はブロック/不可のため不採用
- **追加**: ランキング上位(RANK_TOP=30)の、ユニバース外・普通株(ETF/投信/REIT/
  レバレッジ等を名前で除外)を hot に追加。sector/group は空で追加され、次回の月次
  universe_refresh がJPX33業種を機械付与する(週次でJPX一覧xlsを引くのは重いため)
- **除外 (hot枠のみ・leader/coreは絶対に外さない)**: ABSENT_WEEKS(4)週連続で全ランキング
  圏外、かつ money_flow.csv の集中度 < KEEP_SURGE(1.3) のhot銘柄を外す。
  custom_groups掲載の恒久テーマ銘柄(半導体等)と、直近で集中している銘柄は保護
- **状態/根拠**: `hot_state.json`(コード別の連続圏外週カウント)、
  `hot_changes_log.csv`(全入れ替え履歴)、`hot_changes_latest.json`(今週分・
  ダッシュボードの「🔁 今週の話題枠 入れ替え」欄に根拠付きで表示)
- **閾値調整**: workflowのenv `RANK_TOP`/`ABSENT_WEEKS`/`KEEP_SURGE` のみで調整(コード変更不要)。
  枠の上限は無い(増えるほど収集が重くなるので、5分cadenceを圧迫し始めたらRANK_TOPを絞るか
  ABSENT_WEEKSを短くする)
- **手動実行**: Actions → JP Hot Bucket Refresh を workflow_dispatch。または hot_refresh.py を
  ローカル実行(ただしサンドボックスからはYahoo JPへproxy403で不可 → ランナー上でのみ疎通)
- **ハマりどころ**: Yahoo JPがHTML構造を変えるとランキングが取れなくなる。その場合
  `hot_refresh.py` の全ランキングが取得失敗し「前週hot枠を維持して終了」する(落ちはしない)。
  ランナーログで「全ランキング取得失敗」が出たら PAIR_RE / RANKINGS のURLを見直す。
  実データ検証は必ずランナー上で(push トリガーで hot-refresh.yml が走る)

### (5) スクリーナー閾値調整

workflowのenvのみで調整する(コード変更不要)。

| 変数 | 既定 | 意味 | 場所 |
|---|---|---|---|
| `WINDOW_MIN` | `30` | 直近窓の分数(集中度surgeの分子) | `jp-stock.yml` / `pages.yml` の `Screen money flow` / `Run money-flow screener` ステップ |
| `TOP_N` | `15` | テキストレポートの表示件数 | `jp_money_flow.py` 環境変数(workflow未指定時は既定) |
| `RANGE`/`INTERVAL` | 通常巡回=`1d`/`1m`、引け後追取り=`5d`/`1m`、履歴=`2y`/`1d` 等 | `jp_stock_fetch.py` の取得レンジ・足種 | 各workflowの `env:` |
| `FETCH_DEADLINE_MIN` | 通常巡回16分・履歴各10分・1m追取り16分 | 全体デッドライン(ハング防止) | 各workflowの `env:` |
| `SLEEP_SEC` | `0.4` | 銘柄間の基本スリープ秒。429検知時は自動で+2秒ずつ延長(最大+8秒) | 各workflowの `env:` |
| `ROLLING_DAYS` | `7` | 1分足ライブファイルに残す営業日数(超過分は月次gzipへ) | `jp-stock-history.yml` の `Rotate` ステップ |

### (6) ダッシュボード改修時の検証手順

1. `python3 dashboard/build_dashboard.py` でローカル生成 → `site/index.html` のサイズ・
   銘柄数・銘柄別JSON数・コメント行数がログに出る(異常に小さい/0銘柄なら壊れている)。
   同時に使い方ガイド `site/help.html` も生成される(`dashboard/help_template.html` に
   銘柄数 `__N__`・最新時刻 `__UPDATED__` を差し込む静的ページ。ヘッダーの「📖 使い方ガイド」
   リンクとフッターから遷移。help_template.html を編集したら `__N__`/`__UPDATED__` の
   差し込み漏れが無いか生成後の help.html で確認する)
2. **必ず `python3 -m http.server` を立てて http://localhost 経由で確認する**。
   銘柄別チャートは `site/data/{code}.json` をfetchで遅延取得するため、
   `file://` で開くとfetchがCORSで失敗してチャートが出ない(それはバグではない):

   ```bash
   cd site && python3 -m http.server 8765 &
   python3 -c "
   from playwright.sync_api import sync_playwright
   with sync_playwright() as p:
       b = p.chromium.launch(executable_path='/opt/pw-browsers/chromium')
       pg = b.new_page(viewport={'width':1400,'height':1000})
       pg.goto('http://localhost:8765/index.html')
       pg.wait_for_timeout(1500)
       pg.screenshot(path='/tmp/dashboard_check.png', full_page=False)
       b.close()
   "
   ```

   (localhostへの接続はサンドボックスのproxy403制約を受けない)
3. 確認観点: 銘柄セレクタ(datalist検索)で別銘柄を選ぶ→チャートがfetchで
   切り替わるか(開発者コンソールに404が出ないか)・チャート足切替(1分〜月足)・
   集計窓切替(1分〜月足)・自動分析コメント欄・急騰アラート欄・
   ライト/ダーク両テーマ(`color_scheme='dark'`)が崩れていないか
4. 問題なければコミット。`site/` 自体は `.gitignore` 済みでコミット対象外
   (Pagesワークフローが毎回ビルドする)

## 3. 実績のあるハマりどころ (再発防止)

1. **サンドボックスからYahoo/github.ioはproxy403で到達不可**。実データ検証は必ず
   GitHub Actionsランナー上の実行結果(run conclusion・コミット・ログ)で判断すること。
   WebFetch/curlでの直接疎通確認はできない
2. **Yahoo月足APIは新規上場銘柄(285A等)に対し日足相当のデータを返すバグがある**。
   このため週足・月足は「Yahooの1wk/1moを直接信用」せず、**自前で日足から集約する設計**にした
   (詳細は `jp_stock_fetch.py`/`dashboard/build_dashboard.py` のコメント参照)
3. **エポック(UNIXタイムスタンプ)基準で複数銘柄の窓を揃えると、銘柄ごとの最終バー
   タイミングのずれで偽の急騰(集中度)が生まれる**。日付・インデックス基準で揃えること
   (`dashboard/build_dashboard.py` の `master` タイムスタンプ列と `day_sep` の実装を参照)
4. **日足ファイルの「当日」バーは寄り直後に取得すると不完全な値になる**
   (高値・安値・出来高が未確定)。当日分は日足CSVをそのまま使わず、**1分足から
   その日のOHLVを再構成**している(`dashboard/build_dashboard.py` の `load_1d()` 内
   `cutoff_date` 以降を1分足から組み立てる処理を参照)
5. **1分足はYahoo側の直近5〜7日制限**があり、それより古いデータは取得できない。
   定期実行(30分間隔)+重複排除(epoch集合との差分のみ追記)で、実行間隔を超えた
   連続履歴を自前で積み上げている。ジョブが数日止まると欠損が生まれ、埋め戻せない
6. **`schedule` トリガーはデフォルトブランチ(main)にマージ後のみ有効**(GitHub仕様)。
   フィーチャーブランチでの動作検証は `push`(該当ファイル変更時)や `workflow_dispatch` で代用する
7. **GitHub Pages の初回有効化はリポジトリ管理者の手動操作が必要**
   (Settings → Pages → Build and deployment → Source を "GitHub Actions" に設定)。
   これをしないと `configure-pages`/`deploy-pages` ステップが
   `Resource not accessible by integration` 等で失敗する。コード側では検知はできても
   直せないので、発生時は管理者への案内を返すこと
8. **GitHub Actions の `schedule` は負荷状況により実際の発火が数時間遅れることがある**
   (本リポジトリの実績で2〜3時間遅延の前例あり。jp-stock.ymlでは間引きで場中1日2〜3回
   まで落ちた実測もあり → §2(3b)の外部cron対策を導入済み)。cron時刻を過ぎても即座に
   走らないのは異常ではない。cron分を `:00/:15/:30` から避けているのもこの負荷回避策の一環
9. **JPX空売り残高報告は旧OLE2/BIFF形式(.xls)で配信され、openpyxlでは開けない**
   (`InvalidFileException: openpyxl does not support .bin file format` で失敗する。
   2026-07-09にActionsランナー上の実データで確認。`xlrd`が必要)。また一覧ページの
   ダウンロードリンクは日付ごとにハッシュ化されたディレクトリ名を含み予測できない
   (例: `.../t13vrt000001iogo-att/20260701_Short_Positions.xls`)ため、`jp_supply_demand.py`
   は毎回一覧HTMLを取得して正規表現でリンクを解決する設計にした。同時に調査した
   空売り比率(市場全体日次)・銘柄別信用取引週末残高はJPX公式配信がPDFのみと判明したため
   不採用(実装しない判断が正しい判断だったケース。詳細はPR本文のPhase1調査結果を参照)
10. **JPX「東証上場銘柄一覧」(data_j.xls)も同じ旧BIFF形式でxlrd必須** (2026-07-09に
    ランナー実データで確認)。TOPIX500は規模区分 Core30+Large70+Mid400 の合算だが、
    定期見直しの端境期は500ちょうどにならない(実測493)。無理に500へ調整しない
11. **東証の証券コードは4文字・先頭1桁のみ数字保証** (新コード体系: 285A・417A・543A等)。
    「4桁数字+英字1桁」のような正規表現は取りこぼす — `^[0-9][0-9A-Za-z]{3}$` を使う。
    日経225公式ウエイトCSVはcp932で、末尾に著作権表示の脚注行が混じるため
    コード列の形式チェックで実データ行だけを採用する
12. **銘柄別チャートJSONのファイル名はbuilderとtemplateで一致させる**
    (`7203.T` → `site/data/7203_T.json`)。templateのfetchパス変更時は
    http.server+Playwrightで404が出ないことを確認する
13. **template.htmlには `<meta charset="utf-8">` が必須**。GitHub Pagesは
    Content-Typeヘッダーで補うため無くても表示されるが、ローカルhttp.serverは
    charsetを付けず文字化けする(2026-07-09のローカル検証で実際に発生)

## 4. 将来ロードマップ(ユーザーと合意済みの構想)

- **約500銘柄化 (TOPIX500+日経225+話題枠) — 実装済み (2026-07-09)**: `universe_refresh.py` が
  JPX「東証上場銘柄一覧」(規模区分)と日経225公式ウエイトCSVから universe.csv を機械構築し、
  `universe-refresh.yml` が月1回自動追従する。話題枠(hot)は手動シードを温存。
  リポジトリ肥大化対策として `jp_stock_rotate.py` が1分足を直近7営業日のローリング窓に
  切り詰め、溢れた分を月次gzipアーカイブへ退避する。ダッシュボードは銘柄別チャートを
  `site/data/{code}.json` に分離しfetchで遅延読み込みする方式へ変更済み。
  なお「話題・これから話題の100社」を機械検出する無料公式ソースは見つかっておらず、
  hotの拡充(現12銘柄→100銘柄構想)は手動シード追加または将来の別ソース調査が必要
- **JPX需給レイヤー(空売り残高報告) — 実装済み (2026-07-09)**: `jp_supply_demand.py` が
  JPX公式「空売りの残高に関する情報」(発行済株式数0.5%以上の大口投資家のみ・日次)を
  収集し、`jp_money_flow.py`の自動分析コメントとダッシュボードのバッジに供給している。
  空売り比率(市場全体日次)・銘柄別信用取引週末残高はJPX公式配信がPDFのみのため不採用。
  日証金の貸借取引情報(taisyaku.jp)はJS動的レンダリング中心で一覧取得が困難なため見送り
  (個別銘柄detail頁単位でのスクレイピングは将来の拡張候補として保留)
- **LLMナラティブ分析**: 急騰の「理由」をLLMに書かせる構想。ただし**APIキーが必要**であり、
  かつ**実データに基づく場合のみ**使用可(ニュース等の裏付けが無い状態での理由の捏造は禁止)
- **真の板・歩み値リアルタイム**: 現行のYahoo非公式チャートAPI(1分足)の枠組みでは不可能。
  証券会社のAPI(楽天証券RSS/SBI等)+常時稼働サーバが必要になり、無料枠を超える。
  **できない、と正直に線引きしておく**(過剰な期待をさせない)

## 5. 分析コメント(commentary)の原則

`jp_money_flow.py` の `_commentary()` が生成する自動分析文、および今後この種のコメントを
拡張する際に必ず守ること:

- **算出可能な事実のみ**を書く(集中度・z値・売買代金・値動き%・時間帯など、CSVから直接
  計算できる数値に基づく記述のみ)
- **ニュース理由・裏付けの無い需給の断定は書かない**。データに裏付けが無い「なぜ上がったか」は
  「※要確認」等と明示し、断定的な表現(「〜が理由」「〜の思惑」等)を使わない
- 末尾に**投資助言ではない旨**を必ず付す(既存の `_commentary()` 末尾のパターンを踏襲)
- 材料(ニュース・信用取引残高・空売り比率(市場全体)等)を実装で保持していない限り、
  それに触れる記述は「本データに含まれず未検証」と明記する。空売り残高報告
  (`jp_supply_demand.py` が蓄積)は実装済みのため銘柄別の件数・合計比率は事実として
  記載してよいが、「発行済株式数0.5%以上の大口報告のみ」という前提条件を必ず併記し、
  「スクイーズが起きる」等の予測・断定は禁止(「ショートカバーが値動きを増幅する
  可能性がある」という一般論の注意喚起までに留める)
