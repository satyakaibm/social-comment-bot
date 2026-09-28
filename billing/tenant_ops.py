import subprocess

from billing import config

COMPOSE_FILE = config.REPO_ROOT / "docker-compose.yml"


def _run_compose(tenant_key: str, env_path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "docker", "compose",
            "-p", tenant_key,
            "-f", str(COMPOSE_FILE),
            "--env-file", env_path,
            *args,
        ],
        cwd=config.REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )


def up(tenant_key: str, env_path: str) -> subprocess.CompletedProcess:
    """First-time start after provisioning: builds and creates the stack."""
    return _run_compose(tenant_key, env_path, "up", "-d", "--build")


def stop(tenant_key: str, env_path: str) -> subprocess.CompletedProcess:
    """Stop a tenant's containers without removing them -- used when a
    subscription lapses (payment failed / cancelled). Cheap to reverse with
    start() the moment they pay again.
    """
    return _run_compose(tenant_key, env_path, "stop")


def start(tenant_key: str, env_path: str) -> subprocess.CompletedProcess:
    """Resume a previously-stopped tenant's containers."""
    return _run_compose(tenant_key, env_path, "start")
