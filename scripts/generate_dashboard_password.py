#!/usr/bin/env python3
import base64
from getpass import getpass

from werkzeug.security import generate_password_hash


def main() -> None:
    password = getpass("Dashboard password: ")
    confirmation = getpass("Confirm password: ")
    if not password:
        raise SystemExit("Password cannot be empty.")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    password_hash = generate_password_hash(password)
    print(base64.urlsafe_b64encode(password_hash.encode()).decode())


if __name__ == "__main__":
    main()
