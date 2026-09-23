"""Import prospects from a directory of markdown files into partners (and a deal).

The files live outside this repo. This module defines the expected shape
(YAML-ish frontmatter) and the find-or-create rules so a re-run does not duplicate
companies, people, or open deals.

Frontmatter is a flat `key: value` map. Literal (`|`) and folded (`>`) block
scalars are supported so multiline `pain_points` / `goals` survive. Nested
maps are not parsed as maps. Indented lines that look like a known field
(`name:`, `email:`, …) are skipped so they are not promoted; ordinary prose
such as `Note: they mentioned budget` is kept. Typed lists are flattened to
text.
"""

import re
from pathlib import Path

from app.services.activities import log_activity
from app.services.catalog import get_service_by_slug
from app.services.deals import create_deal, get_open_deal_for_partner_service
from app.services.partners import (
    create_partner, get_company_by_name, get_partner, get_partner_by_email,
    get_person_by_name, update_partner,
)

_ALIASES = {
    "company_name": "company",
    "contact": "name",
    "service": "service_slug",
}

_PERSON_FIELDS = ("email", "phone", "title", "social_url", "preferred_channel")
_COMPANY_FIELDS = ("website", "address", "industry", "team_size")
_KNOWN_KEYS = frozenset(_ALIASES) | {
    "company", "name", "email", "phone", "title", "social_url", "preferred_channel",
    "website", "address", "industry", "team_size", "service_slug", "source",
    "value_estimate", "pain_points", "goals", "next_action", "next_action_date",
    "notes",
}


def parse_client_markdown(text: str) -> dict:
    """Return a flat record plus `notes` (the markdown body). Empty dict if unusable."""
    fields, body = _split_frontmatter(text or "")
    record = {}
    aliases = []
    for key, value in fields.items():
        if value in (None, ""):
            continue
        if key in _ALIASES:
            aliases.append((_ALIASES[key], value))
        else:
            record[key] = value
    for mapped, value in aliases:
        record.setdefault(mapped, value)
    if not record.get("company") and not record.get("name"):
        heading = _first_heading(body)
        if heading:
            record["company"] = heading
    notes = body.strip()
    if notes:
        record["notes"] = notes
    if not record.get("company") and not record.get("name"):
        return {}
    return record


def parse_client_file(path) -> dict:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    return parse_client_markdown(text)


