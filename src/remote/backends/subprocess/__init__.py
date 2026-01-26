import asyncio
from pathlib import Path
from pydantic import BaseModel
from remote.backends import AnyBackendConfig, Subprocess, SHELL_EXECUTABLES, BackendShell
import shutil
import subprocess
import re


class SubprocessBackend:
    """Backend implementation for local subprocess execution."""

    @staticmethod
    def pre_check(config: AnyBackendConfig, local_project_root: Path) -> None:
        """
        Verify that the required shell is available and supports dynamic FD assignment.

        Args:
            config: Backend configuration (must be Subprocess config)
            local_project_root: Path to the local project root directory

        Raises:
            RuntimeError: If the configured shell is not found or doesn't support required features
        """
        if not isinstance(config, Subprocess):
            raise TypeError(f"SubprocessBackend requires Subprocess config, got {type(config)}")

        shell_executable = SHELL_EXECUTABLES[config.shell]
        if not shutil.which(shell_executable):
            raise RuntimeError(
                f"Shell '{shell_executable}' not found in PATH. "
                f"Please install {shell_executable} or use a different shell configuration."
            )

        # Verify shell version supports dynamic FD assignment (required for execution harness)
        # Bash 4+ and Zsh support dynamic FD assignment via {varname}<>file syntax
        if config.shell == BackendShell.BASH4:
            try:
                result = subprocess.run(
                    [shell_executable, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                # Parse bash version from output like "GNU bash, version 5.2.15(1)-release"
                version_match = re.search(r"version (\d+)\.(\d+)", result.stdout)
                if version_match:
                    major_version = int(version_match.group(1))
                    if major_version < 4:
                        raise RuntimeError(
                            f"Bash version {major_version}.x does not support dynamic FD assignment. "
                            f"Please use Bash 4+ or switch to ZSH shell configuration."
                        )
            except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
                raise RuntimeError(f"Failed to verify bash version: {e}")

        elif config.shell == BackendShell.ZSH:
            # Zsh has supported dynamic FDs since very early versions (pre-4.0)
            # Just verify it runs without error
            try:
                subprocess.run(
                    [shell_executable, "--version"],
                    capture_output=True,
                    timeout=5,
                    check=True,
                )
            except (subprocess.TimeoutExpired, subprocess.SubprocessError) as e:
                raise RuntimeError(f"Failed to verify zsh installation: {e}")

    @staticmethod
    async def execute[O: BaseModel](
        config: AnyBackendConfig,
        local_project_root: Path,
        bash_script: str,
        output_model_class: type[O],
        timeout_millis: int,
    ) -> O:
        """
        Execute the remote function in a subprocess and return the parsed result.

        Args:
            config: The backend configuration
            local_project_root: Path to the local project root directory
            bash_script: Fully formatted bash script to execute (including execution harness)
            output_model_class: The Pydantic model class to parse the output
            timeout_millis: Maximum time to wait for execution in milliseconds

        Returns:
            Parsed output model instance

        Raises:
            TimeoutError: If execution exceeds timeout_millis
            Exception: If execution fails with non-zero exit code
        """
        if not isinstance(config, Subprocess):
            raise TypeError(f"SubprocessBackend requires Subprocess config, got {type(config)}")

        shell_executable = SHELL_EXECUTABLES[config.shell]

        # Execute with the configured shell to support {IPC_FD} dynamic file descriptors
        process = await asyncio.create_subprocess_exec(
            shell_executable,
            "-c",
            bash_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            # Convert milliseconds to seconds for timeout
            timeout_seconds = timeout_millis / 1000.0
            # Use asyncio.wait_for to allow other async tasks to run during execution
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            # Kill the process immediately
            process.kill()
            # Try to get partial output with a very short timeout (1ms max)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=0.001)
                partial_output = f"\nPartial stdout: {stdout.decode()[:500]}\nPartial stderr: {stderr.decode()[:500]}"
            except asyncio.TimeoutError:
                # Process didn't die quickly, force terminate and give up on partial output
                process.terminate()
                await process.wait()
                partial_output = ""

            raise TimeoutError(
                f"Remote execution exceeded timeout of {timeout_millis}ms.{partial_output}"
            )

        if process.returncode != 0:
            # TODO: We should really reconstruct the error in a more sophisticated way so that
            # the stack trace can be used as if it were called locally.
            raise Exception(f"Error executing remotely!: {stderr.decode()}")

        return output_model_class.model_validate_json(stdout.decode())
