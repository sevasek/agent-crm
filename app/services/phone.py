import re


def to_tel_href(phone: str) -> str:
    """`tel:` URIs can't carry raw spaces/punctuation from free-text phone
    fields — keep only digits and a single leading `+` (a stray `+` further
    in, e.g. a typo like "0400+111", is dropped rather than kept mid-number)."""
    phone = phone or ""
    digits = re.sub(r"[^0-9+]", "", phone)
    if digits.startswith("+"):
        digits = "+" + digits[1:].replace("+", "")
    else:
        digits = digits.replace("+", "")
    return f"tel:{digits}"


def phone_digits(phone: str) -> str:
    """Digits only — used to match scraped phones that differ in spaces/dashes."""
    return re.sub(r"[^0-9]", "", phone or "")


def has_callable_phone(phone: str) -> bool:
    """True if the sanitized phone has at least one digit — a phone field
    like "---" or "(ext)" sanitizes to nothing dialable."""
    return bool(phone_digits(phone))
