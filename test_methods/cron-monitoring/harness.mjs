/**
 * Harness for the WS-06-A cron/monitoring suite (stock-data side).
 *
 * Two things it provides, both fully offline:
 *
 *  A. `stepBody(workflow, stepName)` — pulls a step's REAL `run:` body out of the REAL
 *     workflow file. No YAML library: this repo has no npm dependencies and is not going
 *     to grow any for a guard test, so the extractor walks the `- name:` / `run: |` block
 *     by indentation and asserts loudly if the shape it expects is gone.
 *
 *  B. `runStepBody(...)` — executes that body under the shell GitHub would use, with
 *     `git` replaced by a recording stub, in a scratch directory that is **not** a git
 *     repository.
 *
 * Isolation, spelled out because "we stubbed it" is not the same as "it cannot escape":
 *   · PATH is prefixed with the stub dir, so `git` resolves to the stub.
 *   · cwd is a fresh temp dir outside any working tree, so even a `git` that somehow
 *     reached the real binary would fail with "not a git repository" rather than touch
 *     this checkout.
 *   · `assertStubbed()` fails the test when the ledger is empty — a stub that silently
 *     did not get used must never be mistaken for a passing test.
 *   · No network, no notifications, no wrangler: the bodies under test only call git.
 */
import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const suiteDir = path.dirname(fileURLToPath(import.meta.url));
export const repoRoot = path.resolve(suiteDir, '../..');
export const workflowPath = (name) => path.join(repoRoot, '.github/workflows', name);

let counter = 0;
export function scratch(label) {
    return fs.realpathSync(
        fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), `cm-${label}-${counter++}-`)),
    );
}

/**
 * Line ranges of each job in a workflow, so a step can be addressed by (job, step).
 *
 * `weekly-shareholder-update.yml` has two jobs and a step called `Commit result` in each.
 * An extractor that takes the first match tests one of them and looks like it tested both —
 * which is precisely the silent-under-coverage failure this suite exists to catch, so
 * `locateStep` refuses to guess (see below).
 */
function jobRanges(lines) {
    const jobsAt = lines.findIndex((l) => /^jobs:\s*$/.test(l));
    assert.ok(jobsAt >= 0, 'no jobs: block');
    const heads = [];
    for (let i = jobsAt + 1; i < lines.length; i++) {
        const m = lines[i].match(/^ {2}([A-Za-z0-9_-]+):\s*$/);
        if (m) heads.push({ name: m[1], from: i });
    }
    return heads.map((h, i) => ({ ...h, to: heads[i + 1]?.from ?? lines.length }));
}

/**
 * Index of a step's `- name:` line, addressed by name and optionally by job.
 *
 * Throws when the name is ambiguous and no job was given, rather than returning the first
 * hit. That turns "the test quietly covered half of what it claimed" into a hard failure.
 */
function locateStep(lines, workflow, stepName, job) {
    const isHead = (l) => l.trim() === `- name: ${stepName}`;
    const ranges = jobRanges(lines);
    const hits = [];
    for (let i = 0; i < lines.length; i++) {
        if (!isHead(lines[i])) continue;
        const owner = ranges.find((r) => i >= r.from && i < r.to);
        hits.push({ index: i, job: owner?.name });
    }
    assert.ok(hits.length > 0, `${workflow}: step "${stepName}" not found`);
    if (job !== undefined) {
        const found = hits.filter((h) => h.job === job);
        assert.equal(found.length, 1,
            `${workflow}: expected exactly one "${stepName}" in job "${job}", found ${found.length}`);
        return found[0].index;
    }
    assert.equal(hits.length, 1,
        `${workflow}: step "${stepName}" appears in ${hits.length} jobs `
        + `(${hits.map((h) => h.job).join(', ')}) — pass { job } to say which one you mean`);
    return hits[0].index;
}

/** Every (job, step) pair whose name is `stepName` — used to prove full coverage. */
export function stepOccurrences(workflow, stepName) {
    const lines = fs.readFileSync(workflowPath(workflow), 'utf-8').split('\n');
    const ranges = jobRanges(lines);
    const out = [];
    for (let i = 0; i < lines.length; i++) {
        if (lines[i].trim() !== `- name: ${stepName}`) continue;
        out.push(ranges.find((r) => i >= r.from && i < r.to)?.name);
    }
    return out;
}

/**
 * The `run:` body of one named step, verbatim from the workflow file.
 *
 * Deliberately strict: if the step name or the `run: |` block is not found — or if the name
 * is ambiguous and no `job` was given — the test fails instead of silently exercising the
 * wrong thing, or only part of what it claims to cover.
 */
