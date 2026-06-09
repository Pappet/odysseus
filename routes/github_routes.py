# routes/github_routes.py
"""GitHub integration routes.

Two ways to connect (hybrid, see docs/github-integration-concept.md):

  * **Fine-grained PAT** (default, zero-infra): the user pastes a token created
    in GitHub settings. Recommended for "Claude-Code-like" write access because
    it can be scoped per-repo and given an expiry.
  * **OAuth device flow** (one-click): reuses the shared device-flow
    scaffolding. Requires an ``ODYSSEUS_GITHUB_CLIENT_ID`` for a registered
    GitHub OAuth App; if unset, the start endpoint returns a clear hint to use
    a PAT instead.

The remaining endpoints expose a browse + act surface over the connected
account (repos, issues, PRs, file contents, code search, plus write actions).
All read/write goes through :class:`services.github.GitHubService`.
"""

import logging
import os
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, Body, HTTPException, Request

from routes.device_flow import (
    DeviceFlowPoll,
    DeviceFlowStart,
    PendingDeviceFlowStore,
    create_device_flow_router,
)
from src.auth_helpers import get_current_user, require_user
from services.github import (
    GitHubService,
    GitHubError,
    RECOMMENDED_SCOPES,
    save_token,
    delete_token,
    get_token,
)

logger = logging.getLogger(__name__)

_DEVICE_FLOW_STORE = PendingDeviceFlowStore()

GITHUB_CLIENT_ID = os.environ.get("ODYSSEUS_GITHUB_CLIENT_ID", "").strip()
# Broad scope for Claude-Code-like write access. Device flow only supports
# classic scopes; fine-grained per-repo control is available via PAT.
DEVICE_SCOPE = os.environ.get("ODYSSEUS_GITHUB_SCOPE", "repo read:org read:user").strip()
_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_OAUTH_HEADERS = {"Accept": "application/json"}


def _svc(request: Request) -> GitHubService:
    return GitHubService(get_current_user(request))


def _handle(err: GitHubError):
    raise HTTPException(err.status, str(err))


