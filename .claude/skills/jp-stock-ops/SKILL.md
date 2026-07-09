---
name: jp-stock-ops
description: 日本株資金集中スクリーナーの運用・点検・修理・拡張ランブック。データが古い/Pagesが落ちた/銘柄追加/閾値調整/障害対応のときに使う
---

# 日本株 資金集中スクリーナー 運用ランブック

このドキュメントだけを読めば、下位モデルでも本システムの点検・修理・拡張ができることを
目標にしている。暗号資産系ツール(①②④)とは完全に別系統・別データなので、
**触ってよいのは `jp_*` / `dashboard/` / `data/jp_stocks/` / `jp-stock*.yml` /
`jp-supply-demand.yml` / `pages.yml` のみ**。
`screener.py` `fr_*` `smart_money*` 等の暗号資産系ファイルには一切手を触れないこと。

## 1. システム全体図

```
data/jp_stocks/universe.csv (46銘柄: code,name,bucket,sector)
        │
        ▼
jp_stock_fetch.py ──(Yahoo Finance非公式 v8/finance/chart API)──┐
   INTERVAL/RANGE で 1分足/日足/週足/月足 を切替             │
        │ 差分追記 (epoch重複排除)                             │
        ▼                                                      │
data/jp_stocks/{code}_T_{interval}.csv                         │
        │                                                      │
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
        │ CSV群 + money_flow.json + short_positions.csv → 単一HTML
        ▼                                                      │
site/index.html (.gitignore済み・コミットしない)                │
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
| `jp_stock_fetch.py` | Yahoo Finance非公式チャートAPI(v8/finance/chart, query1→query2フォールバック)から1分足(既定)を取得し `data/jp_stocks/{code}_T_1m.csv` に重複排除で差分追記。`INTERVAL`/`RANGE` で日足(1d/2y)・週足(1wk/5y)・月足(1mo/max)も取得可 | `jp-stock.yml` | 東証立会時間の平日 `23,53 0-6 * * 1-5` (JST 9:00-15:00相当、毎時23分・53分の30分間隔) |
| `jp_stock_fetch.py` (履歴用) | 同スクリプトをINTERVAL/RANGE違いで3回呼び日足2年/週足5年/月足maxを蓄積 | `jp-stock-history.yml` | 引け後の平日 `33 7 * * 1-5` (07:33 UTC=16:33 JST) |
| `jp_money_flow.py` | 売買代金(終値×出来高)の異常集中スクリーナー。直近窓(既定30分)vs履歴中央値でsurge/z/share_deltaを算出し `data/jp_stocks/money_flow.{csv,json}` を出力。json内commentaryは事実ベースの自動分析文 | `jp-stock.yml` 内の1ステップ、および `pages.yml` の再計算ステップ | jp-stock.ymlに同期 / pages.yml実行毎 |
| `dashboard/build_dashboard.py` + `dashboard/template.html` | 収集済みCSV(1m/1d/1wk/1mo) + universe.csv + money_flow.json + short_positions.csv を読み、テンプレートの `__MFR__`/`__COMMENTARY__`/`__SUPPLY__` プレースホルダを埋めて `site/index.html`(単一HTML、外部依存なし)を生成 | `pages.yml` の1ステップ | 同上 |
| — | 生成された `site/index.html` を GitHub Pages にデプロイ | `pages.yml` | `jp-stock.yml` 完了ごとに `workflow_run` で発火(収集→即再デプロイで「開けば最新」を実現) + `37 6 * * 1-5` バックアップ + `workflow_dispatch` |
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
  投資家がいた銘柄・日だけがそもそも存在するため、universe46銘柄のうち該当が
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

### (4) 銘柄の追加/削除

`data/jp_stocks/universe.csv` を編集するだけ。`code,name,bucket,sector` の4列。

- `code`: 証券コード(`.T`無しでも可、スクリプト側が自動補完)
- `bucket`: `leader`(けん引大型) / `core`(主力) / `hot`(話題・噂) のいずれか。
  `jp_money_flow.py` のバケット別集計・ダッシュボードのバケットラベルに使われる
- 追加すると次回の `jp-stock.yml` 実行で自動的に収集対象になる(コード変更不要)。
  過去分の1分足は無い(Yahoo側5〜7日制限のため、追加後から蓄積が始まる)。
  日足/週足/月足は次回 `jp-stock-history.yml` で `RANGE=2y/5y/max` により
  さかのぼって一括取得される
- 削除時は universe.csv から行を消すだけ。既存の `{code}_T_*.csv` は残る
  (ダッシュボードのセレクタにも出なくなるだけで、データは消さなくてよい)

### (5) スクリーナー閾値調整

workflowのenvのみで調整する(コード変更不要)。

| 変数 | 既定 | 意味 | 場所 |
|---|---|---|---|
| `WINDOW_MIN` | `30` | 直近窓の分数(集中度surgeの分子) | `jp-stock.yml` / `pages.yml` の `Screen money flow` / `Run money-flow screener` ステップ |
| `TOP_N` | `15` | テキストレポートの表示件数 | `jp_money_flow.py` 環境変数(workflow未指定時は既定) |
| `RANGE`/`INTERVAL` | 1分足=`5d`/`1m`、履歴=`2y`/`1d` 等 | `jp_stock_fetch.py` の取得レンジ・足種 | 各workflowの `env:` |
| `FETCH_DEADLINE_MIN` | 収集14分・履歴各6分 | 全体デッドライン(ハング防止) | 各workflowの `env:` |

### (6) ダッシュボード改修時の検証手順

1. `python3 dashboard/build_dashboard.py` でローカル生成 → `site/index.html` のサイズ・
   銘柄数・コメント行数がログに出る(異常に小さい/0銘柄なら壊れている)
2. Playwrightでスクリーンショットして目視確認:

   ```bash
   python3 -c "
   from playwright.sync_api import sync_playwright
   with sync_playwright() as p:
       b = p.chromium.launch(executable_path='/opt/pw-browsers/chromium')
       pg = b.new_page(viewport={'width':1400,'height':900})
       pg.goto('file:///home/user/arbitrage-signal/site/index.html')
       pg.wait_for_timeout(1500)
       pg.screenshot(path='/tmp/dashboard_check.png', full_page=False)
       b.close()
   "
   ```

   (パスはローカルのfileプロトコルなのでサンドボックスのproxy403制約を受けない)
3. 銘柄セレクタ切替・チャート足切替(1分〜月足)・自動分析コメント欄・急騰アラート欄が
   崩れていないかを画像で確認
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
   (本リポジトリの実績で2〜3時間遅延の前例あり)。cron時刻を過ぎても即座に走らないのは
   異常ではない。cron分を `:00/:15/:30` から避けているのもこの負荷回避策の一環
9. **JPX空売り残高報告は旧OLE2/BIFF形式(.xls)で配信され、openpyxlでは開けない**
   (`InvalidFileException: openpyxl does not support .bin file format` で失敗する。
   2026-07-09にActionsランナー上の実データで確認。`xlrd`が必要)。また一覧ページの
   ダウンロードリンクは日付ごとにハッシュ化されたディレクトリ名を含み予測できない
   (例: `.../t13vrt000001iogo-att/20260701_Short_Positions.xls`)ため、`jp_supply_demand.py`
   は毎回一覧HTMLを取得して正規表現でリンクを解決する設計にした。同時に調査した
   空売り比率(市場全体日次)・銘柄別信用取引週末残高はJPX公式配信がPDFのみと判明したため
   不採用(実装しない判断が正しい判断だったケース。詳細はPR本文のPhase1調査結果を参照)

## 4. 将来ロードマップ(ユーザーと合意済みの構想)

- **850銘柄化**: TOPIX500 + 日経225 + 話題100 の統合。構成銘柄はJPX等から機械取得する想定。
  ただし銘柄数が増えるとリポジトリが肥大化するため、**生データの日次gzip化を前提**とする
  (現状の非圧縮CSV蓄積のままでは46銘柄でもファイルサイズが積み上がっている)
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
