# GitLab monitor

`gitlab-monitor` waits for a specific GitLab event. Agents start a watch when a task needs one.
The command prints one JSON event and exits. `--follow` keeps the watch active.

## Install

```sh
python -m pip install .
```

The command uses `glab` authentication by default. Set `--glab-bin glab-agent` on hosts with that wrapper.
Set `--url https://git.vs-point.cz` for a self-hosted GitLab instance.
Alternatively, set `GITLAB_TOKEN` to use the `python-gitlab` client.
Use `--token` for a direct token argument when process listings are acceptable.
`GITLAB_URL` and `GITLAB_MONITOR_GLAB_BIN` set the `--url` and `--glab-bin` defaults.
`gitlab-monitor --help` lists every kind and exit code. `--version` prints the version.
The client interface has the same `get` method for both transports.

## Watch

```sh
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch issue-comments 171 1373
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch mr-feedback 171 350
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch mr-conflicts 171 350
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch mr-approvals 171 350
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch mr-state 171 350
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch mr-pipeline 171 350 --until success
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch pipeline 171 32147 --until success
```

`mr-state` reports when the MR is merged or closed.
`mr-pipeline` reads the MR head pipeline on every poll.
After a push, it follows the new pipeline. `pipeline` stays on one pipeline id.

Issue and MR comment watches start from the current note set. They report new human comments.
State and pipeline watches report a matching current state immediately.
Every watch supports `--interval`, `--timeout`, and `--follow`.
Timeout exits with code 124. API errors exit with code 1.
`inspect` prints a current snapshot without waiting.

Use `watch-many` to watch several targets in one process. Repeat `--target` for CLI input:

```sh
gitlab-monitor watch-many \
  --target issue-comments 171 1373 \
  --target mr-feedback 171 350 \
  --target pipeline 171 32147
```

Use `--input FILE` for a JSON array. Use `--input -` to read standard input.
You can combine `--input` with repeated `--target` options.

```json
[
  {"kind": "issue-comments", "project": 171, "resource": 1373},
  {"kind": "pipeline", "project": 171, "resource": 32147, "until": "success"}
]
```

Each output line identifies its `kind`, `project`, and `resource`.
The default command exits after the first matching event across all targets.
Use `--follow` to keep reporting later events from every target.
`--interval` and `--timeout` apply to the whole watch process.
JSON targets accept `until` for pipeline status. `--until` sets the status for CLI pipeline targets.
`until` applies to `pipeline` and `mr-pipeline` targets.

Use `--query` to select the JSON output with a JMESPath expression.
The option works with `watch`, `watch-many`, and `inspect`.
The default output keeps the full event, including comment text.

```sh
gitlab-monitor watch-many \
  --target pipeline 171 32147 \
  --query '{event: event, project: project, resource: resource, status: status}'
```

The example emits `{"event":"pipeline","project":"171","resource":"32147","status":"failed"}`.

The CLI does not install a service or store a persistent baseline.
It does not post comments or change GitLab resources.

## Development

Run `direnv allow` once. Then enter the project environment with `direnv exec .`.
Run `direnv exec . devenv test` for lint, workflow checks, tests, and the wheel build.
Run `direnv exec . devenv tasks run quality:lint` for lint only.

Release Please creates release pull requests, updates `CHANGELOG.md`, and tags releases.
Use Conventional Commit titles. The workflow uses one release and changelog engine.
Dependabot checks GitHub Actions for new releases each week.

## Job attempts and duration alerts

Use `pipeline-jobs` for one pipeline. Use `mr-jobs` for the current MR head pipeline.
Each snapshot includes all job attempts, including retries, manual jobs, and skipped jobs.
The client requests every API page. The client does not request job traces.

```sh
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent \
  inspect pipeline-jobs 237 40736

gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent \
  watch mr-jobs 237 225 --follow --interval 15 --timeout 3600 \
  --duration-budget 'verify:apps-parents=250' \
  --duration-budget 'verify:packages-domain=300' \
  --wait-budget '*=900'
```

