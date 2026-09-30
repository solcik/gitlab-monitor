"""Job regression fixtures use deterministic API responses and clocks."""

import json
from datetime import UTC, datetime, timedelta

import click
import pytest
from click.testing import CliRunner

from gitlab_monitor import cli
from gitlab_monitor.jobs import job_events, measurements

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


def job(job_id=1, status="running", **fields):
    return {
        "id": job_id,
        "name": "test",
        "stage": "test",
        "status": status,
        "created_at": (NOW - timedelta(seconds=50)).isoformat(),
        "started_at": (NOW - timedelta(seconds=30)).isoformat(),
        "duration": None,
        **fields,
    }


class FixtureClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, path, *, paginate=False):
        self.calls.append((path, paginate))
        return next(self.responses)


def target(**options):
    return cli.WatchTarget(
        kind="pipeline-jobs", project="237", resource="20", **options
    )


def pipeline(pipeline_id=20):
    return {"id": pipeline_id, "status": "running", "ref": "feature/a"}


def test_separate_execution_wait_and_runner_queue():
    observed = measurements(job(queued_duration=5, duration=0), NOW)
    assert observed["execution_seconds"] == 30
    assert observed["pre_start_seconds"] == 20
    assert observed["queued_seconds"] == 5
    assert observed["execution_source"] == "observed_elapsed"
    assert observed["pre_start_source"] == "timestamps"
    completed = measurements(job(status="success", duration=262.2), NOW)
    assert completed["execution_seconds"] == 262.2
    assert completed["execution_source"] == "api"


@pytest.mark.parametrize(
    "status",
    ["pending", "waiting_for_resource", "manual", "created", "scheduled", "preparing"],
)
def test_wait_states_are_visible(status):
    client = FixtureClient([pipeline(), [job(status=status, started_at=None)]])
    state = cli.snapshot(client, "pipeline-jobs", "237", "20", now=NOW)
    entry = state["jobs"][0]
    assert entry["status"] == status
    assert entry["execution_seconds"] is None
    assert entry["pre_start_seconds"] == 50
    assert job_events(None, state, set())[0]["event"] == "job-state"


@pytest.mark.parametrize("status", ["skipped", "canceled"])
def test_terminal_unstarted_job_does_not_accumulate_wait(status):
    measured = measurements(job(status=status, started_at=None), NOW)
    assert measured["pre_start_seconds"] is None
    state = {
        "pipeline": 20,
        "jobs": [
            {
                **job(status=status),
                **measured,
                "execution_budget_seconds": None,
                "wait_budget_seconds": None,
            }
        ],
    }
    assert job_events(None, state, set())[0]["event"] == "job-finished"


def test_early_failure_precedes_other_job_events(capsys):
    client = FixtureClient(
        [pipeline(), [job(status="manual"), job(2, "failed", allow_failure=True)]]
    )
    cli.watch_many(client, [target()], 1, 0, False, now=lambda: NOW)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["event"] for event in events] == ["job-failed", "job-state"]
    assert events[0]["job"]["allow_failure"] is True
    assert events[0]["pipeline"] == 20
    assert client.calls[-1] == (
        "projects/237/pipelines/20/jobs?include_retried=true&per_page=100",
        True,
    )


def test_retry_attempts_and_once_per_job_alerts():
    client = FixtureClient([pipeline(), [job(2), job(1, "failed", duration=40)]])
    state = cli.snapshot(
        client,
        "pipeline-jobs",
        "237",
        "20",
        policy=target(duration_budgets={"test": 10}, wait_budgets={"*": 5}),
        now=NOW,
    )
    assert [item["attempt"] for item in state["jobs"]] == [1, 2]
    alerted = set()
    first = job_events(None, state, alerted)
    alerts = [event for event in first if event["event"] == "job-overrun"]
    assert len(alerts) == 2
    assert len(alerts[0]["reasons"]) == 2
    assert job_events(state, state, alerted) == []


def test_observed_overrun_after_start_without_status_change(capsys):
    client = FixtureClient(
        [pipeline(), [job()], pipeline(), [job()], pipeline(), [job()]]
    )
    ticks = iter([0, 1, 2, 3])
    dates = iter([NOW, NOW + timedelta(seconds=20), NOW + timedelta(seconds=40)])
    with pytest.raises(click.exceptions.Exit) as result:
        cli.watch_many(
            client,
            [target(duration_budgets={"*": 40})],
            1,
            3,
            True,
            now=lambda: next(dates),
            monotonic=lambda: next(ticks),
            sleep=lambda _: None,
        )
    assert result.value.exit_code == 124
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["event"] for event in events] == ["job-started", "job-overrun"]
    assert events[1]["reasons"][0]["observed_seconds"] == 50


def test_history_uses_latest_attempt_and_excludes_current_and_newer():
    client = FixtureClient(
        [
            pipeline(),
            [job()],
            [
                {"id": 21},
                {"id": 20},
                {"id": 19, "status": "failed"},
                {"id": 18, "status": "running"},
            ],
            [job(1, "success", duration=900), job(2, "success", duration=10)],
            [job(3, "success", duration=30)],
            pipeline(),
            [job()],
        ]
    )
    policy = target(baseline_samples=2, baseline_multiplier=2)
    cache = {}
    state = cli.snapshot(
        client,
        policy.kind,
        policy.project,
        policy.resource,
        policy=policy,
        cache=cache,
        now=NOW,
    )
    assert state["jobs"][0]["baseline"] == {
        "seconds": 20,
        "samples": 2,
        "source": "historical_estimate",
    }
    assert state["jobs"][0]["execution_budget_seconds"] == 40
    assert "ref=feature%2Fa" in client.calls[2][0]
    assert "status=success" not in client.calls[2][0]
    assert all(paginate for _, paginate in client.calls[1:5])
    cli.snapshot(
        client,
        policy.kind,
        policy.project,
        policy.resource,
        policy=policy,
        cache=cache,
        now=NOW,
    )
    assert len(client.calls) == 7


