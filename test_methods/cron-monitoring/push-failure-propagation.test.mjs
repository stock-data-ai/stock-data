/**
 * WS-06-A / W6-F3: a commit step whose push never lands must fail its job.
 *
 * The bug, in one line:
 *
 *     for i in 1 2 3; do
 *       git pull --rebase -X ours origin main && git push && break
 *       sleep $((i * 5))
 *     done
 *
 * When all three attempts fail the loop just ends. `set -e` does **not** trip, because a
 * command on the left of `&&` is exempt, so the last thing the step ran was `sleep` —
 * exit 0. The job went green while the data it had just generated never reached the repo,
 * so GitHub Pages (the static API the app reads) silently served yesterday's numbers and
 * the 23:00 health check reported ✅.
 *
 * stock_map had already found and fixed exactly this in update-heatmap.yml, with a comment
 * spelling out the failure mode. The fix was never carried across to this repo — these
 * tests are what stops it drifting back.
 *
 * Every body here is the REAL `run:` body from the REAL workflow file, executed under the
 * shell GitHub resolves (`bash -e`), with `git` replaced by a recording stub in a scratch
 * directory that is not a git repository. Nothing is pushed, pulled, rebased or notified.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
    stepBody, runStepBody, assertStubbed, interpolate,
    assertDefaultShell, stepOccurrences, repoRoot,
} from './harness.mjs';

/**
 * Every commit-and-push step that a schedule can reach.
 *
 * Enumerated from the workflow files rather than copied from the WS-06 report: the report
 * said 10 loops, the source actually has 12 (one of which — update-stock-tags.yml — was
 * already correct and is covered below as the reference implementation).
 *
 * `weekly-shareholder-update.yml` needs `job`: it has a step called `Commit result` in each
 * of its two jobs. The first version of this table listed it once, so `compute-big-holders`
 * silently went untested while the table looked complete — caught in review, and now
 * prevented two ways: `stepBody` refuses an ambiguous name without a `job`, and
 * `every fixed push loop is exercised by this table` below enumerates the loops from source
 * and fails if any is missing from here.
 */
const STEPS = [
    { workflow: 'daily-update.yml', step: '提交變更', inputs: { 'inputs.date': '', 'inputs.force': 'true' } },
    { workflow: 'etf-active-daily.yml', step: 'Commit & push updated ETF data' },
    { workflow: 'etf-active-daily.yml', step: 'Commit & push reverse index' },
    { workflow: 'foreign-shares-backfill.yml', step: 'Commit' },
    { workflow: 'margin-trading-update.yml', step: 'Commit margin trading data' },
    { workflow: 'market-sentiment.yml', step: 'Commit sentiment data' },
    { workflow: 'update-us-financials.yml', step: 'Commit and push' },
    { workflow: 'weekly-dividend-update.yml', step: 'Commit updated data' },
    { workflow: 'weekly-shareholder-update.yml', step: 'Commit result', job: 'fetch-tdcc' },
    { workflow: 'weekly-shareholder-update.yml', step: 'Commit result', job: 'compute-big-holders' },
    {
        workflow: '_reusable-data-job.yml',
        step: 'Commit and push all results',
        inputs: {
            'inputs.commands_json': '[{"id":"financials_update","cli":"financials-update","label":"財報"}]',
            'inputs.rerun_round': '0',
            'inputs.commit_message': 'test commit message',
            'inputs.summary_title': '財務報表更新',
        },
    },
];

const bodyOf = ({ workflow, step, job, inputs }) =>
    interpolate(stepBody(workflow, step, { job }), inputs ?? {});

