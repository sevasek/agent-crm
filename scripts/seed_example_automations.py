"""Create two example stage automations.

Does not insert services, stages, or prices. The example services from
scripts/seed_services.py must exist first (or create your own with matching
slugs). Re-running does not duplicate a name and does not overwrite an
automation you have edited. Pass --replace to reset these two names to the
definitions below.

    docker compose -f docker-compose.yml -f docker-compose.prod.yml \
        -f docker-compose.traefik.yml run --rm app \
        python scripts/seed_example_automations.py
"""
import sys

sys.path.insert(0, ".")

from app.database import db_timeout, init_db
from app.services.catalog import get_service_by_slug
from app.services import pipeline_stages
from app.services.stage_automations import (
    create_automation,
    get_automation_by_name,
    update_automation,
)

# Names are the idempotency key. Rename in the admin UI if you want a
# different label; this script will then create the original name again
# the next time it runs without --replace.
SEED_AUTOMATIONS = (
    {
        "name": "Proposal: send follow-up",
        "stage_key": "proposal",
        "service_slug": "consulting",
        "actions": [
            {
                "type": "delegated_task",
                "owner": "agent",
                "title": "Follow up on proposal",
                "brief": "Proposal sent. Send the follow-up.",
            },
        ],
    },
    {
        "name": "Won: spawn follow-on deals",
        "stage_key": "won",
        "service_slug": "consulting",
        "actions": [
            {
                "type": "spawn_child_deals",
                "service_slugs": ["website-rebuild", "monthly-retainer"],
            },
        ],
    },
)


def _missing_catalog():
    problems = []
    seen = set()
    for spec in SEED_AUTOMATIONS:
        stage_key = spec["stage_key"]
        if stage_key not in seen and not pipeline_stages.get_stage(stage_key):
            problems.append(f"stage {stage_key} is not in the pipeline")
            seen.add(stage_key)
        for slug in [spec["service_slug"], *spec["actions"][0].get("service_slugs", [])]:
            if slug in seen:
                continue
            seen.add(slug)
            if not get_service_by_slug(slug):
                problems.append(f"service {slug} is not in the catalog")
    return problems


def seed_example_automations(replace=False):
    """Returns {"created": [...], "existing": [...], "replaced": [...]}.

    Raises SystemExit when a required stage or service is missing.
    Does not create catalog rows.
    """
    problems = _missing_catalog()
    if problems:
        raise SystemExit(
            "Cannot seed stage automations:\n- "
            + "\n- ".join(problems)
            + "\nCreate those stages and services first. This script does not insert them."
        )
    created, existing, replaced = [], [], []
    for spec in SEED_AUTOMATIONS:
        current = get_automation_by_name(spec["name"])
        if current and not replace:
            existing.append(spec["name"])
            continue
        fields = {
            "name": spec["name"],
            "stage_key": spec["stage_key"],
            "service_slug": spec["service_slug"],
            "enabled": True,
            "actions": spec["actions"],
        }
        if current and replace:
            automation, error = update_automation(
                current["id"],
                stage_key=spec["stage_key"],
                service_slug=spec["service_slug"],
                offer_id=None,
                enabled=True,
                actions=spec["actions"],
            )
            bucket = replaced
        else:
            automation, error = create_automation(**fields)
            bucket = created
        if error or not automation:
            raise SystemExit(f"Could not save {spec['name']!r}: {error}")
        bucket.append(spec["name"])
    return {"created": created, "existing": existing, "replaced": replaced}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    replace = "--replace" in args
    init_db()
    with db_timeout(30):
        result = seed_example_automations(replace=replace)
    for name in result["created"]:
        print(f"created {name}")
    for name in result["replaced"]:
        print(f"replaced {name}")
    for name in result["existing"]:
        print(f"already present {name}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