def import_client(record: dict, source_label: str = "markdown-import") -> dict:
    """Find-or-create company, person, and optional deal. Never duplicates an
    open deal for the same partner+service. Fills only empty partner fields on
    a re-run so later CRM edits are not overwritten.

    Returns a result dict with `status` and any ids.
    """
    if not record or (not record.get("company") and not record.get("name")):
        return {"status": "invalid"}

    result = {"status": "ok", "company_id": None, "partner_id": None, "deal_id": None}

    parent_id = None
    company_name = (record.get("company") or "").strip()
    if company_name:
        existing = get_company_by_name(company_name)
        if existing:
            parent_id = existing["id"]
            _fill_empty(parent_id, {k: record.get(k) for k in _COMPANY_FIELDS})
            result["company"] = "existing"
        else:
            parent_id = create_partner(
                company_name, is_company=True,
                website=record.get("website", ""),
                address=record.get("address", ""),
                industry=record.get("industry", ""),
                team_size=record.get("team_size"),
            )
            result["company"] = "created"
        result["company_id"] = parent_id

    person_name = (record.get("name") or "").strip()
    partner_id = parent_id
    if person_name:
        existing_person = None
        email = (record.get("email") or "").strip()
        if email:
            found = get_partner_by_email(email)
            if found and found.get("is_company"):
                # Email is already on a company. Never mint a second partner
                # with the same address; attach the deal to that company
                # (or to the company we already matched by name).
                if parent_id is None:
                    parent_id = found["id"]
                    result["company_id"] = parent_id
                    result["company"] = "existing"
                partner_id = parent_id
                result["partner_id"] = partner_id
                result["person"] = "skipped_company_email"
            elif found:
                # Email match must share this file's company (including
                # "no company"). A person-only file must not attach to
                # Acme's Pat just because the inbox matches.
                found_parent = found.get("parent_id")
                if parent_id is None:
                    if not found.get("is_company") and found_parent is None:
                        existing_person = found
                elif found_parent == parent_id:
                    existing_person = found
        if result.get("person") != "skipped_company_email":
            if existing_person is None and parent_id is not None:
                # Name+company only. A person with no company is not
                # merged with every other nameless-email "Alex Smith".
                existing_person = get_person_by_name(person_name, parent_id=parent_id)

            if existing_person:
                partner_id = existing_person["id"]
                _fill_empty(partner_id, {
                    **{k: record.get(k) for k in _PERSON_FIELDS},
                    "parent_id": parent_id or existing_person.get("parent_id"),
                })
                result["person"] = "existing"
            else:
                partner_id = create_partner(
                    person_name, parent_id=parent_id,
                    email=record.get("email", ""),
                    phone=record.get("phone", ""),
                    title=record.get("title", ""),
                    social_url=record.get("social_url", ""),
                    preferred_channel=record.get("preferred_channel", ""),
                )
                result["person"] = "created"
            result["partner_id"] = partner_id
    else:
        result["partner_id"] = partner_id

    if not partner_id:
        return {"status": "invalid"}

    service_slug = (record.get("service_slug") or "").strip()
    if service_slug:
        service = get_service_by_slug(service_slug)
        if not service:
            result["deal"] = "unknown_service"
        else:
            open_deal = get_open_deal_for_partner_service(partner_id, service["id"])
            if open_deal:
                result["deal"] = "duplicate_open_deal"
                result["deal_id"] = open_deal["id"]
            else:
                deal_id = create_deal(
                    partner_id, service["id"],
                    source=record.get("source") or source_label,
                    value_estimate=_optional_float(record.get("value_estimate")),
                    pain_points=record.get("pain_points", ""),
                    goals=record.get("goals", ""),
                    next_action=record.get("next_action", ""),
                    next_action_date=record.get("next_action_date", ""),
                )
                result["deal"] = "created"
                result["deal_id"] = deal_id

    notes = (record.get("notes") or "").strip()
    should_log = notes and (
        result.get("person") == "created"
        or (not person_name and result.get("company") == "created")
    )
    if should_log:
        log_activity(partner_id, "note", notes, deal_id=result.get("deal_id"))

    return result


def import_paths(paths, dry_run: bool = False) -> list:
    """Import every `*.md` in the given files/directories. Skip README.md.

    A single unreadable file is recorded as `status=error` and the rest of
    the batch continues.
    """
    results = []
    for path in _expand_paths(paths):
        entry = {"file": str(path), "record": {}}
        try:
            record = parse_client_file(path)
        except (OSError, UnicodeError) as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)
            results.append(entry)
            continue
        entry["record"] = record
        if dry_run:
            entry["status"] = "dry_run" if record else "invalid"
        elif not record:
            entry["status"] = "invalid"
        else:
            imported = import_client(record)
            entry.update(imported)
        results.append(entry)
    return results


def _expand_paths(paths):
    files = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(p for p in path.glob("*.md") if p.name.lower() != "readme.md"))
        elif path.is_file() and path.suffix.lower() == ".md":
            files.append(path)
    return files


_BLOCK_SCALAR = re.compile(r"^([|>])([+-]?)(\d*)$")


def _split_frontmatter(text: str):
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    rest = text[3:]
    if rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n---")
    if end < 0:
        return {}, text
    block = rest[:end]
    body = rest[end + 4:].lstrip("\n")
    return _parse_frontmatter_fields(block), body


