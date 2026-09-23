"""Today's calls — ranked queue of qualified deals a salesperson can work.

Candidate pool is `deals.stage = 'qualified'` with a callable partner
(`partners.phone` present). Ranked by a deterministic weighted formula over
fields already on the deal/partner — computed at query time, not a stored
column, and not a learned/predictive model. See docs/DATA_MODEL.md → Scoring.
"""

import re
from datetime import datetime

from app.database import get_db
from app.services.auth import clean_owner_key
from app.services.deals import STAGE_CHANGED_PREFIX, STAGE_CHANGED_SEP, stage_changed_to_like
from app.services import pipeline_stages
from app.services.phone import has_callable_phone

CALL_QUEUE_LIMIT = 10
SCORE_MAX = 100

# Component maxima. Sum to SCORE_MAX so a "perfect" lead scores 100.
VALUE_MAX = 30
SOURCE_MAX = 25
CONTACT_MAX = 25
RECENCY_MAX = 20

# Source buckets. First matching group wins (warm before cold before web) so
# "referral from the website" stays a referral and "cold outreach via website"
# stays cold. Token-boundary match (split on non-letters), plus the two-word
# phrases "web form" / "linked-in" which would otherwise be split apart.
_SOURCE_WARM = frozenset({"referral", "inbound", "existing", "repeat"})
_SOURCE_COLD = frozenset({"cold", "outreach", "linkedin", "linked-in"})
_SOURCE_WEB = frozenset({"website", "form", "landing", "web form"})
_SOURCE_PHRASES = ("web form", "linked-in")

# Contact fields beyond the required phone, 5 points each (= CONTACT_MAX).
_CONTACT_FIELDS = (
    ("partner_email", "email"),
    ("partner_preferred_channel", "preferred channel"),
    ("partner_title", "title"),
    ("partner_social_url", "social URL"),
    ("partner_website", "website"),
)
_CONTACT_POINTS_EACH = CONTACT_MAX // len(_CONTACT_FIELDS)
if CONTACT_MAX % len(_CONTACT_FIELDS) != 0:
    raise RuntimeError(
        "CONTACT_MAX must divide evenly by contact fields so a complete "
        "contact scores CONTACT_MAX"
    )
if VALUE_MAX + SOURCE_MAX + CONTACT_MAX + RECENCY_MAX != SCORE_MAX:
    raise RuntimeError("component maxima must sum to SCORE_MAX")


