"""GitHub App client — repos, files, branches, pull requests, workflow runs.

Authenticates as a GitHub App: RS256 JWTs (minted from the App's private key)
authenticate App-level endpoints; per-installation access tokens (optionally
scoped to one repository) authenticate repo-level endpoints. Both are cached
with an expiry margin.
"""

import base64
import os
import time
from datetime import datetime
from typing import Any

import httpx
import jwt

from centaur_sdk import secret

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"


class GitHubClient:
    """Client for the GitHub REST API using GitHub App (centaur-ops) auth.

    GitHub App client (centaur-ops). Write path is branch + commit + PR only:
    no merges, no deletes, no direct commits to default branches.

    Reads ``CENTAUR_GITHUB_APP_ID`` and ``CENTAUR_GITHUB_APP_PEM`` (the App's
    private key; literal ``\\n`` sequences are normalized to real newlines).
    ``CENTAUR_GITHUB_API_URL`` overrides the base URL (default
    ``https://api.github.com``).
    """

    def __init__(
        self,
        app_id: str | None = None,
        private_key: str | None = None,
        api_url: str | None = None,
        timeout: float = 30.0,
    ):
        self._app_id = app_id
        self._private_key = private_key
        self._api_url = api_url
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self._jwt_cache: tuple[str, float] | None = None
        self._installations_cache: list | None = None
        # (owner, repo|None) -> (token, reuse-until epoch seconds)
        self._tokens: dict[tuple[str, str | None], tuple[str, float]] = {}
        # (owner, repo) -> default branch name
        self._default_branches: dict[tuple[str, str], str] = {}

    # -- Config / auth internals ------------------------------------------------

    @property
    def base_url(self) -> str:
        return (
            self._api_url or os.getenv("CENTAUR_GITHUB_API_URL", DEFAULT_API_URL)
        ).rstrip("/")  # noqa: TID251

    def _app_id_value(self) -> str:
        return (
            self._app_id
            or secret("CENTAUR_GITHUB_APP_ID", "")
            or os.getenv("CENTAUR_GITHUB_APP_ID", "")
        )

    def _pem(self) -> str:
        pem = self._private_key or secret("CENTAUR_GITHUB_APP_PEM", "")
        # Env-provided PEMs often arrive with literal \n sequences
        if "\\n" in pem:
            pem = pem.replace("\\n", "\n")
        return pem

    def _jwt(self) -> str:
        now = int(time.time())
        if self._jwt_cache and now < self._jwt_cache[1]:
            return self._jwt_cache[0]
        claims = {"iat": now - 60, "exp": now + 540, "iss": self._app_id_value()}
        token = jwt.encode(claims, self._pem(), algorithm="RS256")
        self._jwt_cache = (token, now + 480)  # reuse for 8 minutes
        return token

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        return self._client

    def _http(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return self.client.request(method, url, **kwargs)

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_data: dict | None = None,
        owner: str | None = None,
        repo: str | None = None,
        jwt_auth: bool = False,
    ) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if jwt_auth:
            headers["Authorization"] = f"Bearer {self._jwt()}"
        elif owner is not None:
            headers["Authorization"] = f"Bearer {self._token(owner, repo)}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        resp = self._http(
            method, f"{self.base_url}{path}", params=clean, json=json_data, headers=headers
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"GitHub API error ({resp.status_code}): {resp.text}")
        return resp.json()

    def _installations(self) -> list:
        if self._installations_cache is None:
            self._installations_cache = self._request("GET", "/app/installations", jwt_auth=True)
        return self._installations_cache

    def _installation_for(self, owner: str) -> dict:
        installations = self._installations()
        for inst in installations:
            if (inst.get("account") or {}).get("login", "").lower() == owner.lower():
                return inst
        available = sorted(
            (inst.get("account") or {}).get("login", "?") for inst in installations
        )
        raise RuntimeError(
            f"no GitHub App installation found for owner '{owner}'; "
            f"installed on: {', '.join(available) or '(none)'}"
        )

    def _token(self, owner: str, repo: str | None = None) -> str:
        key = (owner.lower(), repo.lower() if repo else None)
        cached = self._tokens.get(key)
        if cached and time.time() < cached[1]:
            return cached[0]
        installation = self._installation_for(owner)
        body = {"repositories": [repo]} if repo else {}
        data = self._request(
            "POST",
            f"/app/installations/{installation['id']}/access_tokens",
            json_data=body,
            jwt_auth=True,
        )
        expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
        self._tokens[key] = (data["token"], expires_at - 60)
        return data["token"]

    def _default_branch(self, owner: str, repo: str) -> str:
        key = (owner.lower(), repo.lower())
        if key not in self._default_branches:
            self._default_branches[key] = self.get_repo(owner, repo)["default_branch"]
        return self._default_branches[key]

    # -- Installations / repos ---------------------------------------------------

    def list_installations(self) -> list:
        """List GitHub App installations.

        Returns entries with: id, account ({login, type}), repository_selection
        ('all' or 'selected').
        """
        return [
            {
                "id": inst.get("id"),
                "account": {
                    "login": (inst.get("account") or {}).get("login"),
                    "type": (inst.get("account") or {}).get("type"),
                },
                "repository_selection": inst.get("repository_selection"),
            }
            for inst in self._installations()
        ]

    def list_repos(self) -> list:
        """List repositories across all installations of the App.

        Returns entries with: full_name, private, default_branch.
        """
        repos: list[dict] = []
        for inst in self._installations():
            owner = (inst.get("account") or {}).get("login")
            if not owner:
                continue
            data = self._request("GET", "/installation/repositories", owner=owner)
            for r in data.get("repositories", []):
                repos.append(
                    {
                        "full_name": r.get("full_name"),
                        "private": r.get("private"),
                        "default_branch": r.get("default_branch"),
                    }
                )
        return repos

    def get_repo(self, owner: str, repo: str) -> dict:
        """Get one repository, trimmed to key fields.

        Args:
            owner: Repository owner (org or user login).
            repo: Repository name.

        Returns: full_name, default_branch, private, description.
        """
        data = self._request("GET", f"/repos/{owner}/{repo}", owner=owner, repo=repo)
        return {
            "full_name": data.get("full_name"),
            "default_branch": data.get("default_branch"),
            "private": data.get("private"),
            "description": data.get("description"),
        }

    # -- Files -------------------------------------------------------------------

    def get_file(self, owner: str, repo: str, path: str, ref: str | None = None) -> dict:
        """Read a single file's contents from a repository.

        Args:
            owner: Repository owner.
            repo: Repository name.
            path: File path within the repo, e.g. 'docs/README.md'.
            ref: Branch, tag, or commit sha. Defaults to the default branch.

        Returns: {path, sha, content} with content base64-decoded to text.
        """
        params = {"ref": ref} if ref else None
        data = self._request(
            "GET", f"/repos/{owner}/{repo}/contents/{path}", params=params, owner=owner, repo=repo
        )
        raw = data.get("content", "")
        content = base64.b64decode(raw).decode("utf-8") if raw else ""
        return {"path": data.get("path", path), "sha": data.get("sha", ""), "content": content}

    def list_dir(self, owner: str, repo: str, path: str = "", ref: str | None = None) -> list:
        """List a directory in a repository.

        Args:
            owner: Repository owner.
            repo: Repository name.
            path: Directory path within the repo; '' or '/' for the root.
            ref: Branch, tag, or commit sha. Defaults to the default branch.

        Returns entries with: name, path, type ('file' or 'dir'), size.
        """
        params = {"ref": ref} if ref else None
        data = self._request(
            "GET",
            f"/repos/{owner}/{repo}/contents/{path.strip('/')}",
            params=params,
            owner=owner,
            repo=repo,
        )
        entries = data if isinstance(data, list) else [data]
        return [
            {
                "name": e.get("name"),
                "path": e.get("path"),
                "type": e.get("type"),
                "size": e.get("size"),
            }
            for e in entries
            if isinstance(e, dict)
        ]

    # -- Branches ------------------------------------------------------------------

    def create_branch(
        self, owner: str, repo: str, branch: str, from_ref: str | None = None
    ) -> dict:
        """Create a branch from another ref (or the default branch).

        Args:
            owner: Repository owner.
            repo: Repository name.
            branch: New branch name.
            from_ref: Source branch/ref. Defaults to the repo's default branch.

        Returns {'branch', 'sha', 'existed': False}; if the branch already
        exists, returns {'branch', 'existed': True} instead of raising.
        """
        source = from_ref or self._default_branch(owner, repo)
        ref = self._request(
            "GET", f"/repos/{owner}/{repo}/git/ref/heads/{source}", owner=owner, repo=repo
        )
        sha = (ref.get("object") or {}).get("sha")
        try:
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/git/refs",
                json_data={"ref": f"refs/heads/{branch}", "sha": sha},
                owner=owner,
                repo=repo,
            )
        except RuntimeError as e:
            if "(422)" in str(e):
                return {"branch": branch, "existed": True}
            raise
        return {"branch": branch, "sha": sha, "existed": False}

    def put_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> dict:
        """Create or update a file on a non-default branch (commit).

        GUARDRAIL: refuses to write to the repository's default branch — create
        a branch first with create_branch and open a PR with create_pull_request.

        Args:
            owner: Repository owner.
            repo: Repository name.
            path: File path within the repo.
            content: New file content (plain text; base64-encoded for the API).
            message: Commit message.
            branch: Target branch. Must NOT be the default branch.
            sha: Existing file sha (required when updating a file; see get_file).

        Returns: {path, sha, commit_sha}.
        """
        default = self._default_branch(owner, repo)
        if branch == default:
            raise RuntimeError(
                f"refusing to write to default branch '{branch}' of {owner}/{repo}; "
                "create a branch and open a PR instead"
            )
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        data = self._request(
            "PUT", f"/repos/{owner}/{repo}/contents/{path}", json_data=body, owner=owner, repo=repo
        )
        return {
            "path": (data.get("content") or {}).get("path", path),
            "sha": (data.get("content") or {}).get("sha"),
            "commit_sha": (data.get("commit") or {}).get("sha"),
        }

    # -- Pull requests -------------------------------------------------------------

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str | None = None,
        body: str = "",
    ) -> dict:
        """Open a pull request.

        Args:
            owner: Repository owner.
            repo: Repository name.
            title: PR title.
            head: Head branch name.
            base: Base branch. Defaults to the repo's default branch.
            body: PR body text.

        Returns: {number, url, head, base}.
        """
        base = base or self._default_branch(owner, repo)
        data = self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json_data={"title": title, "head": head, "base": base, "body": body},
            owner=owner,
            repo=repo,
        )
        return {
            "number": data.get("number"),
            "url": data.get("html_url"),
            "head": (data.get("head") or {}).get("ref", head),
            "base": (data.get("base") or {}).get("ref", base),
        }

    def list_pull_requests(self, owner: str, repo: str, state: str = "open") -> list:
        """List pull requests, trimmed to key fields.

        Args:
            owner: Repository owner.
            repo: Repository name.
            state: 'open', 'closed', or 'all'.

        Returns entries with: number, title, user, head, base, state, draft.
        """
        data = self._request(
            "GET", f"/repos/{owner}/{repo}/pulls", params={"state": state}, owner=owner, repo=repo
        )
        return [
            {
                "number": pr.get("number"),
                "title": pr.get("title"),
                "user": (pr.get("user") or {}).get("login"),
                "head": (pr.get("head") or {}).get("ref"),
                "base": (pr.get("base") or {}).get("ref"),
                "state": pr.get("state"),
                "draft": pr.get("draft"),
            }
            for pr in data
        ]

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict:
        """Get one pull request, including mergeable/merged state.

        Args:
            owner: Repository owner.
            repo: Repository name.
            number: PR number.
        """
        data = self._request(
            "GET", f"/repos/{owner}/{repo}/pulls/{number}", owner=owner, repo=repo
        )
        return {
            "number": data.get("number"),
            "title": data.get("title"),
            "state": data.get("state"),
            "draft": data.get("draft"),
            "user": (data.get("user") or {}).get("login"),
            "head": (data.get("head") or {}).get("ref"),
            "base": (data.get("base") or {}).get("ref"),
            "mergeable": data.get("mergeable"),
            "merged": data.get("merged"),
        }

    # -- Actions ---------------------------------------------------------------------

    def list_workflow_runs(self, owner: str, repo: str, limit: int = 10) -> list:
        """List recent GitHub Actions workflow runs.

        Args:
            owner: Repository owner.
            repo: Repository name.
            limit: Max runs to return (per_page).

        Returns entries with: id, name, status, conclusion, head_branch,
        created_at.
        """
        data = self._request(
            "GET",
            f"/repos/{owner}/{repo}/actions/runs",
            params={"per_page": limit},
            owner=owner,
            repo=repo,
        )
        return [
            {
                "id": run.get("id"),
                "name": run.get("name"),
                "status": run.get("status"),
                "conclusion": run.get("conclusion"),
                "head_branch": run.get("head_branch"),
                "created_at": run.get("created_at"),
            }
            for run in data.get("workflow_runs", [])
        ]

    # -- Lifecycle ---------------------------------------------------------------------

    def close(self):
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _client() -> GitHubClient:
    return GitHubClient()
