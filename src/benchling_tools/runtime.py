"""Wiring of auth, API client, service and planner for one or more environments."""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from .api import BenchlingAPI
from .auth import AuthManager
from .environment import EnvironmentConfigStore
from .keychain import Keychain
from .plans import ChangePlanner, PlanRepository
from .services import BenchlingService


class Runtime:
    """Fully wired object graph for one resolved environment."""

    def __init__(
        self,
        env: str | None,
        *,
        config: EnvironmentConfigStore | None = None,
        secrets: Mapping[str, str] | None = None,
        async_http: httpx.AsyncClient | None = None,
        download_transport: httpx.AsyncBaseTransport | None = None,
        auth_http: httpx.Client | None = None,
    ) -> None:
        self.config = config or EnvironmentConfigStore()
        self.environment = self.config.resolve(env)
        self.secrets = secrets if secrets is not None else load_secrets(self.environment.name)
        self.auth = AuthManager(self.secrets, http=auth_http)
        self.api = BenchlingAPI(
            self.environment, self.auth, http=async_http, download_transport=download_transport
        )
        self.service = BenchlingService(self.api)
        self.repository = PlanRepository(self.config)
        self.planner = ChangePlanner(self.service, self.api, self.repository)

    async def close(self) -> None:
        """Release HTTP clients owned by the API and auth layers."""
        await self.api.close()
        self.auth.close()


class RuntimePool:
    """Lazily create and cache one Runtime per environment."""

    def __init__(
        self,
        *,
        config: EnvironmentConfigStore | None = None,
        secrets: Mapping[str, str] | None = None,
        runtimes: dict[str, Runtime] | None = None,
    ) -> None:
        self.config = config or EnvironmentConfigStore()
        self.secrets = secrets
        self._runtimes: dict[str, Runtime] = dict(runtimes or {})

    def available(self) -> list[str]:
        """Sorted names of cached runtimes plus environments saved in environments.json."""
        return sorted(set(self._runtimes) | set(self.config.metadata()))

    def get(self, env: str) -> Runtime:
        """Return the cached runtime, building it on first use; raises if the environment is invalid."""
        runtime = self._runtimes.get(env)
        if runtime is None:
            runtime = Runtime(env, config=self.config, secrets=self.secrets)
            self._runtimes[runtime.environment.name] = runtime
        return runtime

    async def close(self) -> None:
        """Empty the cache and close every runtime."""
        runtimes, self._runtimes = list(self._runtimes.values()), {}
        for runtime in runtimes:
            await runtime.close()


def load_secrets(name: str, keychain: Keychain | None = None) -> dict[str, str]:
    """Read one environment's client secret from the OS keychain."""
    secret = (keychain or Keychain()).get(name)
    if secret is None:
        return {}
    return {name: secret}