def _parse_ts(value):
    if not value:
        return None
    text = str(value).strip().replace(" ", "T", 1)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(str(value).strip()[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def days_in_qualified(qualified_at, now=None):
    """Whole days since the deal last entered `qualified`. None if unknown."""
    ts = _parse_ts(qualified_at)
    if not ts:
        return None
    now = now or datetime.utcnow()
    return max(0, (now - ts).days)


def _score_value(value_estimate):
    if value_estimate in (None, ""):
        return 0, "no estimate"
    try:
        value = float(value_estimate)
    except (TypeError, ValueError):
        return 0, "no estimate"
    if value < 0:
        return 0, "invalid estimate"
    if value >= 5000:
        return VALUE_MAX, f"${value:.0f} (≥ $5k)"
    if value >= 1000:
        return 20, f"${value:.0f} ($1k–$5k)"
    if value > 0:
        return 10, f"${value:.0f} (under $1k)"
    return 0, "no estimate"


def _source_tokens(text: str) -> set:
    lowered = (text or "").strip().lower()
    tokens = set(re.findall(r"[a-z0-9]+", lowered))
    for phrase in _SOURCE_PHRASES:
        if phrase in lowered:
            tokens.add(phrase)
    return tokens


def _score_source(source):
    text = (source or "").strip().lower()
    if not text:
        return 0, "blank"
    tokens = _source_tokens(text)
    if tokens & _SOURCE_WARM:
        return SOURCE_MAX, "referral/inbound"
    if tokens & _SOURCE_COLD:
        return 10, "cold/outreach"
    if tokens & _SOURCE_WEB:
        return 15, "website/form"
    return 5, "other"


def _score_contact(deal):
    present = []
    for key, label in _CONTACT_FIELDS:
        if (deal.get(key) or "").strip():
            present.append(label)
    points = _CONTACT_POINTS_EACH * len(present)
    if not present:
        return 0, "phone only"
    return points, ", ".join(present)


def _score_recency(days):
    if days is None:
        return 0, "unknown when qualified"
    if days <= 7:
        return RECENCY_MAX, f"{days}d in qualified (≤7d)"
    if days <= 14:
        return 15, f"{days}d in qualified (8–14d)"
    if days <= 30:
        return 10, f"{days}d in qualified (15–30d)"
    if days <= 60:
        return 5, f"{days}d in qualified (31–60d)"
    return 0, f"{days}d in qualified (>60d)"


def score_deal(deal, now=None):
    """Score one candidate. `deal` is a mapping with deal + partner fields.

    Returns a dict with `score` (int 0–SCORE_MAX), per-component points,
    human-readable `reasons`, and `days_in_qualified`.
    """
    # Recency is "when they last entered qualified", not last field edit.
    # No fallback to updated_at — that always exists and would make the
    # documented unknown=0 bucket unreachable on the list path.
    days = days_in_qualified(deal.get("qualified_at"), now=now)
    value_pts, value_why = _score_value(deal.get("value_estimate"))
    source_pts, source_why = _score_source(deal.get("source"))
    contact_pts, contact_why = _score_contact(deal)
    recency_pts, recency_why = _score_recency(days)
    score = value_pts + source_pts + contact_pts + recency_pts
    return {
        "score": score,
        "value_points": value_pts,
        "source_points": source_pts,
        "contact_points": contact_pts,
        "recency_points": recency_pts,
        "days_in_qualified": days,
        "reasons": {
            "value": value_why,
            "source": source_why,
            "contact": contact_why,
            "recency": recency_why,
        },
        "breakdown_summary": (
            f"{value_why} · {source_why} · {contact_why} · {recency_why}"
        ),
    }


def list_todays_calls(limit: int = CALL_QUEUE_LIMIT, now=None, *,
                      stage: str = None, service_slug: str = None,
                      source_prefix: str = None, include_new: bool = False,
                      owner_key: str = None):
    """Top `limit` callable deals, highest score first (default pool).

    Hard filters, not scored away: the partner must have an actually dialable
    phone (has_callable_phone — SQL only checks non-blank, so a phone like
    "---" is filtered out here instead).

    Default pool is the pipeline's `is_qualified_pool` stages. Campaign
    work (dead-lead lists at stage=new) uses `include_new=True` and/or an
    explicit `stage` / `service_slug` / `source_prefix`. Explicit `stage`
    replaces the pool; `include_new` unions the default stage with it.
    No matching stage configured means nothing to call.

    Ranking is query-time Python. The default qualified-pool path uses the
    weighted score. Campaign filters use a simpler recency sort (has
    next_action_date, then created_at, then name).
    """
    now = now or datetime.utcnow()
    pool_keys = pipeline_stages.qualified_pool_keys()
    if stage:
        pool_keys = [stage]
    elif include_new:
        default_key = pipeline_stages.default_stage_key()
        if default_key and default_key not in pool_keys:
            pool_keys = list(pool_keys) + [default_key]
    if not pool_keys:
        return []

    campaign = bool(stage or include_new or service_slug or source_prefix)

    # "When did this deal last enter *its own current* qualified-pool stage"
    # — deals.stage varies per row (several stages can carry the role), so
    # the LIKE pattern is built in SQL from each row's own stage rather than
    # bound as a single fixed string.
    like_prefix = f"{STAGE_CHANGED_PREFIX}%{STAGE_CHANGED_SEP}"
    stage_placeholders = ",".join("?" * len(pool_keys))
    query = f"""
        SELECT deals.*,
               partners.name AS partner_name,
               partners.phone AS partner_phone,
               partners.email AS partner_email,
               partners.title AS partner_title,
               partners.website AS partner_website,
               COALESCE(
                   NULLIF(partners.linkedin_url, ''),
                   NULLIF(partners.x_url, ''),
                   NULLIF(partners.instagram_url, ''),
                   NULLIF(partners.facebook_url, ''),
                   NULLIF(partners.youtube_url, ''),
                   partners.social_url
               ) AS partner_social_url,
               partners.preferred_channel AS partner_preferred_channel,
               services.name AS service_name,
               services.slug AS service_slug,
               (
                   SELECT activities.occurred_at
                   FROM activities
                   WHERE activities.deal_id = deals.id
                     AND activities.type = 'system'
                     AND activities.body LIKE (? || deals.stage)
                   ORDER BY activities.occurred_at DESC, activities.id DESC
                   LIMIT 1
               ) AS qualified_at
        FROM deals
        JOIN partners ON partners.id = deals.partner_id
        JOIN services ON services.id = deals.service_id
        WHERE deals.stage IN ({stage_placeholders})
          AND partners.phone IS NOT NULL
          AND TRIM(partners.phone) != ''
    """
    params = [like_prefix, *pool_keys]
    slug = (service_slug or "").strip().lower()
    if slug:
        query += " AND services.slug = ?"
        params.append(slug)
    prefix = (source_prefix or "").strip()
    if prefix:
        query += " AND lower(IFNULL(deals.source, '')) LIKE lower(?) || '%'"
        params.append(prefix)
    owner = clean_owner_key(owner_key)
    if owner:
        query += " AND deals.owner_key = ?"
        params.append(owner)

    with get_db() as db:
        rows = [dict(r) for r in db.execute(query, params).fetchall()]

    # SQL's non-blank check lets a phone like "---" or "(ext)" through — no
    # digit in it, so it's not actually dialable. Filter those out here
    # rather than teaching SQLite regex for one edge case.
    rows = [row for row in rows if has_callable_phone(row["partner_phone"])]

    scored = []
    for row in rows:
        result = score_deal(row, now=now)
        row.update(result)
        scored.append(row)

    def _score_sort_key(row):
        days = row["days_in_qualified"]
        # Higher score first; among equals, newer-to-qualified first; then name.
        return (
            -row["score"],
            days if days is not None else 10**9,
            (row.get("partner_name") or "").lower(),
            row["id"],
        )

    if campaign:
        # Stable: name, then newest created_at, then dated next_action first.
        scored.sort(key=lambda row: ((row.get("partner_name") or "").lower(), row["id"]))
        scored.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        scored.sort(key=lambda row: 0 if (row.get("next_action_date") or "").strip() else 1)
    else:
        scored.sort(key=_score_sort_key)

    ranked = scored[:limit]
    for i, row in enumerate(ranked, start=1):
        row["rank"] = i
    return ranked


def get_qualified_at(deal_id: int, stage: str):
    """When this one deal last entered `stage` — the same lookup
    list_todays_calls does inline for every row in its query, exposed here
    for a single deal looked at outside the ranked list (e.g. the call
    view), so its score agrees with what the queue would show."""
    with get_db() as db:
        row = db.execute(
            """SELECT occurred_at FROM activities
               WHERE deal_id = ? AND type = 'system' AND body LIKE ?
               ORDER BY occurred_at DESC, id DESC LIMIT 1""",
            (deal_id, stage_changed_to_like(stage)),
        ).fetchone()
        return row["occurred_at"] if row else None
