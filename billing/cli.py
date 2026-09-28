"""Operator-run commands for onboarding a new tenant.

    python -m billing.cli add-tenant <tenant_key> <name> <email>
    python -m billing.cli provision <tenant_key>

add-tenant records a customer who paid outside the /signup flow (e.g. you
invoiced them manually). provision turns a DB row -- however it got there --
into a running stack: it writes tenants/<tenant_key>.env from .env.example
with fresh secrets and a unique port, creates that tenant's data/reply_examples
directories, and starts the containers. It always leaves the platform
credential fields (FACEBOOK_*, INSTAGRAM_*, YOUTUBE_*, GEMINI_API_KEY) blank
with a TODO marker -- there is no way to get those without the customer, and
guessing/reusing your own is a real risk of cross-posting to the wrong page.
"""

import argparse
import re
import secrets
import string
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

from app.password_policy import password_meets_policy
from billing import config, db, tenant_ops

ENV_EXAMPLE = config.REPO_ROOT / ".env.example"

# Vars that need a real per-tenant value before the stack can be useful.
# Left blank in the generated .env with a TODO comment above each.
_CREDENTIAL_TODOS = (
    "GEMINI_API_KEY",
    "FACEBOOK_PAGE_ID",
    "FACEBOOK_PAGE_ACCESS_TOKEN",
    "INSTAGRAM_USER_ID",
    "META_APP_SECRET",
    "META_WEBHOOK_VERIFY_TOKEN",
    "YOUTUBE_OAUTH_CLIENT_ID",
    "YOUTUBE_OAUTH_CLIENT_SECRET",
    "YOUTUBE_REFRESH_TOKEN",
)


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(18))
    password = f"{body}A1!"
    assert password_meets_policy(password)
    return password


def _host_data_gid() -> str:
    """Reuse the same HOST_DATA_GID as the main deployment's .env, if set --
    it's a property of the VM's OS Login user, not of any one tenant.
    """
    main_env = config.REPO_ROOT / ".env"
    if not main_env.exists():
        return ""
    match = re.search(r"^HOST_DATA_GID=(.*)$", main_env.read_text(), re.MULTILINE)
    return match.group(1).strip() if match else ""


def _render_tenant_env(tenant_key: str, port: int) -> str:
    lines = ENV_EXAMPLE.read_text().splitlines()
    overrides = {
        "DASHBOARD_PORT": str(port),
        "DASHBOARD_SECRET": secrets.token_urlsafe(32),
        "DASHBOARD_USERNAME": "admin",
        "DASHBOARD_PASSWORD_HASH_B64": None,  # filled in below, printed once
        "DASHBOARD_COOKIE_SECURE": "true",
        "DB_ENCRYPTION_KEY": secrets.token_urlsafe(32),
        "HOST_DATA_GID": _host_data_gid(),
        "TENANT_DATA_DIR": f"./tenants/{tenant_key}/data",
        "TENANT_REPLY_EXAMPLES_DIR": f"./tenants/{tenant_key}/reply_examples",
    }
    password = _generate_password()
    overrides["DASHBOARD_PASSWORD_HASH_B64"] = _b64_hash(password)

    seen = set()
    out = []
    for line in lines:
        key_match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if key_match and key_match.group(1) in overrides:
            key = key_match.group(1)
            seen.add(key)
            out.append(f"{key}={overrides[key]}")
            continue
        out.append(line)
    for key, value in overrides.items():
        if key not in seen:
            out.append(f"{key}={value}")

    out.append("")
    out.append("# --- TODO: fill in from the customer before this instance is live ---")
    for key in _CREDENTIAL_TODOS:
        out.append(f"# {key}=")
    return "\n".join(out) + "\n", password


def _b64_hash(password: str) -> str:
    import base64

    return base64.urlsafe_b64encode(generate_password_hash(password).encode()).decode()


def cmd_add_tenant(args: argparse.Namespace) -> None:
    with db.connect() as conn:
        if db.get_tenant(conn, args.tenant_key) is not None:
            raise SystemExit(f"Tenant {args.tenant_key!r} already exists.")
        port = db.next_free_port(conn)
        db.create_tenant(
            conn,
            tenant_key=args.tenant_key,
            name=args.name,
            email=args.email,
            port=port,
        )
    print(f"Added tenant {args.tenant_key!r} on port {port}. Run `provision` next.")


def cmd_provision(args: argparse.Namespace) -> None:
    tenant_key = args.tenant_key
    with db.connect() as conn:
        tenant = db.get_tenant(conn, tenant_key)
        if tenant is None:
            raise SystemExit(
                f"No tenant {tenant_key!r} in billing.db -- run add-tenant first "
                "(or let a customer complete /signup)."
            )
        port = tenant["port"]

    config.TENANTS_DIR.mkdir(parents=True, exist_ok=True)
    tenant_dir = config.TENANTS_DIR / tenant_key
    (tenant_dir / "data").mkdir(parents=True, exist_ok=True)
    (tenant_dir / "reply_examples").mkdir(parents=True, exist_ok=True)

    env_content, admin_password = _render_tenant_env(tenant_key, port)
    env_path = config.TENANTS_DIR / f"{tenant_key}.env"
    if env_path.exists() and not args.force:
        raise SystemExit(f"{env_path} already exists. Pass --force to overwrite.")
    env_path.write_text(env_content)
    print(f"Wrote {env_path}")
    print(f"Dashboard login: admin / {admin_password}  (save this now, shown once)")
    print(
        f"Edit {env_path} and fill in the TODO credential lines before this "
        "instance can actually reply to anything."
    )

    if args.start:
        result = tenant_ops.up(tenant_key, str(env_path))
        print(result.stdout)
        with db.connect() as conn:
            db.set_provisioned(conn, tenant_key)
            db.set_status(conn, tenant_key, "active")
        print(
            f"Started. Next: run scripts/add_tenant_route.sh {tenant_key} {port} "
            "to expose it on its own hostname."
        )
    else:
        print(
            "Not started (fill in credentials first, then run: "
            f"docker compose -p {tenant_key} --env-file {env_path} up -d --build)"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m billing.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add-tenant", help="Record a new tenant that paid outside /signup")
    add_p.add_argument("tenant_key")
    add_p.add_argument("name")
    add_p.add_argument("email")
    add_p.set_defaults(func=cmd_add_tenant)

    prov_p = sub.add_parser("provision", help="Write .env + start a tenant's stack")
    prov_p.add_argument("tenant_key")
    prov_p.add_argument("--force", action="store_true", help="Overwrite an existing .env")
    prov_p.add_argument(
        "--start", action="store_true",
        help="Also run docker compose up (skip if credentials aren't filled in yet)",
    )
    prov_p.set_defaults(func=cmd_provision)

    args = parser.parse_args(argv)
    db.init_db()
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
