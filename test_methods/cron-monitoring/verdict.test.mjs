/**
 * WS-06-B / W6-F7: a shift's verdict must be the LAST thing that happened in its window.
 *
 * The old rule scanned the window for any successful run and stopped there:
 *
 *     success_run = next(r for r in runs if r['conclusion'] == 'success')
 *     if success_run: status = 'ok'
 *
 * So "succeeded, then failed" reported ✅. In this repo the shape that hits it is built in:
 * `_reusable-data-job.yml` chains itself up to MAX_RERUN_ROUNDS, and every round except the
 * last exits 0 — round 0 succeeds, the final round exits 1, and the daily email said the
 * weekly job was fine.
 *
 * The displayed run time moved to the deciding run too. Reporting "失敗" next to the start
 * time of a different, successful run is its own way of being wrong.
 *
 * ── how this file is built, and why it was rebuilt ──────────────────────────
 *
 * WS-06-B1 / V-B2: the first version lifted only `run_order` out of the workflow and
 * reimplemented window filtering, selection, status mapping and `superseded` in the test's
 * own epilogue. Independent review mutated the production `status = 'failed'` branch to
 * `'ok'` and this suite stayed green — it was grading its own copy of the algorithm.
 *
 * Now the whole region runs: real `job_window`, real `fmt_tw_time`/`fmt_duration`, real
 * `run_order`, real `max(...)`, real status mapping, real `superseded`, real display fields.
 * Only CHECK_JOBS, the clock and the run list are injected. `MUTATIONS` at the bottom proves
 * it: every mutation of a production branch must make the shared case matrix fail.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runVerdictRegion, inlinePython } from './harness.mjs';

const WF = 'Daily Update｜每日更新（市值＋三大法人）';

/** The two real Daily Update shifts: TW 19:30 / 21:05 → UTC 11:30 / 13:05. */
const JOBS = [
    { label: '19:30', workflow_name: WF, hour: 19, minute: 30 },
    { label: '21:05', workflow_name: WF, hour: 21, minute: 5 },
];

let seq = 0;
function run(started, {
    status = 'completed', conclusion = 'success', attempt = 1, id = ++seq,
    name = WF, created = null, noStartedAt = false, updated = null,
} = {}) {
    const r = {
        id,
        name,
        status,
        conclusion,
        html_url: `https://github.com/x/y/actions/runs/${id}`,
        created_at: `2026-09-09T${created ?? started}:00Z`,
        run_started_at: `2026-09-09T${started}:00Z`,
        updated_at: `2026-09-09T${updated ?? started}:00Z`,
        run_attempt: attempt,
    };
    if (noStartedAt) delete r.run_started_at;
    return r;
}
const ok = (t, o) => run(t, { ...o, status: 'completed', conclusion: 'success' });
const bad = (t, o) => run(t, { ...o, status: 'completed', conclusion: 'failure' });
const busy = (t, o) => run(t, { ...o, status: 'in_progress', conclusion: null });

const verdicts = (runs, mutate = null) => runVerdictRegion(runs, JOBS, mutate);
const shape = (r) => ({ status: r.status, run_id: r.run_id, superseded: r.label.includes('稍早曾成功') });

/** TW clock time of a run, derived from its own fields — the display must match this. */
function expectedRunTime(runs, id) {
    if (id === null) return '—';
    const r = runs.find((x) => x.id === id);
    assert.ok(r, `fixture has no run ${id}`);
    const iso = r.run_started_at ?? r.created_at;
    const tw = new Date(Date.parse(iso) + 8 * 3600_000);
    return `${String(tw.getUTCHours()).padStart(2, '0')}:${String(tw.getUTCMinutes()).padStart(2, '0')}`;
}

/**
 * Assert one shift, including that the displayed time and URL belong to the deciding run.
 * Display is part of the matrix on purpose: a verdict shown next to another run's timestamp
 * is its own defect, and keeping it out of the matrix let a display mutation slip past.
 */
function assertShift(got, expected, runs, where) {
    assert.deepEqual(shape(got), expected, where);
    assert.equal(got.run_time, expectedRunTime(runs, expected.run_id),
        `${where} — displayed time must belong to the deciding run`);
    assert.equal(got.url, expected.run_id === null ? null
        : `https://github.com/x/y/actions/runs/${expected.run_id}`,
        `${where} — displayed URL must belong to the deciding run`);
}

/**
 * Shared case matrix — used against production and against every mutant.
 * `expect` is per shift: status, which run decided it, and whether the "earlier success"
 * note is present.
 */
