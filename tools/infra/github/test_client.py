from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
from typing import Any

import pytest

spec = importlib.util.spec_from_file_location("github_client", Path(__file__).with_name("client.py"))
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
GitHubClient = module.GitHubClient


class FakeResponse:
    def __init__(self, payload: Any = None, status_code: int = 200, text: str = ""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        return self._payload


INSTALLATIONS = [
    {"id": 7, "account": {"login": "acme", "type": "Organization"}, "repository_selection": "all"},
    {"id": 8, "account": {"login": "octocat", "type": "User"}, "repository_selection": "selected"},
]
TOKEN = {"token": "inst-tok", "expires_at": "2999-01-01T00:00:00Z"}
REPO = {
    "full_name": "acme/web",
    "default_branch": "main",
    "private": True,
    "description": "web app",
    "extra": "ignored",
}


class RecordingGitHubClient(GitHubClient):
    """Captures HTTP calls and returns canned per-path responses."""

    def __init__(self) -> None:
        super().__init__(app_id="42", private_key="fake-pem")
        self.calls: list[dict[str, Any]] = []
        self.by_path: dict[str, tuple[Any, int]] = {}
        self.default_payload: Any = {}

    def _jwt(self) -> str:  # never touch real crypto in tests
        return "jwt-tok"

    def _http(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        path = url[len(self.base_url):] if url.startswith(self.base_url) else url
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": kwargs.get("params"),
                "json": kwargs.get("json"),
                "headers": kwargs.get("headers", {}),
            }
        )
        payload, status = self.by_path.get(path, (self.default_payload, 200))
        return FakeResponse(payload, status_code=status, text=f"error {status}" if status >= 400 else "")

    def calls_to(self, path: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"] == path]


def _client_with_install() -> RecordingGitHubClient:
    client = RecordingGitHubClient()
    client.by_path["/app/installations"] = (INSTALLATIONS, 200)
    client.by_path["/app/installations/7/access_tokens"] = (TOKEN, 200)
    client.by_path["/repos/acme/web"] = (REPO, 200)
    return client


# -- Auth internals ------------------------------------------------------------


def test_jwt_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_encode(claims: dict, key: str, algorithm: str | None = None) -> str:
        captured["claims"] = claims
        captured["key"] = key
        captured["algorithm"] = algorithm
        return "jwt.tok.en"

    monkeypatch.setattr(module.jwt, "encode", fake_encode)
    client = GitHubClient(app_id="42", private_key="pem-data")

    token = client._jwt()

    assert token == "jwt.tok.en"
    claims = captured["claims"]
    assert claims["iss"] == "42"
    assert claims["exp"] - claims["iat"] == 600  # iat = now-60, exp = now+540
    assert captured["algorithm"] == "RS256"
    assert captured["key"] == "pem-data"
    # cached for reuse — encode called once
    assert client._jwt() == "jwt.tok.en"


def test_pem_literal_newlines_normalized() -> None:
    client = GitHubClient(app_id="1", private_key="line1\\nline2\\n")
    assert client._pem() == "line1\nline2\n"

    real = GitHubClient(app_id="1", private_key="line1\nline2\n")
    assert real._pem() == "line1\nline2\n"


def test_installation_for_unknown_owner_lists_available() -> None:
    client = _client_with_install()

    with pytest.raises(RuntimeError, match="missing-org") as excinfo:
        client._installation_for("missing-org")

    assert "acme" in str(excinfo.value)
    assert "octocat" in str(excinfo.value)


def test_installation_token_cached_per_owner_repo() -> None:
    client = _client_with_install()

    client.get_repo("acme", "web")
    client.get_repo("acme", "web")

    assert len(client.calls_to("/app/installations/7/access_tokens")) == 1
    repo_calls = client.calls_to("/repos/acme/web")
    assert len(repo_calls) == 2
    for call in repo_calls:
        assert call["headers"]["Authorization"] == "Bearer inst-tok"
        assert call["headers"]["Accept"] == "application/vnd.github+json"
        assert call["headers"]["X-GitHub-Api-Version"] == "2022-11-28"


def test_repo_scoped_token_body() -> None:
    client = _client_with_install()

    client.get_repo("acme", "web")

    token_call = client.calls_to("/app/installations/7/access_tokens")[0]
    assert token_call["json"] == {"repositories": ["web"]}


# -- Public methods --------------------------------------------------------------


def test_list_installations_trimmed() -> None:
    client = _client_with_install()

    result = client.list_installations()

    assert result == [
        {"id": 7, "account": {"login": "acme", "type": "Organization"}, "repository_selection": "all"},
        {"id": 8, "account": {"login": "octocat", "type": "User"}, "repository_selection": "selected"},
    ]


def test_list_repos_across_installations() -> None:
    client = RecordingGitHubClient()
    client.by_path["/app/installations"] = ([INSTALLATIONS[0]], 200)
    client.by_path["/app/installations/7/access_tokens"] = (TOKEN, 200)
    client.by_path["/installation/repositories"] = (
        {"repositories": [dict(REPO), {"full_name": "acme/api", "private": False, "default_branch": "main"}]},
        200,
    )

    repos = client.list_repos()

    assert client.calls_to("/installation/repositories")[0]["method"] == "GET"
    assert repos == [
        {"full_name": "acme/web", "private": True, "default_branch": "main"},
        {"full_name": "acme/api", "private": False, "default_branch": "main"},
    ]


def test_get_repo_trimmed() -> None:
    client = _client_with_install()

    result = client.get_repo("acme", "web")

    assert result == {
        "full_name": "acme/web",
        "default_branch": "main",
        "private": True,
        "description": "web app",
    }


def test_get_file_decodes_base64() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web/contents/docs/a.md"] = (
        {"path": "docs/a.md", "sha": "s1", "content": base64.b64encode(b"hello\n").decode()},
        200,
    )

    result = client.get_file("acme", "web", "docs/a.md", ref="feat")

    assert result == {"path": "docs/a.md", "sha": "s1", "content": "hello\n"}
    call = client.calls_to("/repos/acme/web/contents/docs/a.md")[0]
    assert call["params"] == {"ref": "feat"}


