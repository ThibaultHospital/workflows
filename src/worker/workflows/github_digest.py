"""GitHub repository activity digest workflows.

Two workflows share the same pipeline:

- ``github-repo-digest``: on-behalf-of workflow, triggered manually by a user.
  It reads the user's Teams chats and posts the digest in the user's
  self-chat (the conversation the user has with themselves).
- ``github-repo-digest-scheduled``: regular workflow for unattended cron runs.
  It posts through the Microsoft Graph API with application-only credentials
  read from the ``TEAMS_TENANT_ID``, ``TEAMS_CLIENT_ID``, ``TEAMS_CLIENT_SECRET``
  and ``TEAMS_SENDER_UPN`` environment variables (workspace secrets bound to
  the deployment).

Both accept the repository (``owner/repo``) and a ``since`` date
(``DD/MM/YYYY`` or ``YYYY-MM-DD``) as input.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import pydantic
import structlog

import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.client import get_mistral_client
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)

logger = structlog.get_logger(__name__)

teams_connector = connector("teams")

DEFAULT_REPO = "ThibaultHospital/cv"
DEFAULT_SINCE = "05/10/2026"

GITHUB_API_BASE = "https://api.github.com"
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
SUMMARY_MODEL = "mistral-small-latest"


class DigestInput(pydantic.BaseModel):
    repo: str = DEFAULT_REPO
    since: str = DEFAULT_SINCE
    dry_run: bool = False


class DigestResult(pydantic.BaseModel):
    repo: str
    since: str
    merged_count: int
    pending_count: int
    posted: bool
    digest: str


def parse_since(raw: str) -> datetime:
    value = raw.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date {raw!r}: expected DD/MM/YYYY or YYYY-MM-DD")


def parse_github_timestamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _github_get(url: str, params: dict[str, Any] | None = None) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, params=params, headers=_github_headers())
        response.raise_for_status()
        return response.json()


def _pr_brief(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "author": (pr.get("user") or {}).get("login"),
        "url": pr.get("html_url"),
        "body": (pr.get("body") or "")[:1000],
        "merged_at": pr.get("merged_at"),
        "created_at": pr.get("created_at"),
    }


async def _github_get_paginated(url: str, params: dict[str, Any], pages: int = 3) -> list[dict[str, Any]]:
    import httpx

    items: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for page in range(1, pages + 1):
            response = await client.get(url, params={**params, "page": page}, headers=_github_headers())
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            items.extend(batch)
            if len(batch) < int(params.get("per_page", 100)):
                break
    return items


@workflows.activity(name="fetch-merged-pull-requests")
async def fetch_merged_pull_requests(repo: str, since: str) -> list[dict[str, Any]]:
    since_dt = parse_since(since)
    pulls = await _github_get_paginated(
        f"{GITHUB_API_BASE}/repos/{repo.strip('/')}/pulls",
        {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 100},
    )
    merged = [
        pr
        for pr in pulls
        if pr.get("merged_at") and parse_github_timestamp(pr["merged_at"]) >= since_dt
    ]
    return [_pr_brief(pr) for pr in merged]


@workflows.activity(name="fetch-pull-requests-awaiting-review")
async def fetch_pull_requests_awaiting_review(repo: str) -> list[dict[str, Any]]:
    data = await _github_get(
        f"{GITHUB_API_BASE}/search/issues",
        {
            "q": f"repo:{repo.strip('/')} is:pr is:open review:required",
            "per_page": 100,
            "sort": "created",
            "order": "desc",
        },
    )
    return [_pr_brief(pr) for pr in data.get("items", [])]


def _extract_json_object(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in model response")
    return json.loads(match.group(0))


async def _llm_one_line_summaries(merged: list[dict[str, Any]], pending: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    client = get_mistral_client(use_executor_credentials=False)
    payload = {"merged": merged, "pending": pending}
    response = await client.chat.complete_async(
        model=SUMMARY_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Tu résumes des pull requests GitHub. Pour chaque pull request, écris UN seul résumé "
                    "d'UNE ligne en français, factuel et concis (max ~120 caractères). "
                    'Réponds uniquement avec un JSON: {"merged": {"<numéro>": "<résumé>", ...}, '
                    '"pending": {"<numéro>": "<résumé>", ...}}'
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.2,
    )
    content = response.choices[0].message.content or "{}"
    parsed = _extract_json_object(content)
    return {
        "merged": {str(k): str(v) for k, v in parsed.get("merged", {}).items()},
        "pending": {str(k): str(v) for k, v in parsed.get("pending", {}).items()},
    }


@workflows.activity(name="summarize-pull-requests")
async def summarize_pull_requests(
    merged: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    try:
        return await _llm_one_line_summaries(merged, pending)
    except Exception:
        logger.exception("LLM summarization failed; falling back to PR titles")
        return {
            "merged": {str(pr["number"]): str(pr["title"]) for pr in merged},
            "pending": {str(pr["number"]): str(pr["title"]) for pr in pending},
        }


def format_pr_line(pr: dict[str, Any], summary: str) -> str:
    title = str(pr.get("title") or "").strip()
    author = pr.get("author") or "auteur inconnu"
    url = pr.get("url") or ""
    return f"• #{pr.get('number')} {title} — {summary} (par {author}) {url}".strip()


def build_digest(
    repo: str,
    since: str,
    merged: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    summaries: dict[str, dict[str, str]],
) -> str:
    merged_summaries = summaries.get("merged", {})
    pending_summaries = summaries.get("pending", {})

    lines = [f"📊 Activité du dépôt {repo} depuis le {since}", ""]

    lines.append(f"✅ Pull requests fusionnées ({len(merged)})")
    if merged:
        lines.extend(format_pr_line(pr, merged_summaries.get(str(pr.get("number")), str(pr.get("title")))) for pr in merged)
    else:
        lines.append("• Aucune pull request fusionnée sur cette période.")
    lines.append("")

    lines.append(f"⏳ En attente de revue ({len(pending)})")
    if pending:
        lines.extend(format_pr_line(pr, pending_summaries.get(str(pr.get("number")), str(pr.get("title")))) for pr in pending)
    else:
        lines.append("• Aucune pull request en attente de revue.")

    return "\n".join(lines)


def find_self_chat(chats: Any) -> str:
    if isinstance(chats, dict):
        chats = chats.get("chats", [])
    if not isinstance(chats, list):
        raise ValueError("unexpected list_chats response shape")
    for chat in chats:
        if not isinstance(chat, dict):
            continue
        members = chat.get("members") or []
        if chat.get("chat_type") == "oneOnOne" and len(members) <= 1:
            return str(chat["id"])
    raise ValueError(
        "self-chat not found: no one-on-one chat with a single member in the user's Teams chats"
    )


@workflows.activity(name="post-digest-to-teams-self-chat")
async def post_digest_to_teams_self_chat(
    content: str,
    teams: ToolCallClient = Depends(teams_connector),
) -> str:
    chats = await teams.call_tool(tool_name="list_chats", arguments={"limit": 50})
    chat_id = find_self_chat(chats)
    result = await teams.call_tool(
        tool_name="post_chat_message",
        arguments={"chat_id": chat_id, "content": content},
    )
    if isinstance(result, dict) and "message_id" in result:
        return str(result["message_id"])
    return str(result)


async def _graph_request(
    method: str,
    url: str,
    token: str,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
            params=params,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()


def _graph_app_credentials() -> tuple[str, str, str, str]:
    tenant = os.environ.get("TEAMS_TENANT_ID", "").strip()
    client_id = os.environ.get("TEAMS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TEAMS_CLIENT_SECRET", "").strip()
    sender_upn = os.environ.get("TEAMS_SENDER_UPN", "").strip()
    missing = [
        name
        for name, value in (
            ("TEAMS_TENANT_ID", tenant),
            ("TEAMS_CLIENT_ID", client_id),
            ("TEAMS_CLIENT_SECRET", client_secret),
            ("TEAMS_SENDER_UPN", sender_upn),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Teams Graph app-only credentials are not configured: bind the workspace secrets "
            + ", ".join(missing)
            + " to the deployment to enable scheduled runs"
        )
    return tenant, client_id, client_secret, sender_upn


async def _graph_app_token(tenant: str, client_id: str, client_secret: str) -> str:
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        response.raise_for_status()
        return str(response.json()["access_token"])


def find_self_chat_graph(user_id: str, chats: list[dict[str, Any]]) -> str:
    for chat in chats:
        members = chat.get("members") or []
        member_ids = [m.get("id") for m in members if isinstance(m, dict)]
        if chat.get("chatType") == "oneOnOne" and member_ids == [user_id]:
            return str(chat["id"])
    raise ValueError("self-chat not found via Graph API for the configured sender")


@workflows.activity(name="post-digest-to-teams-graph")
async def post_digest_to_teams_graph(content: str) -> str:
    tenant, client_id, client_secret, sender_upn = _graph_app_credentials()
    token = await _graph_app_token(tenant, client_id, client_secret)
    user = await _graph_request("GET", f"{GRAPH_API_BASE}/users/{sender_upn}", token)
    chats = await _graph_request(
        "GET",
        f"{GRAPH_API_BASE}/users/{sender_upn}/chats",
        token,
        params={"$filter": "chatType eq 'oneOnOne'", "$expand": "members"},
    )
    chat_id = find_self_chat_graph(str(user["id"]), chats.get("value", []))
    message = await _graph_request(
        "POST",
        f"{GRAPH_API_BASE}/chats/{chat_id}/messages",
        token,
        json_body={"body": {"contentType": "text", "content": content}},
    )
    return str(message.get("id", ""))


async def _collect_digest(input: DigestInput) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    merged = await fetch_merged_pull_requests(input.repo, input.since)
    pending = await fetch_pull_requests_awaiting_review(input.repo)
    summaries = await summarize_pull_requests(merged, pending)
    digest = build_digest(input.repo, input.since, merged, pending, summaries)
    return digest, merged, pending


@workflows.workflow.define(
    name="github-repo-digest",
    on_behalf_of=True,
    workflow_display_name="GitHub repo digest ( Teams self-chat )",
    workflow_description=(
        "Résume les pull requests fusionnées depuis une date et celles en attente de revue "
        "d'un dépôt GitHub, puis publie le digest dans la conversation Teams de l'utilisateur."
    ),
)
@uses_connectors(teams_connector)
class GitHubRepoDigestWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: DigestInput) -> DigestResult:
        digest, merged, pending = await _collect_digest(input)
        posted = False
        if not input.dry_run:
            await post_digest_to_teams_self_chat(digest)
            posted = True
        return DigestResult(
            repo=input.repo,
            since=input.since,
            merged_count=len(merged),
            pending_count=len(pending),
            posted=posted,
            digest=digest,
        )


@workflows.workflow.define(
    name="github-repo-digest-scheduled",
    workflow_display_name="GitHub repo digest (scheduled)",
    workflow_description=(
        "Version non-attachée pour exécution planifiée : publie le digest via l'API Microsoft "
        "Graph avec des identifiants application (secrets workspace TEAMS_*)."
    ),
)
class GitHubRepoDigestScheduledWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: DigestInput) -> DigestResult:
        digest, merged, pending = await _collect_digest(input)
        posted = False
        if not input.dry_run:
            await post_digest_to_teams_graph(digest)
            posted = True
        return DigestResult(
            repo=input.repo,
            since=input.since,
            merged_count=len(merged),
            pending_count=len(pending),
            posted=posted,
            digest=digest,
        )