export function stepBody(workflow, stepName, { job } = {}) {
    const lines = fs.readFileSync(workflowPath(workflow), 'utf-8').split('\n');
    const head = locateStep(lines, workflow, stepName, job);
    const stepIndent = lines[head].indexOf('-');

    let runAt = -1;
    for (let i = head + 1; i < lines.length; i++) {
        const l = lines[i];
        if (l.trim() === '' ) continue;
        const ind = l.length - l.trimStart().length;
        if (ind <= stepIndent) break;                  // next step / end of list
        if (/^\s*run:\s*\|\s*$/.test(l)) { runAt = i; break; }
    }
    assert.ok(runAt >= 0, `${workflow}: step "${stepName}" has no "run: |" block`);

    const bodyIndent = lines[runAt + 1].length - lines[runAt + 1].trimStart().length;
    const out = [];
    for (let i = runAt + 1; i < lines.length; i++) {
        const l = lines[i];
        if (l.trim() === '') { out.push(''); continue; }
        const ind = l.length - l.trimStart().length;
        if (ind < bodyIndent) break;
        out.push(l.slice(bodyIndent));
    }
    while (out.length && out[out.length - 1] === '') out.pop();
    assert.ok(out.length > 0, `${workflow}: step "${stepName}" body came out empty`);
    return out.join('\n');
}

/**
 * The shell GitHub resolves for a step with no `shell:` key on Linux: `bash -e`.
 *
 * Every step this suite touches is that case; `assertDefaultShell` proves it rather than
 * assuming it, so a future `shell: bash` (which adds pipefail) cannot change the meaning
 * of these tests without the tests noticing.
 */
export const DEFAULT_SHELL_ARGV = ['bash', '-e'];

export function assertDefaultShell(workflow, stepName, { job } = {}) {
    const lines = fs.readFileSync(workflowPath(workflow), 'utf-8').split('\n');
    const head = locateStep(lines, workflow, stepName, job);
    const stepIndent = lines[head].indexOf('-');
    for (let i = head + 1; i < lines.length; i++) {
        const l = lines[i];
        if (l.trim() === '') continue;
        const ind = l.length - l.trimStart().length;
        if (ind <= stepIndent) break;
        assert.ok(!/^\s*shell:/.test(l), `${workflow}/${stepName}: unexpected "shell:" — retune the suite`);
    }
}

/**
 * Recording `git` stub.
 *
 * `failPushes: n` makes the first n `git push` calls fail, so a test can ask for
 * "all three attempts fail", "succeeds first time", or "succeeds on the second retry"
 * without any real git ever running.
 */
function writeGitStub(bin, ledger, { failPushes = 0, failPulls = 0 } = {}) {
    const src = [
        '#!/bin/sh',
        `echo "$@" >> ${JSON.stringify(ledger)}`,
        'case "$1" in',
        '  push)',
        `    n=$(grep -c "^push" ${JSON.stringify(ledger)})`,
        `    if [ "$n" -le ${failPushes} ]; then echo "stub: remote rejected push" >&2; exit 1; fi`,
        '    exit 0 ;;',
        '  pull)',
        `    n=$(grep -c "^pull" ${JSON.stringify(ledger)})`,
        `    if [ "$n" -le ${failPulls} ]; then echo "stub: pull failed" >&2; exit 1; fi`,
        '    exit 0 ;;',
        '  diff)',
        // `git diff --cached --quiet` must report "there ARE staged changes" (exit 1) so the
        // body proceeds to commit+push; every other diff form returns nothing, successfully.
        '    case "$*" in *--cached*) exit 1 ;; *) exit 0 ;; esac ;;',
        '  status) echo " M src/data/example.json" ; exit 0 ;;',
        '  *) exit 0 ;;',
        'esac',
    ].join('\n');
    fs.writeFileSync(path.join(bin, 'git'), src);
    fs.chmodSync(path.join(bin, 'git'), 0o755);

    // The real bodies back off with `sleep $((i * 5))`, i.e. 15s of wall clock per fully
    // failed loop. The back-off is not what is under test and a suite that takes minutes
    // stops being run, so `sleep` records its argument and returns immediately.
    // The argument is still asserted on, so the back-off itself cannot silently disappear.
    fs.writeFileSync(path.join(bin, 'sleep'), [
        '#!/bin/sh',
        `echo "sleep $*" >> ${JSON.stringify(ledger)}`,
        'exit 0',
    ].join('\n'));
    fs.chmodSync(path.join(bin, 'sleep'), 0o755);
}

/**
 * Run a shell body with git stubbed.
 * @param {string} body            the run body (already free of ${{ }} expressions)
 * @param {object} opts
 * @param {number} opts.failPushes how many leading `git push` calls should fail
 * @param {number} opts.failPulls  how many leading `git pull` calls should fail
 * @param {string[]} opts.argv     shell argv override (reverse verification only)
 */
export function runStepBody(body, { failPushes = 0, failPulls = 0, argv = DEFAULT_SHELL_ARGV } = {}) {
    const dir = scratch('step');
    const bin = path.join(dir, 'bin');
    fs.mkdirSync(bin, { recursive: true });
    const ledger = path.join(dir, 'git-calls.txt');
    fs.writeFileSync(ledger, '');
    writeGitStub(bin, ledger, { failPushes, failPulls });

    const script = path.join(dir, 'step.sh');
    fs.writeFileSync(script, body);

    const res = spawnSync(argv[0], [...argv.slice(1), script], {
        // cwd is NOT a git repository: a real git would fail here rather than reach the checkout.
        cwd: dir,
        encoding: 'utf-8',
        env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, GIT_CONFIG_GLOBAL: '/dev/null' },
    });

    const calls = fs.readFileSync(ledger, 'utf-8').split('\n').filter(Boolean);
    return {
        status: res.status,
        stdout: res.stdout ?? '',
        stderr: res.stderr ?? '',
        calls,
        pushes: calls.filter((c) => c.startsWith('push')).length,
        sleeps: calls.filter((c) => c.startsWith('sleep ')).length,
        dir,
    };
}

