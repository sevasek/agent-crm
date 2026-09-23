import math

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.routers.auth import require_login
from app.services.auth import generate_csrf_token, validate_csrf_token, is_valid_slug
from app.services.partners import (
    create_partner, get_partner, update_partner, list_partners, list_companies, get_children,
    SOCIAL_PLATFORMS, listed_social_links, primary_social_url,
)
from app.services.catalog import create_service, get_service, update_service, list_services
from app.services.deals import (
    create_deal, get_deal, list_deals, set_deal_stage, update_deal_fields,
    record_call_outcome, CALL_OUTCOMES,
)
from app.services import pipeline_stages
from app.services.call_queue import (
    list_todays_calls, score_deal, get_qualified_at, CALL_QUEUE_LIMIT, SCORE_MAX,
)
from app.services.staleness import annotate_deals, list_due_deals, parse_action_date, activity_display_body
from app.services.activities import log_activity, list_activities_for_partner, list_activities_for_deal
from app.services.phone import to_tel_href, has_callable_phone
from app.services import offers as offers_service
from app.services import icp as icp_service
from app.services import api_keys as api_keys_service

router = APIRouter(prefix="", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["tel_href"] = to_tel_href
templates.env.tests["dialable"] = has_callable_phone

# SQLite INTEGER is signed 64-bit; Python's int is unbounded, so an
# out-of-range value would raise OverflowError on bind rather than fail
# validation here. Same bounds/shape as api.py's _safe_int for /api/v1/stages.
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


def _safe_int(value):
    """int from a form field's string (or a real int/float), within
    SQLite's signed 64-bit range; None for anything else — never raises."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX else None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        value = int(value)
        return value if _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX else None
    return None


@router.get("/partners", response_class=HTMLResponse)
async def dashboard(request: Request, q: str = "", user=Depends(require_login)):
    return templates.TemplateResponse("admin/partners.html", {
        "request": request, "user": user, "partners": list_partners(q), "q": q,
    })


def _partner_form_context(request, user, partner, csrf_token):
    return {
        "request": request, "user": user, "companies": list_companies(),
        "partner": partner, "csrf_token": csrf_token,
        "social_fields": [(field, label) for field, label, _hosts in SOCIAL_PLATFORMS],
    }


def _partner_fields_from_form(
    *, name, is_company, parent_id, email, phone, website, title, address,
    preferred_channel, industry, team_size,
    linkedin_url, x_url, instagram_url, facebook_url, youtube_url,
    owner_key="",
):
    """Drop person-only / company-only fields that do not apply to this partner."""
    company = bool(is_company)
    return {
        "name": name,
        "is_company": company,
        "parent_id": None if company else (int(parent_id) if parent_id else None),
        "email": email,
        "phone": phone,
        "website": website,
        "title": "" if company else title,
        "address": address,
        "preferred_channel": preferred_channel,
        "industry": industry,
        "team_size": team_size if company else "",
        "linkedin_url": linkedin_url,
        "x_url": x_url,
        "instagram_url": instagram_url,
        "facebook_url": facebook_url,
        "youtube_url": youtube_url,
        "owner_key": owner_key,
    }


# ==================== Partners ====================
@router.get("/partners/new", response_class=HTMLResponse)
async def new_partner_page(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse("admin/partner_form.html", _partner_form_context(
        request, user, None, generate_csrf_token(),
    ))


@router.post("/partners/new")
async def new_partner_submit(
    request: Request,
    name: str = Form(...), is_company: str = Form(""), parent_id: str = Form(""),
    email: str = Form(""), phone: str = Form(""), website: str = Form(""), title: str = Form(""),
    address: str = Form(""), preferred_channel: str = Form(""),
    industry: str = Form(""), team_size: str = Form(""),
    linkedin_url: str = Form(""), x_url: str = Form(""), instagram_url: str = Form(""),
    facebook_url: str = Form(""), youtube_url: str = Form(""),
    owner_key: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/partners/new", status_code=303)
    partner_id = create_partner(**_partner_fields_from_form(
        name=name, is_company=is_company, parent_id=parent_id, email=email,
        phone=phone, website=website, title=title, address=address,
        preferred_channel=preferred_channel, industry=industry, team_size=team_size,
        linkedin_url=linkedin_url, x_url=x_url, instagram_url=instagram_url,
        facebook_url=facebook_url, youtube_url=youtube_url, owner_key=owner_key,
    ))
    return RedirectResponse(f"/partners/{partner_id}", status_code=303)


@router.get("/partners/{partner_id}", response_class=HTMLResponse)
async def partner_detail(request: Request, partner_id: int, user=Depends(require_login)):
    partner = get_partner(partner_id)
    if not partner:
        return RedirectResponse("/partners", status_code=303)
    activities = list_activities_for_partner(partner_id)
    for item in activities:
        item["display_body"] = activity_display_body(item.get("body") or "")
    return templates.TemplateResponse("admin/partner_detail.html", {
        "request": request, "user": user, "partner": partner,
        "children": get_children(partner_id) if partner["is_company"] else [],
        "deals": annotate_deals(list_deals(partner_id=partner_id)),
        "activities": activities,
        "services": list_services(active_only=True),
        "stages": pipeline_stages.list_stages(),
        "social_links": listed_social_links(partner),
        "csrf_token": generate_csrf_token(),
    })


@router.get("/partners/{partner_id}/edit", response_class=HTMLResponse)
async def edit_partner_page(request: Request, partner_id: int, user=Depends(require_login)):
    partner = get_partner(partner_id)
    if not partner:
        return RedirectResponse("/partners", status_code=303)
    return templates.TemplateResponse("admin/partner_form.html", _partner_form_context(
        request, user, partner, generate_csrf_token(),
    ))


@router.post("/partners/{partner_id}/edit")
async def edit_partner_submit(
    request: Request, partner_id: int,
    name: str = Form(...), is_company: str = Form(""), parent_id: str = Form(""),
    email: str = Form(""), phone: str = Form(""), website: str = Form(""), title: str = Form(""),
    address: str = Form(""), preferred_channel: str = Form(""),
    industry: str = Form(""), team_size: str = Form(""),
    linkedin_url: str = Form(""), x_url: str = Form(""), instagram_url: str = Form(""),
    facebook_url: str = Form(""), youtube_url: str = Form(""),
    owner_key: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse(f"/partners/{partner_id}/edit", status_code=303)
    fields = _partner_fields_from_form(
        name=name, is_company=is_company, parent_id=parent_id, email=email,
        phone=phone, website=website, title=title, address=address,
        preferred_channel=preferred_channel, industry=industry, team_size=team_size,
        linkedin_url=linkedin_url, x_url=x_url, instagram_url=instagram_url,
        facebook_url=facebook_url, youtube_url=youtube_url, owner_key=owner_key,
    )
    fields["is_company"] = 1 if fields["is_company"] else 0
    update_partner(partner_id, **fields)
    return RedirectResponse(f"/partners/{partner_id}", status_code=303)


@router.post("/partners/{partner_id}/activities")
async def add_activity(
    request: Request, partner_id: int,
    type: str = Form("note"), body: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if validate_csrf_token(csrf_token) and body.strip():
        log_activity(partner_id, type, body)
    return RedirectResponse(f"/partners/{partner_id}", status_code=303)


# ==================== Services (catalog) ====================
@router.get("/services", response_class=HTMLResponse)
async def services_list(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse("admin/services.html", {
        "request": request, "user": user, "services": list_services(),
        "csrf_token": generate_csrf_token(),
    })


@router.get("/services/new", response_class=HTMLResponse)
async def new_service_page(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse("admin/service_form.html", {
        "request": request, "user": user, "service": None, "csrf_token": generate_csrf_token(),
    })


@router.post("/services/new")
async def new_service_submit(
    request: Request,
    name: str = Form(...), slug: str = Form(...), description: str = Form(""),
    nurture_list_slug: str = Form(""), csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token) or not is_valid_slug(slug):
        return templates.TemplateResponse("admin/service_form.html", {
            "request": request, "user": user, "service": None,
            "error": "Invalid submission or slug (lowercase letters, numbers, hyphens only).",
            "csrf_token": generate_csrf_token(),
        }, status_code=400)
    if nurture_list_slug and not is_valid_slug(nurture_list_slug):
        return templates.TemplateResponse("admin/service_form.html", {
            "request": request, "user": user, "service": None,
            "error": "List slug must be lowercase letters, numbers, and hyphens.",
            "csrf_token": generate_csrf_token(),
        }, status_code=400)
    create_service(name, slug, description, nurture_list_slug)
    return RedirectResponse("/services", status_code=303)


@router.get("/services/{service_id}/edit", response_class=HTMLResponse)
async def edit_service_page(request: Request, service_id: int, user=Depends(require_login)):
    service = get_service(service_id)
    if not service:
        return RedirectResponse("/services", status_code=303)
    return templates.TemplateResponse("admin/service_form.html", {
        "request": request, "user": user, "service": service, "csrf_token": generate_csrf_token(),
    })


@router.post("/services/{service_id}/edit")
async def edit_service_submit(
    request: Request, service_id: int,
    name: str = Form(...), description: str = Form(""),
    nurture_list_slug: str = Form(""), active: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/services", status_code=303)
    if nurture_list_slug and not is_valid_slug(nurture_list_slug):
        return templates.TemplateResponse("admin/service_form.html", {
            "request": request, "user": user, "service": get_service(service_id),
            "error": "List slug must be lowercase letters, numbers, and hyphens.",
            "csrf_token": generate_csrf_token(),
        }, status_code=400)
    update_service(
        service_id, name=name, description=description,
        nurture_list_slug=nurture_list_slug, active=1 if active else 0,
    )
    return RedirectResponse("/services", status_code=303)


# ==================== Today's calls ====================
@router.get("/calls", response_class=HTMLResponse)
async def todays_calls(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse("admin/calls.html", {
        "request": request, "user": user,
        "calls": list_todays_calls(),
        "limit": CALL_QUEUE_LIMIT,
        "score_max": SCORE_MAX,
        "outcomes": CALL_OUTCOMES,
        "csrf_token": generate_csrf_token(),
    })


@router.post("/deals/{deal_id}/call-outcome")
async def log_call_outcome(
    request: Request, deal_id: int,
    outcome: str = Form(...), note: str = Form(""), next: str = Form("calls"),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if validate_csrf_token(csrf_token):
        record_call_outcome(deal_id, outcome, note)
    if next == "call_view":
        return RedirectResponse(f"/deals/{deal_id}/call", status_code=303)
    return RedirectResponse("/calls", status_code=303)


@router.get("/deals/{deal_id}/call", response_class=HTMLResponse)
async def deal_call_view(request: Request, deal_id: int, user=Depends(require_login)):
    """The phone-in-hand view: just what a rep needs mid-call — the number,
    the talk track (pain points/goals/next action), recent history, and the
    outcome buttons. Everything else on the full partner page is noise here."""
    deal = get_deal(deal_id)
    if not deal:
        return RedirectResponse("/calls", status_code=303)
    partner = get_partner(deal["partner_id"])
    service = get_service(deal["service_id"])
    if not partner or not service:
        return RedirectResponse("/calls", status_code=303)

    activities = list_activities_for_deal(deal_id)[:5]
    for item in activities:
        item["display_body"] = activity_display_body(item.get("body") or "")

    score = None
    if deal["stage"] in pipeline_stages.qualified_pool_keys():
        scoring_row = dict(deal)
        scoring_row.update({
            "partner_email": partner.get("email"),
            "partner_title": partner.get("title"),
            "partner_website": partner.get("website"),
            "partner_social_url": primary_social_url(partner),
            "partner_preferred_channel": partner.get("preferred_channel"),
            "qualified_at": get_qualified_at(deal_id, deal["stage"]),
        })
        score = score_deal(scoring_row)

    icp_criteria = icp_service.list_criteria(active_only=True)
    fit = None
    if icp_criteria:
        fit_row = icp_service.build_matchable_row(partner, deal)
        fit = icp_service.score_fit(fit_row, icp_criteria)

    offer = offers_service.get_offer(deal.get("offer_id"))
    offer_options = offers_service.list_offers(active_only=True)

    return templates.TemplateResponse("admin/call_view.html", {
        "request": request, "user": user,
        "deal": deal, "partner": partner, "service": service,
        "activities": activities, "score": score, "score_max": SCORE_MAX,
        "fit": fit, "offer": offer, "offer_options": offer_options,
        "outcomes": CALL_OUTCOMES,
        "csrf_token": generate_csrf_token(),
    })


@router.post("/deals/{deal_id}/offer")
async def set_deal_offer(
    request: Request, deal_id: int,
    offer_id: str = Form(""), csrf_token: str = Form(...), user=Depends(require_login),
):
    deal = get_deal(deal_id)
    if deal and validate_csrf_token(csrf_token):
        if not offer_id.strip():
            # The select's "— none —" option: an explicit clear.
            update_deal_fields(deal_id, offer_id=None)
        else:
            parsed = _safe_int(offer_id)
            if parsed is not None:
                update_deal_fields(deal_id, offer_id=parsed)
            # else: unparseable (tampered request — the <select> only ever
            # emits "" or a valid id) — leave the existing offer as-is
            # rather than wiping it on garbage input.
    if deal:
        return RedirectResponse(f"/deals/{deal_id}/call", status_code=303)
    return RedirectResponse("/calls", status_code=303)


@router.post("/deals/{deal_id}/offer/new")
async def create_offer_for_deal(
    request: Request, deal_id: int,
    name: str = Form(...), pitch: str = Form(""), proof_point: str = Form(""),
    price_anchor: str = Form(""), csrf_token: str = Form(...), user=Depends(require_login),
):
    """"Create a new offer" from the call view itself — no detour through
    /offers when you think of a better pitch mid-prep."""
    deal = get_deal(deal_id)
    if not deal or not validate_csrf_token(csrf_token):
        return RedirectResponse(f"/deals/{deal_id}/call", status_code=303)
    offer, error = offers_service.create_offer(
        name, pitch=pitch, proof_point=proof_point, price_anchor=price_anchor,
    )
    if offer:
        update_deal_fields(deal_id, offer_id=offer["id"])
    return RedirectResponse(f"/deals/{deal_id}/call", status_code=303)


@router.post("/deals/{deal_id}/call-tel")
async def deal_call_tel(
    request: Request, deal_id: int,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    """Fired via `navigator.sendBeacon` alongside a real `<a href="tel:">`
    tap — the actual dial goes through the browser's native, reliable
    tel: link handling; this just logs it. (An earlier version tried to log
    by redirecting the link itself to tel: via a 303, but a custom-scheme
    HTTP redirect isn't the same as a user-gesture link tap and mobile
    Safari in particular doesn't reliably treat it as one.) Best-effort: if
    the deal/CSRF/phone don't check out, the call still happens via the
    href regardless of this endpoint's outcome, so failures here are silent
    rather than surfaced anywhere the user would see them.
    """
    deal = get_deal(deal_id)
    if not deal or not validate_csrf_token(csrf_token):
        return Response(status_code=204)
    partner = get_partner(deal["partner_id"])
    phone = (partner or {}).get("phone") or ""
    if not has_callable_phone(phone):
        return Response(status_code=204)
    log_activity(deal["partner_id"], "call", "Called (tapped phone number)", deal_id=deal_id)
    return Response(status_code=204)


@router.post("/partners/{partner_id}/call-tel")
async def partner_call_tel(
    request: Request, partner_id: int,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    partner = get_partner(partner_id)
    phone = (partner or {}).get("phone") or ""
    if not partner or not validate_csrf_token(csrf_token) or not has_callable_phone(phone):
        return Response(status_code=204)
    log_activity(partner_id, "call", "Called (tapped phone number)")
    return Response(status_code=204)


# ==================== Deals ====================
@router.get("/deals", response_class=HTMLResponse)
async def deals_list(request: Request, stage: str = "", due: str = "", user=Depends(require_login)):
    due_filter = due.strip().lower() in {"1", "true", "yes", "due"}
    if due_filter:
        deals = list_due_deals(stage=stage or None)
    else:
        deals = annotate_deals(list_deals(stage=stage or None))
    return templates.TemplateResponse("admin/deals.html", {
        "request": request, "user": user,
        "deals": deals, "stages": pipeline_stages.list_stages(),
        "current_stage": stage, "current_due": due_filter,
    })


@router.get("/deals/new", response_class=HTMLResponse)
async def new_deal_page(request: Request, partner_id: int = 0, user=Depends(require_login)):
    return templates.TemplateResponse("admin/deal_form.html", {
        "request": request, "user": user, "partners": list_partners(),
        "services": list_services(active_only=True), "preselect_partner_id": partner_id,
        "deal": None, "partner": None, "service": None,
        "csrf_token": generate_csrf_token(),
    })


@router.post("/deals/new")
async def new_deal_submit(
    request: Request,
    partner_id: int = Form(...), service_id: int = Form(...), source: str = Form(""),
    value_estimate: str = Form(""), pain_points: str = Form(""), goals: str = Form(""),
    next_action: str = Form(""), next_action_date: str = Form(""),
    owner_key: str = Form(""), external_ref: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/deals/new", status_code=303)
    deal_id = create_deal(
        partner_id, service_id, source=source,
        value_estimate=float(value_estimate) if value_estimate else None,
        pain_points=pain_points, goals=goals,
        next_action=next_action, next_action_date=next_action_date or None,
        owner_key=owner_key, external_ref=external_ref,
    )
    return RedirectResponse(f"/partners/{partner_id}", status_code=303)


@router.get("/deals/{deal_id}/edit", response_class=HTMLResponse)
async def edit_deal_page(request: Request, deal_id: int, user=Depends(require_login)):
    deal = get_deal(deal_id)
    if not deal:
        return RedirectResponse("/deals", status_code=303)
    partner = get_partner(deal["partner_id"])
    service = get_service(deal["service_id"])
    if not partner or not service:
        return RedirectResponse("/deals", status_code=303)
    return templates.TemplateResponse("admin/deal_form.html", {
        "request": request, "user": user, "deal": deal,
        "partner": partner, "service": service,
        "csrf_token": generate_csrf_token(),
    })


@router.post("/deals/{deal_id}/edit")
async def edit_deal_submit(
    request: Request, deal_id: int,
    source: str = Form(""), value_estimate: str = Form(""),
    pain_points: str = Form(""), goals: str = Form(""),
    next_action: str = Form(""), next_action_date: str = Form(""),
    owner_key: str = Form(""), external_ref: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    deal = get_deal(deal_id)
    if not deal:
        return RedirectResponse("/deals", status_code=303)
    if not get_partner(deal["partner_id"]) or not get_service(deal["service_id"]):
        return RedirectResponse("/deals", status_code=303)
    if validate_csrf_token(csrf_token):
        raw_value = value_estimate.strip()
        if not raw_value:
            value = None
        else:
            try:
                value = float(raw_value)
            except ValueError:
                value = deal["value_estimate"]
        update_deal_fields(
            deal_id,
            source=source,
            value_estimate=value,
            pain_points=pain_points,
            goals=goals,
            next_action=next_action,
            next_action_date=(next_action_date or "").strip()[:10] or None,
            owner_key=owner_key,
            external_ref=external_ref,
        )
    return RedirectResponse(f"/partners/{deal['partner_id']}", status_code=303)


@router.post("/deals/{deal_id}/next-action")
async def save_deal_next_action(
    request: Request, deal_id: int,
    next_action: str = Form(""), next_action_date: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    deal = get_deal(deal_id)
    if deal and validate_csrf_token(csrf_token):
        parsed = parse_action_date(next_action_date)
        update_deal_fields(
            deal_id,
            next_action=next_action,
            next_action_date=parsed.isoformat() if parsed else None,
        )
    if deal:
        return RedirectResponse(f"/partners/{deal['partner_id']}", status_code=303)
    return RedirectResponse("/deals", status_code=303)


@router.post("/deals/{deal_id}/stage")
async def change_deal_stage(
    request: Request, deal_id: int,
    stage: str = Form(...), next: str = Form("partner"),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    deal = get_deal(deal_id)
    if deal and validate_csrf_token(csrf_token):
        set_deal_stage(deal_id, stage)
    if next == "pipeline":
        return RedirectResponse("/pipeline", status_code=303)
    if deal:
        return RedirectResponse(f"/partners/{deal['partner_id']}", status_code=303)
    return RedirectResponse("/deals", status_code=303)


# ==================== Pipeline (Kanban board) ====================
@router.get("/pipeline", response_class=HTMLResponse)
async def pipeline_board(request: Request, user=Depends(require_login)):
    stages = pipeline_stages.list_stages()
    columns = [
        {"stage": s, "deals": annotate_deals(list_deals(stage=s["key"]))}
        for s in stages
    ]
    return templates.TemplateResponse("admin/pipeline.html", {
        "request": request, "user": user,
        "columns": columns, "stages": stages,
        "csrf_token": generate_csrf_token(),
    })


# ==================== Pipeline stage settings ====================
_STAGE_ERROR_MESSAGES = {
    "invalid_key": "Key must be 2–50 lowercase letters, numbers, hyphens, or underscores.",
    "label_required": "Label is required.",
    "duplicate_key": "A stage with that key already exists.",
    "stage_in_use": "Can't delete a stage that deals are currently in — move them first.",
    "last_stage": "Can't delete the only remaining stage.",
    "not_found": "Stage not found.",
    "won_lost_conflict": "A stage can't be both Won and Lost.",
}


@router.get("/stages", response_class=HTMLResponse)
async def stages_settings(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse("admin/stages.html", {
        "request": request, "user": user,
        "stages": pipeline_stages.list_stages(),
        "csrf_token": generate_csrf_token(),
    })


@router.post("/stages/new")
async def create_stage_submit(
    request: Request,
    key: str = Form(...), label: str = Form(...),
    is_default: str = Form(""), is_qualified_pool: str = Form(""),
    triggers_nurture: str = Form(""), is_won: str = Form(""), is_lost: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/stages", status_code=303)
    _, error = pipeline_stages.create_stage(
        key, label,
        is_default=bool(is_default), is_qualified_pool=bool(is_qualified_pool),
        triggers_nurture=bool(triggers_nurture), is_won=bool(is_won), is_lost=bool(is_lost),
    )
    if error:
        return templates.TemplateResponse("admin/stages.html", {
            "request": request, "user": user,
            "stages": pipeline_stages.list_stages(),
            "csrf_token": generate_csrf_token(),
            "error": _STAGE_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/stages", status_code=303)


@router.post("/stages/{key}/edit")
async def edit_stage_submit(
    request: Request, key: str,
    label: str = Form(...),
    is_default: str = Form(""), is_qualified_pool: str = Form(""),
    triggers_nurture: str = Form(""), is_won: str = Form(""), is_lost: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/stages", status_code=303)
    _, error = pipeline_stages.update_stage(
        key, label=label,
        is_default=bool(is_default), is_qualified_pool=bool(is_qualified_pool),
        triggers_nurture=bool(triggers_nurture), is_won=bool(is_won), is_lost=bool(is_lost),
    )
    if error:
        return templates.TemplateResponse("admin/stages.html", {
            "request": request, "user": user,
            "stages": pipeline_stages.list_stages(),
            "csrf_token": generate_csrf_token(),
            "error": _STAGE_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/stages", status_code=303)


@router.post("/stages/{key}/move")
async def move_stage_submit(
    request: Request, key: str,
    direction: str = Form(...), csrf_token: str = Form(...), user=Depends(require_login),
):
    if validate_csrf_token(csrf_token) and direction in ("up", "down"):
        pipeline_stages.move_stage(key, direction)
    return RedirectResponse("/stages", status_code=303)


@router.post("/stages/{key}/delete")
async def delete_stage_submit(
    request: Request, key: str,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/stages", status_code=303)
    _, error = pipeline_stages.delete_stage(key)
    if error:
        return templates.TemplateResponse("admin/stages.html", {
            "request": request, "user": user,
            "stages": pipeline_stages.list_stages(),
            "csrf_token": generate_csrf_token(),
            "error": _STAGE_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/stages", status_code=303)


# ==================== Offers ====================
_OFFER_ERROR_MESSAGES = {
    "name_required": "Name is required.",
    "not_found": "Offer not found.",
    "offer_in_use": "Can't delete an offer that deals are currently using — reassign them first.",
    "invalid_service": "Unknown service.",
}


def _offer_form_service_id(service_id: str):
    if not (service_id or "").strip():
        return None
    parsed = _safe_int(service_id)
    return parsed


def _offer_form_price(price: str):
    text = (price or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


@router.get("/offers", response_class=HTMLResponse)
async def offers_settings(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse("admin/offers.html", {
        "request": request, "user": user,
        "offers": offers_service.list_offers(),
        "services": list_services(),
        "csrf_token": generate_csrf_token(),
    })


@router.post("/offers/new")
async def create_offer_submit(
    request: Request,
    name: str = Form(...), pitch: str = Form(""), proof_point: str = Form(""),
    price_anchor: str = Form(""), is_default: str = Form(""), active: str = Form(""),
    service_id: str = Form(""), price: str = Form(""), currency: str = Form("USD"),
    description: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/offers", status_code=303)
    _, error = offers_service.create_offer(
        name, pitch=pitch, proof_point=proof_point, price_anchor=price_anchor,
        is_default=bool(is_default), active=bool(active),
        service_id=_offer_form_service_id(service_id),
        price=_offer_form_price(price),
        currency=currency or "USD",
        description=description,
    )
    if error:
        return templates.TemplateResponse("admin/offers.html", {
            "request": request, "user": user,
            "offers": offers_service.list_offers(),
            "services": list_services(),
            "csrf_token": generate_csrf_token(),
            "error": _OFFER_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/offers", status_code=303)


@router.post("/offers/{offer_id}/edit")
async def edit_offer_submit(
    request: Request, offer_id: int,
    name: str = Form(...), pitch: str = Form(""), proof_point: str = Form(""),
    price_anchor: str = Form(""), is_default: str = Form(""), active: str = Form(""),
    service_id: str = Form(""), price: str = Form(""), currency: str = Form("USD"),
    description: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/offers", status_code=303)
    _, error = offers_service.update_offer(
        offer_id, name=name, pitch=pitch, proof_point=proof_point, price_anchor=price_anchor,
        is_default=bool(is_default), active=bool(active),
        service_id=_offer_form_service_id(service_id),
        price=_offer_form_price(price),
        currency=currency or "USD",
        description=description,
    )
    if error:
        return templates.TemplateResponse("admin/offers.html", {
            "request": request, "user": user,
            "offers": offers_service.list_offers(),
            "services": list_services(),
            "csrf_token": generate_csrf_token(),
            "error": _OFFER_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/offers", status_code=303)


@router.post("/offers/{offer_id}/delete")
async def delete_offer_submit(
    request: Request, offer_id: int,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/offers", status_code=303)
    _, error = offers_service.delete_offer(offer_id)
    if error:
        return templates.TemplateResponse("admin/offers.html", {
            "request": request, "user": user,
            "offers": offers_service.list_offers(),
            "services": list_services(),
            "csrf_token": generate_csrf_token(),
            "error": _OFFER_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/offers", status_code=303)


# ==================== ICP fit criteria ====================
_ICP_ERROR_MESSAGES = {
    "invalid_field": "Unknown field.",
    "invalid_operator": "Unknown operator.",
    "invalid_value": "Value doesn't match what this operator needs (e.g. a number, or true/false).",
    "invalid_weight": "Weight must be a whole number.",
    "not_found": "Criterion not found.",
}


@router.get("/icp", response_class=HTMLResponse)
async def icp_settings(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse("admin/icp.html", {
        "request": request, "user": user,
        "criteria": icp_service.list_criteria(),
        "fields": icp_service.MATCHABLE_FIELDS,
        "operators": icp_service.OPERATORS,
        "csrf_token": generate_csrf_token(),
    })


@router.post("/icp/new")
async def create_icp_criterion_submit(
    request: Request,
    label: str = Form(""), field: str = Form(...), operator: str = Form(...),
    value: str = Form(""), weight: str = Form("1"), active: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/icp", status_code=303)
    weight_int = _safe_int(weight)
    if weight_int is None:
        return templates.TemplateResponse("admin/icp.html", {
            "request": request, "user": user,
            "criteria": icp_service.list_criteria(),
            "fields": icp_service.MATCHABLE_FIELDS,
            "operators": icp_service.OPERATORS,
            "csrf_token": generate_csrf_token(),
            "error": _ICP_ERROR_MESSAGES.get("invalid_weight"),
        }, status_code=400)
    _, error = icp_service.create_criterion(
        field, operator, value, weight=weight_int, label=label, active=bool(active),
    )
    if error:
        return templates.TemplateResponse("admin/icp.html", {
            "request": request, "user": user,
            "criteria": icp_service.list_criteria(),
            "fields": icp_service.MATCHABLE_FIELDS,
            "operators": icp_service.OPERATORS,
            "csrf_token": generate_csrf_token(),
            "error": _ICP_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/icp", status_code=303)


@router.post("/icp/{criterion_id}/edit")
async def edit_icp_criterion_submit(
    request: Request, criterion_id: int,
    label: str = Form(""), field: str = Form(...), operator: str = Form(...),
    value: str = Form(""), weight: str = Form("1"), active: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/icp", status_code=303)
    weight_int = _safe_int(weight)
    if weight_int is None:
        return templates.TemplateResponse("admin/icp.html", {
            "request": request, "user": user,
            "criteria": icp_service.list_criteria(),
            "fields": icp_service.MATCHABLE_FIELDS,
            "operators": icp_service.OPERATORS,
            "csrf_token": generate_csrf_token(),
            "error": _ICP_ERROR_MESSAGES.get("invalid_weight"),
        }, status_code=400)
    _, error = icp_service.update_criterion(
        criterion_id, label=label, field=field, operator=operator,
        value=value, weight=weight_int, active=bool(active),
    )
    if error:
        return templates.TemplateResponse("admin/icp.html", {
            "request": request, "user": user,
            "criteria": icp_service.list_criteria(),
            "fields": icp_service.MATCHABLE_FIELDS,
            "operators": icp_service.OPERATORS,
            "csrf_token": generate_csrf_token(),
            "error": _ICP_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/icp", status_code=303)


@router.post("/icp/{criterion_id}/delete")
async def delete_icp_criterion_submit(
    request: Request, criterion_id: int,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if validate_csrf_token(csrf_token):
        icp_service.delete_criterion(criterion_id)
    return RedirectResponse("/icp", status_code=303)


# ==================== Account settings / API keys ====================
@router.get("/settings", response_class=HTMLResponse)
async def account_settings(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse("admin/settings.html", {
        "request": request, "user": user,
        "api_keys": api_keys_service.list_api_keys(user["id"]),
        "csrf_token": generate_csrf_token(),
    })


@router.post("/settings/api-keys/new")
async def create_api_key_submit(
    request: Request,
    label: str = Form(""), csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token):
        return RedirectResponse("/settings", status_code=303)
    _, new_key = api_keys_service.create_api_key(user["id"], label)
    # Rendered directly (not redirected) so the plaintext key can be shown
    # once — it's never recoverable after this response, since only its hash
    # is stored.
    return templates.TemplateResponse("admin/settings.html", {
        "request": request, "user": user,
        "api_keys": api_keys_service.list_api_keys(user["id"]),
        "csrf_token": generate_csrf_token(),
        "new_key": new_key,
    })


@router.post("/settings/api-keys/{key_id}/delete")
async def delete_api_key_submit(
    request: Request, key_id: int,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if validate_csrf_token(csrf_token):
        api_keys_service.delete_api_key(user["id"], key_id)
    return RedirectResponse("/settings", status_code=303)
