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
