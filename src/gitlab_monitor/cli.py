"""Command line watches for GitLab events."""

import json
import subprocess
import time
from dataclasses import dataclass
from importlib import metadata
from typing import Literal, get_args
from urllib.parse import quote

import click
import gitlab
import jmespath
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from .jobs import job_events, job_snapshot, utc_now

WatchKind = Literal[
    "issue-comments",
    "mr-feedback",
    "mr-conflicts",
    "mr-approvals",
    "pipeline-jobs",
    "mr-jobs",
    "mr-state",
    "mr-pipeline",
    "pipeline",
]
PipelineStatus = Literal["failed", "success", "canceled", "skipped"]
WATCH_KINDS = get_args(WatchKind)
PIPELINE_STATUSES = get_args(PipelineStatus)
MR_FINAL_STATES = ("merged", "closed")


class WatchTarget(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_min_length=1, coerce_numbers_to_str=True
    )

    kind: WatchKind
    project: str
    resource: str
    until: PipelineStatus = "failed"
    duration_budgets: dict[str, float] = Field(default_factory=dict)
    wait_budgets: dict[str, float] = Field(default_factory=dict)
    baseline_samples: int = Field(default=0, ge=0)
    baseline_multiplier: float = Field(default=1.5, gt=0, allow_inf_nan=False)

    @field_validator("duration_budgets", "wait_budgets")
    @classmethod
    def validate_budgets(cls, budgets):
        import math

        if any(not math.isfinite(value) or value <= 0 for value in budgets.values()):
            raise ValueError("Budgets must contain positive finite seconds.")
        return budgets


TARGETS = TypeAdapter(list[WatchTarget])

try:
    VERSION = metadata.version("gitlab-agent-monitor")
except metadata.PackageNotFoundError:
    VERSION = "unknown"

TARGET_HELP = """
\b
KIND selects the event. RESOURCE is the IID or id that KIND names:
  issue-comments  new human note on issue IID
  mr-feedback     new human note or thread reply on MR IID
  mr-conflicts    MR IID has merge conflicts
  mr-approvals    MR IID becomes approved
  mr-state        MR IID is merged or closed
  mr-pipeline     head pipeline of MR IID reaches the --until status;
                  after a push, the watch follows the new pipeline
  pipeline-jobs   all job attempts in pipeline id
  mr-jobs         all job attempts in the current MR head pipeline
  pipeline        pipeline id reaches the --until status
PROJECT is a numeric project id or a full path such as group/repo.
"""

EXIT_HELP = """
\b
Output is one JSON object per line on stdout.
Exit codes:
  0    a matching event occurred (without --follow)
  1    GitLab API or glab failure
  2    usage error
  124  --timeout expired (with --follow, after any events)
"""


def parse_query(value):
    if value is None:
        return None
    try:
        return jmespath.compile(value)
    except jmespath.exceptions.JMESPathError as error:
        raise click.BadParameter(str(error), param_hint="--query") from error


def emit(value, query):
    click.echo(json.dumps(query.search(value) if query else value, sort_keys=True))


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


