"""GitHub integration service.

Thin async wrapper around the GitHub REST API plus credential helpers that
read/write the owner-scoped token from the encrypted ``ProviderAuthSession``
table (``provider="github"``).

This is the single source of truth used by both the HTTP routes
(``routes/github_routes.py``) and the agent tool (``do_manage_github``), so
browse and agent actions always go through the same code path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from core.database import SessionLocal, ProviderAuthSession

GITHUB_API = "https://api.github.com"
PROVIDER = "github"
# Default scope hint shown to users for "Claude-Code-like" write access.
RECOMMENDED_SCOPES = "repo, read:org, read:user"


# --------------------------------------------------------------------------- #
# Credential helpers — encrypted at rest via ProviderAuthSession.access_token
# --------------------------------------------------------------------------- #
def get_token(owner: Optional[str]) -> Optional[str]:
    """Return the decrypted GitHub token for ``owner`` (or a global one)."""
    db = SessionLocal()
    try:
        row = (
            db.query(ProviderAuthSession)
            .filter(ProviderAuthSession.provider == PROVIDER)
            .filter((ProviderAuthSession.owner == owner) | (ProviderAuthSession.owner.is_(None)))
            .order_by(ProviderAuthSession.owner.is_(None))  # prefer owner-specific
            .first()
        )
        return (row.access_token or None) if row else None
    finally:
        db.close()


def save_token(owner: Optional[str], token: str, *, auth_mode: str = "pat", login: Optional[str] = None) -> None:
    """Create or update the owner's GitHub credential row."""
    db = SessionLocal()
    try:
        row = (
            db.query(ProviderAuthSession)
            .filter(ProviderAuthSession.provider == PROVIDER)
            .filter(ProviderAuthSession.owner == owner)
            .first()
        )
        if row is None:
            row = ProviderAuthSession(
                id=str(uuid.uuid4())[:8],
                provider=PROVIDER,
                owner=owner,
                base_url=GITHUB_API,
            )
            db.add(row)
        row.access_token = token  # transparently encrypted by EncryptedText
        row.auth_mode = auth_mode
        row.label = login or row.label
        row.last_refresh = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def delete_token(owner: Optional[str]) -> bool:
    """Remove the owner's GitHub credential. Returns True if a row was deleted."""
    db = SessionLocal()
    try:
        row = (
            db.query(ProviderAuthSession)
            .filter(ProviderAuthSession.provider == PROVIDER)
            .filter(ProviderAuthSession.owner == owner)
            .first()
        )
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Lightweight DTOs (mirrors the services/research dataclass style)
# --------------------------------------------------------------------------- #
@dataclass
class Repo:
    full_name: str
    name: str
    private: bool
    description: Optional[str]
    language: Optional[str]
    stars: int
    open_issues: int
    default_branch: str
    updated_at: Optional[str]
    html_url: str
    fork: bool = False

    @classmethod
    def from_api(cls, d: Dict[str, Any]) -> "Repo":
        return cls(
            full_name=d.get("full_name", ""),
            name=d.get("name", ""),
            private=bool(d.get("private")),
            description=d.get("description"),
            language=d.get("language"),
            stars=int(d.get("stargazers_count") or 0),
            open_issues=int(d.get("open_issues_count") or 0),
            default_branch=d.get("default_branch") or "main",
            updated_at=d.get("updated_at"),
            html_url=d.get("html_url", ""),
            fork=bool(d.get("fork")),
        )


