import asyncio
import base64
from dataclasses import dataclass
from logging import getLogger
from pathlib import Path

import daytona as daytona_sdk

from remote.backends import AnyBackendConfig, Daytona, MissingImageError, PauseSemantics
from remote.backends._common import image_name, require_api_key, resolve_dockerfile

logger = getLogger(__name__)


def _snapshot_name(config: Daytona, local_project_root: Path) -> str:
    dockerfile = resolve_dockerfile(config.dockerfile_path, local_project_root)
    base = image_name(
        prefix=config.snapshot_name,
        version=config.snapshot_version,
        dockerfile=dockerfile,
        local_project_root=local_project_root,
    )
    # The sandbox class is baked into the snapshot (sandboxes inherit it), so it
    # must be part of the identity: the same Dockerfile as 'container' and as
    # 'linux-vm' are different snapshots.
    return f"{base}-{config.sandbox_class}"


def _api_key(config: Daytona) -> str:
    return require_api_key(config.daytona_api_key, "DAYTONA_API_KEY", "Daytona")


@dataclass
class DaytonaHandle:
    """Handle for a live Daytona sandbox (owns the client used to create it)."""

    client: daytona_sdk.AsyncDaytona
    sandbox: daytona_sdk.AsyncSandbox
    # From the config's sandbox_class: True means the sandbox supports true
    # pause (memory + processes frozen); False means only stop (disk-only).
    suspend_capable: bool


def _suspend_capable(config: Daytona) -> bool:
    return config.sandbox_class == "linux-vm"


