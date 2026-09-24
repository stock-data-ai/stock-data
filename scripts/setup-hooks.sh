#!/bin/sh
# 安裝本 repo 的 git hooks。
#
#   sh scripts/setup-hooks.sh
#
# clone 到新機器後跑一次。**本檔只是模板，不會自己執行**——沒跑過這支的機器上
# 沒有任何 hook，guard 也就不會擋任何東西（見下方「這道防線的極限」）。
#
# 與 stock_map 的同名腳本刻意不同：那邊的 pre-push 會發布 KV，**這裡不做任何發布、
# 不同步、不寫遠端**。本 repo 的 hook 只跑離線檢查。

set -e

HOOK_DIR="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOK_DIR"

cat > "$HOOK_DIR/pre-push" <<'HOOK'
#!/bin/sh
# 自動產生自 scripts/setup-hooks.sh —— 不要直接改這個檔，改模板再重跑一次。
set -e

command -v node >/dev/null 2>&1 || {
  echo "⚠️  找不到 node，略過 cron 一致性檢查（請自行確認排程三份清單一致）"
  exit 0
}

echo "▶ cron 一致性檢查…"
node scripts/guards/check-cron-consistency.mjs || {
  echo ""
  echo "❌ cron 與監控清單對不上，push 中止。"
  echo "   要略過請用 git push --no-verify（但請先確認你知道自己在做什麼）。"
  exit 1
}

echo "▶ guard 測試…"
node --test 'test_methods/cron-monitoring/*.test.mjs' >/dev/null 2>&1 || {
  echo ""
  echo "❌ test_methods/cron-monitoring 有失敗，push 中止。"
  echo "   完整輸出：node --test 'test_methods/cron-monitoring/*.test.mjs'"
  exit 1
}
HOOK

chmod +x "$HOOK_DIR/pre-push"

echo "✅ 已安裝 pre-push hook 到 $HOOK_DIR"
echo ""
echo "這道防線的極限（別高估它）："
echo "  · 只在**本機 push 前**跑。--no-verify 會繞過。"
echo "  · CI 的 github-actions[bot] 直接推 main 時完全不會跑到。"
echo "  · 沒跑過這支的機器等於沒有防線。"
echo "  · main 目前沒有 branch protection、沒有 required status check，"
echo "    所以這支 hook 是唯一的自動檢查——它不是 gate，是提醒。"