def snapshot(
    client, kind: str, project: str, resource: str, *, policy=None, cache=None, now=None
):
    base = project_path(project)
    if kind in ("pipeline-jobs", "mr-jobs"):
        policy = policy or WatchTarget(kind=kind, project=project, resource=resource)
        try:
            return job_snapshot(
                client,
                base,
                kind,
                resource,
                policy,
                cache if cache is not None else {},
                now if now is not None else utc_now(),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise MonitorError(f"Invalid job response: {error}") from error
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
    if kind == "mr-state":
        return {"state": mr.get("state"), "url": mr.get("web_url")}
    if kind == "mr-pipeline":
        pipeline = mr.get("head_pipeline") or {}
        return {
            "pipeline": pipeline.get("id"),
            "status": pipeline.get("status"),
            "url": pipeline.get("web_url") or mr.get("web_url"),
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
    if kind == "mr-pipeline":
        status = after["status"]
        if status == until and (
            initial
            or before["pipeline"] != after["pipeline"]
            or before["status"] != status
        ):
            return [{"event": "pipeline", **after}]
    if (
        kind == "mr-state"
        and after["state"] in MR_FINAL_STATES
        and (initial or before["state"] != after["state"])
    ):
        return [{"event": "mr-state", **after}]
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


def watch_many(
    client,
    targets,
    interval,
    timeout,
    follow,
    query=None,
    *,
    now=utc_now,
    monotonic=None,
    sleep=None,
):
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    start = monotonic()
    previous = [None] * len(targets)
    alerted = [set() for _ in targets]
    cache = {}
    while True:
        for index, target in enumerate(targets):
            before = previous[index]
            is_jobs = target.kind in ("pipeline-jobs", "mr-jobs")
            after = snapshot(
                client,
                target.kind,
                target.project,
                target.resource,
                policy=target,
                cache=cache,
                now=now(),
            )
            found = (
                job_events(before, after, alerted[index])
                if is_jobs
                else events(
                    target.kind, before, after, target.until, initial=before is None
                )
            )
            for event in found:
                emit(
                    {
                        "kind": target.kind,
                        "project": target.project,
                        "resource": target.resource,
                        **event,
                    },
                    query,
                )
                if not follow and not is_jobs:
                    return
            if found and not follow:
                return
            previous[index] = after
        if timeout is not None and monotonic() - start >= timeout:
            raise click.exceptions.Exit(124)
        sleep(interval)


@click.group(context_settings={"show_default": True})
@click.version_option(VERSION)
@click.option(
    "--url",
    default="https://gitlab.com",
    envvar="GITLAB_URL",
    show_envvar=True,
    help="GitLab instance. Its hostname is passed to glab.",
)
@click.option(
    "--token",
    envvar="GITLAB_TOKEN",
    show_envvar=True,
    help="GitLab private token. Prefer GITLAB_TOKEN. Without a token, glab runs.",
)
@click.option(
    "--glab-bin",
    default="glab",
    envvar="GITLAB_MONITOR_GLAB_BIN",
    show_envvar=True,
    help="glab executable or wrapper, such as glab-agent.",
)
@click.pass_context
def main(ctx, url, token, glab_bin):
    """Watch GitLab resources when an agent needs a specific event.

    \b
    inspect     print the current state and exit
    watch       wait for one event on one resource
    watch-many  wait for events on several resources

    The command never changes GitLab. It only reads.

    \b
    Example:
      gitlab-monitor --url https://git.vs-point.cz --glab-bin glab-agent \\
        watch pipeline group/repo 32147 --until success --timeout 3600
    """
    if token:
        ctx.obj = TokenClient(url, token)
    else:
        from urllib.parse import urlparse

        host = urlparse(url).hostname
        if not host:
            raise click.UsageError("The GitLab URL needs a hostname.")
        ctx.obj = GlabClient(glab_bin, host)


def job_options(command):
    for option in (
        click.option(
            "--duration-budget",
            multiple=True,
            metavar="NAME=SECONDS",
            help="Execution limit per job name. Use * for all names.",
        ),
        click.option(
            "--wait-budget",
            multiple=True,
            metavar="NAME=SECONDS",
            help="Pre-start limit per job name. Use * for all names.",
        ),
        click.option(
            "--baseline-samples",
            type=click.IntRange(min=0),
            default=0,
            help="Recent pipelines on the same ref. Only successful jobs supply samples.",
        ),
        click.option(
            "--baseline-multiplier",
            type=click.FloatRange(min=0, min_open=True),
            default=1.5,
            help="Historical median multiplier for jobs without explicit limits.",
        ),
    ):
        command = option(command)
    return command


def job_policy(duration_budget, wait_budget, baseline_samples, baseline_multiplier):
    def parse(values):
        result = {}
        for value in values:
            try:
                name, seconds = value.rsplit("=", 1)
                if not name:
                    raise ValueError
                result[name] = float(seconds)
            except ValueError as error:
                raise click.BadParameter("Use NAME=SECONDS for each budget.") from error
        try:
            return WatchTarget.validate_budgets(result)
        except ValueError as error:
            raise click.BadParameter(str(error)) from error

    import math

    if not math.isfinite(baseline_multiplier):
        raise click.BadParameter("The baseline multiplier must be finite.")
    return {
        "duration_budgets": parse(duration_budget),
        "wait_budgets": parse(wait_budget),
        "baseline_samples": baseline_samples,
        "baseline_multiplier": baseline_multiplier,
    }


@main.command(epilog=TARGET_HELP + EXIT_HELP)
@click.argument(
    "kind",
    type=click.Choice(WATCH_KINDS),
)
@click.argument("project")
@click.argument("resource")
@click.option(
    "--until",
    type=click.Choice(PIPELINE_STATUSES),
    default="failed",
    help="Pipeline status for pipeline and mr-pipeline. Other kinds ignore it.",
)
@click.option(
    "--interval",
    type=click.FloatRange(min=0.1),
    default=15.0,
    help="Seconds between polls.",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0),
    help="Stop after this many seconds with exit code 124.",
)
@click.option(
    "--follow", is_flag=True, help="Emit later events until stopped or timed out."
)
@click.option("--query", help="Select output fields with a JMESPath expression.")
@job_options
@click.pass_obj
def watch_command(
    client, kind, project, resource, until, interval, timeout, follow, query, **options
):
    """Emit JSON when a matching event occurs.

    Comment watches report only notes that appear after the first poll.
    State watches report a matching current state at once.
    """
    try:
        target = WatchTarget(
            kind=kind,
            project=project,
            resource=resource,
            until=until,
            **job_policy(**options),
        )
        watch_many(client, [target], interval, timeout, follow, parse_query(query))
    except MonitorError as error:
        raise click.ClickException(str(error)) from error


@main.command(
    "watch-many",
    epilog=TARGET_HELP
    + """
\b
The --input JSON is an array of targets:
  [{"kind": "pipeline", "project": "group/repo",
    "resource": 32147, "until": "success"}]
"""
    + EXIT_HELP,
)
@click.option(
    "--target",
    type=(click.Choice(WATCH_KINDS), str, str),
    multiple=True,
    metavar="KIND PROJECT RESOURCE",
    help="One target. Repeat the option for more targets.",
)
@click.option(
    "--input",
    "input_file",
    type=click.File("r"),
    help="Read a JSON array of targets from a file or stdin (-).",
)
@click.option(
    "--until",
    type=click.Choice(PIPELINE_STATUSES),
    default="failed",
    help="Status for CLI pipeline and mr-pipeline targets. JSON sets its own.",
)
@click.option(
    "--interval",
    type=click.FloatRange(min=0.1),
    default=15.0,
    help="Seconds between polls.",
)
@click.option(
    "--timeout",
    type=click.FloatRange(min=0),
    help="Stop after this many seconds with exit code 124.",
)
@click.option(
    "--follow", is_flag=True, help="Emit later events until stopped or timed out."
)
@click.option("--query", help="Select output fields with a JMESPath expression.")
@job_options
@click.pass_obj
def watch_many_command(
    client, target, input_file, until, interval, timeout, follow, query, **options
):
    """Watch several resources in one process.

    Each event names its kind, project and resource. Without --follow, the
    command exits after the first event from any target.
    """
    policy = job_policy(**options)
    targets = [
        WatchTarget(
            kind=kind, project=project, resource=resource, until=until, **policy
        )
        for kind, project, resource in target
    ]
    if input_file is not None:
        try:
            targets.extend(TARGETS.validate_json(input_file.read()))
        except ValidationError as error:
            details = "; ".join(
                f"{'.'.join(map(str, item['loc'])) or 'JSON'}: {item['msg']}"
                for item in error.errors(include_input=False)
            )
            raise click.BadParameter(details, param_hint="--input") from error
    if not targets:
        raise click.UsageError("Give at least one --target or --input target.")
    try:
        watch_many(client, targets, interval, timeout, follow, parse_query(query))
    except MonitorError as error:
        raise click.ClickException(str(error)) from error


@main.command(epilog=TARGET_HELP + EXIT_HELP)
@click.argument(
    "kind",
    type=click.Choice(WATCH_KINDS),
)
@click.argument("project")
@click.argument("resource")
@click.option("--query", help="Select output fields with a JMESPath expression.")
@job_options
@click.pass_obj
def inspect(client, kind, project, resource, query, **options):
    """Read one current resource snapshot without a watch.

    Comment kinds print an object of notes keyed by note id.
    """
    try:
        policy = WatchTarget(
            kind=kind, project=project, resource=resource, **job_policy(**options)
        )
        emit(
            snapshot(client, kind, project, resource, policy=policy), parse_query(query)
        )
    except MonitorError as error:
        raise click.ClickException(str(error)) from error
