interface Env {
  GH_TOKEN: string;
  AWS_SES_ACCESS_KEY_ID: string;
  AWS_SES_SECRET_ACCESS_KEY: string;
  AWS_SES_REGION: string;
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
  '0 1 * * 7':            { workflow: 'weekly-shareholder-update.yml', inputs: { force: 'true' } }, // 台灣 09:00 週六（第一次）
  '30 1 * * 7':          { workflow: 'weekly-shareholder-update.yml', inputs: { force: 'true' } }, // 台灣 09:30 週六（第二次備援）
  '1 1 * * 1':            { workflow: 'weekly-financials-update.yml' },                             // 台灣 09:01 週日
  '0 2 * * 1':            { workflow: 'update-us-financials.yml' },                                 // 台灣 10:00 週日
  '0 3 * * 7':            { workflow: 'weekly-dividend-update.yml' },                               // 台灣 11:00 週六
  '0 3 * * 1':            { workflow: 'weekly-balance-sheet-update.yml' },                          // 台灣 11:00 週日
  '0 8 * * *':            { workflow: 'etf-active-daily.yml' },                                     // 台灣 16:00 每天（第一次）
  '55 7 * * 2,3,4,5,6':   { workflow: 'market-sentiment.yml' },                                    // 台灣 15:55 週一到週五（第一次）
  '55 8 * * 2,3,4,5,6':   { workflow: 'market-sentiment.yml' },                                    // 台灣 16:55 週一到週五（第二次）
  '55 9 * * *':           { workflow: 'etf-active-daily.yml' },                                     // 台灣 17:55 每天（第二次）
  '05 11 * * *':          { workflow: 'daily-update.yml', inputs: { force: 'true' } },              // 台灣 19:05 每天（第一次）
  '05 13 * * *':          { workflow: 'daily-update.yml', inputs: { force: 'true' } },              // 台灣 21:05 每天（第二次，TWSE資料結算延遲備援）
  '0 12 * * 2,3,4,5,6':   { workflow: 'generate-chip-topic.yml' },                                 // 台灣 20:00 週一到週五
  '30 13 * * 2,3,4,5,6':  { workflow: 'margin-trading-update.yml' },                               // 台灣 21:30 週一到週五
  '0 11 * * *':           { workflow: 'scraper-mops.yml' },                                         // 台灣 19:00 每天
  '55 12 * * 2,3,4,5,6':  { workflow: 'market-sentiment.yml' },                                    // 台灣 20:55 週一到週五（第三次）
  '55 13 * * 2,3,4,5,6':  { workflow: 'market-sentiment.yml' },                                    // 台灣 21:55 週一到週五（第四次，融資融券公布後）
  '05 11 * * 2,3,4,5,6':  { workflow: 'generate-disposition-forecast.yml' },                       // 台灣 19:05 週一到週五（提早出爐；價量已結算。此時融資融券款7尚未公布，由下方備援補齊）
  '30 15 * * 2,3,4,5,6':  { workflow: 'generate-disposition-forecast.yml' },                       // 台灣 23:30 週一到週五（備援，融資融券公布後補上款7）
  '30 12 * * *':          { workflow: 'etf-active-daily.yml' },                                     // 台灣 20:30 每天（第三次）
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
  { label: '07:00 Economic Daily Scraper｜爬取經濟日報（早）', workflowName: 'Economic Daily Scraper｜經濟日報爬蟲', twHour: 7, twMinute: 0 },
  { label: '16:00 Active ETF Holdings Update (Daily)｜更新主動型 ETF 持股（第一次）', workflowName: 'Active ETF Holdings Update (Daily)｜主動型ETF持股（每日）', twHour: 16, twMinute: 0 },
  { label: '19:05 Daily Update｜每日更新（市值 + 三大法人，第一次）', workflowName: 'Daily Update｜每日更新（市值＋三大法人）', twHour: 19, twMinute: 5 },
  { label: '21:05 Daily Update｜每日更新（市值 + 三大法人，第二次備援）', workflowName: 'Daily Update｜每日更新（市值＋三大法人）', twHour: 21, twMinute: 5 },
  { label: '19:00 MOPS Scraper｜爬取公開資訊觀測站重訊', workflowName: 'MOPS Scraper｜公開資訊觀測站爬蟲', twHour: 19, twMinute: 0 },
  { label: '17:55 Active ETF Holdings Update (Daily)｜更新主動型 ETF 持股（第二次）', workflowName: 'Active ETF Holdings Update (Daily)｜主動型ETF持股（每日）', twHour: 17, twMinute: 55 },
  { label: '20:30 Active ETF Holdings Update (Daily)｜更新主動型 ETF 持股（第三次）', workflowName: 'Active ETF Holdings Update (Daily)｜主動型ETF持股（每日）', twHour: 20, twMinute: 30 },
  { label: '21:30 Economic Daily Scraper｜爬取經濟日報（晚）', workflowName: 'Economic Daily Scraper｜經濟日報爬蟲', twHour: 21, twMinute: 30 },
  { label: '15:55 Market Sentiment Update｜更新市場情緒指標（第一次）', workflowName: 'Market Sentiment Update｜市場情緒更新', twHour: 15, twMinute: 55, days: [1, 2, 3, 4, 5] },
  { label: '16:55 Market Sentiment Update｜更新市場情緒指標（第二次）', workflowName: 'Market Sentiment Update｜市場情緒更新', twHour: 16, twMinute: 55, days: [1, 2, 3, 4, 5] },
  { label: '20:55 Market Sentiment Update｜更新市場情緒指標（第三次）', workflowName: 'Market Sentiment Update｜市場情緒更新', twHour: 20, twMinute: 55, days: [1, 2, 3, 4, 5] },
  { label: '21:55 Market Sentiment Update｜更新市場情緒指標（第四次，融資融券公布後）', workflowName: 'Market Sentiment Update｜市場情緒更新', twHour: 21, twMinute: 55, days: [1, 2, 3, 4, 5] },
  { label: '20:00 Generate Chip Topic｜生成籌碼題材標籤', workflowName: 'Generate Chip Topic｜生成籌碼題材', twHour: 20, twMinute: 0, days: [1, 2, 3, 4, 5] },
  { label: '21:30 Margin Trading Update｜更新融資融券數據', workflowName: 'Margin Trading Update｜融資融券更新', twHour: 21, twMinute: 30, days: [1, 2, 3, 4, 5] },
  { label: '週六 08:00 ETF Holdings Update (Weekly)｜每週 ETF 持股更新', workflowName: 'ETF Holdings Update (Weekly)｜ETF持股（週更）', twHour: 8, twMinute: 0, days: [6] },
  { label: '週六 09:00 Weekly Shareholder Update (Saturday)｜每週股東結構更新（第一次）', workflowName: 'Weekly Shareholder Update (Saturday)｜股東結構（週六）', twHour: 9, twMinute: 0, days: [6] },
  { label: '週六 09:30 Weekly Shareholder Update (Saturday)｜每週股東結構更新（第二次備援）', workflowName: 'Weekly Shareholder Update (Saturday)｜股東結構（週六）', twHour: 9, twMinute: 30, days: [6] },
  { label: '週六 11:00 Weekly Dividend Update (Saturday)｜每週股利資料更新', workflowName: 'Weekly Dividend Update (Saturday)｜股利資料（週六）', twHour: 11, twMinute: 0, days: [6] },
  { label: '週日 09:01 Weekly Financials Update (Sunday)｜每週財務報表更新（台股）', workflowName: 'Weekly Financials Update (Sunday)｜財務報表更新（週日）', twHour: 9, twMinute: 1, days: [0] },
  { label: '週日 10:00 Update US Financials｜更新美股財務資料', workflowName: 'Update US Financials｜美股財務更新', twHour: 10, twMinute: 0, days: [0] },
  { label: '週日 11:00 Weekly Balance Sheet Update (Monday)｜每週資產負債表更新', workflowName: 'Weekly Balance Sheet Update (Monday)｜資產負債表（週）', twHour: 11, twMinute: 0, days: [0] },
  { label: '週一 03:00 Cleanup old workflow runs｜清理舊 workflow 記錄', workflowName: 'Cleanup old workflow runs｜清理舊 workflow 記錄', twHour: 3, twMinute: 0, days: [1] },
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
      await alertError(`健康檢查失敗：無法讀取 GitHub Actions runs: ${res.status}`, env);
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

