from __future__ import annotations

import pytest

import api.warm_pool as warm_pool
from api.sandbox.base import SandboxSession


@pytest.fixture(autouse=True)
def clear_warm_pool_state():
    warm_pool._pool.clear()
    yield
    warm_pool._pool.clear()
    if warm_pool._replenish_task is not None:
        warm_pool._replenish_task.cancel()
        warm_pool._replenish_task = None


@pytest.mark.asyncio
async def test_start_replenish_loop_skips_unsupported_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        name = "fake"
        supports_warm_pool = False

    monkeypatch.setattr("api.warm_pool.get_backend", lambda: FakeBackend())

    async def unexpected_get_assigned_sandbox_ids() -> set[str]:
        raise AssertionError("unsupported backends should not start the warm pool loop")

    monkeypatch.setattr(
        "api.warm_pool._get_assigned_sandbox_ids",
        unexpected_get_assigned_sandbox_ids,
    )

    task = await warm_pool.start_replenish_loop()

    assert task is None


@pytest.mark.asyncio
async def test_spawn_warm_container_uses_hermes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        supports_warm_pool = True

        async def create(
            self,
            thread_key: str,
            harness: str,
            engine: str,
            *,
            warm: bool,
        ) -> SandboxSession:
            assert harness == "hermes"
            assert engine == "hermes"
            assert warm is True
            return SandboxSession(
                sandbox_id="sandbox-hermes",
                thread_key=thread_key,
                harness=harness,
                engine=engine,
            )

    monkeypatch.setattr(warm_pool, "POOL_HARNESS", "hermes")
    monkeypatch.setattr("api.warm_pool.get_backend", lambda: FakeBackend())

    spawned = await warm_pool._spawn_warm_container()

    assert spawned is not None
    assert spawned.harness == "hermes"
    assert spawned.engine == "hermes"


@pytest.mark.asyncio
async def test_claim_container_skips_kubernetes_persona_or_repo_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        name = "kubernetes"
        supports_warm_pool = True

    monkeypatch.setattr("api.warm_pool.get_backend", lambda: FakeBackend())
    warm_pool._pool.append(
        warm_pool.WarmContainer(sandbox_id="sandbox-1", harness="amp", engine="amp")
    )

    claimed = await warm_pool.claim_container("thread-1", "amp", persona="eng")

    assert claimed is None
    assert len(warm_pool._pool) == 1


@pytest.mark.asyncio
async def test_claim_container_refreshes_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        name = "fake"
        supports_warm_pool = True

        def __init__(self) -> None:
            self.exec_calls: list[tuple[str, list[str], dict | None]] = []

        async def status_by_id(self, _sandbox_id: str) -> str:
            return "running"

        async def refresh_token_by_id(self, _sandbox_id: str, _new_token: str) -> None:
            return None

        async def exec_run(
            self,
            sandbox_id: str,
            cmd: list[str],
            *,
            environment: dict | None = None,
            user: str = "",
        ) -> tuple[int, bytes]:
            self.exec_calls.append((sandbox_id, cmd, environment))
            assert user == "agent"
            return 0, b""

    backend = FakeBackend()
    monkeypatch.setattr("api.warm_pool.get_backend", lambda: backend)
    monkeypatch.setattr(
        "api.warm_pool.mint_sandbox_token", lambda _thread_key, _sandbox_id: "token"
    )
    warm_pool._pool.append(
        warm_pool.WarmContainer(sandbox_id="sandbox-1", harness="codex", engine="codex")
    )

    claimed = await warm_pool.claim_container(
        "thread-1", "codex", trace_id="00000000-0000-0000-0000-000000000123"
    )

    assert claimed is not None
    assert claimed.trace_id == "00000000-0000-0000-0000-000000000123"
    assert backend.exec_calls == [
        (
            "sandbox-1",
            ["sh", "-c", 'printf "%s" "$TRACE_ID" > /home/agent/.trace_id'],
            {"TRACE_ID": "00000000-0000-0000-0000-000000000123"},
        )
    ]
