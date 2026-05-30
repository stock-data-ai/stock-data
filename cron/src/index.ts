interface Env {
  GH_TOKEN: string;
  BREVO_API_KEY: string;
}

const REPO = 'stock-data-ai/stock-data';

interface CronJob {
  workflow: string;
  inputs?: Record<string, string>;
}

const CRON_MAP: Record<string, CronJob> = {
  '0 8 * * *':   { workflow: 'etf-active-daily.yml' },                                   // 台灣 16:00
  '30 11 * * *': { workflow: 'etf-active-daily.yml' },                                   // 台灣 19:30
  '30 10 * * *': { workflow: 'daily-update.yml', inputs: { force: 'true' } },            // 台灣 18:30
  '30 14 * * *': { workflow: 'daily-update.yml', inputs: { force: 'true' } },            // 台灣 22:30
  '35 10 * * 1-5': { workflow: 'market-sentiment.yml' },                                  // 台灣 18:35 週一到週五（第一次）
  '0 13 * * 1-5':  { workflow: 'market-sentiment.yml' },                                  // 台灣 21:00 週一到週五（第二次備援）
  '0 11 * * *':  { workflow: 'scraper-mops.yml' },                                       // 台灣 19:00
  '0 23 * * *':  { workflow: 'scraper-economic-daily.yml' },                             // 台灣 07:00
  '0 15 * * *':  { workflow: 'scraper-economic-daily.yml' },                             // 台灣 23:00
  '0 0 * * 7':   { workflow: 'etf-holdings-update.yml' },                                // 台灣 08:00 週六
  '0 1 * * 7':   { workflow: 'weekly-shareholder-update.yml', inputs: { force: 'true' } }, // 台灣 09:00 週六
  '0 3 * * 7':   { workflow: 'weekly-dividend-update.yml' },                             // 台灣 11:00 週六
  '0 1 * * 1':   { workflow: 'weekly-full-update.yml' },                                 // 台灣 09:00 週日
  '0 3 * * 1':   { workflow: 'weekly-balance-sheet-update.yml' },                        // 台灣 11:00 週日
  '0 19 * * 1':  { workflow: 'cleanup-workflow-runs.yml' },                              // 台灣 03:00 週一
  '0 2 * * 1':   { workflow: 'update-us-financials.yml' },                               // 台灣 10:00 週日
};

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
    try {
      await dispatch(job, token);
      return;
    } catch (err) {
      lastErr = err as Error;
    }
  }
  throw lastErr;
}

async function alertError(message: string, brevoKey: string) {
  await fetch('https://api.brevo.com/v3/smtp/email', {
    method: 'POST',
    headers: {
      'api-key': brevoKey,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      sender: { name: 'Stock Data Cron', email: 'noreply@aistockmap.com' },
      to: [{ email: 'ricky.wu@whalechip.com' }],
      subject: '⚠️ Stock Data Cron 失敗通知',
      textContent: message,
    }),
  });
}

export default {
  async scheduled(event: ScheduledEvent, env: Env, _ctx: ExecutionContext) {
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
