"""workflow の失敗を Discord に知らせる (標準ライブラリのみ)。

なぜ必要か (2026-10-04 の反証で判明): 銘柄リストの安全装置・保存の再試行などで「おかしい時は赤で
止める」ようにしたが、赤 (失敗) の知らせは GitHub のメールにしか届かず、見られなければ緑と同じ。

使い方 (workflow の最後の step。ブランチでの検証実行では送らないよう main に限定する):
      - name: Notify failure (Discord)
        if: failure() && github.ref == 'refs/heads/main'
        env:
          WEBHOOK_URL: ${{ secrets.SMART_MONEY_WEBHOOK_URL || secrets.DISCORD_WEBHOOK_URL }}
          NOTIFY_WHAT: 何が止まったかの一言 (例: 銘柄リストの月次更新)
        run: python .github/scripts/notify_failure.py
Webhook の URL は表示しない (public repo のため。secret scanning で無効化される)。
送信に失敗しても workflow の結果は変えない (exit 0)。
"""
import json
import os
import urllib.error
import urllib.request

url = os.environ.get("WEBHOOK_URL") or ""
if not url:
    print("Webhook 未設定のため通知なし")
    raise SystemExit(0)

what = os.environ.get("NOTIFY_WHAT") or ""
mention = (os.environ.get("MENTION_EVERYONE") or "1") == "1"
run_url = "{}/{}/actions/runs/{}".format(os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
                                         os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""))
lines = [("@everyone " if mention else "") + f"⚠️ **{os.environ.get('GITHUB_WORKFLOW', 'workflow')}** が失敗しました"
         + (f" — {what}" if what else ""),
         "データを書き換えずに止めたか、保存に失敗しています。ログの赤いエラー行 (::error::) を確認してください。",
         run_url]
payload = {"content": "\n".join(lines), "allowed_mentions": {"parse": ["everyone"] if mention else []}}
req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                             headers={"Content-Type": "application/json", "User-Agent": "arbitrage-signal-failure-notify/1.0"})
try:
    with urllib.request.urlopen(req, timeout=15):
        pass
    print("Discord に失敗を通知しました")
except (urllib.error.URLError, OSError) as e:
    print(f"通知の送信に失敗 (workflow の結果には影響なし): {getattr(e, 'code', '')} {type(e).__name__}")
