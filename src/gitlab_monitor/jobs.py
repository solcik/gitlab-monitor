"""Read job attempts and compare observed durations with explicit limits."""

from datetime import UTC, datetime
from statistics import median
from urllib.parse import quote

TERMINAL = {"success", "failed", "canceled", "skipped"}


def utc_now():
    return datetime.now(UTC)


def timestamp(value):
    return datetime.fromisoformat(value) if value else None


def measurements(job, now):
    created = timestamp(job.get("created_at"))
    started = timestamp(job.get("started_at"))
    finished = timestamp(job.get("finished_at"))
    terminal = job["status"] in TERMINAL
    execution = job.get("duration")
    execution_source = "api" if execution is not None else None
    if started and (execution is None or not terminal):
        end = finished or (now if not terminal else None)
        execution = max(0, (end - started).total_seconds()) if end else None
        execution_source = (
            ("timestamps" if finished else "observed_elapsed") if end else None
        )
    wait_end = started or finished or (now if not terminal else None)
    waiting = (
        max(0, (wait_end - created).total_seconds()) if created and wait_end else None
    )
    return {
        "execution_seconds": execution,
        "execution_source": execution_source,
        "pre_start_seconds": waiting,
        "pre_start_source": (
            "timestamps" if started or finished else "observed_elapsed"
        )
        if waiting is not None
        else None,
        "queued_seconds": job.get("queued_duration"),
    }


def historical_baselines(client, base, pipeline, count):
    path = f"{base}/pipelines?ref={quote(pipeline['ref'], safe='')}&per_page=100&order_by=id&sort=desc"
    history = client.get(path, paginate=True)
    if not isinstance(history, list):
        raise TypeError("GitLab returned invalid pipeline history.")
    selected = sorted(
        (item for item in history if int(item["id"]) < int(pipeline["id"])),
        key=lambda item: int(item["id"]),
        reverse=True,
    )[:count]
    samples = {}
    for item in selected:
        jobs = client.get(
            f"{base}/pipelines/{item['id']}/jobs?include_retried=true&per_page=100",
            paginate=True,
        )
        if not isinstance(jobs, list):
            raise TypeError("GitLab returned invalid historical jobs.")
        latest = {}
        for job in sorted(jobs, key=lambda job: int(job["id"])):
            latest[(job["name"], job.get("stage"))] = job
        for key, job in latest.items():
            if job["status"] == "success" and job.get("duration") is not None:
                samples.setdefault(key, []).append(float(job["duration"]))
    return {
        key: {
            "seconds": median(values),
            "samples": len(values),
            "source": "historical_estimate",
        }
        for key, values in samples.items()
    }


def job_snapshot(client, base, kind, resource, policy, cache, now):
    if kind == "mr-jobs":
        mr = client.get(f"{base}/merge_requests/{resource}")
        pipeline_id = (mr.get("head_pipeline") or {}).get("id")
        if pipeline_id is None:
            return {
                "pipeline": None,
                "status": None,
                "jobs": [],
                "url": mr.get("web_url"),
            }
    else:
        pipeline_id = resource
    pipeline = client.get(f"{base}/pipelines/{pipeline_id}")
    path = f"{base}/pipelines/{pipeline_id}/jobs?include_retried=true&per_page=100"
    jobs = client.get(path, paginate=True)
    if not isinstance(jobs, list):
        raise TypeError("GitLab returned invalid jobs.")
    key = (base, str(pipeline_id), policy.baseline_samples)
    if key not in cache:
        cache[key] = (
            historical_baselines(client, base, pipeline, policy.baseline_samples)
            if policy.baseline_samples
            else {}
        )
    baselines = cache[key]
    attempts = {}
    result = []
    for job in sorted(jobs, key=lambda item: int(item["id"])):
        identity = (job["name"], job.get("stage"))
        attempts[identity] = attempts.get(identity, 0) + 1
        baseline = baselines.get(identity)
        execution_limit = policy.duration_budgets.get(
            job["name"], policy.duration_budgets.get("*")
        )
        if execution_limit is None and baseline:
            execution_limit = baseline["seconds"] * policy.baseline_multiplier
        wait_limit = policy.wait_budgets.get(job["name"], policy.wait_budgets.get("*"))
        result.append(
            {
                "id": job["id"],
                "name": job["name"],
                "stage": job.get("stage"),
                "status": job["status"],
                "attempt": attempts[identity],
                "allow_failure": job.get("allow_failure", False),
                "failure_reason": job.get("failure_reason"),
                "url": job.get("web_url"),
                **measurements(job, now),
                "baseline": baseline,
                "execution_budget_seconds": execution_limit,
                "wait_budget_seconds": wait_limit,
            }
        )
    return {
        "pipeline": pipeline["id"],
        "status": pipeline["status"],
        "url": pipeline.get("web_url"),
        "jobs": result,
    }


def job_events(before, after, alerted):
    previous = (
        {job["id"]: job for job in (before or {}).get("jobs", [])}
        if before and before["pipeline"] == after["pipeline"]
        else {}
    )
    output = []
    if before is not None and before["pipeline"] != after["pipeline"]:
        output.append(
            {
                "event": "job-pipeline-changed",
                "previous_pipeline": before["pipeline"],
                "pipeline": after["pipeline"],
            }
        )
    for job in after["jobs"]:
        old = previous.get(job["id"])
        if old is None or old["status"] != job["status"]:
            status = job["status"]
            event = (
                "job-failed"
                if status == "failed"
                else "job-finished"
                if status in TERMINAL
                else "job-started"
                if status == "running"
                else "job-state"
            )
            output.append({"event": event, "pipeline": after["pipeline"], "job": job})
        reasons = []
        for metric, budget in (
            ("execution_seconds", "execution_budget_seconds"),
            ("pre_start_seconds", "wait_budget_seconds"),
        ):
            if (
                job[metric] is not None
                and job[budget] is not None
                and job[metric] > job[budget]
            ):
                reasons.append(
                    {
                        "metric": metric,
                        "observed_seconds": job[metric],
                        "budget_seconds": job[budget],
                    }
                )
        key = (after["pipeline"], job["id"])
        if reasons and key not in alerted:
            alerted.add(key)
            output.append(
                {
                    "event": "job-overrun",
                    "pipeline": after["pipeline"],
                    "job": job,
                    "reasons": reasons,
                }
            )
    return sorted(
        output,
        key=lambda event: {"job-failed": 0, "job-overrun": 1}.get(event["event"], 2),
    )
