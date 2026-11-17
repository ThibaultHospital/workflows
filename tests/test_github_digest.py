from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from worker.workflows.github_digest import (
    DigestInput,
    DigestResult,
    GitHubRepoDigestScheduledWorkflow,
    build_digest,
    find_self_chat,
    find_self_chat_graph,
    format_pr_line,
    parse_github_timestamp,
    parse_since,
)


def test_parse_since_formats():
    assert parse_since("05/10/2026") == datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert parse_since("2026-10-05") == datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert parse_since(" 2026-10-05 ") == datetime(2026, 10, 5, tzinfo=timezone.utc)


def test_parse_since_rejects_garbage():
    with pytest.raises(ValueError):
        parse_since("not a date")


def test_parse_github_timestamp():
    assert parse_github_timestamp("2026-11-01T12:00:00Z") == datetime(
        2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc
    )


def test_format_pr_line():
    line = format_pr_line(
        {"number": 7, "title": "Add CI", "author": "alice", "url": "https://github.com/o/r/pull/7"},
        "Ajoute un pipeline CI",
    )
    assert "#7 Add CI" in line
    assert "Ajoute un pipeline CI" in line
    assert "alice" in line
    assert "https://github.com/o/r/pull/7" in line


def test_build_digest_sections():
    merged = [
        {"number": 1, "title": "Fix header", "author": "bob", "url": "u1"},
        {"number": 2, "title": "Add footer", "author": "bob", "url": "u2"},
    ]
    pending = [{"number": 3, "title": "WIP", "author": "carol", "url": "u3"}]
    summaries = {"merged": {"1": "Corrige l'en-tête"}, "pending": {}}
    digest = build_digest("o/r", "05/10/2026", merged, pending, summaries)
    assert "📊 Activité du dépôt o/r depuis le 05/10/2026" in digest
    assert "✅ Pull requests fusionnées (2)" in digest
    assert "⏳ En attente de revue (1)" in digest
    assert "Corrige l'en-tête" in digest
    assert "u3" in digest


def test_build_digest_empty():
    digest = build_digest("o/r", "05/10/2026", [], [], {"merged": {}, "pending": {}})
    assert "Aucune pull request fusionnée" in digest
    assert "Aucune pull request en attente" in digest


def test_find_self_chat_prefers_single_member_one_on_one():
    chats = {
        "chats": [
            {"id": "group", "chat_type": "group", "members": [{"display_name": "a"}, {"display_name": "b"}]},
            {"id": "self", "chat_type": "oneOnOne", "members": [{"display_name": "me"}]},
            {"id": "duo", "chat_type": "oneOnOne", "members": [{"display_name": "me"}, {"display_name": "you"}]},
        ]
    }
    assert find_self_chat(chats) == "self"


def test_find_self_chat_accepts_raw_list():
    chats = [{"id": "self", "chat_type": "oneOnOne", "members": []}]
    assert find_self_chat(chats) == "self"


def test_find_self_chat_raises_when_absent():
    with pytest.raises(ValueError, match="self-chat not found"):
        find_self_chat({"chats": [{"id": "duo", "chat_type": "oneOnOne", "members": [{}, {}]}]})


def test_find_self_chat_graph():
    chats = [
        {"id": "duo", "chatType": "oneOnOne", "members": [{"id": "me"}, {"id": "you"}]},
        {"id": "self", "chatType": "oneOnOne", "members": [{"id": "me"}]},
    ]
    assert find_self_chat_graph("me", chats) == "self"


async def test_scheduled_workflow_dry_run_end_to_end(temporal_env):
    from mistralai.workflows.testing import create_test_worker

    merged_pr = {
        "number": 1,
        "title": "Fix header",
        "user": {"login": "bob"},
        "html_url": "https://github.com/o/r/pull/1",
        "merged_at": "2026-11-01T10:00:00Z",
        "created_at": "2026-10-20T10:00:00Z",
        "body": "fixes the header",
    }
    open_pr = {
        "number": 2,
        "title": "Add footer",
        "user": {"login": "carol"},
        "html_url": "https://github.com/o/r/pull/2",
        "created_at": "2026-11-10T10:00:00Z",
        "body": "wip",
    }

    async def fake_github_get_paginated(url, params, pages=3):
        return [merged_pr]

    async def fake_github_get(url, params=None):
        return {"items": [open_pr]}

    async def fake_llm(merged, pending):
        return {"merged": {"1": "Corrige l'en-tête"}, "pending": {"2": "Ajoute un pied de page"}}

    with (
        patch("worker.workflows.github_digest._github_get_paginated", side_effect=fake_github_get_paginated),
        patch("worker.workflows.github_digest._github_get", side_effect=fake_github_get),
        patch("worker.workflows.github_digest._llm_one_line_summaries", side_effect=fake_llm),
    ):
        async with create_test_worker(temporal_env, [GitHubRepoDigestScheduledWorkflow]):
            result = await GitHubRepoDigestScheduledWorkflow().run(
                {"repo": "o/r", "since": "05/10/2026", "dry_run": True}
            )

    if isinstance(result, dict):
        result = DigestResult(**result)
    assert result.merged_count == 1
    assert result.pending_count == 1
    assert result.posted is False
    assert "Corrige l'en-tête" in result.digest
    assert "Ajoute un pied de page" in result.digest