  await alertError(body, env, subject);
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

async function alertError(message: string, env: Env, subject = '⚠️ Stock Data Cron 失敗通知') {
  const region = env.AWS_SES_REGION || 'ap-northeast-1';
  const payload = JSON.stringify({
    FromEmailAddress: 'noreply@aistockmap.com',
    Destination: { ToAddresses: ['rf9550106@gmail.com'] },
    Content: {
      Simple: {
        Subject: { Data: subject, Charset: 'UTF-8' },
        Body: { Text: { Data: message, Charset: 'UTF-8' } },
      },
    },
  });

  const now = new Date();
  const amzDate = now.toISOString().replace(/[:\-]|\.\d{3}/g, '').slice(0, 15) + 'Z';
  const dateStamp = amzDate.slice(0, 8);
  const host = `email.${region}.amazonaws.com`;
  const payloadHash = await sha256hex(payload);
  const canonicalHeaders = `content-type:application/json\nhost:${host}\nx-amz-date:${amzDate}\n`;
  const signedHeaders = 'content-type;host;x-amz-date';
  const canonicalRequest = ['POST', '/v2/email/outbound-emails', '', canonicalHeaders, signedHeaders, payloadHash].join('\n');
  const credentialScope = `${dateStamp}/${region}/ses/aws4_request`;
  const stringToSign = ['AWS4-HMAC-SHA256', amzDate, credentialScope, await sha256hex(canonicalRequest)].join('\n');
  const signingKey = await getSigningKey(env.AWS_SES_SECRET_ACCESS_KEY, dateStamp, region);
  const signature = await hmacHex(signingKey, stringToSign);

  await fetch(`https://${host}/v2/email/outbound-emails`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Amz-Date': amzDate,
      Authorization: `AWS4-HMAC-SHA256 Credential=${env.AWS_SES_ACCESS_KEY_ID}/${credentialScope}, SignedHeaders=${signedHeaders}, Signature=${signature}`,
    },
    body: payload,
  });
}

