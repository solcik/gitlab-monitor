"""Command line watches for GitLab events."""

import json
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import quote

import click
import gitlab


class MonitorError(Exception):
    """An API request or response failed."""


@dataclass
class GlabClient:
    binary: str
    host: str

    def get(self, path: str, *, paginate: bool = False):
        command = [self.binary, "api", "--hostname", self.host, "--method", "GET"]
        if paginate:
            command += ["--paginate", "--output", "json"]
        command.append(path)
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=60, check=True
            )
            return json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise MonitorError(f"GitLab request failed: {path}: {error}") from error


@dataclass
class TokenClient:
    url: str
    token: str

    def __post_init__(self):
        self.client = gitlab.Gitlab(self.url, private_token=self.token)

    def get(self, path: str, *, paginate: bool = False):
        try:
            if paginate:
                return self.client.http_list(path, get_all=True)
            return self.client.http_get(path)
        except gitlab.GitlabError as error:
            raise MonitorError(f"GitLab request failed: {path}: {error}") from error


def project_path(project: str) -> str:
    return f"projects/{quote(project, safe='')}"


def require_object(value, path):
    if not isinstance(value, dict):
        raise MonitorError(f"GitLab returned an invalid object: {path}")
    return value


def require_list(value, path):
    if not isinstance(value, list):
        raise MonitorError(f"GitLab returned an invalid list: {path}")
    return value


def snapshot(client, kind: str, project: str, resource: str):
    base = project_path(project)
    if kind == "pipeline":
        path = f"{base}/pipelines/{resource}"
        pipeline = require_object(client.get(path), path)
        return {"status": pipeline.get("status"), "url": pipeline.get("web_url")}

    prefix = "issues" if kind == "issue-comments" else "merge_requests"
    path = f"{base}/{prefix}/{resource}"
    if kind == "issue-comments":
        notes_path = f"{path}/notes?per_page=100&sort=asc"
        notes = require_list(client.get(notes_path, paginate=True), notes_path)
        return {str(note["id"]): note for note in notes if not note.get("system")}

    mr = require_object(client.get(path), path)
    if kind == "mr-conflicts":
        return {
            "conflicts": mr.get("has_conflicts") is True
            or mr.get("detailed_merge_status") == "conflict",
            "status": mr.get("detailed_merge_status"),
            "url": mr.get("web_url"),
        }
    if kind == "mr-approvals":
        approval_path = f"{path}/approvals"
        approvals = require_object(client.get(approval_path), approval_path)
        return {
            "approved": approvals.get("approved") is True,
            "approved_by": sorted(
                user["user"]["username"]
                for user in approvals.get("approved_by", [])
                if isinstance(user, dict) and isinstance(user.get("user"), dict)
            ),
            "url": mr.get("web_url"),
        }
    discussions_path = f"{path}/discussions?per_page=100"
    discussions = require_list(
        client.get(discussions_path, paginate=True), discussions_path
    )
    notes = {}
    for discussion in discussions:
        for note in discussion.get("notes", []):
            if not note.get("system"):
                notes[str(note["id"])] = {
                    "id": note["id"],
                    "body": note.get("body"),
                    "author": note.get("author", {}).get("username"),
                    "url": note.get("web_url") or mr.get("web_url"),
                    "resolved": discussion.get("resolved"),
                }
    return notes


def events(kind: str, before, after, until: str, *, initial: bool):
    if kind in ("issue-comments", "mr-feedback"):
        if initial:
            return []
        return [
            {"event": "comment", "note": after[key]}
            for key in sorted(after.keys() - before.keys(), key=int)
        ]
    if kind == "pipeline":
        status = after["status"]
        if status == until and (initial or before["status"] != status):
            return [{"event": "pipeline", **after}]
    if (
        kind == "mr-conflicts"
        and after["conflicts"]
        and (initial or not before["conflicts"])
    ):
        return [{"event": "conflict", **after}]
    if (
        kind == "mr-approvals"
        and after["approved"]
        and (initial or not before["approved"])
    ):
        return [{"event": "approval", **after}]
    return []


def watch(client, kind, project, resource, until, interval, timeout, follow):
    start = time.monotonic()
    before = None
    while True:
        after = snapshot(client, kind, project, resource)
        for event in events(kind, before, after, until, initial=before is None):
            click.echo(
                json.dumps(
                    {"project": project, "resource": resource, **event}, sort_keys=True
                )
            )
            if not follow:
                return
        before = after
        if timeout is not None and time.monotonic() - start >= timeout:
            raise click.exceptions.Exit(124)
        time.sleep(interval)


@click.group()
@click.option(
    "--url", default="https://gitlab.com", show_default=True, envvar="GITLAB_URL"
)
@click.option(
    "--token", envvar="GITLAB_TOKEN", help="GitLab private token. Prefer GITLAB_TOKEN."
)
@click.option(
    "--glab-bin", default="glab", show_default=True, envvar="GITLAB_MONITOR_GLAB_BIN"
)
@click.pass_context
def main(ctx, url, token, glab_bin):
    """Watch GitLab resources when an agent needs a specific event."""
    if token:
        ctx.obj = TokenClient(url, token)
    else:
        from urllib.parse import urlparse

        host = urlparse(url).hostname
        if not host:
            raise click.UsageError("The GitLab URL needs a hostname.")
        ctx.obj = GlabClient(glab_bin, host)


@main.command()
@click.argument(
    "kind",
    type=click.Choice(
        ["issue-comments", "mr-feedback", "mr-conflicts", "mr-approvals", "pipeline"]
    ),
)
@click.argument("project")
@click.argument("resource")
@click.option(
    "--until",
    type=click.Choice(["failed", "success", "canceled", "skipped"]),
    default="failed",
    show_default=True,
)
@click.option(
    "--interval", type=click.FloatRange(min=0.1), default=15.0, show_default=True
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0),
    help="Stop after this many seconds with exit code 124.",
)
@click.option(
    "--follow", is_flag=True, help="Emit later events until stopped or timed out."
)
@click.pass_obj
def watch_command(client, kind, project, resource, until, interval, timeout, follow):
    """Emit JSON when a matching event occurs."""
    try:
        watch(client, kind, project, resource, until, interval, timeout, follow)
    except MonitorError as error:
        raise click.ClickException(str(error)) from error


@main.command()
@click.argument(
    "kind",
    type=click.Choice(
        ["issue-comments", "mr-feedback", "mr-conflicts", "mr-approvals", "pipeline"]
    ),
)
@click.argument("project")
@click.argument("resource")
@click.pass_obj
def inspect(client, kind, project, resource):
    """Read one current resource snapshot without a watch."""
    try:
        click.echo(
            json.dumps(snapshot(client, kind, project, resource), sort_keys=True)
        )
    except MonitorError as error:
        raise click.ClickException(str(error)) from error
