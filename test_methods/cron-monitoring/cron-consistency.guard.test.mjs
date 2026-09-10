/**
 * WS-06-A / W6-F1 + W6-F2: the cron/monitoring consistency guard.
 *
 * W6-F1: `5804508637` (2026-09-03) moved the heatmap back to stock_map — it deleted this
 * repo's `update-heatmap.yml` and its cron, but left the matching entry in
 * `health-check.yml`'s CHECK_JOBS. From 2026-09-04 to 2026-09-09 the 23:00 health check
 * therefore looked for a workflow that no longer existed and reported it "未執行" every
 * single day: 19 items green, one phantom red, one "異常" email per day. The cost is not
 * the email — it is that a real failure became indistinguishable from the standing one.
 *
 * W6-F2: stock_map has had a guard for this since 2026-09-03. This repo had none, which
 * is why the same class of mistake could reach production here and not there.
 *
 * These tests run the REAL guard against the REAL repository, and — for the negative
 * cases — against scratch copies with one specific thing broken. Mutations never touch
 * the working tree: every fixture is a full copy under a temp directory.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { repoRoot, scratch } from './harness.mjs';

const GUARD = 'scripts/guards/check-cron-consistency.mjs';

/** Copy just the inputs the guard reads, so a fixture is cheap and obviously scoped. */
function fixture(label) {
    const dir = scratch(label);
    fs.mkdirSync(path.join(dir, 'cron'), { recursive: true });
    fs.mkdirSync(path.join(dir, 'cron/src'), { recursive: true });
    fs.mkdirSync(path.join(dir, '.github/workflows'), { recursive: true });
    fs.mkdirSync(path.join(dir, 'scripts/guards'), { recursive: true });
    fs.copyFileSync(path.join(repoRoot, 'cron/wrangler.toml'), path.join(dir, 'cron/wrangler.toml'));
    fs.copyFileSync(path.join(repoRoot, 'cron/src/index.ts'), path.join(dir, 'cron/src/index.ts'));
    fs.copyFileSync(path.join(repoRoot, GUARD), path.join(dir, GUARD));
    const wf = path.join(repoRoot, '.github/workflows');
    for (const f of fs.readdirSync(wf).filter((f) => f.endsWith('.yml'))) {
        fs.copyFileSync(path.join(wf, f), path.join(dir, '.github/workflows', f));
    }
    return dir;
}

const runGuard = (cwd = repoRoot) => {
    const r = spawnSync(process.execPath, [GUARD], { cwd, encoding: 'utf-8' });
    return { status: r.status, out: `${r.stdout}${r.stderr}` };
};

const edit = (dir, rel, fn) => {
    const p = path.join(dir, rel);
    fs.writeFileSync(p, fn(fs.readFileSync(p, 'utf-8')));
};

// ── the real repository must be consistent ───────────────────────────────────

test('the guard passes on the repository as it stands', () => {
    const r = runGuard();
    assert.equal(r.status, 0, r.out);
    assert.match(r.out, /✅/);
});

test('the phantom heatmap entry really is gone from CHECK_JOBS', () => {
    const yml = fs.readFileSync(path.join(repoRoot, '.github/workflows/health-check.yml'), 'utf-8');
    assert.doesNotMatch(yml, /'workflow_name':\s*'Update Heatmap Data/,
        'the entry that produced six days of false alarms must not come back');
    const names = fs.readdirSync(path.join(repoRoot, '.github/workflows'))
        .filter((f) => f.endsWith('.yml'))
        .map((f) => fs.readFileSync(path.join(repoRoot, '.github/workflows', f), 'utf-8')
            .match(/^name:\s*(.+)$/m)?.[1].trim());
    assert.ok(!names.includes('Update Heatmap Data｜更新熱力圖（stock_map）'),
        'and the workflow it pointed at is indeed absent — that is why it was phantom');
});

// ── T1: a CHECK_JOBS entry with no matching workflow (the W6-F1 shape) ───────

