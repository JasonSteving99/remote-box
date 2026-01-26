import os
from pathlib import Path
from pydantic import BaseModel
from remote.backends import E2B, AnyBackendConfig
from e2b import Template, default_build_logger, wait_for_timeout, AsyncSandbox
import tomllib
from logging import getLogger
from functools import cache

logger = getLogger(__name__)


@cache
def _get_api_key(*, e2b_api_key: str | None) -> str:
    """
    Get the E2B API key from config or environment.

    This function is memoized to avoid repeated environment variable lookups.

    Args:
        e2b_api_key: API key from config (optional)

    Returns:
        E2B API key string

    Raises:
        ValueError: If API key cannot be found
    """
    api_key = e2b_api_key or os.environ.get("E2B_API_KEY")
    if not api_key:
        raise ValueError(
            "E2B API key is required. Provide it in the config or set the E2B_API_KEY environment variable."
        )
    return api_key


@cache
def _get_template_alias(
    *,
    template_prefix: str,
    template_version: str | None,
    local_project_root: Path,
) -> str:
    """
    Get the E2B template alias for the given configuration.

    Determines the template version from template_version if provided,
    otherwise reads it from pyproject.toml. Returns the formatted template alias
    string: {prefix}-v{version}

    This function is memoized to avoid repeated file I/O when determining the
    template alias across multiple decorator invocations with the same config.

    Args:
        template_prefix: Prefix for E2B template naming
        template_version: Optional version string. If None, reads from pyproject.toml
        local_project_root: Path to the local project root directory

    Returns:
        Template alias string in format: {prefix}-v{version}

    Raises:
        ValueError: If version cannot be determined
    """
    project_root_path = local_project_root.resolve()

    # Determine version for template naming
    if template_version:
        version = template_version
    else:
        # Read project version from pyproject.toml
        pyproject_path = project_root_path / "pyproject.toml"
        if not pyproject_path.exists():
            raise ValueError(
                f"pyproject.toml not found in project root: {project_root_path}. "
                "E2B backend requires project version."
            )

        with open(pyproject_path, "rb") as f:
            pyproject_data = tomllib.load(f)

        version = pyproject_data.get("project", {}).get("version")
        if not version:
            raise ValueError(
                "Project version not found in pyproject.toml. E2B backend requires a version."
            )

    # Construct template alias: {prefix}-v{version}
    return f"{template_prefix}-v{version}"


class E2BBackend:
    """Backend implementation for E2B sandbox execution."""

    @staticmethod
    def pre_check(config: AnyBackendConfig, local_project_root: Path) -> None:
        """
        Validate E2B backend configuration and ensure template exists.

        Checks:
        1. E2B API key is available (config or environment variable)
        2. Dockerfile path exists (specified or default)
        3. Project version is available (from config or pyproject.toml)
        4. E2B template for current version exists (creates and builds if not)

        If the template doesn't exist, it will be created from the Dockerfile and
        built/deployed to E2B automatically. The version used for template naming
        comes from config.template_version if provided, otherwise from pyproject.toml.

        Args:
            config: Backend configuration (must be E2B config)
            local_project_root: Path to the local project root directory

        Raises:
            ValueError: If validation fails
        """
        if not isinstance(config, E2B):
            raise TypeError(f"E2BBackend requires E2B config, got {type(config)}")

        project_root_path = local_project_root.resolve()

        # Check API key availability (memoized)
        api_key = _get_api_key(e2b_api_key=config.e2b_api_key)

        # Determine Dockerfile path (specified or default)
        if config.dockerfile_path:
            dockerfile = Path(config.dockerfile_path)
            if not dockerfile.is_absolute():
                dockerfile = project_root_path / dockerfile
        else:
            # Default to Dockerfile in project root
            dockerfile = project_root_path / "Dockerfile"

        # Validate Dockerfile exists
        if not dockerfile.exists():
            raise ValueError(
                f"Dockerfile not found at path: {dockerfile}. "
                "Specify dockerfile_path in config or ensure Dockerfile exists in project root."
            )
        if not dockerfile.is_file():
            raise ValueError(f"Dockerfile path is not a file: {dockerfile}")

        # Get template alias (memoized)
        template_alias = _get_template_alias(
            template_prefix=config.template_prefix,
            template_version=config.template_version,
            local_project_root=local_project_root,
        )

        # Check if template exists, if not create and build it
        template_exists = Template.alias_exists(alias=template_alias, api_key=api_key)

        if not template_exists:
            logger.info(f"E2B template '{template_alias}' does not exist. Creating and building...")

            # Build and deploy the template
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

            logger.info(f"E2B template '{template_alias}' successfully built and deployed.")

    @staticmethod
    async def execute[O: BaseModel](
        config: AnyBackendConfig,
        local_project_root: Path,
        bash_script: str,
        output_model_class: type[O],
        timeout_millis: int,
    ) -> O:
        """
        Execute bash script in E2B sandbox and return parsed output.

        Args:
            config: E2B backend configuration
            local_project_root: Path to the local project root
            bash_script: Bash script to execute in the sandbox
            output_model_class: Pydantic model class for parsing output
            timeout_millis: Maximum execution time in milliseconds

        Returns:
            Parsed output as instance of output_model_class

        Raises:
            TypeError: If config is not E2B config
            ValueError: If execution fails or output cannot be parsed
        """
        if not isinstance(config, E2B):
            raise TypeError(f"E2BBackend requires E2B config, got {type(config)}")

        # Create sandbox from template
        async_sandbox = await AsyncSandbox.create(
            # Get the template alias (memoized, same as used in pre_check)
            template=_get_template_alias(
                template_prefix=config.template_prefix,
                template_version=config.template_version,
                local_project_root=local_project_root,
            ),
            # Convert timeout from milliseconds to seconds for E2B
            timeout=timeout_millis // 1000,
            # Get API key from config or environment (memoized)
            api_key=_get_api_key(e2b_api_key=config.e2b_api_key),
        )

        try:
            # Execute the bash script in the sandbox
            result = await async_sandbox.commands.run(bash_script, user="root")

            # Check for execution errors
            if result.exit_code != 0:
                raise ValueError(
                    f"Bash script execution failed with exit code {result.exit_code}.\n"
                    f"stdout: {result.stdout}\n"
                    f"stderr: {result.stderr}"
                )

            # Parse the JSON output and validate against the output model
            try:
                return output_model_class.model_validate_json(result.stdout)
            except Exception as e:
                raise ValueError(
                    f"Failed to parse and validate output against {output_model_class.__name__}: {e}\n"
                    f"stdout: {result.stdout}"
                )

        finally:
            # Always kill the sandbox to avoid resource leaks
            await async_sandbox.kill()