for (const target of STEPS) {
    const id = `${target.workflow}${target.job ? ` [${target.job}]` : ''} / ${target.step}`;

    test(`${id}: uses GitHub's default shell (bash -e)`, () => {
        assertDefaultShell(target.workflow, target.step, { job: target.job });
    });

    test(`${id}: three failed pushes fail the step`, () => {
        const r = runStepBody(bodyOf(target), { failPushes: 3 });
        assertStubbed(r);
        assert.notEqual(r.status, 0, `step exited 0 despite every push failing:\n${r.stdout}\n${r.stderr}`);
        assert.equal(r.pushes, 3, 'should have attempted exactly three pushes');
        assert.match(r.stdout + r.stderr, /::error::/, 'should emit a GitHub error annotation');
    });

    test(`${id}: a push that succeeds first time exits 0 and does not retry`, () => {
        const r = runStepBody(bodyOf(target), { failPushes: 0 });
        assertStubbed(r);
        assert.equal(r.status, 0, `step failed on the happy path:\n${r.stdout}\n${r.stderr}`);
        assert.equal(r.pushes, 1, 'a successful push must stop the retry loop');
        assert.doesNotMatch(r.stdout + r.stderr, /::error::/, 'happy path must not emit an error annotation');
    });

    test(`${id}: succeeds on the second attempt and then stops`, () => {
        const r = runStepBody(bodyOf(target), { failPushes: 1 });
        assertStubbed(r);
        assert.equal(r.status, 0, `retry should have recovered:\n${r.stdout}\n${r.stderr}`);
        assert.equal(r.pushes, 2, 'must stop as soon as a push succeeds, not run the full three');
    });

    test(`${id}: preserves its existing pull/rebase arguments and adds no force push`, () => {
        const body = bodyOf(target);
        assert.doesNotMatch(body, /--force|\+HEAD|-f\s+origin/, 'no force push may be introduced');
        const r = runStepBody(body, { failPushes: 0 });
        const pull = r.calls.find((c) => c.startsWith('pull'));
        assert.ok(pull, 'the step should still pull before pushing');
        assert.match(pull, /--rebase/, 'rebase must be preserved');
    });
}

/**
 * update-stock-tags.yml already handled this correctly (loop then `exit 1`) and is the
 * shape the others were brought in line with. It is here so a regression there is caught
 * too — it is the reference, not an exception.
 */
test('update-stock-tags.yml (already-correct reference): three failed pushes fail the step', () => {
    const body = interpolate(stepBody('update-stock-tags.yml', 'Commit and push if changed'));
    const r = runStepBody(body, { failPushes: 3 });
    assertStubbed(r);
    assert.notEqual(r.status, 0);
    assert.equal(r.pushes, 3);
});

/**
 * Reverse verification: rebuild the OLD shape in a scratch copy and prove the defect
 * reproduces. Without this, "the tests pass" would only mean the tests agree with today's
 * code, not that they can detect the bug coming back.
 *
 * The mutation lives only in the string handed to the shell; no workflow file is touched.
 */
test('reverse: the pre-fix loop shape exits 0 after three failed pushes', () => {
    const old = [
        'git config user.name "github-actions[bot]"',
        'git add src/data/',
        'for i in 1 2 3; do',
        '  git pull --rebase -X ours origin main && git push && break',
        '  sleep 0',
        'done',
    ].join('\n');
    const r = runStepBody(old, { failPushes: 3 });
    assertStubbed(r);
    assert.equal(r.status, 0, 'the old shape is supposed to be silently green — that was the bug');
    assert.equal(r.pushes, 3, 'it did try three times; it just never reported the failure');
});

/**
 * The reason the old shape survived `set -e`, pinned as an executable fact rather than a
 * claim in a comment: a failing command on the left of `&&` does not trip errexit, so the
 * loop body's last executed command is `sleep`, and the script ends 0.
 */
test('reverse: set -e does not trip on `false && cmd` (why the bug was invisible)', () => {
    const r = runStepBody('for i in 1 2 3; do\n  false && echo pushed\n  sleep 0\ndone\necho END\n');
    assert.equal(r.status, 0, 'errexit must not fire on the left-hand side of &&');
    assert.match(r.stdout, /END/, 'the loop must have run to completion');
    assert.equal(r.pushes, 0, 'this probe intentionally invokes no git at all');
    assert.equal(r.sleeps, 3, 'all three iterations ran; the last command was the back-off sleep');
});

/**
 * Coverage completeness: every fixed loop must actually be exercised by STEPS.
 *
 * The structural sweep below proves the *fix* is everywhere. This proves the *tests* are.
 * They are different claims, and conflating them is how `compute-big-holders` ended up
 * unexercised while the table read as complete: the loop had the fix, the sweep was green,
 * and the behavioural tests silently ran the other job's step twice.
 *
 * Loops are enumerated from source and matched to STEPS by (workflow, job), so adding a
 * push loop without adding a test entry fails here.
 */
