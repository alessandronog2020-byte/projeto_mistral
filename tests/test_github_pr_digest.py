import uuid

import pytest
from mistralai.workflows.plugins.mistralai.connectors.client import ToolCallClient

from worker.workflows.github_pr_digest import (
    GitHubPrDigestWorkflow,
    PrDigestInput,
    _as_pr_list,
    _merged_since,
    _one_line,
    _summary_of,
    build_digest_message,
    parse_repo,
    parse_since,
)

TASK_QUEUE = "test-task-queue"

MERGED_SEARCH_RESPONSE = [
    {
        "number": 12,
        "title": "Fix login redirect",
        "state": "closed",
        "html_url": "https://github.com/acme/app/pull/12",
        "user": {"login": "alice"},
        "closed_at": "2025-11-03T10:00:00Z",
    },
    {
        "number": 10,
        "title": "Old PR before the date",
        "state": "closed",
        "html_url": "https://github.com/acme/app/pull/10",
        "user": {"login": "bob"},
        "closed_at": "2025-10-20T10:00:00Z",
    },
]

OPEN_LIST_RESPONSE = [
    {
        "number": 15,
        "title": "Refactor auth module",
        "state": "open",
        "draft": False,
        "html_url": "https://github.com/acme/app/pull/15",
        "user": {"login": "carol"},
        "requested_reviewers": [{"login": "dave"}],
        "created_at": "2025-11-04T09:00:00Z",
    },
    {
        "number": 14,
        "title": "Draft: experiment",
        "state": "open",
        "draft": True,
        "html_url": "https://github.com/acme/app/pull/14",
        "user": {"login": "erin"},
        "requested_reviewers": [],
        "created_at": "2025-11-04T08:00:00Z",
    },
]


def test_parse_repo_accepts_owner_and_name():
    assert parse_repo("acme/app") == ("acme", "app")


def test_parse_repo_rejects_invalid_format():
    with pytest.raises(ValueError, match="owner/repo"):
        parse_repo("just-a-name")


def test_parse_since_accepts_iso_date():
    assert parse_since("2025-11-01").isoformat() == "2025-11-01"


def test_parse_since_rejects_bad_format():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_since("01/11/2025")


def test_as_pr_list_handles_list_and_wrappers():
    assert _as_pr_list(None) == []
    assert _as_pr_list([{"number": 1}]) == [{"number": 1}]
    assert _as_pr_list({"items": [{"number": 2}]}) == [{"number": 2}]
    assert _as_pr_list({"unexpected": "shape"}) == []


def test_summary_of_extracts_fields_and_reviewers():
    summary = _summary_of(OPEN_LIST_RESPONSE[0])
    assert summary.number == 15
    assert summary.author == "carol"
    assert summary.requested_reviewers == ["dave"]


def test_summary_of_requires_number_and_url():
    assert _summary_of({"title": "no number"}) is None


def test_merged_since_filters_by_date():
    kept = _merged_since(MERGED_SEARCH_RESPONSE, parse_since("2025-11-01"))
    assert [pr["number"] for pr in kept] == [12]


def test_one_line_contains_number_title_author_and_link():
    summary = _summary_of(MERGED_SEARCH_RESPONSE[0])
    line = _one_line(summary)
    assert line.startswith("• #12 — Fix login redirect (_alice_) — https://")
    assert "aguardando:" not in line


def test_one_line_truncates_long_titles():
    summary = _summary_of({**MERGED_SEARCH_RESPONSE[0], "title": "x" * 200})
    assert len(_one_line(summary)) < 200


def test_build_digest_message_has_both_sections():
    merged = [s for s in map(_summary_of, MERGED_SEARCH_RESPONSE) if s]
    awaiting = [s for s in map(_summary_of, OPEN_LIST_RESPONSE) if s]
    message = build_digest_message("acme", "app", "2025-11-01", merged, awaiting)
    assert "Mesclados (2)" in message
    assert "Aguardando revisão (2)" in message
    assert "aguardando: @dave" in message


def test_build_digest_message_empty_state():
    message = build_digest_message("acme", "app", "2025-11-01", [], [])
    assert "Nenhum PR mesclado" in message
    assert "Nenhum PR aberto aguardando revisão" in message


async def test_workflow_posts_digest_to_slack(temporal_env, monkeypatch):
    calls = []

    async def fake_call_tool(self, tool_name, arguments=None):
        calls.append((tool_name, arguments))
        if tool_name == "search_pull_requests":
            return MERGED_SEARCH_RESPONSE
        if tool_name == "list_pull_requests":
            return OPEN_LIST_RESPONSE
        if tool_name == "slack_send_message":
            return {"ok": True}
        raise AssertionError(f"unexpected tool {tool_name}")

    monkeypatch.setattr(ToolCallClient, "call_tool", fake_call_tool)

    from mistralai.workflows.testing import create_test_worker

    from worker.workflows.github_pr_digest import (
        fetch_merged_pull_requests,
        fetch_open_pull_requests,
        post_slack_digest,
    )

    async with create_test_worker(
        temporal_env,
        [GitHubPrDigestWorkflow],
        activities=[fetch_merged_pull_requests, fetch_open_pull_requests, post_slack_digest],
        task_queue=TASK_QUEUE,
    ):
        handle = await temporal_env.client.start_workflow(
            GitHubPrDigestWorkflow.run,
            PrDigestInput(repo="acme/app", since="2025-11-01"),
            id=f"github-pr-digest-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        result = await handle.result()

    assert result["repo"] == "acme/app"
    assert result["merged_count"] == 1
    assert result["awaiting_review_count"] == 1
    assert result["slack_channel_id"] == "U0CGP0K9CKX"
    tool_names = [name for name, _ in calls]
    assert tool_names == ["search_pull_requests", "list_pull_requests", "slack_send_message"]
    sent = calls[-1][1]
    assert sent["channel_id"] == "U0CGP0K9CKX"
    assert "Mesclados (1)" in sent["message"]
