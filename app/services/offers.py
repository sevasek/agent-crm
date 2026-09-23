"""Sales offers: what you're pitching on a call — a one-line value prop,
a proof point, and a priced USD figure. Deals reference one via
`deals.offer_id`. Configurable rather than hardcoded, same reasoning as the
pipeline stages.
"""

from app.database import get_db
from app.services.auth import sanitize_text

CURRENCY_DEFAULT = "USD"

# Seeded once when the offers table is empty AND the matching service exists
# (see scripts/seed_services.py). Prices are placeholders; edit via MCP / admin.
STARTER_OFFERS = (
    {
        "name": "Discovery Session",
        "service_slug": "consulting",
        "price": 350,
        "description": "Paid discovery session.",
        "pitch": "A focused session to map where the business is losing time.",
        "price_anchor": "$350",
        "is_default": True,
    },
    {
        "name": "Website Rebuild",
        "service_slug": "website-rebuild",
        "price": None,
        "description": "Package price TBD — edit before pitching.",
        "pitch": "A new site, live in weeks, that you own.",
        "price_anchor": "TBD",
    },
    {
        "name": "Monthly Retainer",
        "service_slug": "monthly-retainer",
        "price": None,
        "description": "Placeholder — set price when scoping.",
        "pitch": "Ongoing support so the work keeps compounding.",
        "price_anchor": "TBD",
    },
)


def _row(r):
    return dict(r) if r else None


def _aud_anchor(price):
    if price is None:
        return None
    try:
        number = float(price)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    if number == int(number):
        return f"${int(number)}"
    return f"${number:.2f}"


def _clean_currency(value):
    text = (value or "").strip().upper() or CURRENCY_DEFAULT
    return text if len(text) <= 8 else CURRENCY_DEFAULT


def _clean_price(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def list_offers(active_only: bool = False, service_id: int = None):
    query = "SELECT * FROM offers"
    clauses = []
    params = []
    if active_only:
        clauses.append("active = 1")
    if service_id:
        clauses.append("service_id = ?")
        params.append(service_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY is_default DESC, name"
    with get_db() as db:
        return [dict(r) for r in db.execute(query, params).fetchall()]


def get_offer(offer_id):
    if not offer_id:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
        return _row(row)


def find_offer(name: str, service_id=None):
    name = (name or "").strip()
    if not name:
        return None
    with get_db() as db:
        if service_id:
            row = db.execute(
                """SELECT * FROM offers
                   WHERE lower(name) = lower(?) AND service_id = ?
                   LIMIT 1""",
                (name, service_id),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM offers WHERE lower(name) = lower(?) LIMIT 1",
                (name,),
            ).fetchone()
        return _row(row)


def default_offer():
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM offers WHERE is_default = 1 AND active = 1 LIMIT 1"
        ).fetchone()
        return _row(row)


def default_offer_id():
    offer = default_offer()
    return offer["id"] if offer else None


def create_offer(name: str, *, pitch: str = "", proof_point: str = "", price_anchor: str = "",
                  is_default: bool = False, active: bool = True, service_id=None,
                  price=None, currency: str = CURRENCY_DEFAULT, description: str = ""):
    """Returns (offer_dict, None) on success or (None, error_code) on failure."""
    name = sanitize_text(name, max_len=120)
    if not name:
        return None, "name_required"
    if service_id:
        from app.services.catalog import get_service
        if not get_service(service_id):
            return None, "invalid_service"
    existing = find_offer(name, service_id=service_id)
    if existing:
        return existing, None
    price = _clean_price(price)
    currency = _clean_currency(currency)
    price_anchor = sanitize_text(price_anchor, max_len=100) or _aud_anchor(price)
    with get_db() as db:
        if is_default:
            db.execute("UPDATE offers SET is_default = 0")
        db.execute("""
            INSERT INTO offers (
                name, is_default, pitch, proof_point, price_anchor, active,
                service_id, price, currency, description
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name, int(bool(is_default)),
            sanitize_text(pitch, max_len=500, allow_newlines=True) or None,
            sanitize_text(proof_point, max_len=300) or None,
            price_anchor or None,
            int(bool(active)),
            service_id or None,
            price,
            currency,
            sanitize_text(description, max_len=2000, allow_newlines=True) or None,
        ))
        db.commit()
        offer_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    return get_offer(offer_id), None


def update_offer(offer_id, **fields):
    allowed = {
        "name", "pitch", "proof_point", "price_anchor", "is_default", "active",
        "service_id", "price", "currency", "description",
    }
    offer = get_offer(offer_id)
    if not offer:
        return None, "not_found"
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "name" in updates:
        name = sanitize_text(updates["name"], max_len=120)
        if not name:
            return None, "name_required"
        updates["name"] = name
    if "pitch" in updates:
        updates["pitch"] = sanitize_text(updates["pitch"], max_len=500, allow_newlines=True) or None
    if "proof_point" in updates:
        updates["proof_point"] = sanitize_text(updates["proof_point"], max_len=300) or None
    if "price_anchor" in updates:
        updates["price_anchor"] = sanitize_text(updates["price_anchor"], max_len=100) or None
    if "description" in updates:
        updates["description"] = sanitize_text(updates["description"], max_len=2000, allow_newlines=True) or None
    if "currency" in updates:
        updates["currency"] = _clean_currency(updates["currency"])
    if "price" in updates:
        updates["price"] = _clean_price(updates["price"])
        if "price_anchor" not in fields and updates["price"] is not None:
            updates["price_anchor"] = _aud_anchor(updates["price"])
    if "service_id" in updates:
        service_id = updates["service_id"]
        if service_id in (None, "", 0):
            updates["service_id"] = None
        else:
            from app.services.catalog import get_service
            if not get_service(service_id):
                return None, "invalid_service"
            updates["service_id"] = service_id
    for flag in ("is_default", "active"):
        if flag in updates and updates[flag] is not None:
            updates[flag] = int(bool(updates[flag]))
    updates = {k: v for k, v in updates.items() if k in allowed}
    if not updates:
        return offer, None
    with get_db() as db:
        if updates.get("is_default"):
            db.execute("UPDATE offers SET is_default = 0 WHERE id != ?", (offer_id,))
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [offer_id]
        db.execute(
            f"UPDATE offers SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            values,
        )
        db.commit()
    return get_offer(offer_id), None


def delete_offer(offer_id):
    """Returns (True, None) or (False, error_code). Refuses to delete an
    offer that any deal currently references."""
    offer = get_offer(offer_id)
    if not offer:
        return False, "not_found"
    with get_db() as db:
        in_use = db.execute("SELECT COUNT(*) AS n FROM deals WHERE offer_id = ?", (offer_id,)).fetchone()["n"]
        if in_use:
            return False, "offer_in_use"
        db.execute("DELETE FROM offers WHERE id = ?", (offer_id,))
        db.commit()
    return True, None


def seed_starter_offers():
    """Create the three starter offers if the catalog is empty.

    No-op when any offer already exists (do not clobber operator edits) or
    when the matching services have not been seeded yet.
    """
    if list_offers():
        return []
    from app.services.catalog import get_service_by_slug
    created = []
    for spec in STARTER_OFFERS:
        service = get_service_by_slug(spec["service_slug"])
        if not service:
            continue
        offer, _error = create_offer(
            spec["name"],
            service_id=service["id"],
            price=spec["price"],
            currency=CURRENCY_DEFAULT,
            description=spec["description"],
            pitch=spec["pitch"],
            price_anchor=spec["price_anchor"],
            is_default=bool(spec.get("is_default")),
        )
        if offer:
            created.append(offer)
    return created
