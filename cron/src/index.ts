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
  '0 12 * * 2,3,4,5,6':   { workflow: 'generate-chip-topic.yml' },                                 // 台灣 20:00 週一到週五
  '0 11 * * *':           { workflow: 'scraper-mops.yml' },                                         // 台灣 19:00 每天
  '30 11 * * *':          { workflow: 'etf-active-daily.yml' },                                     // 台灣 19:30 每天
  '0 13 * * 2,3,4,5,6':   { workflow: 'market-sentiment.yml' },                                    // 台灣 21:00 週一到週五（第二次備援）
  '30 13 * * *':          { workflow: 'scraper-economic-daily.yml' },                               // 台灣 21:30 每天
  '0 19 * * 1':           { workflow: 'cleanup-workflow-runs.yml' },                                // 台灣 03:00 週一（CF 1=Sun, UTC Sun 19:00 = TW Mon 03:00）
};

// ── Health check (台灣 23:00 = UTC 15:00) ─────────────────────────────────────

const HEALTH_CHECK_CRON = '0 15 * * *';
const HEALTH_CHECK_JOB: CronJob = { workflow: 'health-check.yml' };
const TAIWAN_OFFSET_MS = 8 * 60 * 60 * 1000;

// Taiwan day via getUTCDay(): 0=Sun 1=Mon 2=Tue 3=Wed 4=Thu 5=Fri 6=Sat
interface CheckJob {
  label: string;
  workflowName: string;
  twHour: number;
  twMinute: number;
  days?: number[];
}

const CHECK_JOBS: CheckJob[] = [
  { label: '07:00 Economic Daily Scraper', workflowName: 'Economic Daily Scraper', twHour: 7, twMinute: 0 },
  { label: '16:00 Active ETF Holdings Update (Daily)', workflowName: 'Active ETF Holdings Update (Daily)', twHour: 16, twMinute: 0 },
  { label: '18:30 Daily Update', workflowName: 'Daily Update', twHour: 18, twMinute: 30 },
  { label: '19:00 MOPS Scraper', workflowName: 'MOPS Scraper', twHour: 19, twMinute: 0 },
  { label: '19:30 Active ETF Holdings Update (Daily)', workflowName: 'Active ETF Holdings Update (Daily)', twHour: 19, twMinute: 30 },
  { label: '21:30 Economic Daily Scraper', workflowName: 'Economic Daily Scraper', twHour: 21, twMinute: 30 },
  { label: '18:35 Market Sentiment Update', workflowName: 'Market Sentiment Update', twHour: 18, twMinute: 35, days: [1, 2, 3, 4, 5] },
  { label: '21:00 Market Sentiment Update', workflowName: 'Market Sentiment Update', twHour: 21, twMinute: 0, days: [1, 2, 3, 4, 5] },
  { label: '20:00 Generate Chip Topic', workflowName: 'Generate Chip Topic', twHour: 20, twMinute: 0, days: [1, 2, 3, 4, 5] },
  { label: '週六 08:00 ETF Holdings Update (Weekly)', workflowName: 'ETF Holdings Update (Weekly)', twHour: 8, twMinute: 0, days: [6] },
  { label: '週六 09:00 Weekly Shareholder Update (Saturday)', workflowName: 'Weekly Shareholder Update (Saturday)', twHour: 9, twMinute: 0, days: [6] },
  { label: '週六 11:00 Weekly Dividend Update (Saturday)', workflowName: 'Weekly Dividend Update (Saturday)', twHour: 11, twMinute: 0, days: [6] },
  { label: '週日 09:00 Weekly Full Update (Sunday)', workflowName: 'Weekly Full Update (Sunday)', twHour: 9, twMinute: 0, days: [0] },
  { label: '週日 10:00 Update US Financials', workflowName: 'Update US Financials', twHour: 10, twMinute: 0, days: [0] },
  { label: '週日 11:00 Weekly Balance Sheet Update (Monday)', workflowName: 'Weekly Balance Sheet Update (Monday)', twHour: 11, twMinute: 0, days: [0] },
  { label: '週一 03:00 Cleanup old workflow runs', workflowName: 'Cleanup old workflow runs', twHour: 3, twMinute: 0, days: [1] },
];

interface WorkflowRun {
  name: string;
  status: string;
  conclusion: string | null;
  html_url: string;
  created_at: string;
}