Job watches emit the current job states on the first poll.
Without `--follow`, the command emits that poll's events and exits.
With `--follow`, the command emits later state changes and duration overruns.
`--until` does not filter job events.

The events are `job-started`, `job-finished`, `job-failed`, `job-state`, and `job-overrun`.
Only the `failed` status emits `job-failed`.
Failures appear before other job events, even when the pipeline remains active.
The event preserves `allow_failure` and `failure_reason`.
Manual, pending, scheduled, preparation, and resource-wait states emit `job-state`.
Success, cancellation, and skipped states emit `job-finished`.
A poll cannot detect intermediate states that finish between requests.

An MR watch resolves the head pipeline on every poll.
A head change emits `job-pipeline-changed` with the old and new pipeline IDs.
Each job event includes its pipeline ID.
The watch compares job states within their pipeline.
A missing head pipeline produces an empty snapshot.
The watch stops tracking the predecessor after a head change.

`execution_seconds` measures job execution.
`pre_start_seconds` measures the time from creation until execution starts.
Pre-start time includes dependency waits, manual waits, and resource waits.
`queued_seconds` preserves GitLab's separate runner queue measurement.
Completed execution uses GitLab's duration when available.
Otherwise, timestamps supply the duration.
Active execution and waits use the injected observation clock.
The source fields identify `api`, `timestamps`, or `observed_elapsed` measurements.
Missing measurements remain `null`.
Unstarted terminal jobs without a finish timestamp have an unknown pre-start duration.
Job IDs identify retry attempts. `attempt` numbers attempts within each name and stage.

Budgets use seconds. Repeat each option for separate job names.
Use `*=SECONDS` to set a fallback budget for all names.
An exact name overrides the fallback.
`--duration-budget` limits execution. `--wait-budget` limits pre-start time.
A measured duration above its budget emits `job-overrun`.
Each watch emits at most one overrun alert per job ID and pipeline.
The alert includes every exceeded budget at that observation.
A retry gets its own alert allowance.
A new process starts a new alert allowance.
The monitor does not cancel, retry, or change jobs.

Enable historical estimates with `--baseline-samples`:

```sh
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent \
  watch mr-jobs 237 225 --follow --baseline-samples 5 --baseline-multiplier 1.5
```

History selects older pipelines on the current pipeline's exact ref.
Successful jobs supply samples even when their pipeline failed.
The client reads all history pages before selecting the requested pipeline count.
Each pipeline contributes its latest attempt for each job name and stage.
Only successful attempts with recorded execution durations supply samples.
The baseline is the median execution duration of those samples.
The baseline includes its sample count and the source `historical_estimate`.
The execution budget equals that estimate multiplied by `--baseline-multiplier`.
Explicit execution budgets take precedence.
Missing history produces no estimated budget.
History does not estimate pre-start waits.
The watch caches baselines separately for each pipeline.
An MR head change loads the new pipeline's baselines.
Estimates describe historical execution. They do not predict completion times.

Job targets also work with `watch-many` and `--query`.
JSON targets accept `duration_budgets`, `wait_budgets`, `baseline_samples`, and `baseline_multiplier`.
CLI budget options apply to CLI targets. JSON targets define their own budgets.

```json
[
  {
    "kind": "mr-jobs",
    "project": 237,
    "resource": 225,
    "duration_budgets": {"verify:apps-parents": 250},
    "wait_budgets": {"*": 900},
    "baseline_samples": 5,
    "baseline_multiplier": 1.5
  }
]
```

For a local source checkout, set `PYTHONPATH=src` inside the project environment:

```sh
cd /home/solcik/dev/github/solcik/gitlab-monitor/job-monitor
direnv exec . env PYTHONPATH=src python -m gitlab_monitor \
  --url https://git.vs-point.cz --glab-bin glab-agent \
  watch mr-jobs 237 225 --follow --interval 15 --timeout 3600 \
  --duration-budget 'verify:apps-parents=250' \
  --duration-budget 'verify:packages-domain=300'
```
