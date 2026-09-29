import subprocess

from billing import config

COMPOSE_FILE = config.REPO_ROOT / "docker-compose.yml"
SERVICE_NAMES = ("dashboard", "video-stats", "youtube-comments")


def _tenant_compose_file(tenant_key: str):
    return config.TENANTS_DIR / f"{tenant_key}.compose.yml"


def _tenant_services(tenant_key: str, services: tuple[str, ...] | None) -> list[str]:
    return [f"{tenant_key}-{name}" for name in (services or SERVICE_NAMES)]


def _run_compose(tenant_key: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "docker", "compose",
            "-p", config.PROJECT_NAME,
            "-f", str(COMPOSE_FILE),
            "-f", str(_tenant_compose_file(tenant_key)),
            *args,
        ],
        cwd=config.REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )


def up(
    tenant_key: str, env_path: str, services: tuple[str, ...] | None = None
) -> subprocess.CompletedProcess:
    """First-time start after provisioning.

    No --build: the tenant's compose override references the main
    instance's already-built images (`config.PROJECT_NAME`-<service>) rather
    than building its own, so every tenant reuses one image per service.
    `env_path` is unused (the override file's own `env_file:` line already
    names this tenant's env file) but kept so callers don't need to change.
    """
    return _run_compose(tenant_key, "up", "-d", *_tenant_services(tenant_key, services))


def stop(
    tenant_key: str, env_path: str, services: tuple[str, ...] | None = None
) -> subprocess.CompletedProcess:
    """Stop a tenant's containers without removing them -- used when a
    subscription lapses (payment failed / cancelled). Cheap to reverse with
    start() the moment they pay again.
    """
    return _run_compose(tenant_key, "stop", *_tenant_services(tenant_key, services))


def start(
    tenant_key: str, env_path: str, services: tuple[str, ...] | None = None
) -> subprocess.CompletedProcess:
    """Resume a previously-stopped tenant's containers."""
    return _run_compose(tenant_key, "start", *_tenant_services(tenant_key, services))