const CASES = [
    {
        name: 'success then failure in one window is a failure',
        runs: () => [ok('11:40', { id: 1 }), bad('12:30', { id: 2 })],
        expect: [{ status: 'failed', run_id: 2, superseded: true }, { status: 'missing', run_id: null, superseded: false }],
    },
    {
        name: 'failure then success in one window is a success',
        runs: () => [bad('11:40', { id: 1 }), ok('12:30', { id: 2 })],
        expect: [{ status: 'ok', run_id: 2, superseded: false }, { status: 'missing', run_id: null, superseded: false }],
    },
    {
        name: 'success then still-running is running, not ok',
        runs: () => [ok('11:40', { id: 1 }), busy('12:30', { id: 2 })],
        expect: [{ status: 'running', run_id: 2, superseded: true }, { status: 'missing', run_id: null, superseded: false }],
    },
    {
        name: 'no runs at all is missing',
        runs: () => [],
        expect: [{ status: 'missing', run_id: null, superseded: false }, { status: 'missing', run_id: null, superseded: false }],
    },
    {
        name: 'a single successful run is just ok',
        runs: () => [ok('11:40', { id: 1 })],
        expect: [{ status: 'ok', run_id: 1, superseded: false }, { status: 'missing', run_id: null, superseded: false }],
    },
    {
        name: 'reversed input order gives the same answer',
        runs: () => [bad('12:30', { id: 2 }), ok('11:40', { id: 1 })],
        expect: [{ status: 'failed', run_id: 2, superseded: true }, { status: 'missing', run_id: null, superseded: false }],
    },
    {
        name: 'a later rerun attempt at the same start time wins',
        runs: () => [bad('11:40', { id: 7, attempt: 1 }), ok('11:40', { id: 7, attempt: 2 })],
        expect: [{ status: 'ok', run_id: 7, superseded: false }, { status: 'missing', run_id: null, superseded: false }],
    },
    {
        name: 'equal start time and attempt fall back to id',
        runs: () => [ok('11:40', { id: 10 }), bad('11:40', { id: 11 })],
        expect: [{ status: 'failed', run_id: 11, superseded: true }, { status: 'missing', run_id: null, superseded: false }],
    },
    {
        name: 'run_started_at wins over created_at when they disagree',
        runs: () => [ok('12:50', { id: 1, created: '11:40' }), bad('12:00', { id: 2 })],
        expect: [{ status: 'ok', run_id: 1, superseded: false }, { status: 'missing', run_id: null, superseded: false }],
    },
    {
        name: 'a run with no run_started_at falls back to created_at',
        runs: () => [ok('11:40', { id: 1 }), bad('12:30', { id: 2, noStartedAt: true })],
        expect: [{ status: 'failed', run_id: 2, superseded: true }, { status: 'missing', run_id: null, superseded: false }],
    },
    {
        name: 'the backup shift owns runs after its start',
        runs: () => [ok('11:40', { id: 1 }), bad('13:30', { id: 2 })],
        expect: [{ status: 'ok', run_id: 1, superseded: false }, { status: 'failed', run_id: 2, superseded: false }],
    },
    {
        name: 'a run belonging to another workflow is ignored',
        runs: () => [ok('11:40', { id: 1 }), bad('11:50', { id: 2, name: 'Other' })],
        expect: [{ status: 'ok', run_id: 1, superseded: false }, { status: 'missing', run_id: null, superseded: false }],
    },
    {
        name: 'the chained-rerun shape: early rounds pass, the last exhausts',
        runs: () => [ok('11:35', { id: 100 }), ok('12:00', { id: 101 }), bad('12:45', { id: 102 })],
        expect: [{ status: 'failed', run_id: 102, superseded: true }, { status: 'missing', run_id: null, superseded: false }],
    },
];

function checkMatrix(runner) {
    for (const c of CASES) {
        const runs = c.runs();
        const got = runner(runs);
        c.expect.forEach((e, i) => assertShift(got[i], e, runs, `${c.name} — shift ${i}`));
    }
}

for (const c of CASES) {
    test(c.name, () => {
        const runs = c.runs();
        const got = verdicts(runs);
        c.expect.forEach((e, i) => assertShift(got[i], e, runs, `shift ${i}`));
    });
}

// ── the displayed time and URL must belong to the deciding run ───────────────

test('the displayed run time and URL come from the deciding run, not an earlier success', () => {
    const got = verdicts([ok('11:40', { id: 1 }), bad('12:30', { id: 2, updated: '12:35' })]);
    assert.equal(got[0].run_id, 2);
    assert.equal(got[0].url, 'https://github.com/x/y/actions/runs/2');
    assert.equal(got[0].run_time, '20:30', 'TW time of the deciding run (UTC 12:30 + 8h)');
    assert.equal(got[0].duration, '5m00s', 'duration measured on the deciding run');
});

test('a missing shift reports no time at all', () => {
    const got = verdicts([]);
    assert.equal(got[0].run_time, '—');
    assert.equal(got[0].duration, '—');
    assert.equal(got[0].url, null);
});

// ── mutation testing: prove the matrix grades production ─────────────────────

const MUTATIONS = [
    { name: 'failure branch returns ok', mutate: "status = 'failed'|||status = 'ok'" },
    { name: 'running branch returns ok', mutate: "status = 'running'|||status = 'ok'" },
    {
        name: 'earliest run decides instead of the latest',
        mutate: 'run = max(runs, key=run_order) if runs else None'
            + '|||run = min(runs, key=run_order) if runs else None',
    },
    {
        name: 'ordering falls back to the input array',
        mutate: 'run = max(runs, key=run_order) if runs else None'
            + '|||run = runs[-1] if runs else None',
    },
    {
        name: 'the displayed time is taken from another run',
        mutate: "started_at = (run or {}).get('run_started_at') or (run or {}).get('created_at')"
            + "|||started_at = (runs[0] if runs else {}).get('run_started_at')",
    },
    {
        name: 'the earlier-success note is never added',
        mutate: "superseded = status != 'ok' and any(|||superseded = False and any(",
    },
];

for (const m of MUTATIONS) {
    test(`mutation caught: ${m.name}`, () => {
        assert.throws(
            () => checkMatrix((runs) => verdicts(runs, m.mutate)),
            assert.AssertionError,
            `the case matrix did NOT catch "${m.name}" — it is grading a copy, not production`,
        );
    });
}

test('the unmutated region passes the same matrix', () => {
    checkMatrix((runs) => verdicts(runs));
});

test('the pre-fix first-success-wins selection is gone from the source', () => {
    const src = inlinePython();
    assert.doesNotMatch(src, /success_run = next\(/, 'first-success-wins must not come back');
    assert.match(src, /run = max\(runs, key=run_order\) if runs else None/);
});
