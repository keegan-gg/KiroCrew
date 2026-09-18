"""Exercise actor/event routing and ensure malformed fleet copies are detected."""

from __future__ import annotations

import json
import re
from test import test_ci_fleet_routing_expression_parity as parity

import pytest
import yaml

_REPO = "kirodotdev/KiroCrew"
_FORK = "contributor/KiroCrew"
_ACTORS = '["101", "202"]'
_EXPRESSIONS = {
    "push-pr": parity._CANONICAL_ROUTING_EXPR,
    "push": parity._CANONICAL_PUSH_EXPR,
    "push-dispatch": parity._CANONICAL_DISPATCH_EXPR,
}


def _evaluate(expression: str, context: dict[str, object]) -> str:
    """Evaluate only the pinned expression subset, with GitHub's operand returns.

    For these concrete string/array inputs, Python and/or, equality, array
    membership and str.format match the corresponding Actions operations.
    This is not a general Actions interpreter or remote execution evidence.
    """
    assert expression in _EXPRESSIONS.values()
    source = expression.removeprefix("${{ ").removesuffix(" }}")
    source = re.sub(r"(?:github|vars)\.[\w.]+", lambda m: repr(context[m[0]]), source)
    source = source.replace("&&", " and ").replace("||", " or ")
    return eval(
        source,
        {"__builtins__": {}},
        {
            "fromJSON": json.loads,
            "contains": lambda values, value: value in values,
            "format": lambda template, *args: template.format(*args),
        },
    )


@pytest.mark.parametrize("policy", _EXPRESSIONS)
@pytest.mark.parametrize(
    "event,repo,head,actor,actors,eligible_policies",
    [
        ("push", _REPO, "", "101", _ACTORS, {"push-pr", "push", "push-dispatch"}),
        ("push", _REPO, "", "202", _ACTORS, {"push-pr", "push", "push-dispatch"}),
        ("pull_request", _REPO, _REPO, "101", _ACTORS, {"push-pr"}),
        ("workflow_dispatch", _REPO, "", "101", _ACTORS, {"push-dispatch"}),
        ("push", _FORK, "", "101", _ACTORS, set()),
        ("pull_request", _REPO, _FORK, "101", _ACTORS, set()),
        ("pull_request", _REPO, "", "101", _ACTORS, set()),
        ("pull_request", _FORK, _FORK, "101", _ACTORS, set()),
        ("push", _REPO, "", "999", _ACTORS, set()),
        ("pull_request", _REPO, _REPO, "999", _ACTORS, set()),
        ("workflow_dispatch", _REPO, "", "999", _ACTORS, set()),
        ("push", _REPO, "", "10", _ACTORS, set()),
        ("push", _REPO, "", "1010", _ACTORS, set()),
        ("push", _REPO, "", "", _ACTORS, set()),
        ("push", _REPO, "", "101", "", set()),
        ("pull_request", _REPO, _REPO, "101", "", set()),
        ("workflow_dispatch", _REPO, "", "101", "", set()),
        ("push", _REPO, "", "101", "[]", set()),
        ("schedule", _REPO, "", "101", _ACTORS, set()),
        ("issues", _REPO, "", "101", _ACTORS, set()),
        ("issue_comment", _REPO, "", "101", _ACTORS, set()),
        ("pull_request_target", _REPO, _REPO, "101", _ACTORS, set()),
        ("workflow_run", _REPO, _REPO, "101", _ACTORS, set()),
        ("workflow_call", _REPO, "", "101", _ACTORS, set()),
        ("merge_group", _REPO, "", "101", _ACTORS, set()),
    ],
)
def test_actor_and_event_truth_table(policy, event, repo, head, actor, actors, eligible_policies):
    context = {
        "github.repository": repo,
        "github.event_name": event,
        "github.event.pull_request.head.repo.full_name": head,
        "github.actor_id": actor,
        "vars.CODEBUILD_ACTOR_IDS": actors,
        "github.run_id": "456",
        "github.run_attempt": "3",
    }
    expected = (
        "codebuild-kirocrew-gha-linux-456-3" if policy in eligible_policies else "ubuntu-latest"
    )
    assert _evaluate(_EXPRESSIONS[policy], context) == expected


@pytest.mark.parametrize("shape", ["no-actor", "wrong-fork", "literal-array", "folded"])
def test_parity_detector_rejects_corrupted_routes(tmp_path, monkeypatch, shape):
    expression = parity._CANONICAL_ROUTING_EXPR
    if shape == "wrong-fork":
        expression = expression.replace("head.repo.full_name ==", "head.repo.full_name !=")
    else:
        expression = expression.replace(parity._ACTOR_PREDICATE + " && ", "")
    value = ["codebuild-kirocrew-gha-linux-456-3"] if shape == "literal-array" else expression
    path = tmp_path / "fast-gate.yml"
    if shape == "folded":
        path.write_text(f"jobs:\n  gate:\n    runs-on: >-\n      {value}\n", encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump({"jobs": {"gate": {"runs-on": value}}}), encoding="utf-8")
    monkeypatch.setattr(parity, "_all_workflow_files", lambda: [path])
    with pytest.raises(AssertionError, match="fleet routing expression drift"):
        parity.test_every_copy_of_the_routing_expression_matches_the_canonical_one()


@pytest.mark.parametrize(
    "filename,job_id",
    [
        ("code-review.yml", "autosde-rules"),
        ("code-review.yml", "inclusive-language"),
        ("code-review.yml", "pr-hygiene"),
        ("pr-merge-conflict-label.yml", "label"),
        ("build-wheel.yml", "build-wheel"),
        ("dependency-vulnerability.yml", "audit-production-dependencies"),
    ],
)
def test_additional_routes_have_no_container_or_cloud_credential_steps(filename, job_id):
    path = parity._WORKFLOWS_DIR / filename
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    job = workflow["jobs"][job_id]
    assert job["runs-on"] == parity._expected_inline_expression(filename)
    assert "container" not in job
    assert "services" not in job
    assert job.get("permissions", workflow["permissions"]).get("id-token") != "write"
    for step in job["steps"]:
        assert "configure-aws-credentials" not in step.get("uses", "")
        assert "claude-code-action" not in step.get("uses", "")


@pytest.mark.parametrize("filename", ["build-wheel.yml", "dependency-vulnerability.yml"])
def test_reusable_routes_keep_schedule_callers_hosted(filename):
    workflow = yaml.safe_load((parity._WORKFLOWS_DIR / filename).read_text(encoding="utf-8"))
    assert "workflow_call" in workflow.get("on", workflow.get(True))
    for caller in ("nightly.yml", "release.yml"):
        parent = yaml.safe_load((parity._WORKFLOWS_DIR / caller).read_text(encoding="utf-8"))
        assert any(
            job.get("uses") == f"./.github/workflows/{filename}" for job in parent["jobs"].values()
        ), f"{caller} must exercise the reusable route"
    for job in workflow["jobs"].values():
        assert job["runs-on"] == parity._CANONICAL_PUSH_EXPR
