"""Offline tests for the GitHub integration (no network).

Covers the encrypted credential roundtrip, the agent tool's guard paths, and
schema/tag/dispatch parity for `manage_github`.
"""

import json

import pytest


def test_token_roundtrip_encrypted(tmp_path, monkeypatch):
    from services.github import save_token, get_token, delete_token
    import core.database as db

    owner = "gh-test-user"
    save_token(owner, "ghp_secretvalue_123", auth_mode="pat", login="octocat")
    assert get_token(owner) == "ghp_secretvalue_123"

    sess = db.SessionLocal()
    row = sess.query(db.ProviderAuthSession).filter_by(provider="github", owner=owner).first()
    assert row.provider == "github"
    assert row.auth_mode == "pat"
    assert row.label == "octocat"

    # Raw column value must be ciphertext (enc: prefix), not the plaintext token.
    from sqlalchemy import text
    raw = sess.execute(
        text("SELECT access_token FROM provider_auth_sessions WHERE owner=:o AND provider='github'"),
        {"o": owner},
    ).scalar()
    sess.close()
    assert raw != "ghp_secretvalue_123"
    assert "ghp_secretvalue_123" not in (raw or "")

    assert delete_token(owner) is True
    assert get_token(owner) is None


def test_service_not_connected():
    from services.github import GitHubService

    svc = GitHubService("nobody-unknown", token=None)
    assert svc.connected is False


@pytest.mark.asyncio
async def test_manage_github_guard_paths():
    from src.tool_implementations import do_manage_github
    from services.github import save_token, delete_token

    # Not connected → friendly error, no exception.
    r = await do_manage_github(json.dumps({"action": "list_repos"}), owner="nobody-unknown")
    assert r.get("exit_code") == 1 and "not connected" in r["error"].lower()

    save_token("gh-guard-user", "ghp_fake", auth_mode="pat")
    try:
        r2 = await do_manage_github(json.dumps({"action": "bogus"}), owner="gh-guard-user")
        assert r2.get("exit_code") == 1 and "unknown action" in r2["error"].lower()

        r3 = await do_manage_github(json.dumps({"action": "list_issues"}), owner="gh-guard-user")
        assert r3.get("exit_code") == 1 and "repo" in r3["error"].lower()
    finally:
        delete_token("gh-guard-user")


def test_schema_tag_dispatch_parity():
    from src.agent_tools import FUNCTION_TOOL_SCHEMAS, TOOL_TAGS

    names = {f["function"]["name"] for f in FUNCTION_TOOL_SCHEMAS}
    assert "manage_github" in names
    assert "manage_github" in TOOL_TAGS


def test_routes_registered():
    from routes.github_routes import setup_github_routes

    router = setup_github_routes()
    paths = {getattr(r, "path", None) for r in router.routes}
    assert "/api/github/status" in paths
    assert "/api/github/connect" in paths
    assert "/api/github/repos" in paths
    assert "/api/github/device/start" in paths
