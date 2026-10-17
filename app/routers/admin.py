import math

from urllib.parse import urlencode

from fastapi import APIRouter, Request, Form, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.routers.auth import get_session_cookie, require_login, set_session_cookie
from app.services.auth import (
    bump_session_version, change_password, generate_csrf_token, is_valid_slug,
    set_user_currency, validate_csrf_token,
)
from app.services.partners import (
    create_partner, get_partner, update_partner, list_partners, list_companies, get_children,
    SOCIAL_PLATFORMS, listed_social_links, primary_social_url,
)
from app.services.partner_tags import (
    apply_partner_tags, attach_tags as attach_partner_tags, parse_tag_list as parse_partner_tag_list,
    coerce_tag_filter as coerce_partner_tag_filter, tags_for_partner,
)
from app.services.catalog import create_service, get_service, update_service, list_services
from app.services.deal_tags import apply_deal_tags, list_tags, normalize_tag, parse_tag_list
from app.services.deals import (
    create_deal, get_deal, list_deals, list_parent_candidates, mark_lost,
    restore_deal, set_deal_stage, update_deal_fields, record_call_outcome,
    validate_parent_link, CALL_OUTCOMES,
)
from app.services.lead_records import create_lead, get_lead, list_leads, update_lead, convert_lead
from app.services.lost_reasons import (
    create_lost_reason, get_lost_reason, list_lost_reasons, update_lost_reason,
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
from app.services import stage_automations
from app.services import api_keys as api_keys_service

router = APIRouter(prefix="", tags=["admin"])
# Deals/Pipeline/Leads live under /crm; Services/Stages/Automations/
# Lost-reasons/Offers/ICP live under /definitions (each route's own path
# literal already includes its prefix) — both share this same router
# object since it carries no prefix of its own. Apps, Contacts and
# Settings' account page stay unprefixed.
crm_router = APIRouter(prefix="", tags=["admin"])
# Calls (today's call queue + the per-deal call view/outcome/offer actions
# reached from it) live under /actions.
actions_router = APIRouter(prefix="", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["tel_href"] = to_tel_href
templates.env.tests["dialable"] = has_callable_phone

# Plain-language help for people with no CRM/technical background — one
# shared doc (docs/WELCOME.md), one section per object, linked by anchor.
# Not served by this app (see docs/ package.json: doc-build-time only), so
# this points at the upstream repo, same as docs/_includes/base.njk already
# does for its own GitHub link.
HELP_DOC_URL = "https://github.com/sevasek/agent-crm/blob/main/docs/WELCOME.md"


def help_url(anchor=None):
    return HELP_DOC_URL if not anchor else f"{HELP_DOC_URL}#{anchor}"


templates.env.globals["help_url"] = help_url

# SQLite INTEGER is signed 64-bit; Python's int is unbounded, so an
# out-of-range value would raise OverflowError on bind rather than fail
# validation here. Same bounds/shape as api.py's _safe_int for /api/v1/stages.
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _render_partial(name, **context):
    return templates.env.get_template(name).render(context)


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


# ==================== Apps launcher ====================
# Fixed-size tiles in a single flex-wrap row (see .apps-grid in style.css)
# so the count per line is left to the browser rather than fixed server
# side — that's what lets it reflow on narrow/mobile viewports.
#
# Background colours are the first five slots of the house categorical
# palette (dataviz skill's references/palette.md), assigned in the
# palette's fixed order for maximum hue variety across these tiles.
_PALETTE_SLOTS = [
    {"name": "blue", "light": "#2a78d6", "dark": "#3987e5"},
    {"name": "orange", "light": "#eb6834", "dark": "#d95926"},
    {"name": "aqua", "light": "#1baf7a", "dark": "#199e70"},
    {"name": "yellow", "light": "#eda100", "dark": "#c98500"},
    {"name": "magenta", "light": "#e87ba4", "dark": "#d55181"},
    {"name": "green", "light": "#008300", "dark": "#008300"},
]


def _srgb_channel(value):
    value /= 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color):
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _srgb_channel(r) + 0.7152 * _srgb_channel(g) + 0.0722 * _srgb_channel(b)


def _contrast_ratio(hex_a, hex_b):
    lum_a, lum_b = _relative_luminance(hex_a), _relative_luminance(hex_b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def _best_icon_color(bg_hex):
    """WCAG contrast of white vs. black against bg_hex — never eyeballed."""
    return "white" if _contrast_ratio(bg_hex, "#ffffff") >= _contrast_ratio(bg_hex, "#000000") else "black"


def _apps_menu():
    # Order here is the palette-assignment order, not necessarily the
    # display order — CRM/Actions/Definitions' tile colors are load-bearing:
    # they match that section's own theme color (test_crm_theme.py), so
    # existing entries keep their original slot. Help is appended on its
    # own new slot, then spliced into the display order below.
    specs = [
        {"label": "Contacts", "href": "/contacts/partners", "icon": "contacts"},
        {"label": "CRM", "href": "/crm/pipeline", "icon": "crm"},
        {"label": "Actions", "href": "/actions/calls", "icon": "actions"},
        {"label": "Settings", "href": "/settings", "icon": "settings"},
        {"label": "Definitions", "href": "/definitions/services", "icon": "definitions"},
        {"label": "Help", "href": help_url(), "icon": "help", "external": True},
    ]
    apps = {}
    for spec, slot in zip(specs, _PALETTE_SLOTS):
        apps[spec["label"]] = {
            **spec,
            "bg_light": slot["light"],
            "bg_dark": slot["dark"],
            # The current six palette slots land on the same icon colour
            # for both their light- and dark-mode hex, so one computed
            # choice (checked against the light value) covers both; if a
            # future slot's modes disagree this would need a per-mode icon.
            "icon_color": _best_icon_color(slot["light"]),
        }
    # Display order: Help sits just before Settings.
    return [apps[label] for label in
            ("Contacts", "CRM", "Actions", "Help", "Settings", "Definitions")]


APPS_MENU = _apps_menu()


@router.get("/apps", response_class=HTMLResponse)
async def apps_launcher(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(request, "admin/apps.html", {
        "request": request, "user": user, "apps": APPS_MENU,
    })


_PARTNER_SORT_COLUMNS = ("name", "type", "email", "phone", "tags")


def _partners_url(q="", tags=(), sort="name", direction="asc"):
    params = []
    if q:
        params.append(("q", q))
    for t in tags:
        params.append(("tag", t))
    if sort and sort != "name":
        params.append(("sort", sort))
    if direction and direction != "asc":
        params.append(("dir", direction))
    qs = urlencode(params)
    return f"/contacts/partners?{qs}" if qs else "/contacts/partners"


def _partner_sort_links(q, tags, sort, direction):
    links = {}
    for col in _PARTNER_SORT_COLUMNS:
        active = col == sort
        next_dir = ("desc" if direction == "asc" else "asc") if active else "asc"
        links[col] = {
            "href": _partners_url(q, tags, sort=col, direction=next_dir),
            "dir": direction if active else None,
        }
    return links


@router.get("/contacts/partners", response_class=HTMLResponse)
async def dashboard(
    request: Request, q: str = "", tag: list[str] = Query(default=[]),
    sort: str = "name", dir: str = "asc", user=Depends(require_login),
):
    sort = sort if sort in _PARTNER_SORT_COLUMNS else "name"
    direction = "desc" if dir == "desc" else "asc"
    wanted_tags, impossible = coerce_partner_tag_filter(tag)
    partners = [] if impossible else list_partners(q, tags=wanted_tags, sort=sort, direction=direction)
    attach_partner_tags(partners)
    return templates.TemplateResponse(request, "admin/partners.html", {
        "request": request, "user": user, "partners": partners, "q": q,
        "current_tags": wanted_tags,
        "sort_links": _partner_sort_links(q, wanted_tags, sort, direction),
        "remove_tag_links": {
            t: _partners_url(q, [o for o in wanted_tags if o != t], sort=sort, direction=direction)
            for t in wanted_tags
        },
        "add_tag_link": lambda t: _partners_url(q, (*wanted_tags, t), sort=sort, direction=direction),
    })


def _partner_form_context(request, user, partner, csrf_token, error=None, form_tags=None):
    return {
        "request": request, "user": user, "companies": list_companies(),
        "partner": partner, "csrf_token": csrf_token,
        "social_fields": [(field, label) for field, label, _hosts in SOCIAL_PLATFORMS],
        "error": error,
        "form_tags": form_tags,
    }


def _partner_form_response(request, user, partner, error, status_code=400, form_tags=None):
    return templates.TemplateResponse(
        request, "admin/partner_form.html",
        _partner_form_context(
            request, user, partner, generate_csrf_token(get_session_cookie(request)),
            error=error, form_tags=form_tags,
        ),
        status_code=status_code,
    )


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


# ==================== Partners (Contacts app) ====================
@router.get("/contacts/partners/new", response_class=HTMLResponse)
async def new_partner_page(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(request, "admin/partner_form.html", _partner_form_context(
        request, user, None, generate_csrf_token(get_session_cookie(request)),
    ))


@router.post("/contacts/partners/new")
async def new_partner_submit(
    request: Request,
    name: str = Form(""), is_company: str = Form(""), parent_id: str = Form(""),
    email: str = Form(""), phone: str = Form(""), website: str = Form(""), title: str = Form(""),
    address: str = Form(""), preferred_channel: str = Form(""),
    industry: str = Form(""), team_size: str = Form(""),
    linkedin_url: str = Form(""), x_url: str = Form(""), instagram_url: str = Form(""),
    facebook_url: str = Form(""), youtube_url: str = Form(""),
    owner_key: str = Form(""), tags: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/contacts/partners/new", status_code=303)
    if not (name or "").strip():
        return _partner_form_response(request, user, None, "Name is required.", form_tags=tags)
    parsed_tags, tag_error = parse_partner_tag_list(tags)
    if tag_error:
        return _partner_form_response(request, user, None, tag_error, form_tags=tags)
    partner_id = create_partner(**_partner_fields_from_form(
        name=name, is_company=is_company, parent_id=parent_id, email=email,
        phone=phone, website=website, title=title, address=address,
        preferred_channel=preferred_channel, industry=industry, team_size=team_size,
        linkedin_url=linkedin_url, x_url=x_url, instagram_url=instagram_url,
        facebook_url=facebook_url, youtube_url=youtube_url, owner_key=owner_key,
    ))
    if parsed_tags:
        apply_partner_tags(partner_id, replace=parsed_tags)
    return RedirectResponse(f"/contacts/partners/{partner_id}", status_code=303)


@router.get("/contacts/partners/{partner_id}", response_class=HTMLResponse)
async def partner_detail(request: Request, partner_id: int, user=Depends(require_login)):
    partner = get_partner(partner_id)
    if not partner:
        return RedirectResponse("/contacts/partners", status_code=303)
    activities = list_activities_for_partner(partner_id)
    for item in activities:
        item["display_body"] = activity_display_body(item.get("body") or "")
    return templates.TemplateResponse(request, "admin/partner_detail.html", {
        "request": request, "user": user, "partner": partner,
        "children": get_children(partner_id) if partner["is_company"] else [],
        "deals": annotate_deals(list_deals(partner_id=partner_id)),
        "activities": activities,
        "services": list_services(active_only=True),
        "stages": pipeline_stages.list_stages(),
        "social_links": listed_social_links(partner),
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
    })


@router.get("/contacts/partners/{partner_id}/edit", response_class=HTMLResponse)
async def edit_partner_page(request: Request, partner_id: int, user=Depends(require_login)):
    partner = get_partner(partner_id)
    if not partner:
        return RedirectResponse("/contacts/partners", status_code=303)
    return templates.TemplateResponse(request, "admin/partner_form.html", _partner_form_context(
        request, user, partner, generate_csrf_token(get_session_cookie(request)),
        form_tags=", ".join(tags_for_partner(partner_id)),
    ))


@router.post("/contacts/partners/{partner_id}/edit")
async def edit_partner_submit(
    request: Request, partner_id: int,
    name: str = Form(""), is_company: str = Form(""), parent_id: str = Form(""),
    email: str = Form(""), phone: str = Form(""), website: str = Form(""), title: str = Form(""),
    address: str = Form(""), preferred_channel: str = Form(""),
    industry: str = Form(""), team_size: str = Form(""),
    linkedin_url: str = Form(""), x_url: str = Form(""), instagram_url: str = Form(""),
    facebook_url: str = Form(""), youtube_url: str = Form(""),
    owner_key: str = Form(""), tags: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse(f"/contacts/partners/{partner_id}/edit", status_code=303)
    if not (name or "").strip():
        return _partner_form_response(
            request, user, get_partner(partner_id), "Name is required.", form_tags=tags,
        )
    parsed_tags, tag_error = parse_partner_tag_list(tags)
    if tag_error:
        return _partner_form_response(
            request, user, get_partner(partner_id), tag_error, form_tags=tags,
        )
    fields = _partner_fields_from_form(
        name=name, is_company=is_company, parent_id=parent_id, email=email,
        phone=phone, website=website, title=title, address=address,
        preferred_channel=preferred_channel, industry=industry, team_size=team_size,
        linkedin_url=linkedin_url, x_url=x_url, instagram_url=instagram_url,
        facebook_url=facebook_url, youtube_url=youtube_url, owner_key=owner_key,
    )
    fields["is_company"] = 1 if fields["is_company"] else 0
    update_partner(partner_id, **fields)
    apply_partner_tags(partner_id, replace=parsed_tags or [])
    return RedirectResponse(f"/contacts/partners/{partner_id}", status_code=303)


@router.post("/contacts/partners/{partner_id}/activities")
async def add_activity(
    request: Request, partner_id: int,
    type: str = Form("note"), body: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if validate_csrf_token(csrf_token, get_session_cookie(request)) and body.strip():
        log_activity(partner_id, type, body)
    return RedirectResponse(f"/contacts/partners/{partner_id}", status_code=303)


# ==================== Services (catalog) ====================
@crm_router.get("/definitions/services", response_class=HTMLResponse)
async def services_list(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(request, "admin/services.html", {
        "request": request, "user": user, "services": list_services(),
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
    })


@crm_router.get("/definitions/services/new", response_class=HTMLResponse)
async def new_service_page(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(request, "admin/service_form.html", {
        "request": request, "user": user, "service": None, "csrf_token": generate_csrf_token(get_session_cookie(request)),
    })


@crm_router.post("/definitions/services/new")
async def new_service_submit(
    request: Request,
    name: str = Form(""), slug: str = Form(""), description: str = Form(""),
    nurture_list_slug: str = Form(""), csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)) or not is_valid_slug(slug):
        return templates.TemplateResponse(request, "admin/service_form.html", {
            "request": request, "user": user, "service": None,
            "error": "Invalid submission or slug (lowercase letters, numbers, hyphens only).",
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
        }, status_code=400)
    if not (name or "").strip():
        return templates.TemplateResponse(request, "admin/service_form.html", {
            "request": request, "user": user, "service": None,
            "error": "Name is required.",
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
        }, status_code=400)
    if nurture_list_slug and not is_valid_slug(nurture_list_slug):
        return templates.TemplateResponse(request, "admin/service_form.html", {
            "request": request, "user": user, "service": None,
            "error": "List slug must be lowercase letters, numbers, and hyphens.",
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
        }, status_code=400)
    create_service(name, slug, description, nurture_list_slug)
    return RedirectResponse("/definitions/services", status_code=303)


@crm_router.get("/definitions/services/{service_id}/edit", response_class=HTMLResponse)
async def edit_service_page(request: Request, service_id: int, user=Depends(require_login)):
    service = get_service(service_id)
    if not service:
        return RedirectResponse("/definitions/services", status_code=303)
    return templates.TemplateResponse(request, "admin/service_form.html", {
        "request": request, "user": user, "service": service, "csrf_token": generate_csrf_token(get_session_cookie(request)),
    })


@crm_router.post("/definitions/services/{service_id}/edit")
async def edit_service_submit(
    request: Request, service_id: int,
    name: str = Form(""), description: str = Form(""),
    nurture_list_slug: str = Form(""), active: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/definitions/services", status_code=303)
    if not (name or "").strip():
        return templates.TemplateResponse(request, "admin/service_form.html", {
            "request": request, "user": user, "service": get_service(service_id),
            "error": "Name is required.",
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
        }, status_code=400)
    if nurture_list_slug and not is_valid_slug(nurture_list_slug):
        return templates.TemplateResponse(request, "admin/service_form.html", {
            "request": request, "user": user, "service": get_service(service_id),
            "error": "List slug must be lowercase letters, numbers, and hyphens.",
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
        }, status_code=400)
    update_service(
        service_id, name=name, description=description,
        nurture_list_slug=nurture_list_slug, active=1 if active else 0,
    )
    return RedirectResponse("/definitions/services", status_code=303)


# ==================== Today's calls ====================
@actions_router.get("/actions/calls", response_class=HTMLResponse)
async def todays_calls(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(request, "admin/calls.html", {
        "request": request, "user": user,
        "calls": list_todays_calls(),
        "limit": CALL_QUEUE_LIMIT,
        "score_max": SCORE_MAX,
        "outcomes": CALL_OUTCOMES,
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
    })


@actions_router.post("/actions/deals/{deal_id}/call-outcome")
async def log_call_outcome(
    request: Request, deal_id: int,
    outcome: str = Form(""), note: str = Form(""), next: str = Form("calls"),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if validate_csrf_token(csrf_token, get_session_cookie(request)):
        record_call_outcome(deal_id, outcome, note)
    if _is_htmx(request):
        if next == "call_view":
            ctx = _call_view_context(request, user, deal_id)
            if not ctx:
                response = HTMLResponse("")
                response.headers["HX-Redirect"] = "/actions/calls"
                return response
            return HTMLResponse(_render_partial("admin/_call_view_body.html", **ctx))
        return HTMLResponse(_render_partial(
            "admin/_calls_table.html",
            request=request, user=user,
            calls=list_todays_calls(),
            limit=CALL_QUEUE_LIMIT,
            score_max=SCORE_MAX,
            outcomes=CALL_OUTCOMES,
            csrf_token=generate_csrf_token(get_session_cookie(request)),
        ))
    if next == "call_view":
        return RedirectResponse(f"/actions/deals/{deal_id}/call", status_code=303)
    return RedirectResponse("/actions/calls", status_code=303)


def _call_view_context(request, user, deal_id):
    deal = get_deal(deal_id)
    if not deal:
        return None
    partner = get_partner(deal["partner_id"])
    service = get_service(deal["service_id"])
    if not partner or not service:
        return None

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

    return {
        "request": request, "user": user,
        "deal": deal, "partner": partner, "service": service,
        "activities": activities, "score": score, "score_max": SCORE_MAX,
        "fit": fit, "offer": offer, "offer_options": offer_options,
        "outcomes": CALL_OUTCOMES,
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
    }


@actions_router.get("/actions/deals/{deal_id}/call", response_class=HTMLResponse)
async def deal_call_view(request: Request, deal_id: int, user=Depends(require_login)):
    """The phone-in-hand view: just what a rep needs mid-call — the number,
    the talk track (pain points/goals/next action), recent history, and the
    outcome buttons. Everything else on the full partner page is noise here."""
    ctx = _call_view_context(request, user, deal_id)
    if not ctx:
        return RedirectResponse("/actions/calls", status_code=303)
    return templates.TemplateResponse(request, "admin/call_view.html", ctx)


@actions_router.post("/actions/deals/{deal_id}/offer")
async def set_deal_offer(
    request: Request, deal_id: int,
    offer_id: str = Form(""), csrf_token: str = Form(...), user=Depends(require_login),
):
    deal = get_deal(deal_id)
    if deal and validate_csrf_token(csrf_token, get_session_cookie(request)):
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
        return RedirectResponse(f"/actions/deals/{deal_id}/call", status_code=303)
    return RedirectResponse("/actions/calls", status_code=303)


@actions_router.post("/actions/deals/{deal_id}/offer/new")
async def create_offer_for_deal(
    request: Request, deal_id: int,
    name: str = Form(""), pitch: str = Form(""), proof_point: str = Form(""),
    price_anchor: str = Form(""), csrf_token: str = Form(...), user=Depends(require_login),
):
    """"Create a new offer" from the call view itself — no detour through
    /offers when you think of a better pitch mid-prep."""
    deal = get_deal(deal_id)
    if not deal or not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse(f"/actions/deals/{deal_id}/call", status_code=303)
    offer, error = offers_service.create_offer(
        name, pitch=pitch, proof_point=proof_point, price_anchor=price_anchor,
    )
    if offer:
        update_deal_fields(deal_id, offer_id=offer["id"])
    return RedirectResponse(f"/actions/deals/{deal_id}/call", status_code=303)


@actions_router.post("/actions/deals/{deal_id}/call-tel")
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
    if not deal or not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return Response(status_code=204)
    partner = get_partner(deal["partner_id"])
    phone = (partner or {}).get("phone") or ""
    if not has_callable_phone(phone):
        return Response(status_code=204)
    log_activity(deal["partner_id"], "call", "Called (tapped phone number)", deal_id=deal_id)
    return Response(status_code=204)


@router.post("/contacts/partners/{partner_id}/call-tel")
async def partner_call_tel(
    request: Request, partner_id: int,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    partner = get_partner(partner_id)
    phone = (partner or {}).get("phone") or ""
    if not partner or not validate_csrf_token(csrf_token, get_session_cookie(request)) or not has_callable_phone(phone):
        return Response(status_code=204)
    log_activity(partner_id, "call", "Called (tapped phone number)")
    return Response(status_code=204)


# ==================== Deals ====================
def _tag_query(raw: str):
    """A single tag from the deals/pipeline query string.

    Returns (slug or "", error). An empty query is no filter. An invalid
    slug matches nothing and is reported so the page does not show every deal.
    """
    text = (raw or "").strip()
    if not text:
        return "", ""
    slug = normalize_tag(text)
    if not slug:
        return "", "That tag is not a valid slug."
    return slug, ""


def _truthy_query(raw: str, *aliases):
    return (raw or "").strip().lower() in {"1", "true", "yes", *aliases}


def _deals_for_board(stage: str, due: bool, tag: str, *, lost_only: bool = False):
    tag_filter = [tag] if tag else None
    include_lost = lost_only
    if due:
        deals = list_due_deals(stage=stage or None, tags=tag_filter, include_lost=include_lost)
    else:
        deals = annotate_deals(list_deals(
            stage=stage or None, tags=tag_filter, include_lost=include_lost,
        ))
    if lost_only:
        deals = [d for d in deals if not d.get("active", 1)]
    return deals


@crm_router.get("/crm/deals", response_class=HTMLResponse)
async def deals_list(
    request: Request, stage: str = "", due: str = "", tag: str = "",
    lost: str = "",
    user=Depends(require_login),
):
    due_filter = _truthy_query(due, "due")
    lost_filter = _truthy_query(lost, "lost")
    current_tag, tag_error = _tag_query(tag)
    if tag_error:
        deals = []
    else:
        deals = _deals_for_board(stage, due_filter, current_tag, lost_only=lost_filter)
    return templates.TemplateResponse(request, "admin/deals.html", {
        "request": request, "user": user,
        "deals": deals, "stages": pipeline_stages.list_stages(),
        "current_stage": stage, "current_due": due_filter,
        "current_lost": lost_filter,
        "current_tag": current_tag, "tag_error": tag_error,
        "tags_in_use": list_tags(),
    })


_PARENT_FORM_ERRORS = {
    "invalid_parent": "Pick a parent deal, or leave it unset.",
    "parent_not_found": "That parent deal does not exist.",
    "parent_partner_mismatch": "Parent deal must belong to the same partner.",
    "parent_cycle": "That parent would create a cycle.",
}


def _parent_form_error(code):
    return _PARENT_FORM_ERRORS.get(code, "Invalid parent deal.")


def _group_parent_candidates(deals):
    """Optgroups labelled by partner so an unfiltered picker is usable without JS."""
    groups = {}
    for d in deals:
        pid = d["partner_id"]
        if pid not in groups:
            groups[pid] = {
                "partner_id": pid,
                "label": d.get("partner_name") or f"Partner #{pid}",
                "candidates": [],
            }
        groups[pid]["candidates"].append(d)
    return sorted(groups.values(), key=lambda g: (g["label"] or "").lower())


def _deal_parent_context(deal=None, partner_id=0):
    if deal:
        return {
            "parent_candidates": list_parent_candidates(
                deal["partner_id"], exclude_deal_id=deal["id"],
            ),
            "parent_candidate_groups": None,
            "child_deals": list_deals(parent_deal_id=deal["id"]),
        }
    if partner_id:
        return {
            "parent_candidates": list_parent_candidates(partner_id),
            "parent_candidate_groups": None,
            "child_deals": [],
        }
    candidates = list_deals()
    return {
        "parent_candidates": candidates,
        "parent_candidate_groups": _group_parent_candidates(candidates),
        "child_deals": [],
    }


def _new_deal_form(request, user, partner_id=0, form_tags=None, form_parent_deal_id=None,
                   error=None, status_code=200):
    ctx = {
        "request": request, "user": user, "partners": list_partners(),
        "services": list_services(active_only=True), "preselect_partner_id": partner_id,
        "deal": None, "partner": None, "service": None,
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
        "form_tags": form_tags, "form_parent_deal_id": form_parent_deal_id,
        "error": error, "lost_reasons": list_lost_reasons(active_only=True),
    }
    ctx.update(_deal_parent_context(partner_id=partner_id))
    return templates.TemplateResponse(request, "admin/deal_form.html", ctx, status_code=status_code)


def _edit_deal_form(request, user, deal, partner, service, form_tags=None,
                    form_parent_deal_id=None, error=None, status_code=200):
    if deal and deal.get("lost_reason_id") and "lost_reason" not in deal:
        deal = dict(deal)
        deal["lost_reason"] = get_lost_reason(deal["lost_reason_id"])
    ctx = {
        "request": request, "user": user, "deal": deal,
        "partner": partner, "service": service,
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
        "form_tags": form_tags, "form_parent_deal_id": form_parent_deal_id,
        "error": error, "lost_reasons": list_lost_reasons(active_only=True),
    }
    ctx.update(_deal_parent_context(deal))
    return templates.TemplateResponse(request, "admin/deal_form.html", ctx, status_code=status_code)


def _annotate_child_counts(columns):
    """Count every child, not only deals still visible after a tag filter."""
    visible = [d for col in columns for d in col["deals"]]
    counts = {}
    for d in list_deals():
        parent = d.get("parent_deal_id")
        if parent:
            counts[parent] = counts.get(parent, 0) + 1
    for d in visible:
        d["child_count"] = counts.get(d["id"], 0)
    return columns


def _pipeline_board_data(current_tag="", tag_error="", include_lost=False):
    stages = pipeline_stages.list_stages()
    tag_filter = [current_tag] if current_tag else None
    columns = [
        {
            "stage": s,
            "deals": [] if tag_error else annotate_deals(list_deals(
                stage=s["key"], tags=tag_filter, include_lost=include_lost,
                order="board",
            )),
        }
        for s in stages
    ]
    if include_lost:
        for col in columns:
            col["deals"] = [d for d in col["deals"] if not d.get("active", 1)]
    _annotate_child_counts(columns)
    return stages, columns


def _pipeline_columns_fragment(request, user, keys, current_tag="", tag_error="",
                               include_lost=False):
    stages, columns = _pipeline_board_data(current_tag, tag_error, include_lost=include_lost)
    by_key = {c["stage"]["key"]: c for c in columns}
    csrf_token = generate_csrf_token(get_session_cookie(request))
    parts = []
    seen = set()
    for key in keys:
        if not key or key in seen:
            continue
        seen.add(key)
        col = by_key.get(key)
        if not col:
            continue
        parts.append(_render_partial(
            "admin/_pipeline_column.html",
            request=request, user=user, col=col, stages=stages,
            csrf_token=csrf_token, current_tag=current_tag,
            current_lost=include_lost, oob=True,
        ))
    return "".join(parts)


@crm_router.get("/crm/deals/new", response_class=HTMLResponse)
async def new_deal_page(request: Request, partner_id: int = 0, user=Depends(require_login)):
    return _new_deal_form(request, user, partner_id=partner_id)


@crm_router.post("/crm/deals/new")
async def new_deal_submit(
    request: Request,
    partner_id: str = Form(""), service_id: str = Form(""), source: str = Form(""),
    value_estimate: str = Form(""), pain_points: str = Form(""), goals: str = Form(""),
    next_action: str = Form(""), next_action_date: str = Form(""),
    owner_key: str = Form(""), external_ref: str = Form(""),
    parent_deal_id: str = Form(""),
    tags: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/crm/deals/new", status_code=303)
    parsed_partner_id = _safe_int(partner_id) or 0
    parsed_service_id = _safe_int(service_id)
    if not parsed_partner_id or not parsed_service_id:
        return _new_deal_form(
            request, user, partner_id=parsed_partner_id, form_tags=tags,
            form_parent_deal_id=parent_deal_id,
            error="Pick a partner and a service.", status_code=400,
        )
    parsed_tags, tag_error = parse_tag_list(tags)
    if tag_error:
        return _new_deal_form(
            request, user, partner_id=parsed_partner_id, form_tags=tags,
            form_parent_deal_id=parent_deal_id, error=tag_error, status_code=400,
        )
    cleaned_parent, parent_error = validate_parent_link(
        None, parent_deal_id, parsed_partner_id,
    )
    if parent_error:
        return _new_deal_form(
            request, user, partner_id=parsed_partner_id, form_tags=tags,
            form_parent_deal_id=parent_deal_id, error=_parent_form_error(parent_error),
            status_code=400,
        )
    create_deal(
        parsed_partner_id, parsed_service_id, source=source,
        value_estimate=float(value_estimate) if value_estimate else None,
        pain_points=pain_points, goals=goals,
        next_action=next_action, next_action_date=next_action_date or None,
        owner_key=owner_key, external_ref=external_ref,
        parent_deal_id=cleaned_parent,
        tags=parsed_tags or None,
    )
    return RedirectResponse(f"/contacts/partners/{parsed_partner_id}", status_code=303)


@crm_router.get("/crm/deals/{deal_id}/edit", response_class=HTMLResponse)
async def edit_deal_page(request: Request, deal_id: int, user=Depends(require_login)):
    deal = get_deal(deal_id)
    if not deal:
        return RedirectResponse("/crm/deals", status_code=303)
    if deal.get("type") == "lead":
        return RedirectResponse(f"/crm/leads/{deal_id}", status_code=303)
    partner = get_partner(deal["partner_id"])
    service = get_service(deal["service_id"])
    if not partner or not service:
        return RedirectResponse("/crm/deals", status_code=303)
    return _edit_deal_form(request, user, deal, partner, service)


@crm_router.post("/crm/deals/{deal_id}/edit")
async def edit_deal_submit(
    request: Request, deal_id: int,
    source: str = Form(""), value_estimate: str = Form(""),
    pain_points: str = Form(""), goals: str = Form(""),
    next_action: str = Form(""), next_action_date: str = Form(""),
    owner_key: str = Form(""), external_ref: str = Form(""),
    parent_deal_id: str = Form(""),
    tags: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    deal = get_deal(deal_id)
    if not deal:
        return RedirectResponse("/crm/deals", status_code=303)
    if deal.get("type") == "lead":
        return RedirectResponse(f"/crm/leads/{deal_id}", status_code=303)
    partner = get_partner(deal["partner_id"])
    service = get_service(deal["service_id"])
    if not partner or not service:
        return RedirectResponse("/crm/deals", status_code=303)
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse(f"/crm/deals/{deal_id}/edit", status_code=303)
    parsed_tags, tag_error = parse_tag_list(tags)
    if tag_error:
        return _edit_deal_form(
            request, user, deal, partner, service, form_tags=tags,
            form_parent_deal_id=parent_deal_id, error=tag_error, status_code=400,
        )
    cleaned_parent, parent_error = validate_parent_link(
        deal_id, parent_deal_id, deal["partner_id"],
    )
    if parent_error:
        return _edit_deal_form(
            request, user, deal, partner, service, form_tags=tags,
            form_parent_deal_id=parent_deal_id, error=_parent_form_error(parent_error),
            status_code=400,
        )
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
        parent_deal_id=cleaned_parent,
    )
    apply_deal_tags(deal_id, replace=parsed_tags or [])
    return RedirectResponse(f"/contacts/partners/{deal['partner_id']}", status_code=303)


_LOST_FORM_ERRORS = {
    "not_found": "That record was not found.",
    "already_lost": "That record is already lost.",
    "invalid_lost_reason": "Pick a lost reason.",
    "not_lost": "That record is not lost.",
    "merged": "A merged record cannot be restored.",
}


@crm_router.post("/crm/deals/{deal_id}/lost")
async def mark_deal_lost_submit(
    request: Request, deal_id: int,
    lost_reason: str = Form(""), note: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    deal = get_deal(deal_id)
    if not deal:
        return RedirectResponse("/crm/deals", status_code=303)
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse(f"/crm/deals/{deal_id}/edit", status_code=303)
    _updated, error = mark_lost(deal_id, lost_reason=lost_reason, note=note)
    if error:
        partner = get_partner(deal["partner_id"]) if deal.get("partner_id") else None
        service = get_service(deal["service_id"]) if deal.get("service_id") else None
        return _edit_deal_form(
            request, user, deal, partner, service,
            error=_LOST_FORM_ERRORS.get(error, error), status_code=400,
        )
    if deal.get("type") == "lead":
        return RedirectResponse(f"/crm/leads/{deal_id}", status_code=303)
    return RedirectResponse(f"/crm/deals/{deal_id}/edit", status_code=303)


@crm_router.post("/crm/deals/{deal_id}/restore")
async def restore_deal_submit(
    request: Request, deal_id: int,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    deal = get_deal(deal_id)
    if not deal:
        return RedirectResponse("/crm/deals", status_code=303)
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse(f"/crm/deals/{deal_id}/edit", status_code=303)
    _updated, error = restore_deal(deal_id)
    if error:
        partner = get_partner(deal["partner_id"]) if deal.get("partner_id") else None
        service = get_service(deal["service_id"]) if deal.get("service_id") else None
        return _edit_deal_form(
            request, user, deal, partner, service,
            error=_LOST_FORM_ERRORS.get(error, error), status_code=400,
        )
    if deal.get("type") == "lead":
        return RedirectResponse(f"/crm/leads/{deal_id}", status_code=303)
    return RedirectResponse(f"/crm/deals/{deal_id}/edit", status_code=303)


@crm_router.post("/crm/deals/{deal_id}/next-action")
async def save_deal_next_action(
    request: Request, deal_id: int,
    next_action: str = Form(""), next_action_date: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    deal = get_deal(deal_id)
    if deal and validate_csrf_token(csrf_token, get_session_cookie(request)):
        parsed = parse_action_date(next_action_date)
        update_deal_fields(
            deal_id,
            next_action=next_action,
            next_action_date=parsed.isoformat() if parsed else None,
        )
    if deal:
        return RedirectResponse(f"/contacts/partners/{deal['partner_id']}", status_code=303)
    return RedirectResponse("/crm/deals", status_code=303)


@crm_router.post("/crm/deals/{deal_id}/stage")
async def change_deal_stage(
    request: Request, deal_id: int,
    stage: str = Form(...), next: str = Form("partner"),
    tag: str = Form(""),
    lost: str = Form(""),
    position: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    deal = get_deal(deal_id)
    old_stage = deal["stage"] if deal else None
    parsed_position = _safe_int(position) if str(position).strip() != "" else None
    # Mirror the MCP guard: only active opportunities change stage over HTTP.
    # Leads must be converted first; lost records must be restored first.
    if (
        deal
        and deal.get("type") == "opportunity"
        and deal.get("active", 1)
        and validate_csrf_token(csrf_token, get_session_cookie(request))
    ):
        set_deal_stage(deal_id, stage, position=parsed_position)
    include_lost = _truthy_query(lost, "lost")
    if _is_htmx(request) and next == "pipeline":
        current_tag, tag_error = _tag_query(tag)
        html = _pipeline_columns_fragment(
            request, user, [old_stage, stage], current_tag, tag_error,
            include_lost=include_lost,
        )
        return HTMLResponse(html)
    if next == "pipeline":
        suffix = []
        if include_lost:
            suffix.append("lost=1")
        if tag.strip():
            suffix.append(f"tag={tag.strip()}")
        query = ("?" + "&".join(suffix)) if suffix else ""
        return RedirectResponse(f"/crm/pipeline{query}", status_code=303)
    if deal:
        return RedirectResponse(f"/contacts/partners/{deal['partner_id']}", status_code=303)
    return RedirectResponse("/crm/deals", status_code=303)


# ==================== Pipeline (Kanban board) ====================
@crm_router.get("/crm/pipeline", response_class=HTMLResponse)
async def pipeline_board(
    request: Request, tag: str = "", lost: str = "", user=Depends(require_login),
):
    current_tag, tag_error = _tag_query(tag)
    include_lost = _truthy_query(lost, "lost")
    stages, columns = _pipeline_board_data(
        current_tag, tag_error, include_lost=include_lost,
    )
    return templates.TemplateResponse(request, "admin/pipeline.html", {
        "request": request, "user": user,
        "columns": columns, "stages": stages,
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
        "current_tag": current_tag, "tag_error": tag_error,
        "current_lost": include_lost,
        "tags_in_use": list_tags(),
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
    "invalid_stage_role": "The Lost stage role is deprecated. Mark a deal record lost instead of adding a Lost stage.",
}


@crm_router.get("/definitions/stages", response_class=HTMLResponse)
async def stages_settings(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(request, "admin/stages.html", {
        "request": request, "user": user,
        "stages": pipeline_stages.list_stages(),
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
    })


@crm_router.post("/definitions/stages/new")
async def create_stage_submit(
    request: Request,
    key: str = Form(""), label: str = Form(""),
    is_default: str = Form(""), is_qualified_pool: str = Form(""),
    triggers_nurture: str = Form(""), is_won: str = Form(""), is_lost: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/definitions/stages", status_code=303)
    _, error = pipeline_stages.create_stage(
        key, label,
        is_default=bool(is_default), is_qualified_pool=bool(is_qualified_pool),
        triggers_nurture=bool(triggers_nurture), is_won=bool(is_won), is_lost=bool(is_lost),
    )
    if error:
        return templates.TemplateResponse(request, "admin/stages.html", {
            "request": request, "user": user,
            "stages": pipeline_stages.list_stages(),
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
            "error": _STAGE_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/definitions/stages", status_code=303)


@crm_router.post("/definitions/stages/{key}/edit")
async def edit_stage_submit(
    request: Request, key: str,
    label: str = Form(""),
    is_default: str = Form(""), is_qualified_pool: str = Form(""),
    triggers_nurture: str = Form(""), is_won: str = Form(""), is_lost: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/definitions/stages", status_code=303)
    _, error = pipeline_stages.update_stage(
        key, label=label,
        is_default=bool(is_default), is_qualified_pool=bool(is_qualified_pool),
        triggers_nurture=bool(triggers_nurture), is_won=bool(is_won), is_lost=bool(is_lost),
    )
    if error:
        return templates.TemplateResponse(request, "admin/stages.html", {
            "request": request, "user": user,
            "stages": pipeline_stages.list_stages(),
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
            "error": _STAGE_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/definitions/stages", status_code=303)


@crm_router.post("/definitions/stages/{key}/move")
async def move_stage_submit(
    request: Request, key: str,
    direction: str = Form(...), csrf_token: str = Form(...), user=Depends(require_login),
):
    if validate_csrf_token(csrf_token, get_session_cookie(request)) and direction in ("up", "down"):
        pipeline_stages.move_stage(key, direction)
    return RedirectResponse("/definitions/stages", status_code=303)


@crm_router.post("/definitions/stages/{key}/delete")
async def delete_stage_submit(
    request: Request, key: str,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/definitions/stages", status_code=303)
    _, error = pipeline_stages.delete_stage(key)
    if error:
        return templates.TemplateResponse(request, "admin/stages.html", {
            "request": request, "user": user,
            "stages": pipeline_stages.list_stages(),
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
            "error": _STAGE_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/definitions/stages", status_code=303)


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


@crm_router.get("/definitions/offers", response_class=HTMLResponse)
async def offers_settings(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(request, "admin/offers.html", {
        "request": request, "user": user,
        "offers": offers_service.list_offers(),
        "services": list_services(),
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
    })


@crm_router.post("/definitions/offers/new")
async def create_offer_submit(
    request: Request,
    name: str = Form(""), pitch: str = Form(""), proof_point: str = Form(""),
    price_anchor: str = Form(""), is_default: str = Form(""), active: str = Form(""),
    service_id: str = Form(""), price: str = Form(""), currency: str = Form("USD"),
    description: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/definitions/offers", status_code=303)
    _, error = offers_service.create_offer(
        name, pitch=pitch, proof_point=proof_point, price_anchor=price_anchor,
        is_default=bool(is_default), active=bool(active),
        service_id=_offer_form_service_id(service_id),
        price=_offer_form_price(price),
        currency=currency or "USD",
        description=description,
    )
    if error:
        return templates.TemplateResponse(request, "admin/offers.html", {
            "request": request, "user": user,
            "offers": offers_service.list_offers(),
            "services": list_services(),
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
            "error": _OFFER_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/definitions/offers", status_code=303)


@crm_router.post("/definitions/offers/{offer_id}/edit")
async def edit_offer_submit(
    request: Request, offer_id: int,
    name: str = Form(""), pitch: str = Form(""), proof_point: str = Form(""),
    price_anchor: str = Form(""), is_default: str = Form(""), active: str = Form(""),
    service_id: str = Form(""), price: str = Form(""), currency: str = Form("USD"),
    description: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/definitions/offers", status_code=303)
    _, error = offers_service.update_offer(
        offer_id, name=name, pitch=pitch, proof_point=proof_point, price_anchor=price_anchor,
        is_default=bool(is_default), active=bool(active),
        service_id=_offer_form_service_id(service_id),
        price=_offer_form_price(price),
        currency=currency or "USD",
        description=description,
    )
    if error:
        return templates.TemplateResponse(request, "admin/offers.html", {
            "request": request, "user": user,
            "offers": offers_service.list_offers(),
            "services": list_services(),
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
            "error": _OFFER_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/definitions/offers", status_code=303)


@crm_router.post("/definitions/offers/{offer_id}/delete")
async def delete_offer_submit(
    request: Request, offer_id: int,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/definitions/offers", status_code=303)
    _, error = offers_service.delete_offer(offer_id)
    if error:
        return templates.TemplateResponse(request, "admin/offers.html", {
            "request": request, "user": user,
            "offers": offers_service.list_offers(),
            "services": list_services(),
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
            "error": _OFFER_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/definitions/offers", status_code=303)


# ==================== ICP fit criteria ====================
_ICP_ERROR_MESSAGES = {
    "invalid_field": "Unknown field.",
    "invalid_operator": "Unknown operator.",
    "invalid_value": "Value doesn't match what this operator needs (e.g. a number, or true/false).",
    "invalid_weight": "Weight must be a whole number.",
    "not_found": "Criterion not found.",
}


@crm_router.get("/definitions/icp", response_class=HTMLResponse)
async def icp_settings(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(request, "admin/icp.html", {
        "request": request, "user": user,
        "criteria": icp_service.list_criteria(),
        "fields": icp_service.MATCHABLE_FIELDS,
        "operators": icp_service.OPERATORS,
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
    })


@crm_router.post("/definitions/icp/new")
async def create_icp_criterion_submit(
    request: Request,
    label: str = Form(""), field: str = Form(""), operator: str = Form(""),
    value: str = Form(""), weight: str = Form("1"), active: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/definitions/icp", status_code=303)
    weight_int = _safe_int(weight)
    if weight_int is None:
        return templates.TemplateResponse(request, "admin/icp.html", {
            "request": request, "user": user,
            "criteria": icp_service.list_criteria(),
            "fields": icp_service.MATCHABLE_FIELDS,
            "operators": icp_service.OPERATORS,
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
            "error": _ICP_ERROR_MESSAGES.get("invalid_weight"),
        }, status_code=400)
    _, error = icp_service.create_criterion(
        field, operator, value, weight=weight_int, label=label, active=bool(active),
    )
    if error:
        return templates.TemplateResponse(request, "admin/icp.html", {
            "request": request, "user": user,
            "criteria": icp_service.list_criteria(),
            "fields": icp_service.MATCHABLE_FIELDS,
            "operators": icp_service.OPERATORS,
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
            "error": _ICP_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/definitions/icp", status_code=303)


@crm_router.post("/definitions/icp/{criterion_id}/edit")
async def edit_icp_criterion_submit(
    request: Request, criterion_id: int,
    label: str = Form(""), field: str = Form(""), operator: str = Form(""),
    value: str = Form(""), weight: str = Form("1"), active: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/definitions/icp", status_code=303)
    weight_int = _safe_int(weight)
    if weight_int is None:
        return templates.TemplateResponse(request, "admin/icp.html", {
            "request": request, "user": user,
            "criteria": icp_service.list_criteria(),
            "fields": icp_service.MATCHABLE_FIELDS,
            "operators": icp_service.OPERATORS,
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
            "error": _ICP_ERROR_MESSAGES.get("invalid_weight"),
        }, status_code=400)
    _, error = icp_service.update_criterion(
        criterion_id, label=label, field=field, operator=operator,
        value=value, weight=weight_int, active=bool(active),
    )
    if error:
        return templates.TemplateResponse(request, "admin/icp.html", {
            "request": request, "user": user,
            "criteria": icp_service.list_criteria(),
            "fields": icp_service.MATCHABLE_FIELDS,
            "operators": icp_service.OPERATORS,
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
            "error": _ICP_ERROR_MESSAGES.get(error, error),
        }, status_code=400)
    return RedirectResponse("/definitions/icp", status_code=303)


@crm_router.post("/definitions/icp/{criterion_id}/delete")
async def delete_icp_criterion_submit(
    request: Request, criterion_id: int,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if validate_csrf_token(csrf_token, get_session_cookie(request)):
        icp_service.delete_criterion(criterion_id)
    return RedirectResponse("/definitions/icp", status_code=303)


# ==================== Account settings / API keys ====================
@router.get("/settings", response_class=HTMLResponse)
async def account_settings(request: Request, user=Depends(require_login)):
    return templates.TemplateResponse(request, "admin/settings.html", {
        "request": request, "user": user,
        "api_keys": api_keys_service.list_api_keys(user["id"]),
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
        "currency_choices": offers_service.CURRENCY_CHOICES,
    })


@router.post("/settings/api-keys/new")
async def create_api_key_submit(
    request: Request,
    label: str = Form(""), csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/settings", status_code=303)
    _, new_key = api_keys_service.create_api_key(user["id"], label)
    # Rendered directly (not redirected) so the plaintext key can be shown
    # once — it's never recoverable after this response, since only its hash
    # is stored.
    return templates.TemplateResponse(request, "admin/settings.html", {
        "request": request, "user": user,
        "api_keys": api_keys_service.list_api_keys(user["id"]),
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
        "currency_choices": offers_service.CURRENCY_CHOICES,
        "new_key": new_key,
    })


@router.post("/settings/api-keys/{key_id}/delete")
async def delete_api_key_submit(
    request: Request, key_id: int,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if validate_csrf_token(csrf_token, get_session_cookie(request)):
        api_keys_service.delete_api_key(user["id"], key_id)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/password")
async def change_password_submit(
    request: Request,
    current_password: str = Form(""), new_password: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/settings", status_code=303)
    ok, error = change_password(user["id"], current_password, new_password)
    if not ok:
        return templates.TemplateResponse(request, "admin/settings.html", {
            "request": request, "user": user,
            "api_keys": api_keys_service.list_api_keys(user["id"]),
            "csrf_token": generate_csrf_token(get_session_cookie(request)),
            "currency_choices": offers_service.CURRENCY_CHOICES,
            "password_error": error,
        }, status_code=400)
    response = templates.TemplateResponse(request, "admin/settings.html", {
        "request": request, "user": user,
        "api_keys": api_keys_service.list_api_keys(user["id"]),
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
        "currency_choices": offers_service.CURRENCY_CHOICES,
        "password_changed": True,
    })
    # Every other outstanding session cookie is now invalid (session_version
    # bumped); reissue this one so the browser that just changed it stays in.
    set_session_cookie(response, user["id"], user["session_version"] + 1)
    return response


@router.post("/settings/logout-everywhere")
async def logout_everywhere_submit(
    request: Request,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/settings", status_code=303)
    new_version = bump_session_version(user["id"])
    response = RedirectResponse("/settings", status_code=303)
    set_session_cookie(response, user["id"], new_version)
    return response


@router.post("/settings/regional")
async def regional_settings_submit(
    request: Request,
    currency: str = Form(...), csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/settings", status_code=303)
    if currency in dict(offers_service.CURRENCY_CHOICES):
        set_user_currency(user["id"], currency)
    return RedirectResponse("/settings", status_code=303)


_AUTOMATION_ERROR_MESSAGES = {
    "name_required": "Name is required.",
    "name_taken": "An automation with that name already exists.",
    "invalid_stage": "Pick a stage that exists.",
    "service_not_found": "That service does not exist.",
    "offer_not_found": "That offer does not exist.",
    "scope_conflict": "The service id and slug do not match.",
    "actions_required": "Add a task title, at least one child service, or both.",
    "duplicate_action": "Only one task and one set of child deals are allowed.",
    "invalid_action": "That action is not supported.",
    "title_required": "A task needs a title.",
    "invalid_owner": "Task owner must be letters, digits, and hyphens.",
    "invalid_due": "Due in days must be a whole number from 0 to 3650, or left blank.",
    "not_found": "That automation no longer exists.",
}


def _automation_error_message(error):
    if isinstance(error, str) and error.startswith("unknown_service:"):
        slug = error.split(":", 1)[1] or "that service"
        return f"No service named {slug}."
    return _AUTOMATION_ERROR_MESSAGES.get(error, "Could not save that automation.")


def _automation_page(request, user, error=None, status_code=200):
    return templates.TemplateResponse(request, "admin/automations.html", {
        "request": request,
        "user": user,
        "automations": stage_automations.list_automations(),
        "stages": pipeline_stages.list_stages(),
        "services": list_services(),
        "offers": offers_service.list_offers(),
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
        "error": error,
    }, status_code=status_code)


def _actions_from_form(form):
    actions = []
    title = form.get("task_title") or ""
    if str(title).strip():
        due = str(form.get("task_due_in_days") or "").strip()
        actions.append({
            "type": "delegated_task",
            "owner": form.get("task_owner") or "",
            "title": title,
            "brief": form.get("task_brief") or "",
            "due_in_days": due or None,
        })
    child_ids = []
    for raw in form.getlist("child_service_ids"):
        text = str(raw).strip()
        if text:
            child_ids.append(text)
    if child_ids:
        actions.append({"type": "spawn_child_deals", "service_ids": child_ids})
    return actions


def _scope_id(form, field):
    text = str(form.get(field) or "").strip()
    return None if text == "" else text


@crm_router.get("/definitions/automations", response_class=HTMLResponse)
async def automations_settings(request: Request, user=Depends(require_login)):
    return _automation_page(request, user)


@crm_router.post("/definitions/automations/new")
async def create_automation_submit(request: Request, user=Depends(require_login)):
    form = await request.form()
    if not validate_csrf_token(str(form.get("csrf_token") or ""), get_session_cookie(request)):
        return RedirectResponse("/definitions/automations", status_code=303)
    automation, error = stage_automations.create_automation(
        form.get("name") or "",
        form.get("stage_key") or "",
        service_id=_scope_id(form, "service_id"),
        offer_id=_scope_id(form, "offer_id"),
        enabled=form.get("enabled") is not None,
        actions=_actions_from_form(form),
    )
    if error or not automation:
        return _automation_page(
            request, user, error=_automation_error_message(error), status_code=400,
        )
    return RedirectResponse("/definitions/automations", status_code=303)


@crm_router.post("/definitions/automations/{automation_id}/edit")
async def edit_automation_submit(automation_id: int, request: Request, user=Depends(require_login)):
    if automation_id < 1 or automation_id > _SQLITE_INT_MAX:
        return _automation_page(request, user, error="That automation no longer exists.", status_code=400)
    form = await request.form()
    if not validate_csrf_token(str(form.get("csrf_token") or ""), get_session_cookie(request)):
        return RedirectResponse("/definitions/automations", status_code=303)
    automation, error = stage_automations.update_automation(
        automation_id,
        name=form.get("name") or "",
        stage_key=form.get("stage_key") or "",
        service_id=_scope_id(form, "service_id"),
        offer_id=_scope_id(form, "offer_id"),
        enabled=form.get("enabled") is not None,
        actions=_actions_from_form(form),
    )
    if error or not automation:
        return _automation_page(
            request, user, error=_automation_error_message(error), status_code=400,
        )
    return RedirectResponse("/definitions/automations", status_code=303)


@crm_router.post("/definitions/automations/{automation_id}/enabled")
async def toggle_automation_submit(automation_id: int, request: Request, user=Depends(require_login)):
    if automation_id < 1 or automation_id > _SQLITE_INT_MAX:
        return _automation_page(request, user, error="That automation no longer exists.", status_code=400)
    form = await request.form()
    if not validate_csrf_token(str(form.get("csrf_token") or ""), get_session_cookie(request)):
        return RedirectResponse("/definitions/automations", status_code=303)
    enabled = str(form.get("enabled") or "").strip() in {"1", "true", "on", "yes"}
    _automation, error = stage_automations.update_automation(automation_id, enabled=enabled)
    if error:
        return _automation_page(
            request, user, error=_automation_error_message(error), status_code=400,
        )
    return RedirectResponse("/definitions/automations", status_code=303)


@crm_router.post("/definitions/automations/{automation_id}/delete")
async def delete_automation_submit(automation_id: int, request: Request, user=Depends(require_login)):
    if automation_id < 1 or automation_id > _SQLITE_INT_MAX:
        return _automation_page(request, user, error="That automation no longer exists.", status_code=400)
    form = await request.form()
    if not validate_csrf_token(str(form.get("csrf_token") or ""), get_session_cookie(request)):
        return RedirectResponse("/definitions/automations", status_code=303)
    _deleted, error = stage_automations.delete_automation(automation_id)
    if error:
        return _automation_page(
            request, user, error=_automation_error_message(error), status_code=400,
        )
    return RedirectResponse("/definitions/automations", status_code=303)


# ==================== Leads ====================
_EMPTY_LEAD_FORM = {
    "company_name": "", "contact_name": "", "name": "", "email": "",
    "phone": "", "website": "", "title": "", "linkedin_url": "",
    "industry": "", "team_size": "", "preferred_channel": "",
    "service_slug": "", "source": "", "pain_points": "", "goals": "",
    "next_action": "", "next_action_date": "", "owner_key": "",
}

_LEAD_CREATE_ERRORS = {
    "invalid": "A lead needs a company, contact, email, phone, website, or LinkedIn URL.",
    "invalid_service": "That service does not exist.",
    "partner_not_found": "That partner does not exist.",
}

_LEAD_UPDATE_ERRORS = {
    "not_found": "That lead was not found.",
    "not_a_lead": "That record is not a lead.",
    "lead_lost": "Restore this lead before editing it.",
    "invalid": "A lead needs a company, contact, email, phone, website, or LinkedIn URL.",
    "invalid_service": "That service does not exist.",
    "partner_not_found": "That partner does not exist.",
}

_CONVERT_ERRORS = {
    "not_a_lead": "This is not a lead.",
    "lead_lost": "Restore this lead before converting it.",
    "missing_service": "Pick a service.",
    "partner_not_found": "That partner does not exist.",
    "partner_ambiguous": "More than one partner matches — pick one and use Link.",
    "duplicate_open_opportunity": "An open opportunity already exists for this partner and service.",
}

_LOST_REASON_ERRORS = {
    "name_required": "Name is required.",
    "duplicate_name": "A lost reason with that name already exists.",
    "not_found": "That lost reason was not found.",
}


def _lead_fields_from_form(
    company_name="", contact_name="", name="", email="", phone="", website="",
    title="", linkedin_url="", industry="", team_size="", preferred_channel="",
    service_slug="", source="", pain_points="", goals="", next_action="",
    next_action_date="", owner_key="",
):
    return {
        "company_name": company_name, "contact_name": contact_name, "name": name,
        "email": email, "phone": phone, "website": website, "title": title,
        "linkedin_url": linkedin_url, "industry": industry,
        "team_size": team_size, "preferred_channel": preferred_channel,
        "service_slug": service_slug, "source": source,
        "pain_points": pain_points, "goals": goals,
        "next_action": next_action,
        "next_action_date": (next_action_date or "").strip()[:10],
        "owner_key": owner_key,
    }


def _lead_service_slug(lead):
    if not lead or not lead.get("service_id"):
        return ""
    service = get_service(lead["service_id"])
    return (service or {}).get("slug") or ""


def _lead_form_page(request, user, lead=None, form=None, form_tags=None, error=None,
                    convert_candidates=None, status_code=200):
    return templates.TemplateResponse(request, "admin/lead_form.html", {
        "request": request, "user": user, "lead": lead,
        "form": form or dict(_EMPTY_LEAD_FORM),
        "form_tags": form_tags,
        "error": error,
        "convert_candidates": convert_candidates or [],
        "services": list_services(active_only=True),
        "stages": pipeline_stages.list_stages(),
        "partners": list_partners(),
        "lost_reasons": list_lost_reasons(active_only=True),
        "lead_service_slug": _lead_service_slug(lead),
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
    }, status_code=status_code)


@crm_router.get("/crm/leads", response_class=HTMLResponse)
async def leads_list(
    request: Request, q: str = "", owner: str = "", source_prefix: str = "",
    tag: str = "", due: str = "", lost: str = "", sort: str = "updated",
    user=Depends(require_login),
):
    current_tag, tag_error = _tag_query(tag)
    leads = [] if tag_error else list_leads(
        query=q, owner=owner, source_prefix=source_prefix,
        tags=[current_tag] if current_tag else None,
        due_only=_truthy_query(due, "due"),
        include_lost=_truthy_query(lost, "lost"),
        sort=sort or "updated",
    )
    return templates.TemplateResponse(request, "admin/leads.html", {
        "request": request, "user": user, "leads": leads,
        "q": q, "owner": owner, "source_prefix": source_prefix,
        "current_tag": current_tag, "tag_error": tag_error,
        "current_due": _truthy_query(due, "due"),
        "current_lost": _truthy_query(lost, "lost"),
        "sort": sort or "updated",
    })


@crm_router.get("/crm/leads/new", response_class=HTMLResponse)
async def new_lead_page(request: Request, user=Depends(require_login)):
    return _lead_form_page(request, user)


@crm_router.post("/crm/leads/new")
async def new_lead_submit(
    request: Request,
    company_name: str = Form(""), contact_name: str = Form(""), name: str = Form(""),
    email: str = Form(""), phone: str = Form(""), website: str = Form(""),
    title: str = Form(""), linkedin_url: str = Form(""), industry: str = Form(""),
    team_size: str = Form(""), preferred_channel: str = Form(""),
    service_slug: str = Form(""), source: str = Form(""),
    pain_points: str = Form(""), goals: str = Form(""),
    next_action: str = Form(""), next_action_date: str = Form(""),
    owner_key: str = Form(""), tags: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/crm/leads/new", status_code=303)
    fields = _lead_fields_from_form(
        company_name=company_name, contact_name=contact_name, name=name,
        email=email, phone=phone, website=website, title=title,
        linkedin_url=linkedin_url, industry=industry, team_size=team_size,
        preferred_channel=preferred_channel, service_slug=service_slug,
        source=source, pain_points=pain_points, goals=goals,
        next_action=next_action, next_action_date=next_action_date,
        owner_key=owner_key,
    )
    parsed_tags, tag_error = parse_tag_list(tags)
    if tag_error:
        return _lead_form_page(
            request, user, form=fields, form_tags=tags, error=tag_error, status_code=400,
        )
    lead, error = create_lead(**fields, tags=parsed_tags or None)
    if error:
        return _lead_form_page(
            request, user, form=fields, form_tags=tags,
            error=_LEAD_CREATE_ERRORS.get(error, error), status_code=400,
        )
    return RedirectResponse(f"/crm/leads/{lead['id']}", status_code=303)


@crm_router.get("/crm/leads/{lead_id}", response_class=HTMLResponse)
async def lead_detail(request: Request, lead_id: int, user=Depends(require_login)):
    lead = get_lead(lead_id)
    if not lead:
        return RedirectResponse("/crm/leads", status_code=303)
    return _lead_form_page(request, user, lead=lead)


@crm_router.post("/crm/leads/{lead_id}/edit")
async def edit_lead_submit(
    request: Request, lead_id: int,
    company_name: str = Form(""), contact_name: str = Form(""), name: str = Form(""),
    email: str = Form(""), phone: str = Form(""), website: str = Form(""),
    title: str = Form(""), linkedin_url: str = Form(""), industry: str = Form(""),
    team_size: str = Form(""), preferred_channel: str = Form(""),
    service_slug: str = Form(""), source: str = Form(""),
    pain_points: str = Form(""), goals: str = Form(""),
    next_action: str = Form(""), next_action_date: str = Form(""),
    owner_key: str = Form(""), tags: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse(f"/crm/leads/{lead_id}", status_code=303)
    lead = get_lead(lead_id)
    if not lead:
        return RedirectResponse("/crm/leads", status_code=303)
    fields = _lead_fields_from_form(
        company_name=company_name, contact_name=contact_name, name=name,
        email=email, phone=phone, website=website, title=title,
        linkedin_url=linkedin_url, industry=industry, team_size=team_size,
        preferred_channel=preferred_channel, service_slug=service_slug,
        source=source, pain_points=pain_points, goals=goals,
        next_action=next_action, next_action_date=next_action_date,
        owner_key=owner_key,
    )
    parsed_tags, tag_error = parse_tag_list(tags)
    if tag_error:
        return _lead_form_page(
            request, user, lead=lead, form=fields, form_tags=tags,
            error=tag_error, status_code=400,
        )
    _updated, error = update_lead(lead_id, fill_empty_only=False, tags=parsed_tags or [], **fields)
    if error:
        return _lead_form_page(
            request, user, lead=get_lead(lead_id) or lead, form=fields, form_tags=tags,
            error=_LEAD_UPDATE_ERRORS.get(error, error), status_code=400,
        )
    return RedirectResponse(f"/crm/leads/{lead_id}", status_code=303)


@crm_router.post("/crm/leads/{lead_id}/convert")
async def convert_lead_submit(
    request: Request, lead_id: int,
    service_slug: str = Form(""), stage: str = Form(""),
    partner_action: str = Form("auto"), partner_id: str = Form(""),
    value_estimate: str = Form(""), next_action: str = Form(""),
    next_action_date: str = Form(""), note: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse(f"/crm/leads/{lead_id}", status_code=303)
    lead = get_lead(lead_id)
    if not lead:
        return RedirectResponse("/crm/leads", status_code=303)
    value = None
    if (value_estimate or "").strip():
        try:
            value = float(value_estimate)
        except ValueError:
            value = None
    result, error = convert_lead(
        lead_id,
        partner_action=partner_action or "auto",
        partner_id=_safe_int(partner_id),
        service_slug=service_slug,
        stage=stage or None,
        value_estimate=value,
        next_action=next_action or None,
        next_action_date=(next_action_date or "").strip()[:10] or None,
        note=note,
    )
    if error == "partner_ambiguous":
        candidates = (result or {}).get("candidates") or []
        return _lead_form_page(
            request, user, lead=lead,
            error=_CONVERT_ERRORS["partner_ambiguous"],
            convert_candidates=candidates, status_code=400,
        )
    if error:
        extra = ""
        if error == "duplicate_open_opportunity" and isinstance(result, dict) and result.get("deal_id"):
            extra = f" (#{result['deal_id']})."
        return _lead_form_page(
            request, user, lead=lead,
            error=_CONVERT_ERRORS.get(error, error) + extra,
            status_code=400,
        )
    opportunity = result["opportunity"]
    return RedirectResponse(f"/crm/deals/{opportunity['id']}/edit", status_code=303)


@crm_router.post("/crm/leads/{lead_id}/lost")
async def mark_lead_lost_submit(
    request: Request, lead_id: int,
    lost_reason: str = Form(""), note: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse(f"/crm/leads/{lead_id}", status_code=303)
    lead = get_lead(lead_id)
    if not lead:
        return RedirectResponse("/crm/leads", status_code=303)
    _updated, error = mark_lost(lead_id, lost_reason=lost_reason, note=note)
    if error:
        return _lead_form_page(
            request, user, lead=lead,
            error=_LOST_FORM_ERRORS.get(error, error), status_code=400,
        )
    return RedirectResponse(f"/crm/leads/{lead_id}", status_code=303)


@crm_router.post("/crm/leads/{lead_id}/restore")
async def restore_lead_submit(
    request: Request, lead_id: int,
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse(f"/crm/leads/{lead_id}", status_code=303)
    lead = get_lead(lead_id, include_extras=False)
    if not lead:
        return RedirectResponse("/crm/leads", status_code=303)
    _updated, error = restore_deal(lead_id)
    if error:
        return _lead_form_page(
            request, user, lead=get_lead(lead_id) or lead,
            error=_LOST_FORM_ERRORS.get(error, error), status_code=400,
        )
    return RedirectResponse(f"/crm/leads/{lead_id}", status_code=303)


@crm_router.post("/crm/leads/{lead_id}/activities")
async def add_lead_activity(
    request: Request, lead_id: int,
    type: str = Form("research"), body: str = Form(""),
    source_url: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    lead = get_lead(lead_id, include_extras=False)
    if lead and validate_csrf_token(csrf_token, get_session_cookie(request)) and body.strip():
        log_activity(lead.get("partner_id"), type, body, deal_id=lead_id, source_url=source_url)
    return RedirectResponse(f"/crm/leads/{lead_id}", status_code=303)


# ==================== Lost reasons ====================
def _lost_reasons_page(request, user, error=None, status_code=200):
    return templates.TemplateResponse(request, "admin/lost_reasons.html", {
        "request": request, "user": user,
        "reasons": list_lost_reasons(),
        "csrf_token": generate_csrf_token(get_session_cookie(request)),
        "error": error,
    }, status_code=status_code)


@crm_router.get("/definitions/lost-reasons", response_class=HTMLResponse)
async def lost_reasons_list(request: Request, user=Depends(require_login)):
    return _lost_reasons_page(request, user)


@crm_router.post("/definitions/lost-reasons/new")
async def new_lost_reason_submit(
    request: Request,
    name: str = Form(""), triggers_nurture: str = Form(""), active: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/definitions/lost-reasons", status_code=303)
    if not (name or "").strip():
        return _lost_reasons_page(request, user, error="Name is required.", status_code=400)
    _created, error = create_lost_reason(
        name, triggers_nurture=bool(triggers_nurture), active=bool(active),
    )
    if error:
        return _lost_reasons_page(
            request, user, error=_LOST_REASON_ERRORS.get(error, error), status_code=400,
        )
    return RedirectResponse("/definitions/lost-reasons", status_code=303)


@crm_router.post("/definitions/lost-reasons/{reason_id}/edit")
async def edit_lost_reason_submit(
    request: Request, reason_id: int,
    name: str = Form(""), triggers_nurture: str = Form(""), active: str = Form(""),
    csrf_token: str = Form(...), user=Depends(require_login),
):
    if not validate_csrf_token(csrf_token, get_session_cookie(request)):
        return RedirectResponse("/definitions/lost-reasons", status_code=303)
    if not (name or "").strip():
        return _lost_reasons_page(request, user, error="Name is required.", status_code=400)
    _updated, error = update_lost_reason(
        reason_id, name=name, triggers_nurture=bool(triggers_nurture), active=bool(active),
    )
    if error:
        return _lost_reasons_page(
            request, user, error=_LOST_REASON_ERRORS.get(error, error), status_code=400,
        )
    return RedirectResponse("/definitions/lost-reasons", status_code=303)

