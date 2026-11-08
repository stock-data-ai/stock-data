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
#
# 程式檢查（ruff、pytest、Node 守門測試、cron Worker 型別）2026-11-08 從 GitHub Actions 的
# code-quality.yml 搬到這裡，做法與 stock_map 的 pre-push 一致。

set -e

HOOK_DIR="$(git rev-parse --git-path hooks)"
mkdir -p "$HOOK_DIR"

cat > "$HOOK_DIR/pre-push" <<'HOOK'
#!/bin/sh
# 自動產生自 scripts/setup-hooks.sh —— 不要直接改這個檔，改模板再重跑一次。

# 這次推的 commit 有沒有動到程式。只推資料（src/data、rerun 佇列）或文件時不跑程式檢查：
# 排程每天只推資料，不該被別人留下的程式問題卡住。分支是新的或比對不出來時一律當作有。
CODE_CHANGED=0
CRON_CHANGED=0
while read -r local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "0000000000000000000000000000000000000000" ] && continue
  if [ "$remote_sha" = "0000000000000000000000000000000000000000" ] \
     || ! CHANGED_FILES=$(git diff --name-only "$remote_sha" "$local_sha" 2>/dev/null); then
    CODE_CHANGED=1
    CRON_CHANGED=1
    continue
  fi
  printf '%s\n' "$CHANGED_FILES" | grep -qE '^(finance_tools|etf_Crawler|Web_Crawler)/.*\.py$|^(scripts|test_methods|cron/src|\.github/workflows)/|^(pyproject\.toml|uv\.lock|ruff\.toml|package\.json|cron/tsconfig\.json)$' && CODE_CHANGED=1
  printf '%s\n' "$CHANGED_FILES" | grep -qE '^cron/(src/|tsconfig\.json)' && CRON_CHANGED=1
done

# 訊息一律走 stderr：VS Code／Git Graph 的推送按鈕只顯示 stderr。
exec 1>&2

# 從 stock_map 的終端機 push 時會帶著那邊的 VIRTUAL_ENV，uv 會警告並可能用錯環境。
unset VIRTUAL_ENV

fail() {
  echo ""
  echo "❌ $1，push 中止。"
  echo "   要略過請用 git push --no-verify（但請先確認你知道自己在做什麼）。"
  exit 1
}

command -v node >/dev/null 2>&1 || {
  echo "⚠️  找不到 node，略過所有檢查（請自行確認 cron 清單一致、程式檢查通過）"
  exit 0
}

echo "⏰ cron 一致性檢查…"
node scripts/guards/check-cron-consistency.mjs >/dev/null || fail "cron 與監控清單對不上（完整輸出：npm run check:cron）"

if [ "$CODE_CHANGED" = "0" ]; then
  echo "✅ 這次沒改到程式，略過 ruff／pytest／守門測試"
  exit 0
fi

command -v uv >/dev/null 2>&1 || fail "找不到 uv，無法跑 Python 檢查"

echo "🐍 ruff…"
uvx ruff@0.14.0 check --quiet || fail "Python 有未定義名稱、重複定義等多半是 bug 的寫法（規則見 ruff.toml）"

echo "🧪 pytest（財報管線，全離線）…"
TESTLOG=$(mktemp "${TMPDIR:-/tmp}/stockdata-prepush.XXXXXX")
uv run pytest finance_tools/tests/ -q > "$TESTLOG" 2>&1 || { tail -30 "$TESTLOG"; rm -f "$TESTLOG"; fail "pytest 有失敗"; }

echo "🛡️  Node 守門測試…"
npm test --silent > "$TESTLOG" 2>&1 || { grep -E "^not ok|✖|^ℹ (pass|fail)" "$TESTLOG" | head -30; rm -f "$TESTLOG"; fail "npm test 有失敗"; }
rm -f "$TESTLOG"

if [ "$CRON_CHANGED" = "1" ]; then
  echo "🔷 cron Worker 型別…"
  # cron/package-lock.json 刻意不進 git，所以第一次要 npm install
  [ -d cron/node_modules ] || (cd cron && npm install --no-audit --no-fund --silent) || fail "cron 套件安裝失敗"
  (cd cron && npx -y -p typescript@5 tsc --noEmit -p tsconfig.json) || fail "cron Worker 型別檢查失敗"
fi

echo "✅ push 前檢查全部通過"
HOOK

chmod +x "$HOOK_DIR/pre-push"

echo "✅ 已安裝 pre-push hook 到 $HOOK_DIR"
echo ""
echo "這道防線的極限（別高估它）："
echo "  · 只在**本機 push 前**跑。--no-verify 會繞過。"
echo "  · CI 的 github-actions[bot] 直接推 main 時完全不會跑到（排程只推資料，影響不大）。"
echo "  · 沒跑過這支的機器等於沒有防線。"
echo "  · main 沒有 branch protection、沒有 required status check，"
echo "    GitHub 上也不再有 code-quality workflow，這支 hook 是唯一的程式檢查。"
