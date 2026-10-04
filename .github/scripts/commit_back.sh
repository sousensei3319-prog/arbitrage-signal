#!/usr/bin/env bash
# データのコミットバック共通処理 (GitHub Actions 用)。
#   使い方: bash .github/scripts/commit_back.sh "<コミットメッセージ>" <path> [<path> ...]
#
# - path は1つずつ git add する (CLAUDE.md規約: まとめて add すると未存在ファイル1つで
#   全体が abort し、状態が永続化されない実績あり)。存在しない path は黙ってスキップする。
#   追跡中のファイルが削除されていれば削除をコミットする。存在するのに add できなかった path が
#   あれば、残りを保存したうえで最後に exit 1 (赤) にする。
# - 他ジョブが先に push していたら、最新を取り込んで (fetch → rebase) 最大5回まで再 push する。
#   fetch の一時的な失敗 (通信断) は衝突ではないので数えて再試行する。
# - rebase が衝突した / commit できなかった / 5回とも push できなかった場合は exit 1 で run を赤くする。
#   旧方式 (「push failed (race) - next run」を表示して success 終了) は、週次ジョブで
#   1週間分の結果が消えたのに緑のまま見逃された (2026-09-28 の話題枠入れ替え) ため使わない。
#   ※ 5分ごとの収集ジョブ (jp-stock.yml 等) は次回実行で自然回復するので旧方式のまま。
set -u
msg="$1"; shift
branch="${GITHUB_REF_NAME:?GITHUB_REF_NAME が未設定}"
git config user.name "${COMMIT_BOT_NAME:-jp-stock-bot}"
git config user.email "actions@users.noreply.github.com"

add_failed=0
for p in "$@"; do
  if [ -e "$p" ] || git ls-files --error-unmatch -- "$p" >/dev/null 2>&1; then
    if ! git add -- "$p"; then
      echo "::error::git add に失敗: $p (他のファイルの保存は続ける)"
      add_failed=1
    fi
  fi
done
if git diff --cached --quiet; then
  echo "変更なし (コミット不要)"
  exit $add_failed
fi
if ! git commit -q -m "$msg"; then
  echo "::error::git commit に失敗。今回の変更は保存されていない"
  exit 1
fi

for i in 1 2 3 4 5; do
  if git push -q origin "HEAD:${branch}"; then
    echo "push成功 (${i}回目)"
    [ "$add_failed" = 1 ] && echo "::error::add できなかったファイルがある (上のエラー参照)"
    exit $add_failed
  fi
  echo "push失敗 (${i}回目): 他ジョブが先に push した → 最新を取り込んで再試行"
  sleep $((i * 4))
  # コミット対象外の生成物 (未コミットの変更) が rebase を妨げないよう退避して捨てる
  git stash push --include-untracked -q >/dev/null 2>&1 || true
  if ! git fetch -q origin "$branch"; then
    echo "最新の取得に失敗 (${i}回目): 通信の一時的な失敗として再試行"
    continue
  fi
  if ! git rebase -q FETCH_HEAD; then
    git rebase --abort 2>/dev/null || true
    echo "::error::rebase衝突: 同じファイルを他ジョブも更新していた。今回の変更は保存されていない"
    exit 1
  fi
done
echo "::error::push を5回試行して失敗。今回の変更は保存されていない"
exit 1