/** A run that never reached the stub proves nothing — say so loudly. */
export function assertStubbed(result) {
    assert.ok(
        result.calls.length > 0,
        `the git stub was never invoked — the body did not run as expected:\n${result.stderr}`,
    );
}

/** Replace GitHub expressions the way GitHub does (before the shell sees the body). */
export function interpolate(body, values = {}) {
    return body.replace(/\$\{\{\s*([^}]*?)\s*\}\}/g, (_, expr) => {
        const key = expr.trim();
        return Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : '';
    });
}

// ── the real inline Python from health-check.yml ─────────────────────────────
//
// No YAML library (this repo has no dependencies): the heredoc is found in the raw file
// text, which is also exactly how it reaches `python3` at runtime.

/** The whole inline Python program, dedented as the shell would see it. */
export function inlinePython(workflow = 'health-check.yml') {
    const src = fs.readFileSync(workflowPath(workflow), 'utf-8');
    const m = src.match(/python3 << 'PYEOF'\n([\s\S]*?)\n\s*PYEOF/);
    assert.ok(m, `${workflow}: no inline python heredoc found`);
    const lines = m[1].split('\n');
    const indent = Math.min(...lines.filter((l) => l.trim()).map((l) => l.length - l.trimStart().length));
    return lines.map((l) => (l.length >= indent ? l.slice(indent) : l)).join('\n');
}

/**
 * A contiguous region of that program, delimited by two source markers.
 * Fails if either marker moved, so a refactor cannot quietly empty the test.
 */
export function pythonRegion(startsWith, endsBefore, workflow = 'health-check.yml') {
    const lines = inlinePython(workflow).split('\n');
    const from = lines.findIndex((l) => l.includes(startsWith));
    assert.ok(from >= 0, `region start not found: ${startsWith}`);
    const to = lines.findIndex((l, i) => i > from && l.includes(endsBefore));
    assert.ok(to > from, `region end not found: ${endsBefore}`);
    const region = lines.slice(from, to).join('\n');
    assert.ok(region.trim().length > 0, 'extracted region is empty');
    return region;
}

/** Run a python source string with a preamble/epilogue, returning parsed JSON from stdout. */
export function runPython(source, { preamble = '', epilogue = '' } = {}) {
    const dir = scratch('py');
    const file = path.join(dir, 'region.py');
    fs.writeFileSync(file, `${preamble}\n${source}\n${epilogue}\n`);
    const r = spawnSync('python3', [file], { encoding: 'utf-8', cwd: dir });
    assert.equal(r.status, 0, `python failed:\n${r.stderr}`);
    return JSON.parse(r.stdout.trim().split('\n').pop());
}

/**
 * Execute the REAL verdict region from health-check.yml against fixture runs.
 *
 * The region is everything from `def job_start_utc(job):` up to the lenient block: the real
 * `job_window`, real `fmt_tw_time`/`fmt_duration`, real `run_order`, real `max(...)`
 * selection, real status mapping, real `superseded`, and the real display fields. Only
 * CHECK_JOBS, the clock and the run list are injected.
 *
 * WS-06-B1 / V-B2: the previous version lifted only `run_order` and rebuilt the rest in the
 * test, so mutating the production `status = 'failed'` branch to `'ok'` left the suite
 * green. Nothing about the verdict may be reimplemented here.
 *
 * @param {string} mutate  optional "from|||to" literal substitution, scratch-only
 */
export function runVerdictRegion(runs, jobs, mutate = null) {
    let region = pythonRegion('def job_start_utc(job):', '早班沒搶到不算異常');
    if (mutate) {
        const [from, to] = mutate.split('|||');
        assert.ok(region.includes(from), `mutation source not found in region: ${from}`);
        region = region.replace(from, to);
    }
    const preamble = [
        'import json',
        'from datetime import datetime, timedelta, timezone',
        'now = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)',   // TW 23:00
        'taiwan_now = now + timedelta(hours=8)',
        'tw_day = int(taiwan_now.strftime("%w"))',
        `CHECK_JOBS = json.loads(r"""${JSON.stringify(jobs)}""")`,
        `workflow_runs = json.loads(r"""${JSON.stringify(runs)}""")`,
    ].join('\n');
    const epilogue = [
        'print(json.dumps([{',
        '    "label": r["label"], "status": r["status"], "run_id": r["run_id"],',
        '    "run_time": r["run_time"], "duration": r["duration"], "url": r["url"],',
        '} for r in results]))',
    ].join('\n');
    return runPython(region, { preamble, epilogue });
}
