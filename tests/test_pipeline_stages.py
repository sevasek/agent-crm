from app.services import pipeline_stages
from app.services.partners import create_partner
from app.services.catalog import create_service
from app.services.deals import create_deal, get_deal, set_deal_stage

DEFAULT_PIPELINE_KEYS = ["new", "contacted", "qualified", "nurture", "proposal", "won", "lost"]


def test_default_pipeline_is_seeded(db):
    stages = pipeline_stages.list_stages()
    keys = [s["key"] for s in stages]
    assert keys == DEFAULT_PIPELINE_KEYS
    assert pipeline_stages.default_stage_key() == "new"
    assert pipeline_stages.qualified_pool_keys() == {"qualified"}
    assert pipeline_stages.nurture_trigger_keys() == {"nurture"}
    assert pipeline_stages.won_keys() == {"won"}
    assert pipeline_stages.lost_keys() == {"lost"}
    assert pipeline_stages.closed_stage_keys() == {"won", "lost"}


def test_create_stage_appends_at_end_by_default(db):
    before = pipeline_stages.list_stages()
    stage, error = pipeline_stages.create_stage("demo", "Demo scheduled")
    assert error is None
    assert stage["position"] == max(s["position"] for s in before) + 1
    assert stage["is_default"] == 0


def test_create_stage_rejects_invalid_key(db):
    stage, error = pipeline_stages.create_stage("Not Valid!", "Bad key")
    assert stage is None
    assert error == "invalid_key"


def test_create_stage_allows_underscore_keys(db):
    stage, error = pipeline_stages.create_stage("in_review", "In review")
    assert error is None
    assert stage["key"] == "in_review"


def test_create_stage_rejects_duplicate_key(db):
    pipeline_stages.create_stage("demo", "Demo")
    stage, error = pipeline_stages.create_stage("demo", "Demo again")
    assert stage is None
    assert error == "duplicate_key"


def test_create_stage_requires_label(db):
    stage, error = pipeline_stages.create_stage("demo", "   ")
    assert stage is None
    assert error == "label_required"


def test_create_default_stage_clears_previous_default(db):
    pipeline_stages.create_stage("demo", "Demo", is_default=True)
    assert pipeline_stages.get_stage("demo")["is_default"] == 1
    assert pipeline_stages.get_stage("new")["is_default"] == 0
    assert pipeline_stages.default_stage_key() == "demo"


def test_update_stage_renames_label_only(db):
    stage, error = pipeline_stages.update_stage("new", label="Fresh lead")
    assert error is None
    assert stage["label"] == "Fresh lead"
    assert stage["key"] == "new"


def test_update_stage_toggles_roles(db):
    stage, error = pipeline_stages.update_stage("proposal", is_qualified_pool=True)
    assert error is None
    assert stage["is_qualified_pool"] == 1
    assert pipeline_stages.qualified_pool_keys() == {"qualified", "proposal"}


def test_create_stage_rejects_won_and_lost_together(db):
    stage, error = pipeline_stages.create_stage("demo", "Demo", is_won=True, is_lost=True)
    assert stage is None
    assert error == "won_lost_conflict"


def test_update_stage_rejects_won_and_lost_together(db):
    stage, error = pipeline_stages.update_stage("won", is_lost=True)
    assert stage is None
    assert error == "won_lost_conflict"
    # Original stage is untouched.
    assert pipeline_stages.get_stage("won")["is_lost"] == 0


def test_update_unknown_stage_not_found(db):
    stage, error = pipeline_stages.update_stage("nope", label="x")
    assert stage is None
    assert error == "not_found"


def test_delete_stage_refused_when_in_use(db):
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    create_deal(pid, sid)  # lands in "new"
    ok, error = pipeline_stages.delete_stage("new")
    assert ok is False
    assert error == "stage_in_use"


def test_delete_stage_succeeds_when_unused(db):
    ok, error = pipeline_stages.delete_stage("proposal")
    assert ok is True
    assert error is None
    assert pipeline_stages.get_stage("proposal") is None


def test_delete_last_stage_refused(db):
    keep = "new"
    for key in [s["key"] for s in pipeline_stages.list_stages() if s["key"] != keep]:
        pipeline_stages.delete_stage(key)
    ok, error = pipeline_stages.delete_stage(keep)
    assert ok is False
    assert error == "last_stage"


def test_move_stage_swaps_position(db):
    ok, error = pipeline_stages.move_stage("contacted", "up")
    assert ok is True
    assert error is None
    keys = [s["key"] for s in pipeline_stages.list_stages()]
    assert keys[0] == "contacted"
    assert keys[1] == "new"


def test_move_stage_at_boundary_refused(db):
    ok, error = pipeline_stages.move_stage("new", "up")
    assert ok is False
    assert error == "cannot_move"
    ok, error = pipeline_stages.move_stage("lost", "down")
    assert ok is False
    assert error == "cannot_move"


def test_reorder_stages_sets_full_order(db):
    keys = pipeline_stages.stage_keys()
    new_order = sorted(keys, reverse=True)
    ok, error = pipeline_stages.reorder_stages(new_order)
    assert ok is True
    assert [s["key"] for s in pipeline_stages.list_stages()] == new_order


def test_reorder_stages_rejects_mismatched_key_set(db):
    ok, error = pipeline_stages.reorder_stages(["new", "contacted"])
    assert ok is False
    assert error == "key_set_mismatch"


def test_new_deal_uses_configured_default_stage(db):
    pipeline_stages.update_stage("new", is_default=False)
    pipeline_stages.update_stage("contacted", is_default=True)
    pid = create_partner("Jane Doe", email="jane@x.example")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    assert get_deal(deal_id)["stage"] == "contacted"


def test_renaming_qualified_pool_role_keeps_call_queue_working(db):
    from app.services.call_queue import list_todays_calls

    pipeline_stages.update_stage("qualified", is_qualified_pool=False)
    pipeline_stages.update_stage("proposal", is_qualified_pool=True)
    pid = create_partner("Jane Doe", email="jane@x.example", phone="555-1111")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid, source="referral", value_estimate=6000)
    set_deal_stage(deal_id, "contacted")
    set_deal_stage(deal_id, "qualified")
    set_deal_stage(deal_id, "proposal")

    calls = list_todays_calls()
    assert len(calls) == 1
    assert calls[0]["id"] == deal_id


def test_no_qualified_pool_stage_means_empty_call_queue(db):
    from app.services.call_queue import list_todays_calls

    pipeline_stages.update_stage("qualified", is_qualified_pool=False)
    pid = create_partner("Jane Doe", email="jane@x.example", phone="555-1111")
    sid = create_service("Consulting", "consulting")
    deal_id = create_deal(pid, sid)
    set_deal_stage(deal_id, "contacted")
    set_deal_stage(deal_id, "qualified")

    assert list_todays_calls() == []
