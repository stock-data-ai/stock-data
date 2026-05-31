interface Env {
  GH_TOKEN: string;
  BREVO_API_KEY: string;
}

// Cloudflare day-of-week: 1=Sun 2=Mon 3=Tue 4=Wed 5=Thu 6=Fri 7=Sat
// Ranges (e.g. 1-5) are NOT supported — use comma-separated values only

const REPO = 'stock-data-ai/stock-data';

interface CronJob {
  workflow: string;
  inputs?: Record<string, string>;
}

const CRON_MAP: Record<string, CronJob> = {
  '0 23 * * *':           { workflow: 'scraper-economic-daily.yml' },                               // 台灣 07:00 每天
  '0 0 * * 7':            { workflow: 'etf-holdings-update.yml' },                                  // 台灣 08:00 週六
  '0 1 * * 7':            { workflow: 'weekly-shareholder-update.yml', inputs: { force: 'true' } }, // 台灣 09:00 週六
  '0 1 * * 1':            { workflow: 'weekly-full-update.yml' },                                   // 台灣 09:00 週日
  '0 2 * * 1':            { workflow: 'update-us-financials.yml' },                                 // 台灣 10:00 週日
  '0 3 * * 7':            { workflow: 'weekly-dividend-update.yml' },                               // 台灣 11:00 週六
  '0 3 * * 1':            { workflow: 'weekly-balance-sheet-update.yml' },                          // 台灣 11:00 週日
  '0 8 * * *':            { workflow: 'etf-active-daily.yml' },                                     // 台灣 16:00 每天
  '30 10 * * *':          { workflow: 'daily-update.yml', inputs: { force: 'true' } },              // 台灣 18:30 每天
  '35 10 * * 2,3,4,5,6':  { workflow: 'market-sentiment.yml' },                                    // 台灣 18:35 週一到週五（第一次）
  '0 11 * * *':           { workflow: 'scraper-mops.yml' },                                         // 台灣 19:00 每天
  '30 11 * * *':          { workflow: 'etf-active-daily.yml' },                                     // 台灣 19:30 每天
  '0 13 * * 2,3,4,5,6':   { workflow: 'market-sentiment.yml' },                                    // 台灣 21:00 週一到週五（第二次備援）
  '30 13 * * *':          { workflow: 'scraper-economic-daily.yml' },                               // 台灣 21:30 每天
  '0 19 * * 2':           { workflow: 'cleanup-workflow-runs.yml' },                                // 台灣 03:00 週一
};

// ── Health check (台灣 23:00 = UTC 15:00) ─────────────────────────────────────

const HEALTH_CHECK_CRON = '0 15 * * *';

// UTC getUTCDay(): 0=Sun 1=Mon 2=Tue 3=Wed 4=Thu 5=Fri 6=Sat
const CHECK_DAILY    = ['Active ETF Holdings Update (Daily)', 'Daily Update', 'MOPS Scraper', 'Economic Daily Scraper'];
const CHECK_WEEKDAY  = ['Market Sentiment Update'];
const CHECK_SATURDAY = ['ETF Holdings Update (Weekly)', 'Weekly Shareholder Update (Saturday)', 'Weekly Dividend Update (Saturday)'];
const CHECK_SUNDAY   = ['Weekly Full Update (Sunday)', 'Update US Financials', 'Weekly Balance Sheet Update (Monday)'];

interface WorkflowRun {
  name: string;
  status: string;
  conclusion: string | null;
  html_url: string;
}

