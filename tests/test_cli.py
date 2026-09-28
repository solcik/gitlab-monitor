import json

from click.testing import CliRunner

from gitlab_monitor import cli


class Client:
    def __init__(self, values):
        self.values = iter(values)

    def get(self, path, *, paginate=False):
        return next(self.values)


def test_current_failure_exits_with_event(monkeypatch):
    monkeypatch.setattr(
        cli,
        "GlabClient",
        lambda *args: Client([{"status": "failed", "web_url": "https://example/p"}]),
    )
    result = CliRunner().invoke(
        cli.main, ["watch", "pipeline", "171", "12", "--interval", "0.1"]
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == "failed"


def test_issue_comment_waits_for_new_note(monkeypatch):
    monkeypatch.setattr(
        cli,
        "GlabClient",
        lambda *args: Client(
            [
                [{"id": 1, "body": "old"}],
                [{"id": 1, "body": "old"}, {"id": 2, "body": "new"}],
            ]
        ),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    result = CliRunner().invoke(cli.main, ["watch", "issue-comments", "171", "4"])
    assert result.exit_code == 0
    assert json.loads(result.output)["note"]["id"] == 2


def test_current_conflict_exits(monkeypatch):
    monkeypatch.setattr(
        cli,
        "GlabClient",
        lambda *args: Client(
            [{"has_conflicts": True, "detailed_merge_status": "conflict"}]
        ),
    )
    result = CliRunner().invoke(cli.main, ["watch", "mr-conflicts", "171", "350"])
    assert result.exit_code == 0
    assert json.loads(result.output)["event"] == "conflict"


def test_timeout_returns_124(monkeypatch):
    monkeypatch.setattr(
        cli, "GlabClient", lambda *args: Client([{"status": "running"}])
    )
    result = CliRunner().invoke(
        cli.main, ["watch", "pipeline", "171", "12", "--timeout", "0"]
    )
    assert result.exit_code == 124


def test_watch_many_checks_repeated_targets(monkeypatch):
    monkeypatch.setattr(
        cli,
        "GlabClient",
        lambda *args: Client(
            [[], {"status": "failed", "web_url": "https://example/p"}]
        ),
    )
    result = CliRunner().invoke(
        cli.main,
        [
            "watch-many",
            "--target",
            "issue-comments",
            "171",
            "4",
            "--target",
            "pipeline",
            "171",
            "12",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "event": "pipeline",
        "kind": "pipeline",
        "project": "171",
        "resource": "12",
        "status": "failed",
        "url": "https://example/p",
    }


def test_watch_many_reads_json_from_stdin(monkeypatch):
    monkeypatch.setattr(
        cli,
        "GlabClient",
        lambda *args: Client([{"status": "success", "web_url": "https://example/p"}]),
    )
    result = CliRunner().invoke(
        cli.main,
        ["watch-many", "--input", "-"],
        input='[{"kind":"pipeline","project":171,"resource":12,"until":"success"}]',
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["status"] == "success"
    assert json.loads(result.output)["project"] == "171"


def test_watch_many_selects_concise_output(monkeypatch):
    monkeypatch.setattr(
        cli,
        "GlabClient",
        lambda *args: Client([{"status": "failed", "web_url": "https://example/p"}]),
    )
    result = CliRunner().invoke(
        cli.main,
        [
            "watch-many",
            "--target",
            "pipeline",
            "171",
            "12",
            "--query",
            "{event: event, status: status, resource: resource}",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "event": "pipeline",
        "resource": "12",
        "status": "failed",
    }


def test_watch_rejects_invalid_query():
    result = CliRunner().invoke(
        cli.main,
        ["watch", "pipeline", "171", "12", "--query", "["],
    )
    assert result.exit_code == 2
    assert "--query" in result.output


def test_watch_many_rejects_invalid_input():
    result = CliRunner().invoke(
        cli.main,
        ["watch-many", "--input", "-"],
        input='[{"kind":"pipeline","project":"171","resource":"12","token":"secret"}]',
    )
    assert result.exit_code == 2
    assert "Extra inputs are not permitted" in result.output


def test_watch_many_requires_a_target():
    result = CliRunner().invoke(cli.main, ["watch-many"])
    assert result.exit_code == 2
    assert "at least one" in result.output


def test_glab_binary_override(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return type("Result", (), {"stdout": '{"status":"success"}'})()

    monkeypatch.setattr(cli.subprocess, "run", run)
    result = CliRunner().invoke(
        cli.main,
        [
            "--url",
            "https://git.example.org",
            "--glab-bin",
            "/bin/glab-agent",
            "inspect",
            "pipeline",
            "171",
            "12",
        ],
    )
    assert result.exit_code == 0
    assert calls[0][:5] == [
        "/bin/glab-agent",
        "api",
        "--hostname",
        "git.example.org",
        "--method",
    ]


def test_token_uses_python_gitlab(monkeypatch):
    calls = []

    class GitLab:
        def __init__(self, url, private_token):
            calls.append((url, private_token))

        def http_get(self, path):
            return {"status": "success"}

    monkeypatch.setattr(cli.gitlab, "Gitlab", GitLab)
    result = CliRunner().invoke(
        cli.main,
        [
            "--url",
            "https://git.example.org",
            "--token",
            "test-token",
            "inspect",
            "pipeline",
            "171",
            "12",
        ],
    )
    assert result.exit_code == 0
    assert calls == [("https://git.example.org", "test-token")]


def test_version_option():
    result = CliRunner().invoke(cli.main, ["--version"])
    assert result.exit_code == 0
    assert "version" in result.output


def test_watch_help_explains_kinds_and_exit_codes():
    result = CliRunner().invoke(cli.main, ["watch", "--help"])
    assert result.exit_code == 0
    assert "mr-feedback     new human note" in result.output
    assert "124  --timeout expired" in result.output
