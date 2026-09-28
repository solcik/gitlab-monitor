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
The client interface has the same `get` method for both transports.

## Watch

```sh
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch issue-comments 171 1373
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch mr-feedback 171 350
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch mr-conflicts 171 350
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch mr-approvals 171 350
gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent watch pipeline 171 32147 --until success
```

Issue and MR comment watches start from the current note set. They report new human comments.
Conflict, approval, and pipeline watches report a matching current state immediately.
Every watch supports `--interval`, `--timeout`, and `--follow`.
Timeout exits with code 124. API errors exit with code 1.
`inspect` prints a current snapshot without waiting.

The CLI does not install a service or store a persistent baseline.
It does not post comments or change GitLab resources.

## Development

Run `direnv allow` once. Then enter the project environment with `direnv exec .`.
Run `direnv exec . devenv test` for lint, workflow checks, tests, and the wheel build.
Run `direnv exec . devenv tasks run quality:lint` for lint only.

Release Please creates release pull requests, updates `CHANGELOG.md`, and tags releases.
Use Conventional Commit titles. The workflow uses one release and changelog engine.
Dependabot checks GitHub Actions for new releases each week.