async function runHealthCheck(env: Env): Promise<void> {
  const now = new Date();
  const since = new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString().replace(/\.\d+Z$/, 'Z');
  const twDate = new Date(now.getTime() + 8 * 60 * 60 * 1000).toISOString().slice(0, 10);
  const utcDay = now.getUTCDay();

  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/runs?created=>=${since}&per_page=100`,
    {
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'stock-data-cron',
      },
    }
  );

  if (!res.ok) {
    await alertError(`健康檢查失敗：無法讀取 GitHub Actions runs: ${res.status}`, env.BREVO_API_KEY);
    return;
  }

  const data = await res.json() as { workflow_runs: WorkflowRun[] };
  const idx: Record<string, { success: boolean; url: string }> = {};
  for (const run of data.workflow_runs) {
    if (run.status !== 'completed') continue;
    if (!idx[run.name]) idx[run.name] = { success: false, url: run.html_url };
    if (run.conclusion === 'success') idx[run.name].success = true;
  }

  const required = [
    ...CHECK_DAILY,
    ...(utcDay >= 1 && utcDay <= 5 ? CHECK_WEEKDAY  : []),
    ...(utcDay === 6               ? CHECK_SATURDAY : []),
    ...(utcDay === 0               ? CHECK_SUNDAY   : []),
  ];

  const ok: string[] = [], failed: string[] = [], missing: string[] = [];
  for (const name of required) {
    const entry = idx[name];
    if (!entry)             missing.push(name);
    else if (entry.success) ok.push(name);
    else                    failed.push(`${name}\n  ${entry.url}`);
  }

  const allGood = failed.length === 0 && missing.length === 0;
  const subject = allGood
    ? `✅ [${twDate}] stock_data 更新完成`
    : `⚠️ [${twDate}] stock_data 更新異常`;

  let body = `stock_data 健康檢查｜${twDate} 23:00 Taiwan\n\n`;
  if (ok.length)      body += `✅ 成功 (${ok.length})\n${ok.map(w => `  • ${w}`).join('\n')}\n\n`;
  if (failed.length)  body += `❌ 失敗\n${failed.map(w => `  • ${w}`).join('\n')}\n\n`;
  if (missing.length) body += `⚠️ 未執行\n${missing.map(w => `  • ${w}`).join('\n')}\n\n`;

  await alertError(body, env.BREVO_API_KEY, subject);
}

// ── Shared helpers ────────────────────────────────────────────────────────────

async function dispatch(job: CronJob, token: string) {
  const body: Record<string, unknown> = { ref: 'main' };
  if (job.inputs) body.inputs = job.inputs;

  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${job.workflow}/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'stock-data-cron',
      },
      body: JSON.stringify(body),
    }
  );
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`dispatch ${job.workflow} failed: ${res.status} ${text}`);
  }
}

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

async function dispatchWithRetry(job: CronJob, token: string, maxRetries = 3) {
  const delays = [0, 5000, 10000];
  let lastErr: Error | undefined;
  for (let i = 0; i < maxRetries; i++) {
    if (delays[i]) await sleep(delays[i]);
    try { await dispatch(job, token); return; }
    catch (err) { lastErr = err as Error; }
  }
  throw lastErr;
}

async function alertError(message: string, brevoKey: string, subject = '⚠️ Stock Data Cron 失敗通知') {
  await fetch('https://api.brevo.com/v3/smtp/email', {
    method: 'POST',
    headers: { 'api-key': brevoKey, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      sender: { name: 'Stock Data Cron', email: 'noreply@aistockmap.com' },
      to: [{ email: 'ricky.wu@whalechip.com' }],
      subject,
      textContent: message,
    }),
  });
}

export default {
  async scheduled(event: ScheduledEvent, env: Env, _ctx: ExecutionContext) {
    if (event.cron === HEALTH_CHECK_CRON) {
      await runHealthCheck(env);
      return;
    }

    const job = CRON_MAP[event.cron];
    if (!job) {
      await alertError(`Unknown cron: ${event.cron}`, env.BREVO_API_KEY);
      throw new Error(`Unknown cron: ${event.cron}`);
    }
    try {
      await dispatchWithRetry(job, env.GH_TOKEN);
    } catch (err) {
      await alertError(String(err), env.BREVO_API_KEY);
      throw err;
    }
  },
};
