import asyncio
from pydantic import BaseModel


async def execute[O: BaseModel](
    bash_script: str,
    output_model_class: type[O],
    timeout_millis: int,
) -> O:
    """
    Execute the remote function in a subprocess and return the parsed result.

    Args:
        bash_script: Fully formatted bash script to execute (including execution harness)
        output_model_class: The Pydantic model class to parse the output
        timeout_millis: Maximum time to wait for execution in milliseconds

    Returns:
        Parsed output model instance

    Raises:
        TimeoutError: If execution exceeds timeout_millis
        Exception: If execution fails with non-zero exit code
    """
    # Execute with zsh explicitly to support {IPC_FD} dynamic file descriptors
    # (bash 3.2 on macOS doesn't support this feature, but zsh does)
    process = await asyncio.create_subprocess_exec(
        "zsh",
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
