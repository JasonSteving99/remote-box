import base64
import os
from pathlib import Path
from remote.backends import Daytona, AnyBackendConfig
import daytona as daytona_sdk
import tomllib
from logging import getLogger
from functools import cache

logger = getLogger(__name__)


@cache
def _get_api_key(*, daytona_api_key: str | None) -> str:
    """
    Get the Daytona API key from config or environment.

    This function is memoized to avoid repeated environment variable lookups.

    Args:
        daytona_api_key: API key from config (optional)

    Returns:
        Daytona API key string

    Raises:
        ValueError: If API key cannot be found
    """
    api_key = daytona_api_key or os.environ.get("DAYTONA_API_KEY")
    if not api_key:
        raise ValueError(
            "Daytona API key is required. Provide it in the config or set the DAYTONA_API_KEY environment variable."
        )
    return api_key


@cache
def _get_snapshot_name(
    *,
    snapshot_name: str,
    snapshot_version: str | None,
    local_project_root: Path,
) -> str:
    """
    Get the full Daytona snapshot name for the given configuration.

    Determines the snapshot version from snapshot_version if provided,
    otherwise reads it from pyproject.toml. Returns the formatted snapshot name
    string: {snapshot_name}-v{version}

    This function is memoized to avoid repeated file I/O when determining the
    snapshot name across multiple decorator invocations with the same config.

    Args:
        snapshot_name: Prefix for Daytona snapshot naming
        snapshot_version: Optional version string. If None, reads from pyproject.toml
        local_project_root: Path to the local project root directory

    Returns:
        Snapshot name string in format: {snapshot_name}-v{version}

    Raises:
        ValueError: If version cannot be determined
    """
    project_root_path = local_project_root.resolve()

    if snapshot_version:
        version = snapshot_version
    else:
        pyproject_path = project_root_path / "pyproject.toml"
        if not pyproject_path.exists():
            raise ValueError(
                f"pyproject.toml not found in project root: {project_root_path}. "
                "Daytona backend requires project version."
            )

        with open(pyproject_path, "rb") as f:
            pyproject_data = tomllib.load(f)

        version = pyproject_data.get("project", {}).get("version")
        if not version:
            raise ValueError(
                "Project version not found in pyproject.toml. Daytona backend requires a version."
            )

    return f"{snapshot_name}-v{version}"


class DaytonaBackend:
    """Backend implementation for Daytona sandbox execution."""

    PYTHON_CMD: str = "/app/.venv/bin/python"

    @staticmethod
    def pre_check(config: AnyBackendConfig, local_project_root: Path) -> None:
        """
        Validate Daytona backend configuration and ensure snapshot exists.

        Checks:
        1. Daytona API key is available (config or environment variable)
        2. Dockerfile path exists (specified or default)
        3. Project version is available (from config or pyproject.toml)
        4. Daytona snapshot for current version exists (creates and builds if not)

        Args:
            config: Backend configuration (must be Daytona config)
            local_project_root: Path to the local project root directory

        Raises:
            ValueError: If validation fails
        """
        if not isinstance(config, Daytona):
            raise TypeError(f"DaytonaBackend requires Daytona config, got {type(config)}")

        project_root_path = local_project_root.resolve()

        api_key = _get_api_key(daytona_api_key=config.daytona_api_key)

        if config.dockerfile_path:
            dockerfile = Path(config.dockerfile_path)
            if not dockerfile.is_absolute():
                dockerfile = project_root_path / dockerfile
        else:
            dockerfile = project_root_path / "Dockerfile"

        if not dockerfile.exists():
            raise ValueError(
                f"Dockerfile not found at path: {dockerfile}. "
                "Specify dockerfile_path in config or ensure Dockerfile exists in project root."
            )
        if not dockerfile.is_file():
            raise ValueError(f"Dockerfile path is not a file: {dockerfile}")

        full_snapshot_name = _get_snapshot_name(
            snapshot_name=config.snapshot_name,
            snapshot_version=config.snapshot_version,
            local_project_root=local_project_root,
        )

        client = daytona_sdk.Daytona(daytona_sdk.DaytonaConfig(api_key=api_key))

        try:
            client.snapshot.get(full_snapshot_name)
            logger.info(f"Daytona snapshot '{full_snapshot_name}' already exists.")
        except daytona_sdk.DaytonaNotFoundError:
            logger.info(
                f"Daytona snapshot '{full_snapshot_name}' does not exist. Creating and building..."
            )

            client.snapshot.create(
                daytona_sdk.CreateSnapshotParams(
                    name=full_snapshot_name,
                    image=daytona_sdk.Image.from_dockerfile(str(dockerfile)),
                    resources=daytona_sdk.Resources(
                        cpu=config.cpu_count,
                        memory=config.memory_gb,
                        disk=config.disk_gb,
                    ),
                ),
                on_logs=lambda msg: print(f"[Daytona snapshot build] {msg}"),
            )

            logger.info(f"Daytona snapshot '{full_snapshot_name}' successfully built.")

    @staticmethod
    async def execute(
        config: AnyBackendConfig,
        local_project_root: Path,
        bash_script: str,
        timeout_millis: int,
    ) -> str:
        """
        Execute bash script in a Daytona sandbox and return raw stdout.

        Args:
            config: Daytona backend configuration
            local_project_root: Path to the local project root
            bash_script: Bash script to execute in the sandbox
            timeout_millis: Maximum execution time in milliseconds

        Returns:
            Raw stdout from execution as string

        Raises:
            TypeError: If config is not Daytona config
            ValueError: If execution fails
        """
        if not isinstance(config, Daytona):
            raise TypeError(f"DaytonaBackend requires Daytona config, got {type(config)}")

        api_key = _get_api_key(daytona_api_key=config.daytona_api_key)
        full_snapshot_name = _get_snapshot_name(
            snapshot_name=config.snapshot_name,
            snapshot_version=config.snapshot_version,
            local_project_root=local_project_root,
        )

        async with daytona_sdk.AsyncDaytona(daytona_sdk.DaytonaConfig(api_key=api_key)) as daytona:
            sandbox = await daytona.create(
                daytona_sdk.CreateSandboxFromSnapshotParams(snapshot=full_snapshot_name),
                timeout=timeout_millis / 1000.0,
            )
            try:
                # process.exec() runs through sh, which ignores the shebang and doesn't
                # support bash 4+ dynamic FD syntax. Write the script to a temp file via
                # base64 (safe against quoting issues) and execute it explicitly with bash.
                encoded = base64.b64encode(bash_script.encode()).decode()
                command = f"printf '%s' '{encoded}' | base64 -d > /tmp/remote_exec_harness.sh && bash /tmp/remote_exec_harness.sh"
                result = await sandbox.process.exec(command, timeout=int(timeout_millis / 1000))
                if result.exit_code != 0:
                    raise ValueError(
                        f"Bash script execution failed with exit code {result.exit_code}.\n"
                        f"stdout: {result.result}"
                    )
                return result.result
            finally:
                await daytona.delete(sandbox)
