#!/usr/bin/env bash
# データのコミットバック共通処理 (GitHub Actions 用)。
#   使い方: bash .github/scripts/commit_back.sh "<コミットメッセージ>" <path> [<path> ...]
#
# - path は1つずつ git add する (CLAUDE.md規約: まとめて add すると未存在ファイル1つで
#   全体が abort し、状態が永続化されない実績あり)。存在しない path は黙ってスキップする。
#   追跡中のファイルが削除されていれば削除をコミットする。
# - 他ジョブが先に push していたら、最新を rebase で取り込んで最大5回まで再 push する。
# - rebase が衝突した / 5回とも push できなかった場合は exit 1 で run を赤くする。
#   旧方式 (「push failed (race) - next run」を表示して success 終了) は、週次ジョブで
#   1週間分の結果が消えたのに緑のまま見逃された (2026-09-28 の話題枠入れ替え) ため使わない。
#   ※ 5分ごとの収集ジョブ (jp-stock.yml 等) は次回実行で自然回復するので旧方式のまま。
set -u
msg="$1"; shift
branch="${GITHUB_REF_NAME:?GITHUB_REF_NAME が未設定}"
git config user.name "${COMMIT_BOT_NAME:-jp-stock-bot}"
git config user.email "actions@users.noreply.github.com"

for p in "$@"; do
  if [ -e "$p" ] || git ls-files --error-unmatch -- "$p" >/dev/null 2>&1; then
    git add -- "$p" || echo "git add 失敗 (スキップ): $p"
  fi
done
if git diff --cached --quiet; then
  echo "変更なし (コミット不要)"
  exit 0
fi
git commit -q -m "$msg"

for i in 1 2 3 4 5; do
  if git push -q origin "HEAD:${branch}"; then
    echo "push成功 (${i}回目)"
    exit 0
  fi
  echo "push失敗 (${i}回目): 他ジョブが先に push した → 最新を取り込んで再試行"
  sleep $((i * 4))
  # コミット対象外の生成物 (未コミットの変更) が rebase を妨げないよう退避して捨てる
  git stash push --include-untracked -q >/dev/null 2>&1 || true
  if ! git pull -q --rebase origin "$branch"; then
    git rebase --abort 2>/dev/null || true
    echo "::error::rebase衝突: 同じファイルを他ジョブも更新していた。今回の変更は保存されていない"
    exit 1
  fi
done
echo "::error::push を5回試行して失敗。今回の変更は保存されていない"
exit 1
