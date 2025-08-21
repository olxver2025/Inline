import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import Optional, Dict


@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool


class SandboxError(Exception):
    pass


def _ensure_docker() -> str:
    docker_bin = os.environ.get("DOCKER_BINARY", "docker")
    if not shutil.which(docker_bin):
        raise SandboxError(
            f"Docker binary '{docker_bin}' not found. Install Docker and ensure it's on PATH." 
        )
    return docker_bin


def ensure_image(image: str, *, pull: bool = True, pull_timeout: int = 300) -> None:
    """Ensure the Docker image exists locally; optionally pull it.

    Raises SandboxError if the image is missing and cannot be pulled or inspected.
    """
    docker_bin = _ensure_docker()
    try:
        inspected = subprocess.run(
            [docker_bin, "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception as e:
        raise SandboxError(f"Failed to inspect Docker image '{image}': {e}") from e

    if inspected.returncode == 0:
        return
    if not pull:
        raise SandboxError(
            f"Docker image '{image}' not found locally and pulling is disabled."
        )
    try:
        pulled = subprocess.run(
            [docker_bin, "pull", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=pull_timeout,
        )
    except subprocess.TimeoutExpired:
        raise SandboxError(
            f"Timed out pulling Docker image '{image}'. Try pulling manually."
        )
    except Exception as e:
        raise SandboxError(f"Failed to pull Docker image '{image}': {e}") from e
    if pulled.returncode != 0:
        raise SandboxError(
            f"Docker failed to pull image '{image}'. Check Docker connectivity."
        )
        

def run_code_in_docker(
    code: str,
    *,
    timeout_seconds: float = 5.0,
    memory: str = "256m",
    cpus: str = "1.0",
    image: str = "python:3.11-alpine",
    max_output_bytes: int = 100_000,
    ensure_image_present: bool = False,
    mount_dir: Optional[str] = None,
    workdir_subpath: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> SandboxResult:
    """
    Execute Python `code` inside a constrained Docker container and capture output.

    Security controls:
    - No network: --network none
    - Read-only FS + tmpfs /tmp
    - Drop all capabilities + no-new-privileges
    - Non-root user
    - CPU, memory, and pids limits

    Returns SandboxResult with stdout/stderr (UTF-8, replacement on decode error).
    May raise SandboxError for environment/setup issues.
    """
    docker_bin = _ensure_docker()
    if ensure_image_present:
        ensure_image(image, pull=True)

    container_name = f"py-sbx-{uuid.uuid4().hex[:12]}"

    # Build Docker run command. We pipe code to `python -` via stdin.
    cmd = [
        docker_bin,
        "run",
        "--rm",
        "--name",
        container_name,
        "-i",  # keep STDIN open to pass code to python -
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--pids-limit",
        "64",
        "--cpus",
        str(cpus),
        "--memory",
        str(memory),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "1000:1000",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "PYTHONUNBUFFERED=1",
    ]

    # Optional mount of persistent workspace
    if mount_dir:
        # Mount host directory as /workspace read-write
        cmd += ["-v", f"{mount_dir}:/workspace:rw"]
        # Set working directory inside container
        wd = "/workspace"
        if workdir_subpath:
            # Normalize possible leading slashes
            sub = workdir_subpath.lstrip("/")
            if sub:
                wd = f"/workspace/{sub}"
        cmd += ["-w", wd]

    # Extra env vars
    if env:
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]

    cmd += [
        image,
        "python",
        "-",
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=code.encode("utf-8", errors="replace"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
        out = proc.stdout
        err = proc.stderr
        truncated = False
        if len(out) > max_output_bytes:
            out = out[:max_output_bytes]
            truncated = True
        if len(err) > max_output_bytes:
            err = err[:max_output_bytes]
            truncated = True
        return SandboxResult(
            returncode=proc.returncode,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            timed_out=False,
            truncated=truncated,
        )
    except subprocess.TimeoutExpired:
        # Attempt to force-remove container on timeout
        try:
            subprocess.run([docker_bin, "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return SandboxResult(
            returncode=124,
            stdout="",
            stderr=(
                "Execution timed out. If this was the first run, the Docker image"
                " may still be pulling. Try pre-pulling or increasing the timeout."
            ),
            timed_out=True,
            truncated=False,
        )
    except FileNotFoundError as e:
        raise SandboxError("Docker not found. Is Docker installed and running?") from e
    except Exception as e:
        raise SandboxError(f"Failed to execute code in Docker: {e}") from e


def build_pip_install_command(
    *,
    mount_dir: str,
    packages: list[str],
    memory: str = "256m",
    cpus: str = "1.0",
    image: str = "python:3.11-alpine",
) -> list[str]:
    """Build a docker command to install packages into /workspace/.site-packages.

    Network is allowed for this command. The workspace is mounted at /workspace.
    """
    docker_bin = _ensure_docker()
    container_name = f"py-pip-{uuid.uuid4().hex[:12]}"
    cmd = [
        docker_bin,
        "run",
        "--rm",
        "--name",
        container_name,
        "-v",
        f"{mount_dir}:/workspace:rw",
        "--cpus",
        str(cpus),
        "--memory",
        str(memory),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "1000:1000",
        "-w",
        "/workspace",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "PYTHONUNBUFFERED=1",
        image,
        "python",
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "-U",
        "-t",
        "/workspace/.site-packages",
    ] + packages
    return cmd


def demo():  # simple local test when running this file
    code = "print('hello from sandbox')"
    res = run_code_in_docker(code)
    print("rc:", res.returncode)
    print("out:", res.stdout)
    print("err:", res.stderr)


if __name__ == "__main__":
    demo()
