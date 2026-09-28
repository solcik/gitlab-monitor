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