def _parse_frontmatter_fields(block: str) -> dict:
    """Flat YAML-ish map: `key: value`, plus `|` / `>` block scalars.

    Indented continuation lines after a bare `key:` are joined as a
    multiline string. `- ` list items are flattened to text. Indented
    `known_field:` lines are skipped (nested maps); other `Word: …` prose
    is kept.
    """
    fields = {}
    lines = block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line[:1] in (" ", "\t"):
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        raw_key, _, raw_value = stripped.partition(":")
        key = raw_key.strip().lower().replace("-", "_")
        if not key:
            continue
        value = raw_value.strip()
        indicator = _BLOCK_SCALAR.fullmatch(value)
        if indicator:
            style, chomp = indicator.group(1), indicator.group(2)
            collected, i = _take_indented(lines, i)
            value = _decode_block_scalar(style, chomp, collected)
        elif value == "":
            collected, i = _take_indented(lines, i)
            value = _decode_plain_multiline(collected)
        else:
            value = value.strip("'\"")
        if value:
            fields[key] = value
    return fields


def _take_indented(lines, start: int):
    collected = []
    i = start
    while i < len(lines):
        line = lines[i]
        if line.strip() == "":
            collected.append(line)
            i += 1
            continue
        if line[:1] in (" ", "\t"):
            collected.append(line)
            i += 1
            continue
        break
    return collected, i


def _common_indent(lines) -> int:
    indents = []
    for line in lines:
        if not line.strip():
            continue
        indents.append(len(line) - len(line.lstrip(" \t")))
    return min(indents) if indents else 0


def _strip_indent(lines, indent: int):
    out = []
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        if len(line) >= indent and line[:indent].strip() == "":
            out.append(line[indent:])
        else:
            out.append(line.lstrip(" \t"))
    return out


def _decode_block_scalar(style: str, chomp: str, raw_lines) -> str:
    if not raw_lines:
        return ""
    # Drop leading empty lines (the newline after `|` / `>`).
    while raw_lines and not raw_lines[0].strip():
        raw_lines = raw_lines[1:]
    stripped = _strip_indent(raw_lines, _common_indent(raw_lines))
    if style == ">":
        paragraphs = []
        current = []
        for line in stripped:
            if line == "":
                if current:
                    paragraphs.append(" ".join(current))
                    current = []
                elif paragraphs:
                    paragraphs.append("")
            else:
                current.append(line.rstrip())
        if current:
            paragraphs.append(" ".join(current))
        value = "\n".join(paragraphs)
    else:
        value = "\n".join(line.rstrip() for line in stripped)
    if chomp == "+":
        return value
    return value.strip()


def _decode_plain_multiline(raw_lines) -> str:
    """Join indented lines after `key:`. `- ` list items become plain lines
    so a markdown-style bullet list still lands in `pain_points` as text.
    Nested maps whose keys are importer fields (`name:`, `email:`, …) are
    skipped; `Note:` / `Status:` prose is kept.
    """
    if not raw_lines:
        return ""
    stripped = _strip_indent(raw_lines, _common_indent(raw_lines))
    parts = []
    for line in stripped:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("- "):
            parts.append(text[2:].strip())
            continue
        nested = re.match(r"^([A-Za-z_][\w-]*)\s*:", text)
        if nested:
            nested_key = nested.group(1).lower().replace("-", "_")
            if nested_key in _KNOWN_KEYS:
                continue
        parts.append(text)
    return "\n".join(parts)


def _first_heading(body: str) -> str:
    for line in (body or "").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _optional_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_empty(value) -> bool:
    """True when a stored field should still be fillable. 0 is a real team_size."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _fill_empty(partner_id: int, fields: dict):
    existing = get_partner(partner_id)
    if not existing:
        return
    updates = {}
    for key, value in fields.items():
        if _is_empty(value):
            continue
        if _is_empty(existing.get(key)):
            updates[key] = value
    if updates:
        update_partner(partner_id, **updates)
