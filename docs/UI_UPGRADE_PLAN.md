# UI upgrade plan: Pico.css + htmx

**Goal (original):** make the app look launch-ready for paying customers
without a build step, a JS framework, or a rewrite of the Jinja2 templates.

**Do not deviate from this constraint:** every frontend library is
downloaded once and committed into `app/static/vendor/`. Nothing is loaded
from a CDN at runtime — see `README.md` / `docs/DEPLOY.md`, this app must work
fully offline and self-hosted. No `package.json`, no Node, no build step.

**Status (verified on `main`, 2026-09-29):** Phases 1–3 have shipped.
Do not re-vendor Pico or htmx, do not redo the polish pass, and do not
re-implement the pipeline / call-outcome swaps. The phase notes below are
history so a future agent can see *how* it was done — they are not a
backlog. Phases 4 and 5 stay deferred and still need a decision; do not
implement them from this file.

| Phase | What | Status | Tracking |
|---|---|---|---|
| 1 | Vendored Pico.css 2.1.1, `data-theme="light"` | **Shipped** | [#43](https://github.com/sevasek/agent-crm/issues/43), [PR #53](https://github.com/sevasek/agent-crm/pull/53) |
| 2 | Polish custom CSS on top of Pico | **Shipped** | [#44](https://github.com/sevasek/agent-crm/issues/44), [PR #53](https://github.com/sevasek/agent-crm/pull/53) |
| 3 | Vendored htmx 2.0.11, pipeline oob swaps, call-outcome swaps | **Shipped** | [#45](https://github.com/sevasek/agent-crm/issues/45), [PR #62](https://github.com/sevasek/agent-crm/pull/62) |
| 4 | SortableJS drag-and-drop kanban | **Deferred** — do not implement | [#46](https://github.com/sevasek/agent-crm/issues/46) |
| 5 | Tom Select / Alpine.js / real dark mode | **Deferred** — do not implement | [#47](https://github.com/sevasek/agent-crm/issues/47) |

Dark mode stays locked to light for launch; `docs/SCOPE.md` already says
that, and unlocking it is #47.

Pinned versions live in `app/static/vendor/VENDOR.md` (Pico 2.1.1, htmx
2.0.11 as of the shipping PRs).

---

## Snapshot of the tree when this plan was written (2026-09-28)

This section is a **point-in-time grep**, not a live inventory. Line
counts, line numbers, test-file counts, template counts, and "no CSP"
claims **will be wrong** on current `main`. Re-grep (`rg`, `grep`) before
you trust any of them. In particular:

- `style.css` is no longer the 152-line hand-written sheet described here
  (Phase 2 rewrote it on top of Pico).
- Pico and htmx are already in `app/static/vendor/` — see `VENDOR.md`.
- CSP, auth cookies, and `docker-entrypoint.sh` are owned by other issues;
  do not edit them from this plan.

Kept below so you can see what the tree looked like *before* Phases 1–3:

- `app/templates/base.html` — single layout, all 16 other templates extend it.
  Loads one stylesheet: `<link rel="stylesheet" href="/static/style.css">`.
- `app/static/style.css` — 152 lines, hand-written, 4 CSS variables
  (`--border`, `--muted`, `--bg-alt`, `--accent: #2c5aa0`), no framework.
- `app/main.py` mounts `/static` → `app/static/` via `StaticFiles`, and
  `Jinja2Templates(directory="app/templates")`.
- No CSP header is set anywhere in the app (`grep -rn CSP app/` returns
  nothing) — a `<script src="/static/vendor/...">` tag needs no CSP change.
- No test asserts on a specific CSS class name or exact HTML string
  (`grep -rn 'assert.*response.text' tests/` shows only status-code/content
  checks, not markup checks) — you are free to add/change classes.
- Test suite: `python -m pytest tests/ -q` (38 test files). CI runs the same
  command — see `.github/workflows/ci.yml`.
- All 18 templates, for reference:
  `app/templates/base.html`, and under `app/templates/admin/`: `calls.html`,
  `call_view.html`, `deal_form.html`, `deals.html`, `icp.html`, `offers.html`,
  `partner_detail.html`, `partner_form.html`, `partners.html`,
  `pipeline.html`, `service_form.html`, `services.html`, `settings.html`,
  `stages.html`; under `app/templates/auth/`: `login.html`,
  `mcp_authorize.html`.

---

## Phase 1 — Vendor Pico.css and wire it in (SHIPPED in PR #53, closes #43)

**Do not re-run these steps.** History of how Pico 2.1.1 was vendored and
how `data-theme="light"` was locked.

This is a single `<link>` line. It is the highest-value, lowest-risk change
available: modern spacing, typography, form controls, button/table styling,
focus states, and automatic light/dark mode, with **zero markup changes**,
because the templates already use plain semantic tags (`table`, `form`,
`button`, `select`, `nav`, `details`) and the layout already wraps page
content in `<main class="container">` (`base.html:25`), which is exactly the
convention Pico's default build expects.

1. Create the vendor directory and fetch Pico v2 (classic, non-classless
   build — the classless build auto-styles direct children of `<body>` and
   would fight the app's own `.container` / `.container-wide` rules; the
   default build only styles bare tags, which is what we want):

   ```bash
   mkdir -p app/static/vendor
   curl -fSL -o app/static/vendor/pico.min.css \
     https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css
   ```

   Confirm it downloaded a real stylesheet, not an error page:
   ```bash
   head -c 200 app/static/vendor/pico.min.css   # should start with a CSS comment / minified CSS, not "<!DOCTYPE"
   wc -l app/static/vendor/pico.min.css          # should be 1 (minified) and non-trivial size (100KB+)
   ```

   Find the exact resolved version (jsdelivr's `@2` alias floats to the
   latest 2.x release) so it's pinned and documented:
   ```bash
   curl -fsSL https://data.jsdelivr.com/v1/packages/npm/@picocss/pico/resolved?specifier=2 \
     | grep -o '"version":"[^"]*"'
   ```
   Record that version number in a new `app/static/vendor/VENDOR.md`:
   ```markdown
   # Vendored frontend assets

   | File | Source | Version | Fetched |
   |---|---|---|---|
   | pico.min.css | https://picocss.com (npm @picocss/pico) | <version from above> | 2026-09-28 |
   ```
   (You'll append rows to this same table in later phases — don't create a
   second file.)

   `.dockerignore` has a blanket `*.md` rule, so `VENDOR.md` won't be present
   in the built image. That's fine — it's a changelog, never served — but
   it's intentional, not a packaging bug; don't "fix" it by carving out an
   exception.

2. Edit `app/templates/base.html`. Add the Pico link **before** the existing
   `style.css` link, so `style.css` still wins the cascade wherever it sets a
   rule Pico also sets (same-origin stylesheets, later wins on equal
   specificity):

   ```html
   <link rel="stylesheet" href="/static/vendor/pico.min.css">
   <link rel="stylesheet" href="/static/style.css">
   ```

3. Map the brand color. Open `app/static/vendor/pico.min.css` and search for
   `--pico-primary` to confirm the exact variable names this version ships
   (they are stable across 2.x but confirm rather than assume). Then add a
   block near the top of `app/static/style.css`, right after the existing
   `:root { ... }` block, overriding just the primary-color tokens to the
   existing brand blue (`--accent: #2c5aa0`) so Pico's buttons/links match
   the rest of the app instead of Pico's default teal:

   ```css
   :root {
       --pico-primary: var(--accent);
       --pico-primary-background: var(--accent);
       --pico-primary-hover: #234a85;      /* ~15% darker than --accent, for hover */
       --pico-primary-hover-background: #234a85;
       --pico-primary-underline: var(--accent);
       --pico-primary-focus: rgba(44, 90, 160, 0.25);
   }
   ```
   If any of those variable names don't exist in the file you downloaded,
   use `grep -o -- '--pico-primary[a-z-]*' app/static/vendor/pico.min.css | sort -u`
   to get the real list and adjust the block to match — don't silently drop
   the mapping.

4. Run the app and visually check **every one of the 18 templates** listed
   above (log in, click through every nav link, open at least one row/detail
   page and one create/edit form per section, open the mobile call view).
   For each page, check:
   - Nothing is visually broken (overlapping elements, unreadable contrast,
     a control that lost its click target).
   - The `.pipeline-board` (kanban columns on `/pipeline`) still spans the
     full window width, not capped at 900px — this is what
     `.container-wide { max-width: none; }` in `style.css:32` exists to
     guarantee; if it looks capped, Pico's cascade won a specificity fight
     it shouldn't have and needs a `!important` or a more specific selector
     on that one rule.
   - The mobile call view (`/calls/<id>` or wherever `call_view.html`
     renders) still looks like a focused single-column mobile screen, not a
     desktop form. Check these widths in browser devtools: 360px (small
     Android phone, the floor), 390px (iPhone), 768px (tablet/iPad
     breakpoint), 1280px (laptop). Browsers: current Chrome and Safari
     (iOS Safari for the call view specifically — that's the real device
     a rep dials from) are the bar; no IE/legacy-Edge support needed.
   - Dark mode: toggle your OS/browser to dark mode and reload one page.
     Pico applies dark mode automatically via `prefers-color-scheme`; the
     app's own hand-picked colors (`.pill-overdue`, `.pill-due`, the pipeline
     column background `--bg-alt: #f7f7f7`, etc.) were written assuming a
     light background and will likely look wrong (a light-grey pill on a
     near-black Pico dark background, low contrast). **Do not attempt to
     fully fix dark mode this week** — instead add one line to force light
     mode for now, so nothing looks broken for customers:
     ```html
     <html lang="en" data-theme="light">
     ```
     in `app/templates/base.html:2`. Revisit real dark-mode support later
     (Phase 5 / #47) once there's time to redo the hand-picked colors as
     `light-dark()` pairs or a second `--bg-alt`-style variable set.

5. Run the test suite and confirm nothing broke:
   ```bash
   python -m pytest tests/ -q
   ```

6. Commit. Suggested message: `Add Pico.css for a modern baseline look
   (vendored, no build step)`.

**Acceptance for Phase 1 (met in PR #53):** all 18 templates render correctly
in light mode, desktop and mobile widths, `pytest` is green, nothing is
loaded from a CDN at runtime (check `grep -rn 'http' app/templates/base.html`
shows no external `http(s)://` src/href).

---

## Phase 2 — Small polish pass (SHIPPED in PR #53, closes #44)

**Do not re-run these steps.** History of the CSS polish that landed with
Phase 1.

Pico gives you the primitives; a few of the app's existing hand-rolled bits
will look dated or slightly off next to them. Go through this list, in
`app/static/style.css` only (don't touch templates unless a fix genuinely
needs a new class):

- [x] `.pill`, `.pill-tag`, `.pill-due`, `.pill-overdue` (style.css:65-77,
      87-88) — these are bespoke badges. Check they still look intentional
      next to Pico's more rounded, more padded buttons/inputs. Nudge
      `border-radius` / padding to match Pico's scale if they look flat.
- [x] `button, .btn` (style.css:50-61) and `.btn-secondary` — Pico already
      styles bare `<button>` well. Decide whether to **delete** this custom
      block and let Pico's defaults + your `--pico-primary` override handle
      it (less code, more consistent), or keep it if it does something Pico
      can't. Prefer deleting if the rendered result looks the same or
      better — fewer overrides is less to maintain.
- [x] `.btn-outcome-*` variants (style.css:117-119, the call-outcome buttons:
      no-answer / not-interested / won) — these rely on setting `background`
      and `border-color` directly; confirm they still read clearly as
      distinct states next to Pico's button padding/shadow.
- [x] `input[type=...], select, textarea` block (style.css:44-48) — Pico
      already styles these. Check for doubled borders/radius (both rules
      applying slightly different `border-radius` looks worse than either
      alone). Likely outcome: delete this block too.
- [x] `.topnav` (style.css:17-27) — confirm the nav still reads as a nav bar,
      not a loose row of links, next to Pico's page chrome. Pico doesn't
      forcibly restyle a `<nav class="topnav">` with no `<ul>/<li>` inside it
      much, so this should be low-risk, but check anyway.
- [x] `<details>` / `<summary>` (style.css:90-91) — Pico styles these nicely
      by default (adds a disclosure triangle, hover state); your two-line
      override may now be redundant. Check where `<details>` is used
      (grep `<details` across `app/templates/`) and confirm it still looks
      right; delete the override if Pico's default is equal or better.

For each item: change it, reload the affected page(s), confirm visually,
move on. Re-run `pytest` once at the end of this phase (should be a no-op
since these are pure CSS changes). Commit as
`Polish custom CSS to sit cleanly on top of Pico`.

**Acceptance for Phase 2 (met in PR #53):** `style.css` is shorter or the
same length, no visual regression, `pytest` green. **This was the launch
bar — Phases 1+2 alone were enough to ship.** Treat everything below as
stretch / deferred.

---

## Phase 3 — htmx: kill full-page reloads on the two worst offenders (SHIPPED in PR #62, closes #45)

**Do not re-run these steps.** History of how htmx 2.0.11 was vendored and
how pipeline / call-outcome swaps were wired. Line numbers in this section
were already drifting when the plan was written; re-grep on current `main`
if you need the live locations.

Every action in the app is a `<form method=post>` that reloads the whole
page. The two that feel worst to a live user are the pipeline stage-move
dropdown and the call-outcome buttons, because both are used repeatedly in a
single sitting (a rep moving several deals, or working down a call list).
htmx fixes this without a rewrite: same server-rendered HTML, same forms, you
just add `hx-*` attributes and return a fragment instead of a redirect when
the request came from htmx.

1. Vendor htmx the same way as Phase 1:
   ```bash
   curl -fSL -o app/static/vendor/htmx.min.js \
     https://cdn.jsdelivr.net/npm/htmx.org@2/dist/htmx.min.js
   curl -fsSL https://data.jsdelivr.com/v1/packages/npm/htmx.org/resolved?specifier=2 \
     | grep -o '"version":"[^"]*"'
   ```
   Add a row to `app/static/vendor/VENDOR.md` with the resolved version.
   Add to `base.html`, right before `</head>` or alongside the existing
   `<script>` block near the end of `<body>` (either works; htmx just needs
   to load before any `hx-*` attributes are interacted with, so put it in
   `<head>` with `defer`, matching how the sendBeacon script already sits
   inline at the bottom — pick one pattern and be consistent):
   ```html
   <script src="/static/vendor/htmx.min.js" defer></script>
   ```

2. **Pipeline stage-move** (`app/templates/admin/pipeline.html:39-47`,
   backend at `app/routers/admin.py:737` `change_deal_stage` — re-grep both,
   line numbers have already drifted once since this plan was written and
   will drift again):
   - Read `change_deal_stage` in full first. It currently does the DB update
     then presumably redirects (check for `RedirectResponse` — the `next`
     hidden field, `pipeline.html:40`, suggests it redirects back to
     wherever it was called from, since this same form is reused elsewhere).
   - Add an htmx path: when the request has an `HX-Request` header, instead
     of redirecting, re-render **just the one column** the deal moved out of
     and **just the one column** it moved into (two `pipeline-column` divs),
     and return both concatenated, using `hx-swap-oob` on the second one so
     a single response can update two DOM locations at once. This needs a
     small new partial template, e.g.
     `app/templates/admin/_pipeline_column.html`, extracted from the
     `{% for col in columns %}` loop body in `pipeline.html` (lines 20-54),
     parameterized on one `col` — render it from both `pipeline.html` (loop
     over it with `{% include %}`) and from the new htmx branch in the
     router (render it twice, once per affected column, wrap the second in
     `<div id="col-{{ col.stage.key }}" hx-swap-oob="true">`).
   - On the `<select>` in the partial, replace:
     ```html
     <select name="stage" onchange="this.form.submit()">
     ```
     with:
     ```html
     <select name="stage"
             hx-post="/deals/{{ d.id }}/stage"
             hx-include="closest form"
             hx-swap="none"
             hx-trigger="change">
     ```
     (`hx-swap="none"` because the response uses out-of-band swaps to
     target both columns directly, not the element the request came from.)
     Keep the `<form>` wrapper and its hidden CSRF field — htmx will pick up
     the CSRF token via `hx-include`.
   - Test manually: move a deal between two visible columns, confirm the
     card jumps columns without a page reload and without a URL change.
     Then **turn off JS in the browser** and confirm the old behavior (plain
     form POST + full reload) still works — this must degrade gracefully
     since it's a `<form>` with a real `action`/`method`, not JS-only.

3. **Call outcome buttons** (`app/templates/admin/call_view.html`, look for
   `.call-outcome-form` / `.btn-outcome-*`, backend
   `app/routers/admin.py:318` `@router.post("/deals/{deal_id}/call-outcome")`
   — re-grep, this has already moved once):
   - Same pattern: on `HX-Request`, return a re-rendered fragment of the
     call view's outcome section (or the whole card) instead of a redirect,
     with `hx-post` + `hx-target="closest .call-outcome-form"` (or the
     nearest sensible wrapping element) + `hx-swap="outerHTML"` on each
     outcome button. A rep working down a call queue should see the next
     call load without a full navigation.
   - Same graceful-degradation check: JS off → falls back to a normal POST
     + redirect.

4. Run `pytest` — the existing tests hit these routes without the
   `HX-Request` header, so they should exercise the exact same code path as
   before (the redirect branch) and stay green; if you added a new branch
   guarded by `if request.headers.get("HX-Request")`, the old tests are your
   regression check that the non-htmx path is untouched. Optionally add one
   new test per route asserting that sending `HX-Request: true` returns a
   200 with a fragment (not a redirect) and no `<html>`/`<!DOCTYPE>` wrapper.

5. Commit each route's htmx conversion separately (two commits), so either
   can be reverted on its own without losing the other if something's off
   in production: `htmx: partial-swap pipeline stage moves`,
   `htmx: partial-swap call outcomes`.

**Acceptance for Phase 3 (met in PR #62):** both flows work with JS on (no
reload) and with JS off (falls back to the original full-page POST),
`pytest` green including new tests, each change is its own revertable
commit.

---

## Phase 4 — SortableJS drag-and-drop kanban ([#46](https://github.com/sevasek/agent-crm/issues/46))

Shipped. Dragging a card onto another column changes that deal's stage.
The drop sets the card's stage `<select>` and fires `change`, so the
request is the same `POST /deals/{id}/stage` the menu already uses
(session CSRF token, `next=pipeline`, htmx out-of-band column swap).
`set_deal_stage` still owns the move, including nurture, won, and
stage-automation side effects. A drop on the deal's current stage does
not post. Posting the current stage is a no-op inside `set_deal_stage`
(no second activity, nurture enrollment, won webhook, or automation).

The stage `<select>` stays on the card. With JavaScript off, a
`<noscript>` submit button posts the same form. If htmx did not load,
the existing `onchange` submits it. Sortable is loaded only from
`pipeline.html` (`app/static/vendor/sortable.min.js`,
`app/static/pipeline-board.js`, `app/static/pipeline-board.css`).

Touch: `delay` is 180ms and `delayOnTouchOnly` is true, with
`touchStartThreshold` 8 and `forceFallback`. Native HTML5 drag forces
the threshold down to 1px and then divides by devicePixelRatio, which
cancels the delay on the first pointer move. Fallback mode keeps an
8px threshold, so a scroll during those 180ms does not start a drag.
A mouse drag starts immediately.

Persisted order inside a column was skipped so this change does not
take a migration number. There is no `position` column. Reordering
cards inside one column is DOM-only until the next refresh.

Vendored file: SortableJS 1.15.7, recorded in
`app/static/vendor/VENDOR.md`. Nothing is loaded from a CDN at runtime.

---

## Phase 5 — optional extras (DEFERRED, [#47](https://github.com/sevasek/agent-crm/issues/47))

**Do not implement this.** Needs a decision; it is not launch work.
`docs/SCOPE.md` already lists dark mode as out of scope for launch.

- **Tom Select / Choices.js** for the partner/service `<select>` fields, but
  only if a customer actually has enough partners or services that a plain
  `<select>` becomes hard to scroll (check `partner_form.html` and
  `service_form.html` for which dropdowns those are). Don't add this
  speculatively.
- **Alpine.js** only if a specific interaction needs client-side state that
  htmx can't express server-side (e.g. a multi-step form section that
  toggles without a round-trip). Nothing identified in the current templates
  needs this yet — don't add it "just in case."
- **Real dark mode** — redo `--bg-alt` and the `.pill-*` colors as proper
  light/dark pairs (Pico's own tokens already flip automatically; the app's
  hand-picked ones don't), then remove the `data-theme="light"` lock added
  in Phase 1 step 4.

Do not vendor Tom Select or Alpine, and do not unlock dark mode, from this
file.

---

## If something goes wrong mid-phase

Each phase was one or two commits. If a shipped phase causes a visible
regression, `git revert` the specific commit(s) for that phase — Phase 1
and 2 are pure CSS/one `<link>` tag, Phase 3's two routes are independent of
each other and of Phases 1-2, so reverting one doesn't require reverting the
others. Don't reach for a broader rollback than the phase that broke.
