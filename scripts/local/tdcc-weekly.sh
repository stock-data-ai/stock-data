#!/usr/bin/env bash
# TDCC 股權分散表＋大戶加碼排行：每週本機更新（Mac launchd）。
#
# 為什麼在本機：2026-11 起 TDCC（smart.tdcc.com.tw）擋 GitHub runner（Errno 101 Network is
# unreachable），11/14、11/21 兩週 Actions 每班都失敗；住宅網路連得到。
# 步驟與 .github/workflows/weekly-shareholder-update.yml 相同：名單 → 刪下市檔 → 抓 TDCC →
# commit → 算大戶榜 → commit → push。
#
# 觸發：stock_map scripts/cron/local/jobs.sh 的 tdcc（週六 09:00／12:00／16:00、週日 08:30）。
# launcher 已把這份專用 clone 同步到 origin/main，這裡直接在 clone 內工作。
# 這不是唯讀測試：會寫檔、commit、push 到 stock-data main。
#
# 結束碼：0＝完成或本週已有資料或「還沒發布、等下一班」；1＝失敗（launcher 寄信）。
# 只有週日最後一班把「TDCC 尚未發布／連不上」當失敗，前面幾班失敗只記 log、交給下一班。

set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1

ts() { date '+%Y-%m-%d %H:%M:%S'; }
fail() { echo "[$(ts)] ❌ $1"; exit 1; }
LOG=/tmp/tdcc-weekly.run.log

uv sync --quiet --all-extras || fail "uv sync 失敗"

# ── 0. 冪等與最後一班判斷（台北時間）────────────────────────────────
read -r STATE LATEST FINAL < <(uv run --quiet python - <<'PY'
import json
from datetime import timedelta
from finance_tools.core.timezone import taipei_today
t = taipei_today()
last_fri = t - timedelta(days=(t.weekday() - 4) % 7)
# 週五遇假日時 TDCC 資料日會提前；往前容忍 3 天，仍不會誤認上一期（差 7 天）
floor = (last_fri - timedelta(days=3)).strftime('%Y%m%d')
with open('src/data/layer3/company-financials/2330.json', encoding='utf-8') as f:
    latest = max(json.load(f).get('shareholderDataHistory') or {'00000000': None})
print('done' if latest >= floor else 'need', latest, 'final' if t.weekday() == 6 else 'early')
PY
)
[ -n "${STATE:-}" ] || fail "讀不到現有資料日期"
if [ "$STATE" = done ]; then
  echo "[$(ts)] 本週已有 TDCC 資料（${LATEST}），不重抓"
  exit 0
fi
echo "[$(ts)] 現有最新 ${LATEST}，開始更新（${FINAL}）"

# 前面幾班的暫時性失敗不寄信，交給下一班；最後一班才算失敗
soft_fail() {
  if [ "$FINAL" = final ]; then fail "$1（本週最後一班）"; fi
  echo "[$(ts)] ⏭️ $1；等下一班再試"
  exit 0
}

# ── 1. 名單：stock_map 的 companies-all.json（同 .github/actions/fetch-roster）─────
ROSTER=src/data/layer3/companies/companies-all.json
mkdir -p "$(dirname "$ROSTER")"
gh api -H "Accept: application/vnd.github.v3.raw" \
  "repos/CHIJUI0128/stock_map/contents/${ROSTER}?ref=publish_cloudflare" > "$ROSTER" \
  || fail "取得 companies-all.json 失敗（gh 是否登入？）"
uv run --quiet python - "$ROSTER" <<'PY' || fail "名單疑似殘缺，中止"
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
tw = sum(1 for c, v in d.items() if c.isdigit() and len(c) == 4 and not v.get("isETF"))
print(f"companies-all.json：{len(d)} 筆，台股非 ETF {tw} 檔")
sys.exit(0 if tw >= 2000 else 1)
PY

# ── 2. 刪下市財報檔（先於抓資料，理由見 workflow）────────────────────
uv run finance_tools/cli.py prune-financials 2>&1 | tail -n 20 || fail "prune-financials 失敗"

# ── 3. 抓 TDCC（30／45／60 秒間隔重試）──────────────────────────────
ok=0
for wait in 30 45 60 0; do
  if uv run finance_tools/cli.py fetch-shareholder-data --force > "$LOG" 2>&1; then ok=1; break; fi
  grep -q "STALE:" "$LOG" && soft_fail "TDCC 尚未發布新一期（$(grep 'STALE:' "$LOG" | tail -1 | sed 's/.*STALE: //')）"
  [ "$wait" -eq 0 ] && break
  echo "[$(ts)] 抓取失敗，${wait} 秒後重試：$(grep -m1 'ERROR' "$LOG" | cut -c1-200)"
  sleep "$wait"
done
[ "$ok" = 1 ] || soft_fail "TDCC 連續 4 次抓取失敗：$(grep -m1 'ERROR' "$LOG" | cut -c1-200)"

DATA_DATE=$(grep -oE 'TDCC 資料日期: [0-9]{8}' "$LOG" | tail -1 | grep -oE '[0-9]{8}')
RESULT=$(grep -oE 'OK 完成: [0-9]+/[0-9]+' "$LOG" | tail -1 | grep -oE '[0-9]+/[0-9]+')
WARNS=$(grep "⚠" "$LOG" | sed 's/.* - WARNING - *//')
echo "[$(ts)] TDCC 資料日期 ${DATA_DATE:-?}，完成 ${RESULT:-?} 家"

# ── 4. commit（財報檔用 JSON 結構合併，見 json_merge_driver.py）──────
git config merge.company-json.driver "python3 finance_tools/scripts/json_merge_driver.py %O %A %B"
TODAY=$(uv run --quiet python -c "from finance_tools.core.timezone import taipei_today; print(taipei_today())")
git add src/data/layer3/company-financials/
if git diff --cached --quiet; then
  echo "[$(ts)] 股權分散沒有變更"
else
  git commit -q -m "每週更新 TDCC 股權分散數據 ${TODAY}（本機排程）" || fail "commit 失敗"
fi

# ── 5. 大戶加碼排行 ───────────────────────────────────────────────
uv run finance_tools/cli.py compute-big-holders > /tmp/tdcc-weekly.big.log 2>&1 \
  || { tail -n 20 /tmp/tdcc-weekly.big.log; fail "compute-big-holders 失敗"; }
grep "period-1:" /tmp/tdcc-weekly.big.log | head -n 2
git add src/data/market/weekly_big_holders.json
git diff --cached --quiet || git commit -q -m "更新大戶加碼排行 ${TODAY}（本機排程）" || fail "commit 失敗"

# ── 6. push（與 workflow 相同：rebase -X ours，三次）───────────────
if [ "$(git rev-list --count origin/main..HEAD)" -gt 0 ]; then
  pushed=0
  for i in 1 2 3; do
    if git pull -q --rebase -X ours origin main && git push -q origin HEAD:main; then pushed=1; break; fi
    echo "[$(ts)] push 第 $i 次失敗"; sleep $((i * 5))
  done
  [ "$pushed" = 1 ] || fail "推送失敗三次，TDCC 資料沒有發布出去"
fi

echo "[$(ts)] ✅ 完成：TDCC 資料日期 ${DATA_DATE:-?}，更新 ${RESULT:-?} 家"
[ -n "$WARNS" ] && printf '⚠ 資料異常跳過：\n%s\n' "$WARNS"
exit 0
