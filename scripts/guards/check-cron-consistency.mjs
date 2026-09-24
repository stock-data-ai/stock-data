#!/usr/bin/env node
/**
 * cron 排程與監控清單的一致性對帳（stock-data 側）。
 *
 * 同一個排程的資訊散在三個檔案，改一處忘了改另一處不會有任何錯誤訊息：
 *
 *   cron/wrangler.toml               真正註冊到 Cloudflare 的 cron（唯一會真的觸發的那份）
 *   cron/src/index.ts                cron → workflow 的 dispatch 表；漏一筆 = cron 觸發了什麼都不做
 *   .github/workflows/health-check.yml  每日 23:00 健康檢查信的清單；漏一筆 = 那班壞掉永遠不會通知
 *
 * 為什麼要這支：2026-09-03 的 `5804508637` 把熱力圖搬回 stock_map，刪了本 repo 的
 * `update-heatmap.yml` 與那條 cron，**但沒刪 health-check.yml 裡的檢查項**。於是
 * 2026-09-04~09-09 連續六天，健康檢查在找一個已經不存在的 workflow，每天報一次
 * 「15:00 Update Heatmap Data 未執行」——19 項全綠、只有那一筆假紅，每天寄一封異常信。
 * 真正的代價不是那封信，是**真故障與常態誤報再也分不出來**。
 *
 * stock_map 早就有同名 guard（`scripts/guards/check-cron-consistency.mjs`）擋住這件事；
 * 本 repo 當時什麼都沒有，所以同一個錯誤能一路溜到 production。這支就是把那道防線補上。
 *
 * 用法：npm run check:cron
 * 新增／搬動排程時，三個檔案一起改，這支會確認你沒漏。
 *
 * 只用 Node 內建模組，沒有任何相依。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';

const TOML = 'cron/wrangler.toml';
const INDEX = 'cron/src/index.ts';
const YML = '.github/workflows/health-check.yml';
const WF_DIR = '.github/workflows';

/**
 * 被 dispatch 但**刻意不列入監控清單**的 workflow，每一筆都要寫得出理由。
 *
 * 這張表存在的目的是「讓豁免變成需要解釋的決定」，不是讓 guard 好過。
 * 想加一筆進來之前先問：這支壞掉時，有沒有別的東西會讓人知道？沒有就不該豁免。
 */
const UNWATCHED_BY_DESIGN = new Map([
  [
    'health-check.yml',
    '健康檢查不能監控自己——它就是那份清單的執行者。它自己沒被觸發時，'
      + '唯一的訊號是「信沒來」，而目前沒有東西在監看這件事（WS-06 的 W6-F14，尚未處理）。'
      + '把它列進 CHECK_JOBS 只會得到一個永遠成立的自我參照，不會增加任何偵測力。',
  ],
]);

const problems = [];
const fail = (msg) => problems.push(msg);
const read = (p) => readFileSync(p, 'utf-8');

// ── wrangler.toml：真正註冊的 cron ────────────────────────────────────
const cronsBlock = read(TOML).split('crons = [')[1]?.split(']')[0];
if (!cronsBlock) throw new Error(`${TOML} 找不到 crons = [...]`);
const registered = [...cronsBlock.matchAll(/^\s*"([^"]+)"/gm)].map((m) => m[1]);