function toGithubTimestamp(date: Date): string {
  return date.toISOString().replace(/\.\d+Z$/, 'Z');
}

function getTaiwanCheckWindow(now: Date) {
  const taiwanNow = new Date(now.getTime() + TAIWAN_OFFSET_MS);
  const twDate = taiwanNow.toISOString().slice(0, 10);
  const [year, month, day] = twDate.split('-').map(Number);
  const twStartUtc = new Date(Date.UTC(year, month - 1, day) - TAIWAN_OFFSET_MS);

  return {
    since: toGithubTimestamp(twStartUtc),
    until: toGithubTimestamp(now),
    twDate,
    twDay: taiwanNow.getUTCDay(),
    twStartUtc,
    taiwanYmd: { year, month, day },
  };
}

function getJobStartUtc(job: CheckJob, ymd: { year: number; month: number; day: number }): Date {
  return new Date(Date.UTC(ymd.year, ymd.month - 1, ymd.day, job.twHour - 8, job.twMinute));
}

function getExpectedJobs(twDay: number): CheckJob[] {
  return CHECK_JOBS.filter(job => !job.days || job.days.includes(twDay));
}

function getJobWindow(job: CheckJob, expected: CheckJob[], ymd: { year: number; month: number; day: number }, until: string) {
  const start = getJobStartUtc(job, ymd);
  const nextSameWorkflow = expected
    .filter(candidate => candidate.workflowName === job.workflowName)
    .map(candidate => getJobStartUtc(candidate, ymd))
    .filter(candidateStart => candidateStart.getTime() > start.getTime())
    .sort((a, b) => a.getTime() - b.getTime())[0];

  return {
    start,
    end: nextSameWorkflow ?? new Date(until),
  };
}

async function runHealthCheck(env: Env): Promise<void> {
  const { since, until, twDate, twDay, taiwanYmd } = getTaiwanCheckWindow(new Date());
  const workflowRuns: WorkflowRun[] = [];

  for (let page = 1; page <= 5; page++) {
    const runsUrl = new URL(`https://api.github.com/repos/${REPO}/actions/runs`);
    runsUrl.searchParams.set('created', `${since}..${until}`);
    runsUrl.searchParams.set('per_page', '100');
    runsUrl.searchParams.set('page', String(page));

    const res = await fetch(runsUrl.toString(), {
      headers: {
        Authorization: `Bearer ${env.GH_TOKEN}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'stock-data-cron',
      },
    });

    if (!res.ok) {
      await alertError(`健康檢查失敗：無法讀取 GitHub Actions runs: ${res.status}`, env.BREVO_API_KEY);
      return;
    }

    const data = await res.json() as { workflow_runs: WorkflowRun[] };
    workflowRuns.push(...data.workflow_runs);
    if (data.workflow_runs.length < 100) break;
  }

  const required = getExpectedJobs(twDay);
  const ok: string[] = [], failed: string[] = [], missing: string[] = [];
  for (const job of required) {
    const { start, end } = getJobWindow(job, required, taiwanYmd, until);
    const runs = workflowRuns.filter(run => {
      const created = new Date(run.created_at);
      return run.name === job.workflowName && created >= start && created < end;
    });
    const success = runs.find(run => run.status === 'completed' && run.conclusion === 'success');
    const completedFailure = runs.find(run => run.status === 'completed' && run.conclusion !== 'success');

    if (success)       ok.push(job.label);
    else if (runs.length === 0) missing.push(job.label);
    else if (completedFailure) failed.push(`${job.label}\n  ${completedFailure.html_url}`);
    else              missing.push(`${job.label}（尚未完成）`);
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
      try {
        await dispatchWithRetry(HEALTH_CHECK_JOB, env.GH_TOKEN);
        return;
      } catch (err) {
        await alertError(String(err), env.BREVO_API_KEY);
        throw err;
      }
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

  async fetch(request: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname !== '/health-check') return new Response('Not Found', { status: 404 });

    const auth = request.headers.get('Authorization');
    if (auth !== `Bearer ${env.GH_TOKEN}`) return new Response('Unauthorized', { status: 401 });

    await runHealthCheck(env);
    return new Response('Health check triggered, email sent.', { status: 200 });
  },
};