def test_list_dir_trims_entries() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web/contents/src"] = (
        [
            {"name": "app.py", "path": "src/app.py", "type": "file", "size": 10, "extra": 1},
            {"name": "lib", "path": "src/lib", "type": "dir", "size": 0},
        ],
        200,
    )

    result = client.list_dir("acme", "web", "src")

    assert result == [
        {"name": "app.py", "path": "src/app.py", "type": "file", "size": 10},
        {"name": "lib", "path": "src/lib", "type": "dir", "size": 0},
    ]


def test_create_branch_from_default() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web/git/ref/heads/main"] = ({"object": {"sha": "abc123"}}, 200)
    client.by_path["/repos/acme/web/git/refs"] = ({"ref": "refs/heads/feat"}, 201)

    result = client.create_branch("acme", "web", "feat")

    assert result == {"branch": "feat", "sha": "abc123", "existed": False}
    post = client.calls_to("/repos/acme/web/git/refs")[0]
    assert post["json"] == {"ref": "refs/heads/feat", "sha": "abc123"}


def test_create_branch_existing_returns_existed() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web/git/ref/heads/main"] = ({"object": {"sha": "abc123"}}, 200)
    client.by_path["/repos/acme/web/git/refs"] = (
        {"message": "Reference already exists"},
        422,
    )

    result = client.create_branch("acme", "web", "feat")

    assert result == {"branch": "feat", "existed": True}


def test_create_branch_from_explicit_ref() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web/git/ref/heads/dev"] = ({"object": {"sha": "def456"}}, 200)
    client.by_path["/repos/acme/web/git/refs"] = ({"ref": "refs/heads/feat"}, 201)

    result = client.create_branch("acme", "web", "feat", from_ref="dev")

    assert result["sha"] == "def456"
    assert client.calls_to("/repos/acme/web/git/ref/heads/dev")
    # default branch was never resolved
    assert not client.calls_to("/repos/acme/web")


