from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pydantic
import structlog

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)

logger = structlog.get_logger(__name__)

github_connector = connector("github_app")
slack_connector = connector("slack")

DEFAULT_SLACK_USER_ID = "U0CGP0K9CKX"
_MAX_PAGES = 5
_PAGE_SIZE = 100
_TITLE_MAX_LENGTH = 90

SEARCH_FIELDS = ["number", "title", "state", "html_url", "user", "closed_at"]
OPEN_FIELDS = [
    "number",
    "title",
    "state",
    "draft",
    "html_url",
    "user",
    "requested_reviewers",
    "created_at",
]


class PrDigestInput(pydantic.BaseModel):
    repo: str
    since: str
    slack_user_id: str = DEFAULT_SLACK_USER_ID


class PrSummary(pydantic.BaseModel):
    number: int
    title: str
    author: str
    url: str
    requested_reviewers: list[str] = pydantic.Field(default_factory=list)


class PrDigestResult(pydantic.BaseModel):
    repo: str
    since: str
    merged_count: int
    awaiting_review_count: int
    slack_channel_id: str
    message: str


def parse_repo(repo: str) -> tuple[str, str]:
    parts = repo.strip().split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Formato de repositório inválido: {repo!r}. Use 'owner/repo', por exemplo 'mistralai/mistralai-python'."
        )
    return parts[0], parts[1]


def parse_since(since: str) -> date:
    try:
        return date.fromisoformat(since.strip())
    except ValueError as exc:
        raise ValueError(f"Data inválida: {since!r}. Use o formato 'YYYY-MM-DD', por exemplo '2025-11-01'.") from exc


def _as_pr_list(response: Any) -> list[dict[str, Any]]:
    if response is None:
        return []
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        for key in ("items", "results", "pull_requests", "data"):
            value = response.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _author_of(pr: dict[str, Any]) -> str:
    user = pr.get("user")
    if isinstance(user, dict):
        return str(user.get("login") or "desconhecido")
    if isinstance(user, str):
        return user
    return "desconhecido"


def _summary_of(pr: dict[str, Any]) -> PrSummary | None:
    number = pr.get("number")
    url = pr.get("html_url") or f"https://github.com/{pr.get('repo', '')}/pull/{number}"
    if not isinstance(number, int) or not url:
        return None
    title = str(pr.get("title") or f"PR #{number}")
    reviewers = pr.get("requested_reviewers") or []
    reviewer_logins = [
        reviewer.get("login") if isinstance(reviewer, dict) else str(reviewer)
        for reviewer in reviewers
        if reviewer
    ]
    return PrSummary(
        number=number,
        title=title,
        author=_author_of(pr),
        url=str(url),
        requested_reviewers=sorted(reviewer_logins),
    )


def _merged_since(prs: list[dict[str, Any]], since: date) -> list[dict[str, Any]]:
    kept = []
    since_start = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
    for pr in prs:
        if pr.get("merged") is False:
            continue
        closed_at = pr.get("closed_at") or pr.get("merged_at")
        if not closed_at:
            kept.append(pr)
            continue
        try:
            merged_at = datetime.fromisoformat(str(closed_at).replace("Z", "+00:00"))
        except ValueError:
            kept.append(pr)
            continue
        if merged_at.tzinfo is None:
            merged_at = merged_at.replace(tzinfo=timezone.utc)
        if merged_at >= since_start:
            kept.append(pr)
    return kept


def _one_line(summary: PrSummary) -> str:
    title = summary.title if len(summary.title) <= _TITLE_MAX_LENGTH else summary.title[: _TITLE_MAX_LENGTH - 1] + "…"
    line = f"• #{summary.number} — {title} (_{summary.author}_) — {summary.url}"
    if summary.requested_reviewers:
        mentions = ", ".join(f"@{login}" for login in summary.requested_reviewers)
        line += f" — aguardando: {mentions}"
    return line


def build_digest_message(
    owner: str,
    repo: str,
    since: str,
    merged: list[PrSummary],
    awaiting_review: list[PrSummary],
) -> str:
    lines = [f":clipboard: *Resumo de PRs — `{owner}/{repo}`* (desde {since})", ""]
    lines.append(f":white_check_mark: *Mesclados ({len(merged)})*")
    if merged:
        lines.extend(_one_line(summary) for summary in merged)
    else:
        lines.append("Nenhum PR mesclado nesse período.")
    lines.append("")
    lines.append(f":eyes: *Aguardando revisão ({len(awaiting_review)})*")
    if awaiting_review:
        lines.extend(_one_line(summary) for summary in awaiting_review)
    else:
        lines.append("Nenhum PR aberto aguardando revisão.")
    return "\n".join(lines)


@workflows.activity(name="fetch-merged-pull-requests")
async def fetch_merged_pull_requests(
    owner: str,
    repo: str,
    since: str,
    github: ToolCallClient = Depends(github_connector),
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        response = await github.call_tool(
            tool_name="search_pull_requests",
            arguments={
                "query": f"repo:{owner}/{repo} is:pr is:merged merged:>={since}",
                "owner": owner,
                "repo": repo,
                "sort": "created",
                "order": "desc",
                "perPage": _PAGE_SIZE,
                "page": page,
                "fields": SEARCH_FIELDS,
            },
        )
        items = _as_pr_list(response)
        results.extend(items)
        if len(items) < _PAGE_SIZE:
            break
    return results


@workflows.activity(name="fetch-open-pull-requests")
async def fetch_open_pull_requests(
    owner: str,
    repo: str,
    github: ToolCallClient = Depends(github_connector),
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        response = await github.call_tool(
            tool_name="list_pull_requests",
            arguments={
                "owner": owner,
                "repo": repo,
                "state": "open",
                "sort": "created",
                "direction": "desc",
                "perPage": _PAGE_SIZE,
                "page": page,
                "fields": OPEN_FIELDS,
            },
        )
        items = _as_pr_list(response)
        results.extend(items)
        if len(items) < _PAGE_SIZE:
            break
    return results


@workflows.activity(name="post-slack-digest")
async def post_slack_digest(
    channel_id: str,
    message: str,
    slack: ToolCallClient = Depends(slack_connector),
) -> Any:
    return await slack.call_tool(
        tool_name="slack_send_message",
        arguments={"channel_id": channel_id, "message": message},
    )


@workflows.workflow.define(name="github-pr-digest", on_behalf_of=True)
@uses_connectors(github_connector, slack_connector)
class GitHubPrDigestWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, digest_input: PrDigestInput) -> PrDigestResult:
        owner, repo = parse_repo(digest_input.repo)
        since_date = parse_since(digest_input.since)

        merged_prs = _merged_since(await fetch_merged_pull_requests(owner, repo, digest_input.since), since_date)
        open_prs = await fetch_open_pull_requests(owner, repo)

        merged_summaries = [summary for summary in map(_summary_of, merged_prs) if summary]
        open_prs_by_number = {pr["number"]: pr for pr in open_prs if isinstance(pr.get("number"), int)}
        awaiting_summaries = [
            summary
            for summary in map(_summary_of, open_prs)
            if summary and not open_prs_by_number.get(summary.number, {}).get("draft", False)
        ]

        message = build_digest_message(
            owner,
            repo,
            digest_input.since,
            merged_summaries,
            awaiting_summaries,
        )
        await post_slack_digest(digest_input.slack_user_id, message)

        return PrDigestResult(
            repo=f"{owner}/{repo}",
            since=digest_input.since,
            merged_count=len(merged_summaries),
            awaiting_review_count=len(awaiting_summaries),
            slack_channel_id=digest_input.slack_user_id,
            message=message,
        )
