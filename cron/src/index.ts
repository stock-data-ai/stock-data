interface Env {
  GH_TOKEN: string;
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
  '0 11 * * *':  { workflow: 'scraper-mops.yml' },                                       // 台灣 19:00
  '0 23 * * *':  { workflow: 'scraper-economic-daily.yml' },                             // 台灣 07:00
  '0 15 * * *':  { workflow: 'scraper-economic-daily.yml' },                             // 台灣 23:00
  '0 0 * * 7':   { workflow: 'etf-holdings-update.yml' },                                // 台灣 08:00 週六
  '0 1 * * 7':   { workflow: 'weekly-shareholder-update.yml', inputs: { force: 'true' } }, // 台灣 09:00 週六
  '0 3 * * 7':   { workflow: 'weekly-dividend-update.yml' },                             // 台灣 11:00 週六
  '0 1 * * 1':   { workflow: 'weekly-full-update.yml' },                                 // 台灣 09:00 週日
  '0 3 * * 1':   { workflow: 'weekly-balance-sheet-update.yml' },                        // 台灣 11:00 週一
  '0 19 * * 1':  { workflow: 'cleanup-workflow-runs.yml' },                              // 台灣 03:00 週日
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

export default {
  async scheduled(event: ScheduledEvent, env: Env, _ctx: ExecutionContext) {
    const job = CRON_MAP[event.cron];
    if (!job) throw new Error(`Unknown cron: ${event.cron}`);
    await dispatch(job, env.GH_TOKEN);
  },
};