def test_put_file_refuses_default_branch() -> None:
    client = _client_with_install()

    with pytest.raises(RuntimeError, match="default branch"):
        client.put_file("acme", "web", "a.txt", "content", "msg", branch="main")

    # no PUT was issued
    assert not [c for c in client.calls if c["method"] == "PUT"]


def test_put_file_on_feature_branch() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web/contents/a.txt"] = (
        {"content": {"path": "a.txt", "sha": "newsha"}, "commit": {"sha": "commitsha"}},
        201,
    )

    result = client.put_file("acme", "web", "a.txt", "hi there", "add a.txt", branch="feat")

    assert result == {"path": "a.txt", "sha": "newsha", "commit_sha": "commitsha"}
    put = [c for c in client.calls if c["method"] == "PUT"][0]
    assert put["json"]["branch"] == "feat"
    assert put["json"]["message"] == "add a.txt"
    assert put["json"]["content"] == base64.b64encode(b"hi there").decode()
    assert "sha" not in put["json"]


def test_put_file_update_sends_sha() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web/contents/a.txt"] = (
        {"content": {"path": "a.txt", "sha": "s2"}, "commit": {"sha": "c2"}},
        200,
    )

    client.put_file("acme", "web", "a.txt", "v2", "update", branch="feat", sha="s1")

    put = [c for c in client.calls if c["method"] == "PUT"][0]
    assert put["json"]["sha"] == "s1"


def test_create_pull_request_defaults_base_to_default_branch() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web/pulls"] = (
        {"number": 5, "html_url": "https://github.com/acme/web/pull/5",
         "head": {"ref": "feat"}, "base": {"ref": "main"}},
        201,
    )

    result = client.create_pull_request("acme", "web", "My PR", "feat")

    assert result == {
        "number": 5,
        "url": "https://github.com/acme/web/pull/5",
        "head": "feat",
        "base": "main",
    }
    post = client.calls_to("/repos/acme/web/pulls")[0]
    assert post["json"]["base"] == "main"
    assert post["json"]["head"] == "feat"


def test_list_pull_requests_trimmed() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web/pulls"] = (
        [
            {
                "number": 3,
                "title": "Fix",
                "user": {"login": "bot"},
                "head": {"ref": "fix"},
                "base": {"ref": "main"},
                "state": "open",
                "draft": False,
                "extra": "ignored",
            }
        ],
        200,
    )

    result = client.list_pull_requests("acme", "web", state="closed")

    assert result == [
        {"number": 3, "title": "Fix", "user": "bot", "head": "fix", "base": "main", "state": "open", "draft": False}
    ]
    call = client.calls_to("/repos/acme/web/pulls")[0]
    assert call["params"] == {"state": "closed"}


def test_get_pull_request_includes_merge_state() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web/pulls/9"] = (
        {
            "number": 9,
            "title": "PR",
            "state": "open",
            "draft": False,
            "user": {"login": "bot"},
            "head": {"ref": "feat"},
            "base": {"ref": "main"},
            "mergeable": True,
            "merged": False,
        },
        200,
    )

    result = client.get_pull_request("acme", "web", 9)

    assert result["mergeable"] is True
    assert result["merged"] is False
    assert result["head"] == "feat"
    assert result["base"] == "main"


def test_list_workflow_runs_trimmed() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web/actions/runs"] = (
        {
            "workflow_runs": [
                {
                    "id": 1,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "created_at": "2026-09-26T00:00:00Z",
                    "extra": "ignored",
                }
            ]
        },
        200,
    )

    result = client.list_workflow_runs("acme", "web", limit=5)

    assert result == [
        {
            "id": 1,
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "created_at": "2026-09-26T00:00:00Z",
        }
    ]
    call = client.calls_to("/repos/acme/web/actions/runs")[0]
    assert call["params"] == {"per_page": 5}


def test_api_error_raises_runtime_error() -> None:
    client = _client_with_install()
    client.by_path["/repos/acme/web"] = ({"message": "Not Found"}, 404)

    with pytest.raises(RuntimeError, match=r"GitHub API error \(404\)"):
        client.get_repo("acme", "web")