class GitHubError(Exception):
    """Raised on GitHub API failures; carries an HTTP-ish status code."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class GitHubService:
    """Async GitHub REST client bound to a single owner's token."""

    def __init__(self, owner: Optional[str], token: Optional[str] = None, *, timeout: float = 20.0):
        self.owner = owner
        self._token = token if token is not None else get_token(owner)
        self._timeout = timeout

    @property
    def connected(self) -> bool:
        return bool(self._token)

    def _headers(self) -> Dict[str, str]:
        if not self._token:
            raise GitHubError("GitHub is not connected", status=401)
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(self, method: str, path: str, *, params: Optional[Dict] = None,
                       json: Optional[Dict] = None) -> Any:
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(method, url, headers=self._headers(), params=params, json=json)
        except httpx.HTTPError as e:
            raise GitHubError(f"GitHub request failed: {e}", status=502)
        if resp.status_code == 401:
            raise GitHubError("GitHub rejected the token (401). Reconnect your account.", status=401)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            raise GitHubError("GitHub API rate limit reached. Try again later.", status=429)
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except Exception:
                detail = resp.text[:200]
            raise GitHubError(f"GitHub API error {resp.status_code}: {detail}", status=resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ----- account ------------------------------------------------------- #
    async def whoami(self) -> Dict[str, Any]:
        d = await self._request("GET", "/user")
        return {"login": d.get("login"), "name": d.get("name"), "avatar_url": d.get("avatar_url"),
                "html_url": d.get("html_url")}

    async def rate_limit(self) -> Dict[str, Any]:
        d = await self._request("GET", "/rate_limit")
        return (d or {}).get("rate", {})

    # ----- repos --------------------------------------------------------- #
    async def list_repos(self, *, sort: str = "updated", per_page: int = 100,
                         affiliation: str = "owner,collaborator,organization_member") -> List[Repo]:
        items = await self._request("GET", "/user/repos", params={
            "sort": sort, "per_page": min(per_page, 100), "affiliation": affiliation,
        })
        return [Repo.from_api(r) for r in (items or [])]

    async def get_repo(self, full_name: str) -> Repo:
        d = await self._request("GET", f"/repos/{full_name}")
        return Repo.from_api(d)

    async def list_issues(self, full_name: str, *, state: str = "open", per_page: int = 50) -> List[Dict]:
        items = await self._request("GET", f"/repos/{full_name}/issues",
                                    params={"state": state, "per_page": min(per_page, 100)})
        # GitHub returns PRs in the issues list; filter them out.
        out = []
        for i in items or []:
            if "pull_request" in i:
                continue
            out.append({"number": i.get("number"), "title": i.get("title"), "state": i.get("state"),
                        "user": (i.get("user") or {}).get("login"), "comments": i.get("comments"),
                        "labels": [l.get("name") for l in i.get("labels", [])],
                        "updated_at": i.get("updated_at"), "html_url": i.get("html_url"),
                        "body": i.get("body")})
        return out

    async def list_pulls(self, full_name: str, *, state: str = "open", per_page: int = 50) -> List[Dict]:
        items = await self._request("GET", f"/repos/{full_name}/pulls",
                                    params={"state": state, "per_page": min(per_page, 100)})
        return [{"number": p.get("number"), "title": p.get("title"), "state": p.get("state"),
                 "user": (p.get("user") or {}).get("login"), "draft": p.get("draft"),
                 "head": (p.get("head") or {}).get("ref"), "base": (p.get("base") or {}).get("ref"),
                 "updated_at": p.get("updated_at"), "html_url": p.get("html_url"),
                 "body": p.get("body")} for p in (items or [])]

    async def get_file(self, full_name: str, path: str, ref: Optional[str] = None) -> Dict:
        params = {"ref": ref} if ref else None
        d = await self._request("GET", f"/repos/{full_name}/contents/{path.lstrip('/')}", params=params)
        if isinstance(d, list):
            # Directory listing
            return {"type": "dir", "entries": [{"name": e.get("name"), "type": e.get("type"),
                                                 "path": e.get("path"), "size": e.get("size")} for e in d]}
        content = d.get("content") or ""
        if d.get("encoding") == "base64" and content:
            import base64
            try:
                text = base64.b64decode(content).decode("utf-8", "replace")
            except Exception:
                text = ""
        else:
            text = content
        return {"type": "file", "path": d.get("path"), "size": d.get("size"),
                "sha": d.get("sha"), "content": text, "html_url": d.get("html_url")}

    async def search_code(self, query: str, *, per_page: int = 30) -> List[Dict]:
        d = await self._request("GET", "/search/code", params={"q": query, "per_page": min(per_page, 100)})
        return [{"name": i.get("name"), "path": i.get("path"),
                 "repo": (i.get("repository") or {}).get("full_name"),
                 "html_url": i.get("html_url")} for i in (d or {}).get("items", [])]

    # ----- write (Claude-Code-like actions) ------------------------------ #
    async def create_issue(self, full_name: str, title: str, body: str = "",
                           labels: Optional[List[str]] = None) -> Dict:
        payload: Dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        d = await self._request("POST", f"/repos/{full_name}/issues", json=payload)
        return {"number": d.get("number"), "html_url": d.get("html_url"), "title": d.get("title")}

    async def comment(self, full_name: str, number: int, body: str) -> Dict:
        d = await self._request("POST", f"/repos/{full_name}/issues/{number}/comments", json={"body": body})
        return {"id": d.get("id"), "html_url": d.get("html_url")}

    async def create_pull(self, full_name: str, title: str, head: str, base: str,
                          body: str = "", draft: bool = False) -> Dict:
        d = await self._request("POST", f"/repos/{full_name}/pulls",
                                json={"title": title, "head": head, "base": base, "body": body, "draft": draft})
        return {"number": d.get("number"), "html_url": d.get("html_url"), "title": d.get("title")}