// ── index.ts：dispatch 表 + 特例常數 ──────────────────────────────────
const idx = read(INDEX);
const dispatch = new Map(
  [...idx.matchAll(/'([\d,*/ -]+)':\s*\{\s*workflow:\s*'([^']+)'/g)].map((m) => [m[1], m[2]]),
);
// 不走 dispatch 表、由 scheduled() 直接比對的特例（health-check 等）
const standalone = [...idx.matchAll(/^const [A-Z_]+_CRON\s*=\s*'([^']+)'/gm)].map((m) => m[1]);
const standaloneWorkflows = [...idx.matchAll(/_JOB[^=]*=\s*\{\s*workflow:\s*'([^']+)'/g)].map((m) => m[1]);

const handled = new Set([...dispatch.keys(), ...standalone]);

for (const cron of registered) {
  if (!handled.has(cron)) {
    fail(`${TOML} 排了 "${cron}"，但 ${INDEX} 沒有對應處理——cron 會觸發，然後什麼都不做`);
  }
}
for (const cron of handled) {
  if (!registered.includes(cron)) {
    fail(`${INDEX} 處理 "${cron}"，但 ${TOML} 沒排——這段程式永遠不會執行`);
  }
}

// ── 本 repo 的 worker **刻意不留第二份 CHECK_JOBS** ────────────────────
// index.ts 曾有一份重複清單，只有手動 /health-check 端點會用到，結果與 workflow 那份
// 長期漂移（標籤都已不同）。移除之後這裡改成「確認它沒有再長回來」。
// 只抓「真的宣告了一份」（const CHECK_JOBS = …），不要被那句叫人別再放一份的註解騙到
if (/^\s*(?:const|let|var)\s+CHECK_JOBS\b/m.test(idx)) {
  fail(
    `${INDEX} 又出現 CHECK_JOBS——本 repo 刻意只保留 ${YML} 一份檢查清單。`
      + `兩份清單必然漂移（2026-08 已發生過一次），要改請改 ${YML}。`,
  );
}

// ── workflow 檔案的 name: 索引（CHECK_JOBS 記的是 name，不是檔名）──────
const workflowNames = new Map(); // name -> filename
for (const file of readdirSync(WF_DIR).filter((f) => f.endsWith('.yml'))) {
  const name = read(`${WF_DIR}/${file}`).match(/^name:\s*(.+)$/m)?.[1].trim();
  if (name) workflowNames.set(name, file);
}

// ── health-check.yml 的檢查清單 ───────────────────────────────────────
const ymlSrc = read(YML);
const checks = [...ymlSrc.matchAll(
  /\{'label':\s*'([^']*)',\s*'workflow_name':\s*'([^']+)'\s*,\s*'hour':\s*(\d+)\s*,\s*'minute':\s*(\d+)([^}]*)\}/g,
)].map((m) => ({
  label: m[1],
  workflow: m[2],
  hour: +m[3],
  minute: +m[4],
  lenient: /'lenient':\s*True/.test(m[5]),
  days: (m[5].match(/'days':\s*\[([\d,\s]*)\]/)?.[1] ?? '')
    .split(',').map((s) => s.trim()).filter(Boolean).map(Number),
}));

if (checks.length === 0) fail(`${YML} 解析不到任何 CHECK_JOBS 條目——正則可能與檔案格式脫節`);

// ① 監控清單指到的 workflow 必須真的存在（2026-09-04 那六天就是敗在這條）
for (const c of checks) {
  if (!workflowNames.has(c.workflow)) {
    fail(
      `${YML} 監控「${c.label}」指向 workflow「${c.workflow}」，但 ${WF_DIR}/ 裡沒有這個 name——`
        + `這一項永遠會是「未執行」，每天寄一封假的異常信`,
    );
  }
}

// ② 每個會被 dispatch 的 workflow 都要有人監控（豁免必須寫在 UNWATCHED_BY_DESIGN）
const watched = new Set(checks.map((c) => c.workflow));
const targets = new Set([...dispatch.values(), ...standaloneWorkflows]);
for (const file of targets) {
  if (!existsSync(`${WF_DIR}/${file}`)) {
    fail(`${INDEX} 要 dispatch ${file}，但 ${WF_DIR}/${file} 不存在`);
    continue;
  }
  if (UNWATCHED_BY_DESIGN.has(file)) continue;
  const name = read(`${WF_DIR}/${file}`).match(/^name:\s*(.+)$/m)?.[1].trim();
  if (name && !watched.has(name)) {
    fail(`${file}（${name}）會被 dispatch，但 ${YML} 的 CHECK_JOBS 沒有它——壞了不會通知`);
  }
}
// 豁免清單不能留著指向已經不存在的檔案，否則它會靜靜地失去意義
for (const file of UNWATCHED_BY_DESIGN.keys()) {
  if (!existsSync(`${WF_DIR}/${file}`)) {
    fail(`UNWATCHED_BY_DESIGN 列了 ${file}，但該檔已不存在——請一併移除這筆豁免`);
  }
}

// ── 標示的時間／星期要對得上真正的排程 ────────────────────────────────
// 視窗是 [預期時間, 下一班)，所以標錯不會誤報，但每日信上顯示的時間會誤導人，
// 而「照信上的時間去改排程」正是排程事故的主要來源。
// Cloudflare day-of-week：1=Sun … 7=Sat（與標準 cron 不同，這裡一起驗）。
const twOf = (cron) => {
  const [min, hr] = cron.split(' ');
  if (min.includes(',') || hr.includes(',') || hr.includes('*')) return null; // 多時段班次不比對
  const tw = (+hr + 8) % 24;
  return { at: `${String(tw).padStart(2, '0')}:${String(+min).padStart(2, '0')}`, rollover: +hr + 8 >= 24 };
};
const twDaysOf = (cron, rollover) => {
  const dow = cron.split(' ')[4];
  if (dow === '*') return null; // 每天，不比對
  return dow.split(',').map((d) => ((+d - 1) + (rollover ? 1 : 0)) % 7).sort((a, b) => a - b);
};

for (const [cron, file] of dispatch) {
  const tw = twOf(cron);
  if (!tw) continue;
  if (!existsSync(`${WF_DIR}/${file}`)) continue; // 上面已回報
  const name = read(`${WF_DIR}/${file}`).match(/^name:\s*(.+)$/m)?.[1].trim();
  const mine = checks.filter((c) => c.workflow === name);
  if (mine.length === 0) continue; // 上面已回報（或屬豁免）

  const slots = mine.map((c) => `${String(c.hour).padStart(2, '0')}:${String(c.minute).padStart(2, '0')}`);
  if (!slots.includes(tw.at)) {
    fail(`${file} 實際排在台灣 ${tw.at}，但 CHECK_JOBS 標的是 ${slots.join('／')}——信上的時間會誤導人`);
    continue;
  }
  const expectDays = twDaysOf(cron, tw.rollover);
  if (!expectDays) continue;
  const slot = mine.find((c) => `${String(c.hour).padStart(2, '0')}:${String(c.minute).padStart(2, '0')}` === tw.at);
  const gotDays = [...slot.days].sort((a, b) => a - b);
  if (gotDays.join() !== expectDays.join()) {
    fail(
      `${file} 台灣 ${tw.at} 那班的 cron "${cron}" 換算後是星期 [${expectDays}]（0=日），`
        + `但 CHECK_JOBS 標成 [${gotDays.length ? gotDays : '每天'}]——`
        + `Cloudflare 的 day-of-week 是 1=Sun，別當成標準 cron 的 1=Mon`,
    );
  }
}

// ── lenient 必須真的有「後面那一班」可以補 ────────────────────────────
// lenient 的語意是「早班沒搶到資料不算異常，前提是後面真的有班補上」。
// 若某筆標了 lenient 卻沒有更晚的同 workflow 班次，它永遠不會被降級，
// 那個旗標就是死設定，會讓人以為已經處理過而其實沒有。
for (const c of checks.filter((x) => x.lenient)) {
  const later = checks.filter(
    (o) => o.workflow === c.workflow && (o.hour * 60 + o.minute) > (c.hour * 60 + c.minute),
  );
  if (later.length === 0) {
    fail(
      `${YML} 的「${c.label}」標了 lenient，但同一個 workflow today 沒有更晚的班次可以補——`
        + `lenient 永遠不會生效，等於沒標`,
    );
  }
}

if (problems.length) {
  console.error(`\n❌ cron 與監控清單對不上：${problems.length} 項\n`);
  for (const p of problems) console.error(`  · ${p}`);
  console.error(`\n改排程時 ${TOML}、${INDEX}、${YML} 要一起改。`);
  console.error('（另提醒：改完 cron/ 必須 `cd cron && npx wrangler deploy`，它不隨 git push 部署）\n');
  process.exit(1);
}
console.log(
  `✅ cron 與監控清單一致（${registered.length} 條排程、${dispatch.size} 條 dispatch、`
    + `${checks.length} 項檢查、${UNWATCHED_BY_DESIGN.size} 項具名豁免）`,
);