test('T1 reverse: re-adding the deleted heatmap entry is caught', () => {
    const dir = fixture('t1');
    edit(dir, '.github/workflows/health-check.yml', (s) => s.replace(
        /(\s+)(\{'label': '15:30 Update Stock Tags')/,
        `$1{'label': '15:00 Update Heatmap Data', 'workflow_name': 'Update Heatmap Data｜更新熱力圖（stock_map）', 'hour': 15, 'minute': 0},$1$2`,
    ));
    const r = runGuard(dir);
    assert.equal(r.status, 1, `guard should have failed:\n${r.out}`);
    assert.match(r.out, /更新熱力圖（stock_map）/);
    assert.match(r.out, /沒有這個 name|永遠會是「未執行」/);
});

test('T1 reverse: any CHECK_JOBS entry pointing at a missing workflow is caught', () => {
    const dir = fixture('t1b');
    edit(dir, '.github/workflows/health-check.yml', (s) =>
        s.replace("'workflow_name': 'MOPS Scraper｜公開資訊觀測站爬蟲'", "'workflow_name': 'Totally Gone｜已刪除'"));
    const r = runGuard(dir);
    assert.equal(r.status, 1, r.out);
    assert.match(r.out, /Totally Gone/);
});

// ── T2: a dispatched workflow nobody monitors ────────────────────────────────

test('T2 reverse: removing a CHECK_JOBS entry for a dispatched workflow is caught', () => {
    const dir = fixture('t2');
    edit(dir, '.github/workflows/health-check.yml', (s) =>
        s.split('\n').filter((l) => !l.includes("'label': '19:00 MOPS Scraper'")).join('\n'));
    const r = runGuard(dir);
    assert.equal(r.status, 1, r.out);
    assert.match(r.out, /scraper-mops\.yml.*壞了不會通知/s);
});

test('T2: the only unmonitored dispatch target is the named, justified exemption', () => {
    const guard = fs.readFileSync(path.join(repoRoot, GUARD), 'utf-8');
    const block = guard.match(/UNWATCHED_BY_DESIGN = new Map\(\[([\s\S]*?)\n\]\);/);
    assert.ok(block, 'the exemption table must stay explicit and greppable');
    const files = [...block[1].matchAll(/'([\w.-]+\.yml)'/g)].map((m) => m[1]);
    assert.deepEqual(files, ['health-check.yml'],
        'a new exemption needs a written reason and a review, not a quiet addition');
    assert.match(block[1], /健康檢查不能監控自己/, 'the reason must be spelled out, not implied');
});

test('T2 reverse: an exemption for a workflow that no longer exists is caught', () => {
    const dir = fixture('t2b');
    edit(dir, GUARD, (s) => s.replace(
        "const UNWATCHED_BY_DESIGN = new Map([",
        "const UNWATCHED_BY_DESIGN = new Map([\n  ['deleted-long-ago.yml', '過期的豁免'],",
    ));
    const r = runGuard(dir);
    assert.equal(r.status, 1, r.out);
    assert.match(r.out, /deleted-long-ago\.yml/);
});

// ── T3: cron ↔ handler ───────────────────────────────────────────────────────

test('T3 reverse: a registered cron with no handler is caught', () => {
    const dir = fixture('t3a');
    edit(dir, 'cron/wrangler.toml', (s) => s.replace('crons = [', 'crons = [\n  "7 7 * * *",'));
    const r = runGuard(dir);
    assert.equal(r.status, 1, r.out);
    assert.match(r.out, /7 7 \* \* \*/);
    assert.match(r.out, /什麼都不做/);
});

test('T3 reverse: a handler with no registered cron is caught', () => {
    const dir = fixture('t3b');
    edit(dir, 'cron/src/index.ts', (s) =>
        s.replace("const CRON_MAP: Record<string, CronJob> = {",
            "const CRON_MAP: Record<string, CronJob> = {\n  '7 7 * * *': { workflow: 'scraper-mops.yml' },"));
    const r = runGuard(dir);
    assert.equal(r.status, 1, r.out);
    assert.match(r.out, /永遠不會執行/);
});

test('T3 reverse: dispatching a workflow file that does not exist is caught', () => {
    const dir = fixture('t3c');
    edit(dir, 'cron/src/index.ts', (s) => s.replace("workflow: 'scraper-mops.yml'", "workflow: 'no-such.yml'"));
    const r = runGuard(dir);
    assert.equal(r.status, 1, r.out);
    assert.match(r.out, /no-such\.yml.*不存在/s);
});

// ── labelled time / weekday must match the real cron ─────────────────────────

test('reverse: a CHECK_JOBS time that drifts from the cron is caught', () => {
    const dir = fixture('time');
    edit(dir, '.github/workflows/health-check.yml', (s) =>
        s.replace("{'label': '19:00 MOPS Scraper',                        'workflow_name': 'MOPS Scraper｜公開資訊觀測站爬蟲',                             'hour': 19, 'minute': 0}",
            "{'label': '19:00 MOPS Scraper',                        'workflow_name': 'MOPS Scraper｜公開資訊觀測站爬蟲',                             'hour': 18, 'minute': 0}"));
    const r = runGuard(dir);
    assert.equal(r.status, 1, r.out);
    assert.match(r.out, /信上的時間會誤導人/);
});

test('reverse: Cloudflare weekday numbering mistaken for standard cron is caught', () => {
    // CF: 1=Sun. Marking the Monday cleanup as days [0] (Sunday) is the classic slip.
    const dir = fixture('dow');
    edit(dir, '.github/workflows/health-check.yml', (s) =>
        s.replace("'hour': 3,  'minute': 0,  'days': [1]", "'hour': 3,  'minute': 0,  'days': [0]"));
    const r = runGuard(dir);
    assert.equal(r.status, 1, r.out);
    assert.match(r.out, /day-of-week 是 1=Sun/);
});

// ── the worker must not grow a second copy of the checklist ──────────────────

test('reverse: a second CHECK_JOBS in the worker is caught', () => {
    const dir = fixture('dup');
    edit(dir, 'cron/src/index.ts', (s) => `${s}\nconst CHECK_JOBS = [];\n`);
    const r = runGuard(dir);
    assert.equal(r.status, 1, r.out);
    assert.match(r.out, /刻意只保留/);
});

test('the prose that warns against a second checklist does not itself trip the guard', () => {
    // The worker comment says "不要在這裡再放一份 CHECK_JOBS"; matching that would make the
    // guard permanently red for the wrong reason. It must match a declaration, not a word.
    const idx = fs.readFileSync(path.join(repoRoot, 'cron/src/index.ts'), 'utf-8');
    assert.match(idx, /CHECK_JOBS/, 'the warning comment is still there');
    assert.equal(runGuard().status, 0, 'and the guard is still green');
});

// ── lenient must be able to do something ─────────────────────────────────────

test('reverse: lenient on a shift with nothing later to cover it is caught', () => {
    const dir = fixture('lenient');
    edit(dir, '.github/workflows/health-check.yml', (s) =>
        s.replace("'hour': 21, 'minute': 55, 'days': [1,2,3,4,5]}",
            "'hour': 21, 'minute': 55, 'days': [1,2,3,4,5], 'lenient': True}"));
    const r = runGuard(dir);
    assert.equal(r.status, 1, r.out);
    assert.match(r.out, /永遠不會生效/);
});

test('the existing lenient entry is the early market-sentiment shift and it is covered', () => {
    const yml = fs.readFileSync(path.join(repoRoot, '.github/workflows/health-check.yml'), 'utf-8');
    const lenient = [...yml.matchAll(/\{'label': '([^']+)'[^}]*'lenient':\s*True[^}]*\}/g)].map((m) => m[1]);
    assert.deepEqual(lenient, ['15:55 Market Sentiment Update'],
        'lenient is for shifts that deliberately race ahead of the data, not for flaky ones');
});

// ── the guard brings no dependencies with it ─────────────────────────────────

test('the guard and its package.json stay dependency-free', () => {
    const guard = fs.readFileSync(path.join(repoRoot, GUARD), 'utf-8');
    const imports = [...guard.matchAll(/from\s+'([^']+)'/g)].map((m) => m[1]);
    assert.ok(imports.every((i) => i.startsWith('node:') || i.startsWith('.')),
        `guard must only use Node builtins, found: ${imports.join(', ')}`);
    const pkg = JSON.parse(fs.readFileSync(path.join(repoRoot, 'package.json'), 'utf-8'));
    assert.equal(pkg.dependencies, undefined, 'this repo runs on uv; no npm dependencies');
    assert.equal(pkg.devDependencies, undefined, 'node --test is built in — nothing to install');
});