class DaytonaBackend:
    """Backend implementation for Daytona sandbox execution."""

    PYTHON_CMD: str = "/app/.venv/bin/python"

    @staticmethod
    def ensure_built(
        config: AnyBackendConfig, local_project_root: Path, *, allow_build: bool
    ) -> bool:
        """
        Validate Daytona configuration and make sure the snapshot exists.

        Checks API key availability, Dockerfile existence, and snapshot existence.
        If the snapshot is missing: builds it when allow_build is True, otherwise
        raises MissingImageError (pointing at `remote-box build`).

        Raises:
            TypeError: If config is not a Daytona config
            ValueError: If validation fails
            MissingImageError: If the snapshot is missing and allow_build is False
        """
        if not isinstance(config, Daytona):
            raise TypeError(f"DaytonaBackend requires Daytona config, got {type(config)}")

        api_key = _api_key(config)
        dockerfile = resolve_dockerfile(config.dockerfile_path, local_project_root)
        full_snapshot_name = _snapshot_name(config, local_project_root)

        client = daytona_sdk.Daytona(daytona_sdk.DaytonaConfig(api_key=api_key))

        try:
            client.snapshot.get(full_snapshot_name)
            logger.info(f"Daytona snapshot '{full_snapshot_name}' already exists.")
            return False
        except daytona_sdk.DaytonaNotFoundError:
            pass

        if not allow_build:
            raise MissingImageError(full_snapshot_name)

        logger.info(
            f"Daytona snapshot '{full_snapshot_name}' does not exist. Creating and building..."
        )
        try:
            client.snapshot.create(
                daytona_sdk.CreateSnapshotParams(
                    name=full_snapshot_name,
                    image=daytona_sdk.Image.from_dockerfile(str(dockerfile)),
                    resources=daytona_sdk.Resources(
                        cpu=config.cpu_count,
                        memory=config.memory_gb,
                        disk=config.disk_gb,
                    ),
                    sandbox_class=daytona_sdk.SandboxClass(config.sandbox_class),
                    region_id=config.region_id,
                ),
                on_logs=lambda msg: logger.info(f"[Daytona snapshot build] {msg}"),
            )
        except Exception as e:
            # Another process may have built the same snapshot concurrently; if it
            # exists now, the goal is met and the failure is benign.
            try:
                client.snapshot.get(full_snapshot_name)
                logger.info(
                    f"Daytona snapshot '{full_snapshot_name}' was built concurrently elsewhere."
                )
                return False
            except daytona_sdk.DaytonaNotFoundError:
                pass
            # Daytona rejects the class/region combination before building, so
            # unsupported configs fail here — at build time — not at pause time.
            if isinstance(e, daytona_sdk.DaytonaError) and "no runners" in str(e).lower():
                region = (
                    f"region '{config.region_id}'"
                    if config.region_id
                    else "your organization's default region"
                )
                raise ValueError(
                    f"Daytona has no '{config.sandbox_class}' runners in {region}. "
                    f"Sandbox class availability varies by region and organization: set "
                    f"region_id to a region that offers '{config.sandbox_class}', or ask "
                    f"Daytona to enable it for your organization. Original error: {e}"
                ) from e
            raise

        logger.info(f"Daytona snapshot '{full_snapshot_name}' successfully built.")
        return True

    @staticmethod
    def image_name(config: AnyBackendConfig, local_project_root: Path) -> str | None:
        if not isinstance(config, Daytona):
            raise TypeError(f"DaytonaBackend requires Daytona config, got {type(config)}")
        return _snapshot_name(config, local_project_root)

    @staticmethod
    async def acquire(
        config: AnyBackendConfig, local_project_root: Path, timeout_millis: int
    ) -> DaytonaHandle:
        if not isinstance(config, Daytona):
            raise TypeError(f"DaytonaBackend requires Daytona config, got {type(config)}")

        client = daytona_sdk.AsyncDaytona(daytona_sdk.DaytonaConfig(api_key=_api_key(config)))
        try:
            sandbox = await client.create(
                daytona_sdk.CreateSandboxFromSnapshotParams(
                    snapshot=_snapshot_name(config, local_project_root),
                    # Snapshots are region-scoped; keep the sandbox in the same
                    # region rather than the organization default.
                    target=config.region_id,
                ),
                timeout=config.create_timeout_seconds,
            )
        except BaseException:
            await client.close()
            raise
        return DaytonaHandle(client=client, sandbox=sandbox, suspend_capable=_suspend_capable(config))

    @staticmethod
    async def run(handle: DaytonaHandle, bash_script: str, timeout_millis: int) -> str:
        # process.exec() runs through sh, which ignores the shebang and lacks some
        # bash features the harness relies on. Pipe the script through base64 into an
        # explicit bash process — safe against quoting issues and leaves no temp
        # files behind to collide across calls sharing the sandbox.
        encoded = base64.b64encode(bash_script.encode()).decode()
        command = f"printf '%s' '{encoded}' | base64 -d | bash"
        result = await handle.sandbox.process.exec(
            command, timeout=max(1, int(timeout_millis / 1000))
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"Remote execution harness failed with exit code {result.exit_code}.\n"
                f"output:\n{result.result}"
            )
        return result.result

    @staticmethod
    def sandbox_id(handle: DaytonaHandle) -> str | None:
        return handle.sandbox.id

    @staticmethod
    def pause_semantics(config: AnyBackendConfig) -> PauseSemantics:
        if not isinstance(config, Daytona):
            raise TypeError(f"DaytonaBackend requires Daytona config, got {type(config)}")
        return PauseSemantics.SUSPEND if _suspend_capable(config) else PauseSemantics.STOP

    @staticmethod
    async def pause(handle: DaytonaHandle) -> None:
        # Only VM sandbox classes support true pause (memory + processes frozen);
        # container sandboxes can only stop (disk persists, processes killed).
        # The choice is declared by the config's sandbox_class — see
        # pause_semantics — so which one runs here is never a surprise.
        if handle.suspend_capable:
            await handle.sandbox.pause()
        else:
            await handle.sandbox.stop()

    @staticmethod
    async def resume(handle: DaytonaHandle) -> None:
        await handle.sandbox.start()

    @staticmethod
    async def reconnect(
        config: AnyBackendConfig,
        sandbox_id: str | None,
        local_project_root: Path,
        timeout_millis: int,
    ) -> DaytonaHandle:
        if not isinstance(config, Daytona):
            raise TypeError(f"DaytonaBackend requires Daytona config, got {type(config)}")
        if sandbox_id is None:
            raise ValueError("Daytona reconnect requires a sandbox_id")

        client = daytona_sdk.AsyncDaytona(daytona_sdk.DaytonaConfig(api_key=_api_key(config)))
        try:
            sandbox = await client.get(sandbox_id)
            if sandbox.state != daytona_sdk.SandboxState.STARTED:
                await sandbox.start(timeout=config.create_timeout_seconds)
        except BaseException:
            await client.close()
            raise
        return DaytonaHandle(client=client, sandbox=sandbox, suspend_capable=_suspend_capable(config))

    @staticmethod
    async def release(handle: DaytonaHandle) -> None:
        try:
            for attempt in range(5):
                try:
                    await handle.client.delete(handle.sandbox)
                    break
                except daytona_sdk.DaytonaNotFoundError:
                    # Already deleted — e.g. a session rehydrated from this
                    # sandbox's SessionRef closed it first. Goal state met.
                    break
                except daytona_sdk.DaytonaConflictError:
                    # A state change is in progress (possibly another session's
                    # delete of this same sandbox finishing) — retry briefly.
                    if attempt == 4:
                        raise
                    await asyncio.sleep(0.5 * 2**attempt)
        finally:
            await handle.client.close()