async function sha256hex(data: string): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(data));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
}

async function hmacRaw(key: BufferSource, data: string): Promise<ArrayBuffer> {
  const k = await crypto.subtle.importKey('raw', key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return crypto.subtle.sign('HMAC', k, new TextEncoder().encode(data));
}

async function hmacHex(key: ArrayBuffer, data: string): Promise<string> {
  const buf = await hmacRaw(key, data);
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
}

async function getSigningKey(secret: string, dateStamp: string, region: string): Promise<ArrayBuffer> {
  const k = await hmacRaw(new TextEncoder().encode('AWS4' + secret), dateStamp);
  const kr = await hmacRaw(k, region);
  const ks = await hmacRaw(kr, 'ses');
  return hmacRaw(ks, 'aws4_request');
}

export default {
  async scheduled(event: ScheduledEvent, env: Env, _ctx: ExecutionContext) {
    if (event.cron === HEALTH_CHECK_CRON) {
      try {
        await dispatchWithRetry(HEALTH_CHECK_JOB, env.GH_TOKEN);
        return;
      } catch (err) {
        await alertError(String(err), env);
        throw err;
      }
    }

    const job = CRON_MAP[event.cron];
    if (!job) {
      await alertError(`Unknown cron: ${event.cron}`, env);
      throw new Error(`Unknown cron: ${event.cron}`);
    }
    try {
      await dispatchWithRetry(job, env.GH_TOKEN);
    } catch (err) {
      await alertError(String(err), env);
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