test('every fixed push loop is exercised by this table', async () => {
    const fs = await import('node:fs');
    const path = await import('node:path');
    const dir = path.join(repoRoot, '.github/workflows');

    /** (workflow, job) pairs that contain a push retry loop, straight from the files. */
    const loops = [];
    for (const file of fs.readdirSync(dir).filter((f) => f.endsWith('.yml'))) {
        const lines = fs.readFileSync(path.join(dir, file), 'utf-8').split('\n');
        const jobsAt = lines.findIndex((l) => /^jobs:\s*$/.test(l));
        const heads = [];
        for (let i = jobsAt + 1; i < lines.length; i++) {
            const m = lines[i].match(/^ {2}([A-Za-z0-9_-]+):\s*$/);
            if (m) heads.push({ name: m[1], from: i });
        }
        const ranges = heads.map((h, i) => ({ ...h, to: heads[i + 1]?.from ?? lines.length }));
        for (let i = 0; i < lines.length; i++) {
            if (!/^\s*for i in 1 2 3; do\s*$/.test(lines[i])) continue;
            const indent = lines[i].length - lines[i].trimStart().length;
            let end = -1;
            for (let j = i + 1; j < Math.min(i + 40, lines.length); j++) {
                if (/^\s*done\s*$/.test(lines[j]) && lines[j].length - lines[j].trimStart().length === indent) {
                    end = j; break;
                }
            }
            if (end < 0 || !lines.slice(i, end + 1).join('\n').includes('git push')) continue;
            loops.push(`${file} [${ranges.find((r) => i >= r.from && i < r.to)?.name}]`);
        }
    }

    // update-stock-tags.yml is covered by its own reference test, not by STEPS.
    const covered = new Set(STEPS.map((s) => {
        const jobs = stepOccurrences(s.workflow, s.step);
        return `${s.workflow} [${s.job ?? jobs[0]}]`;
    }));
    covered.add('update-stock-tags.yml [update]');

    const uncovered = loops.filter((l) => !covered.has(l));
    assert.deepEqual(uncovered, [],
        `these push loops have no behavioural test: ${uncovered.join(', ')}`);
    assert.equal(loops.length, 12, 'twelve push loops in total — eleven fixed, one already correct');
    assert.equal(STEPS.length, 11, 'STEPS must list all eleven fixed loops');
});

test('an ambiguous step name without a job is rejected rather than silently resolved', () => {
    // The exact gap found in review: two jobs, one step name. Asking without a job must fail.
    assert.equal(stepOccurrences('weekly-shareholder-update.yml', 'Commit result').length, 2);
    assert.throws(() => stepBody('weekly-shareholder-update.yml', 'Commit result'),
        /appears in 2 jobs.*pass \{ job \}/s);
    // And each job resolves to a genuinely different body.
    const a = stepBody('weekly-shareholder-update.yml', 'Commit result', { job: 'fetch-tdcc' });
    const b = stepBody('weekly-shareholder-update.yml', 'Commit result', { job: 'compute-big-holders' });
    assert.notEqual(a, b, 'the two jobs must not resolve to the same body');
    assert.match(a, /company-financials/);
    assert.match(b, /weekly_big_holders\.json/);
});

/** The fix must be present in every scheduled push loop — a structural sweep, not a count. */
test('no scheduled push loop is left without failure propagation', async () => {
    const fs = await import('node:fs');
    const path = await import('node:path');
    const dir = path.join(repoRoot, '.github/workflows');
    const offenders = [];
    for (const file of fs.readdirSync(dir).filter((f) => f.endsWith('.yml'))) {
        const lines = fs.readFileSync(path.join(dir, file), 'utf-8').split('\n');
        for (let i = 0; i < lines.length; i++) {
            if (!/^\s*for i in 1 2 3; do\s*$/.test(lines[i])) continue;
            const indent = lines[i].length - lines[i].trimStart().length;
            let end = -1;
            for (let j = i + 1; j < Math.min(i + 40, lines.length); j++) {
                if (/^\s*done\s*$/.test(lines[j]) && lines[j].length - lines[j].trimStart().length === indent) {
                    end = j; break;
                }
            }
            if (end < 0) continue;
            const region = lines.slice(i, end + 20).join('\n');
            if (!region.includes('git push')) continue;
            if (!/exit 1/.test(region)) offenders.push(`${file}:${i + 1}`);
        }
    }
    assert.deepEqual(offenders, [], `these push loops still swallow failure: ${offenders.join(', ')}`);
});