def test_explicit_budget_overrides_history_and_missing_baseline_is_null():
    client = FixtureClient(
        [
            pipeline(),
            [job(), job(2, name="other")],
            [{"id": 19}],
            [job(3, "success", duration=10)],
        ]
    )
    policy = target(baseline_samples=1, duration_budgets={"test": 500})
    state = cli.snapshot(
        client, policy.kind, policy.project, policy.resource, policy=policy, now=NOW
    )
    assert state["jobs"][0]["execution_budget_seconds"] == 500
    assert state["jobs"][1]["baseline"] is None
    assert state["jobs"][1]["execution_budget_seconds"] is None


def test_mr_head_switch_and_missing_pipeline():
    client = FixtureClient(
        [
            {"head_pipeline": None},
            {"head_pipeline": {"id": 20}},
            pipeline(),
            [job()],
            {"head_pipeline": {"id": 21}},
            pipeline(21),
            [job(2, "skipped")],
        ]
    )
    previous = None
    for expected in (None, 20, 21):
        state = cli.snapshot(client, "mr-jobs", "237", "225", now=NOW)
        assert state["pipeline"] == expected
        emitted = job_events(previous, state, set())
        if previous is not None:
            assert any(event["event"] == "job-pipeline-changed" for event in emitted)
        assert not any(event["event"] == "job-failed" for event in emitted)
        previous = state


@pytest.mark.parametrize("value", ["bad", "x=0", "x=-1", "x=nan", "x=inf", "=5"])
def test_invalid_cli_budget_rejected(value):
    result = CliRunner().invoke(
        cli.main, ["inspect", "pipeline-jobs", "237", "20", "--duration-budget", value]
    )
    assert result.exit_code == 2


def test_json_budget_validation_and_job_watch_many(monkeypatch):
    runner = CliRunner()
    invalid = runner.invoke(
        cli.main,
        ["watch-many", "--input", "-"],
        input='[{"kind":"pipeline-jobs","project":237,"resource":20,"wait_budgets":{"*":-1}}]',
    )
    assert invalid.exit_code == 2
    monkeypatch.setattr(
        cli,
        "GlabClient",
        lambda *args: FixtureClient(
            [pipeline(), [job(status="success", duration=100)]]
        ),
    )
    result = runner.invoke(
        cli.main,
        ["watch-many", "--input", "-"],
        input='[{"kind":"pipeline-jobs","project":237,"resource":20,"duration_budgets":{"*":50}}]',
    )
    assert result.exit_code == 0
    assert [json.loads(line)["event"] for line in result.output.splitlines()] == [
        "job-overrun",
        "job-finished",
    ]


def test_invalid_job_api_response_is_cli_error(monkeypatch):
    monkeypatch.setattr(
        cli, "GlabClient", lambda *args: FixtureClient([pipeline(), {}])
    )
    result = CliRunner().invoke(cli.main, ["inspect", "pipeline-jobs", "237", "20"])
    assert result.exit_code == 1
    assert "Invalid job response" in result.output


def test_glab_pagination_and_token_pagination(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return type("Result", (), {"stdout": '[{"id":1},{"id":101}]'})()

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert (
        len(cli.GlabClient("glab-agent", "git.vs-point.cz").get("jobs", paginate=True))
        == 2
    )
    assert commands[0][-4:] == ["--paginate", "--output", "json", "jobs"]
    calls = []

    class GitLab:
        def __init__(self, *args, **kwargs):
            pass

        def http_list(self, path, **kwargs):
            calls.append((path, kwargs))
            return [{"id": 1}, {"id": 101}]

    monkeypatch.setattr(cli.gitlab, "Gitlab", GitLab)
    assert (
        len(
            cli.TokenClient("https://example.org", "fixture").get("jobs", paginate=True)
        )
        == 2
    )
    assert calls == [("jobs", {"get_all": True})]


def test_job_lifecycle_and_retry_in_one_active_pipeline(capsys):
    client = FixtureClient(
        [
            pipeline(),
            [job(status="pending", started_at=None)],
            pipeline(),
            [job()],
            pipeline(),
            [job(status="failed", duration=40)],
            pipeline(),
            [job(status="failed", duration=40), job(2)],
        ]
    )
    ticks = iter([0, 1, 2, 3, 4])
    with pytest.raises(click.exceptions.Exit):
        cli.watch_many(
            client,
            [target()],
            1,
            4,
            True,
            now=lambda: NOW,
            monotonic=lambda: next(ticks),
            sleep=lambda _: None,
        )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["event"] for event in events] == [
        "job-state",
        "job-started",
        "job-failed",
        "job-started",
    ]
    assert events[-1]["job"]["attempt"] == 2
    assert all(event["pipeline"] == 20 for event in events)


def test_more_than_one_page_of_job_attempts_remains_visible():
    client = FixtureClient(
        [
            pipeline(),
            [job(index, "skipped", started_at=None) for index in range(1, 206)],
        ]
    )
    state = cli.snapshot(client, "pipeline-jobs", "237", "20", now=NOW)
    assert len(state["jobs"]) == 205
    assert state["jobs"][-1]["id"] == 205
    assert state["jobs"][-1]["attempt"] == 205
    assert client.calls[-1][1] is True
