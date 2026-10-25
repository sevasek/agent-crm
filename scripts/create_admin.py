"""One-off CLI to seed the first (and typically only) login user.

No public signup route exists on purpose — this is a single-operator tool
with no onboarding flow. Run inside the container:
    docker compose exec app python scripts/create_admin.py you@example.com yourname

Non-interactive (scripts/new-instance.sh): set CREATE_ADMIN_PASSWORD in the
environment instead of using getpass. The password is not written to .env.
A CREATE_ADMIN_PASSWORD is someone else's choice, so it is a one-time
password: it expires after ONE_TIME_PASSWORD_TTL_HOURS (default 24) and must
be changed at first login. A password typed at the prompt is the user's own.

Reset a password (forgotten, or an expired one-time password):
    docker compose exec app python scripts/create_admin.py --reset you@example.com
This logs out every session for that user.
"""
import os
import sys
import getpass

sys.path.insert(0, ".")

from app.database import init_db
from app.services.auth import create_user, get_user_by_email, reset_password


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
    args = sys.argv[1:]
    reset = "--reset" in args
    args = [a for a in args if a != "--reset"]
    if not args:
        print("Usage: python scripts/create_admin.py [--reset] <email> [name]")
        sys.exit(1)
    email = args[0].strip().lower()
    name = args[1] if len(args) > 1 else ""
    one_time = bool(os.environ.get("CREATE_ADMIN_PASSWORD"))

    init_db()
    user = get_user_by_email(email)
    if reset:
        if not user:
            print(f"No user with email {email}.")
            sys.exit(1)
        reset_password(user["id"], _read_password(), one_time=one_time)
        print(f"Reset the password for user #{user['id']} ({email}).")
        _print_expiry(email)
        return
    if user:
        print(f"A user with email {email} already exists. Use --reset to set a new password.")
        sys.exit(1)

    password = _read_password()

    user_id = create_user(email, name, password, one_time=one_time)
    print(f"Created user #{user_id} ({email}).")
    _print_expiry(email)


def _print_expiry(email: str) -> None:
    expires_at = get_user_by_email(email)["password_expires_at"]
    if expires_at:
        print(f"One-time password: change it at first login, before {expires_at} UTC.")


if __name__ == "__main__":
    main()
