import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

from remote.backends import AnyBackendConfig, PauseSemantics, Subprocess


@dataclass
class SubprocessHandle:
    """Handle for the subprocess backend — there's no persistent sandbox, just config."""

    local_project_root: Path


class SubprocessBackend:
    """Backend implementation for local subprocess execution.

    There is no isolation here: every `run` is a fresh local process working
    directly against the local filesystem (which trivially persists across calls
    in a session).
    """

    PYTHON_CMD: str = "uv run python"

    @staticmethod
    def ensure_built(
        config: AnyBackendConfig, local_project_root: Path, *, allow_build: bool
    ) -> bool:
        """
        Verify that bash and uv are available. Nothing to build for this backend.

        Args:
            config: Backend configuration (must be Subprocess config)
            local_project_root: Path to the local project root directory
            allow_build: Unused — there is no image to build

        Raises:
            RuntimeError: If a required executable is not found
        """
        if not isinstance(config, Subprocess):
            raise TypeError(f"SubprocessBackend requires Subprocess config, got {type(config)}")

        for executable in ("bash", "uv"):
            if not shutil.which(executable):
                raise RuntimeError(
                    f"'{executable}' not found in PATH. The subprocess backend requires "
                    f"{executable} to be installed."
                )
        return False

    @staticmethod
    def image_name(config: AnyBackendConfig, local_project_root: Path) -> str | None:
        return None

    @staticmethod
    async def acquire(
        config: AnyBackendConfig, local_project_root: Path, timeout_millis: int
    ) -> SubprocessHandle:
        if not isinstance(config, Subprocess):
            raise TypeError(f"SubprocessBackend requires Subprocess config, got {type(config)}")
        return SubprocessHandle(local_project_root=local_project_root.resolve())

    @staticmethod
    async def run(handle: SubprocessHandle, bash_script: str, timeout_millis: int) -> str:
        """
        Execute the harness script in a local subprocess and return the raw stdout.

        Runs with cwd set to the project root so `uv run python` resolves the
        project environment and imports regardless of the caller's cwd.
        """
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            bash_script,
            cwd=handle.local_project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            timeout_seconds = timeout_millis / 1000.0
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            # Try to get partial output with a very short timeout (1ms max)
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=0.001)
                partial_output = f"\nPartial stdout: {stdout.decode()[:500]}\nPartial stderr: {stderr.decode()[:500]}"
            except asyncio.TimeoutError:
                # Process didn't die quickly, force terminate and give up on partial output
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass  # died between kill() and terminate()
                await process.wait()
                partial_output = ""

            raise TimeoutError(
                f"Remote execution exceeded timeout of {timeout_millis}ms.{partial_output}"
            )

        if process.returncode != 0:
            raise RuntimeError(
                f"Remote execution harness failed with exit code {process.returncode}.\n"
                f"stderr:\n{stderr.decode()}"
            )

        return stdout.decode()

    @staticmethod
    def sandbox_id(handle: SubprocessHandle) -> str | None:
        """No persistent sandbox, so nothing to reference."""
        return None

    @staticmethod
    def pause_semantics(config: AnyBackendConfig) -> PauseSemantics:
        return PauseSemantics.NOOP

    @staticmethod
    async def pause(handle: SubprocessHandle) -> None:
        """Nothing to pause — processes are per-run and the local FS persists."""

    @staticmethod
    async def resume(handle: SubprocessHandle) -> None:
        """Nothing to resume."""

    @staticmethod
    async def reconnect(
        config: AnyBackendConfig,
        sandbox_id: str | None,
        local_project_root: Path,
        timeout_millis: int,
    ) -> SubprocessHandle:
        """A fresh handle is equivalent — the local filesystem persisted on its own."""
        return await SubprocessBackend.acquire(config, local_project_root, timeout_millis)

    @staticmethod
    async def release(handle: SubprocessHandle) -> None:
        """Nothing to release — processes are per-run."""
