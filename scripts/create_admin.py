"""One-off CLI to seed the first (and typically only) login user.

No public signup route exists on purpose — this is a single-operator tool
with no onboarding flow. Run inside the container:
    docker compose exec app python scripts/create_admin.py you@example.com yourname

Non-interactive (scripts/new-instance.sh): set CREATE_ADMIN_PASSWORD in the
environment instead of using getpass. The password is not written to .env.
"""
import os
import sys
import getpass

sys.path.insert(0, ".")

from app.database import init_db
from app.services.auth import create_user, get_user_by_email


def _read_password() -> str:
    env = os.environ.get("CREATE_ADMIN_PASSWORD") or ""
    if env:
        if len(env) < 8:
            print("CREATE_ADMIN_PASSWORD must be at least 8 characters.")
            sys.exit(1)
        return env
    if not sys.stdin.isatty():
        print("No TTY: set CREATE_ADMIN_PASSWORD or run on a terminal.")
        sys.exit(1)
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        sys.exit(1)
    if len(password) < 8:
        print("Password must be at least 8 characters.")
        sys.exit(1)
    return password


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/create_admin.py <email> [name]")
        sys.exit(1)
    email = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else ""

    init_db()
    if get_user_by_email(email):
        print(f"A user with email {email} already exists.")
        sys.exit(1)

    password = _read_password()

    user_id = create_user(email, name, password)
    print(f"Created user #{user_id} ({email}).")


if __name__ == "__main__":
    main()