# --------------------------------------------------------------------------- #
# Device flow (optional — needs a registered OAuth App client id)
# --------------------------------------------------------------------------- #
def _start_device_flow(request: Request, form) -> DeviceFlowStart:
    if not GITHUB_CLIENT_ID:
        raise HTTPException(
            400,
            "OAuth device flow is not configured. Set ODYSSEUS_GITHUB_CLIENT_ID "
            "for a registered GitHub OAuth App, or connect with a Personal "
            "Access Token instead.",
        )
    try:
        r = httpx.post(_DEVICE_CODE_URL, headers=_OAUTH_HEADERS,
                       json={"client_id": GITHUB_CLIENT_ID, "scope": DEVICE_SCOPE}, timeout=10.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise HTTPException(502, f"GitHub device-code request failed: {e}")

    device_code = data.get("device_code")
    if not device_code:
        raise HTTPException(502, "GitHub did not return a device code")
    return DeviceFlowStart(
        pending={"device_code": device_code, "owner": get_current_user(request)},
        response={
            "user_code": data.get("user_code"),
            "verification_uri": data.get("verification_uri"),
            "verification_uri_complete": data.get("verification_uri_complete"),
        },
        interval=int(data.get("interval") or 5),
        expires_in=int(data.get("expires_in") or 900),
    )


def _poll_device_flow(_request: Request, pending: Dict) -> DeviceFlowPoll:
    try:
        r = httpx.post(_ACCESS_TOKEN_URL, headers=_OAUTH_HEADERS, json={
            "client_id": GITHUB_CLIENT_ID,
            "device_code": pending["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, timeout=10.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return DeviceFlowPoll.pending(f"poll error: {e}")

    token = data.get("access_token")
    if token:
        owner = pending.get("owner")
        login = None
        try:
            login = httpx.get("https://api.github.com/user",
                              headers={"Authorization": f"Bearer {token}",
                                       "Accept": "application/vnd.github+json"},
                              timeout=10.0).json().get("login")
        except Exception:
            pass
        save_token(owner, token, auth_mode="device", login=login)
        return DeviceFlowPoll.authorized({"login": login, "auth_mode": "device"})

    err = data.get("error")
    if err == "authorization_pending":
        return DeviceFlowPoll.pending()
    if err == "slow_down":
        return DeviceFlowPoll.slow_down(int(data.get("interval") or 0) or None)
    if err in ("expired_token", "access_denied"):
        return DeviceFlowPoll.failed(err)
    return DeviceFlowPoll.pending(err or "unknown")


# --------------------------------------------------------------------------- #
# REST surface
# --------------------------------------------------------------------------- #
def _rest_router() -> APIRouter:
    router = APIRouter(prefix="/api/github", tags=["github"])

    @router.get("/status")
    async def status(request: Request):
        owner = require_user(request)
        token = get_token(owner)
        if not token:
            return {"connected": False, "device_flow_available": bool(GITHUB_CLIENT_ID),
                    "recommended_scopes": RECOMMENDED_SCOPES}
        svc = GitHubService(owner, token)
        try:
            me = await svc.whoami()
            rate = await svc.rate_limit()
        except GitHubError as e:
            return {"connected": False, "error": str(e), "device_flow_available": bool(GITHUB_CLIENT_ID)}
        return {"connected": True, "user": me, "rate_limit": rate,
                "device_flow_available": bool(GITHUB_CLIENT_ID)}

    @router.post("/connect")
    async def connect(request: Request, payload: Dict = Body(...)):
        """Connect with a Personal Access Token (fine-grained recommended)."""
        owner = require_user(request)
        token = (payload.get("token") or "").strip()
        if not token:
            raise HTTPException(400, "Missing token")
        svc = GitHubService(owner, token)
        try:
            me = await svc.whoami()
        except GitHubError as e:
            raise HTTPException(e.status, f"Token validation failed: {e}")
        save_token(owner, token, auth_mode="pat", login=me.get("login"))
        return {"connected": True, "user": me}

    @router.delete("/connect")
    async def disconnect(request: Request):
        owner = require_user(request)
        return {"disconnected": delete_token(owner)}

    @router.get("/repos")
    async def repos(request: Request, sort: str = "updated"):
        require_user(request)
        try:
            items = await _svc(request).list_repos(sort=sort)
        except GitHubError as e:
            _handle(e)
        return {"repos": [r.__dict__ for r in items]}

    @router.get("/repos/{owner}/{repo}")
    async def repo(request: Request, owner: str, repo: str):
        require_user(request)
        try:
            return (await _svc(request).get_repo(f"{owner}/{repo}")).__dict__
        except GitHubError as e:
            _handle(e)

    @router.get("/repos/{owner}/{repo}/issues")
    async def issues(request: Request, owner: str, repo: str, state: str = "open"):
        require_user(request)
        try:
            return {"issues": await _svc(request).list_issues(f"{owner}/{repo}", state=state)}
        except GitHubError as e:
            _handle(e)

    @router.get("/repos/{owner}/{repo}/pulls")
    async def pulls(request: Request, owner: str, repo: str, state: str = "open"):
        require_user(request)
        try:
            return {"pulls": await _svc(request).list_pulls(f"{owner}/{repo}", state=state)}
        except GitHubError as e:
            _handle(e)

    @router.get("/repos/{owner}/{repo}/contents/{path:path}")
    async def contents(request: Request, owner: str, repo: str, path: str, ref: Optional[str] = None):
        require_user(request)
        try:
            return await _svc(request).get_file(f"{owner}/{repo}", path, ref=ref)
        except GitHubError as e:
            _handle(e)

    @router.get("/search/code")
    async def search_code(request: Request, q: str):
        require_user(request)
        try:
            return {"results": await _svc(request).search_code(q)}
        except GitHubError as e:
            _handle(e)

    # ----- write actions ------------------------------------------------- #
    @router.post("/repos/{owner}/{repo}/issues")
    async def create_issue(request: Request, owner: str, repo: str, payload: Dict = Body(...)):
        require_user(request)
        title = (payload.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "Missing title")
        try:
            return await _svc(request).create_issue(
                f"{owner}/{repo}", title, payload.get("body", ""), payload.get("labels"))
        except GitHubError as e:
            _handle(e)

    @router.post("/repos/{owner}/{repo}/issues/{number}/comments")
    async def comment(request: Request, owner: str, repo: str, number: int, payload: Dict = Body(...)):
        require_user(request)
        body = (payload.get("body") or "").strip()
        if not body:
            raise HTTPException(400, "Missing body")
        try:
            return await _svc(request).comment(f"{owner}/{repo}", number, body)
        except GitHubError as e:
            _handle(e)

    @router.post("/repos/{owner}/{repo}/pulls")
    async def create_pull(request: Request, owner: str, repo: str, payload: Dict = Body(...)):
        require_user(request)
        try:
            return await _svc(request).create_pull(
                f"{owner}/{repo}", payload["title"], payload["head"], payload["base"],
                payload.get("body", ""), bool(payload.get("draft")))
        except KeyError as e:
            raise HTTPException(400, f"Missing field: {e}")
        except GitHubError as e:
            _handle(e)

    return router


def setup_github_routes() -> APIRouter:
    parent = APIRouter()
    parent.include_router(create_device_flow_router(
        prefix="/api/github",
        tags=["github"],
        store=_DEVICE_FLOW_STORE,
        start_flow=_start_device_flow,
        poll_flow=_poll_device_flow,
    ))
    parent.include_router(_rest_router())
    return parent
