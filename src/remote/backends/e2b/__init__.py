from dataclasses import dataclass
from logging import getLogger
from pathlib import Path

from e2b import AsyncSandbox, CommandExitException, Template, default_build_logger, wait_for_timeout
from e2b.exceptions import TimeoutException

from remote.backends import E2B, AnyBackendConfig, MissingImageError, PauseSemantics
from remote.backends._common import image_name, require_api_key, resolve_dockerfile

logger = getLogger(__name__)


def _template_alias(config: E2B, local_project_root: Path) -> str:
    dockerfile = resolve_dockerfile(config.dockerfile_path, local_project_root)
    return image_name(
        prefix=config.template_prefix,
        version=config.template_version,
        dockerfile=dockerfile,
        local_project_root=local_project_root,
    )


def _api_key(config: E2B) -> str:
    return require_api_key(config.e2b_api_key, "E2B_API_KEY", "E2B")


@dataclass
class E2BHandle:
    """Handle for a live E2B sandbox."""

    sandbox: AsyncSandbox
    ttl_seconds: int


class E2BBackend:
    """Backend implementation for E2B sandbox execution."""

    PYTHON_CMD: str = "/app/.venv/bin/python"

    @staticmethod
    def ensure_built(
        config: AnyBackendConfig, local_project_root: Path, *, allow_build: bool
    ) -> bool:
        """
        Validate E2B configuration and make sure the template exists.

        Checks API key availability, Dockerfile existence, and template existence.
        If the template is missing: builds it when allow_build is True, otherwise
        raises MissingImageError (pointing at `remote-box build`).

        Raises:
            TypeError: If config is not an E2B config
            ValueError: If validation fails
            MissingImageError: If the template is missing and allow_build is False
        """
        if not isinstance(config, E2B):
            raise TypeError(f"E2BBackend requires E2B config, got {type(config)}")

        project_root_path = local_project_root.resolve()
        api_key = _api_key(config)
        dockerfile = resolve_dockerfile(config.dockerfile_path, local_project_root)
        template_alias = _template_alias(config, local_project_root)

        if Template.alias_exists(alias=template_alias, api_key=api_key):
            logger.info(f"E2B template '{template_alias}' already exists.")
            return False

        if not allow_build:
            raise MissingImageError(template_alias)

        logger.info(f"E2B template '{template_alias}' does not exist. Creating and building...")
        try:
            Template.build(
                template=(
                    Template(file_context_path=str(project_root_path))
                    .from_dockerfile(str(dockerfile))
                    .set_start_cmd("echo ready", wait_for_timeout(1))
                ),
                alias=template_alias,
                cpu_count=config.cpu_count,
                memory_mb=config.memory_mb,
                on_build_logs=default_build_logger(),
                api_key=api_key,
            )
        except Exception:
            # Another process may have built the same alias concurrently; if the
            # template exists now, the goal is met and the failure is benign.
            if Template.alias_exists(alias=template_alias, api_key=api_key):
                logger.info(f"E2B template '{template_alias}' was built concurrently elsewhere.")
                return False
            raise

        logger.info(f"E2B template '{template_alias}' successfully built and deployed.")
        return True

    @staticmethod
    def image_name(config: AnyBackendConfig, local_project_root: Path) -> str | None:
        if not isinstance(config, E2B):
            raise TypeError(f"E2BBackend requires E2B config, got {type(config)}")
        return _template_alias(config, local_project_root)

    @staticmethod
    async def acquire(
        config: AnyBackendConfig, local_project_root: Path, timeout_millis: int
    ) -> E2BHandle:
        if not isinstance(config, E2B):
            raise TypeError(f"E2BBackend requires E2B config, got {type(config)}")

        # The sandbox lifetime (TTL) is deliberately decoupled from the per-call
        # timeout: sessions keep the sandbox alive across many calls by refreshing
        # the TTL before each run.
        sandbox = await AsyncSandbox.create(
            template=_template_alias(config, local_project_root),
            timeout=config.sandbox_ttl_seconds,
            api_key=_api_key(config),
        )
        return E2BHandle(sandbox=sandbox, ttl_seconds=config.sandbox_ttl_seconds)

    @staticmethod
    async def run(handle: E2BHandle, bash_script: str, timeout_millis: int) -> str:
        # Refresh the sandbox lifetime so long-lived sessions don't expire mid-use.
        await handle.sandbox.set_timeout(handle.ttl_seconds)

        try:
            result = await handle.sandbox.commands.run(
                bash_script,
                user="root",
                timeout=timeout_millis / 1000.0,
            )
        except CommandExitException as e:
            raise RuntimeError(
                f"Remote execution harness failed with exit code {e.exit_code}.\n"
                f"stderr:\n{e.stderr}"
            ) from e
        except TimeoutException as e:
            raise TimeoutError(
                f"Remote execution exceeded timeout of {timeout_millis}ms: {e}"
            ) from e

        return result.stdout

    @staticmethod
    def sandbox_id(handle: E2BHandle) -> str | None:
        return handle.sandbox.sandbox_id

    @staticmethod
    def pause_semantics(config: AnyBackendConfig) -> PauseSemantics:
        return PauseSemantics.SUSPEND

    @staticmethod
    async def pause(handle: E2BHandle) -> None:
        # Persists both filesystem and memory state.
        await handle.sandbox.pause()

    @staticmethod
    async def resume(handle: E2BHandle) -> None:
        # Instance connect() auto-resumes a paused sandbox and refreshes its TTL.
        await handle.sandbox.connect(timeout=handle.ttl_seconds)

    @staticmethod
    async def reconnect(
        config: AnyBackendConfig,
        sandbox_id: str | None,
        local_project_root: Path,
        timeout_millis: int,
    ) -> E2BHandle:
        if not isinstance(config, E2B):
            raise TypeError(f"E2BBackend requires E2B config, got {type(config)}")
        if sandbox_id is None:
            raise ValueError("E2B reconnect requires a sandbox_id")

        # Classmethod connect() reattaches by ID from any process, auto-resuming
        # a paused sandbox.
        sandbox = await AsyncSandbox.connect(
            sandbox_id,
            timeout=config.sandbox_ttl_seconds,
            api_key=_api_key(config),
        )
        return E2BHandle(sandbox=sandbox, ttl_seconds=config.sandbox_ttl_seconds)

    @staticmethod
    async def release(handle: E2BHandle) -> None:
        await handle.sandbox.kill()
