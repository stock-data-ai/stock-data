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
  '30 8 * * *':           { workflow: 'daily-update.yml', inputs: { force: 'true' } },              // 台灣 16:30 每天（第一次，T86 公布後即抓）
  '05 9 * * *':           { workflow: 'daily-update.yml', inputs: { force: 'true' } },              // 台灣 17:05 每天（第二次，補 TPEx 上櫃結算）
  '05 13 * * *':          { workflow: 'daily-update.yml', inputs: { force: 'true' } },              // 台灣 21:05 每天（第三次備援，TWSE資料結算延遲）
  '0 10 * * 2,3,4,5,6':   { workflow: 'generate-chip-topic.yml' },                                 // 台灣 18:00 週一到週五（上游 daily-update 17:05 已收工；提早是為了讓 stock_map 的每日焦點在 20:00 題材信之前產出）
  '30 13 * * 2,3,4,5,6':  { workflow: 'margin-trading-update.yml' },                               // 台灣 21:30 週一到週五
  '0 11 * * *':           { workflow: 'scraper-mops.yml' },                                         // 台灣 19:00 每天
  '55 12 * * 2,3,4,5,6':  { workflow: 'market-sentiment.yml' },                                    // 台灣 20:55 週一到週五（第三次）
  '55 13 * * 2,3,4,5,6':  { workflow: 'market-sentiment.yml' },                                    // 台灣 21:55 週一到週五（第四次，融資融券公布後）
  '05 11 * * 2,3,4,5,6':  { workflow: 'generate-disposition-forecast.yml' },                       // 台灣 19:05 週一到週五（提早出爐；價量已結算。此時融資融券款7尚未公布，由下方備援補齊）
  '30 14 * * 2,3,4,5,6':  { workflow: 'generate-disposition-forecast.yml' },                       // 台灣 22:30 週一到週五（備援，補款7；款7 直接讀 TWSE openapi MI_MARGN，21:30 就有當日資料。排在 23:00 健康檢查之前才蓋得到）
  '30 12 * * *':          { workflow: 'etf-active-daily.yml' },                                     // 台灣 20:30 每天（第三次）
  '30 13 * * *':          { workflow: 'scraper-economic-daily.yml' },                               // 台灣 21:30 每天
  '0 19 * * 1':           { workflow: 'cleanup-workflow-runs.yml' },                                // 台灣 03:00 週一（CF 1=Sun, UTC Sun 19:00 = TW Mon 03:00）
};

// ── Health check ─────────────────────────────────────────────────────────────
// 這裡只負責「叫 health-check.yml 起來跑」。檢查清單、比對邏輯、寄信全在那個
// workflow 裡，**不要在這裡再放一份 CHECK_JOBS**——本檔曾有一份重複清單，只有
// 手動 /health-check 端點會用到，結果與 workflow 那份長期漂移（標籤都已不同）。
//
// 檢查固定 23:00。要被它蓋到的排程一律安排在 23:00 之前收工——處置股備援班原本排
// 23:30，因此長期不在檢查清單內（2026-08-17 那班失敗了整天沒人發現），現已提前到 22:30。

const HEALTH_CHECK_CRON = '0 15 * * *';   // 台灣 23:00
const HEALTH_CHECK_JOB: CronJob = { workflow: 'health-check.yml' };

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

    await dispatchWithRetry(HEALTH_CHECK_JOB, env.GH_TOKEN);
    return new Response('Health check workflow dispatched.', { status: 200 });
  },
};
