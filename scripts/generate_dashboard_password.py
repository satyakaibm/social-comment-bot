#!/usr/bin/env python3
import base64
from getpass import getpass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from werkzeug.security import generate_password_hash

from app.password_policy import PASSWORD_HINT, password_meets_policy


def main() -> None:
    print(PASSWORD_HINT)
    password = getpass("Dashboard password: ")
    confirmation = getpass("Confirm password: ")
    if not password:
        raise SystemExit("Password cannot be empty.")
    if not password_meets_policy(password):
        raise SystemExit(PASSWORD_HINT)
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    password_hash = generate_password_hash(password)
    print(base64.urlsafe_b64encode(password_hash.encode()).decode())


if __name__ == "__main__":
    main()
