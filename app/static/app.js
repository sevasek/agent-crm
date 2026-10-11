// Page helpers extracted from inline <script> / onclick so CSP can stay
// script-src 'self' (no unsafe-inline). Each block no-ops if its nodes
// are not on the page.

(function () {
    var box = document.getElementById("is_company");
    if (!box) return;
    function syncCompanyFields() {
        var isCompany = box.checked;
        document.querySelectorAll(".js-person-only").forEach(function (el) {
            el.hidden = isCompany;
        });
        document.querySelectorAll(".js-company-only").forEach(function (el) {
            el.hidden = !isCompany;
        });
    }
    box.addEventListener("change", syncCompanyFields);
    syncCompanyFields();
})();

(function () {
    var partner = document.getElementById("partner_id");
    var parent = document.getElementById("parent_deal_id");
    if (!partner || !parent) return;
    function filterParents() {
        var pid = partner.value;
        Array.prototype.forEach.call(parent.options, function (opt) {
            if (!opt.value) { opt.hidden = false; return; }
            opt.hidden = !pid || opt.getAttribute("data-partner-id") !== pid;
            if (opt.hidden && opt.selected) parent.value = "";
        });
        Array.prototype.forEach.call(parent.getElementsByTagName("optgroup"), function (og) {
            var any = false;
            Array.prototype.forEach.call(og.getElementsByTagName("option"), function (opt) {
                if (!opt.hidden) any = true;
            });
            og.hidden = !any;
        });
    }
    partner.addEventListener("change", filterParents);
    filterParents();
})();

// CSP blocks inline onchange="this.form.submit()". Pipeline selects also
// carry hx-trigger="change"; let htmx handle those so we don't POST twice.
document.addEventListener("change", function (event) {
    var el = event.target;
    if (!el || el.tagName !== "SELECT" || !el.form) return;
    var handler = el.getAttribute("onchange") || "";
    if (handler.indexOf("this.form.submit()") === -1) return;
    if (el.hasAttribute("hx-post") && window.htmx) return;
    el.form.submit();
});

document.addEventListener("submit", function (event) {
    var btn = event.submitter;
    if (!btn) return;
    var handler = btn.getAttribute("onclick") || "";
    var match = handler.match(/return confirm\('(.*)'\)/);
    if (!match) return;
    if (!window.confirm(match[1])) event.preventDefault();
});

document.addEventListener("click", function (event) {
    var btn = event.target.closest && event.target.closest("button[onclick]");
    if (!btn) return;
    var handler = btn.getAttribute("onclick") || "";
    if (handler.indexOf("navigator.clipboard.writeText") === -1) return;
    var key = document.getElementById("new-api-key");
    if (key && navigator.clipboard) navigator.clipboard.writeText(key.textContent);
});

document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter") return;
    var field = event.target;
    if (!field || field.name !== "note" || !field.form) return;
    if (!field.form.classList.contains("call-outcome-form")) return;
    event.preventDefault();
});

// Pipeline kanban: SortableJS drop → the same hx-post the stage <select>
// already uses (htmx-ext-sortable style). delayOnTouchOnly keeps board
// scrolling from stealing every mobile gesture.
(function () {
    function destStage(list) {
        var col = list && list.closest && list.closest(".pipeline-column");
        if (!col) return "";
        if (col.getAttribute("data-stage")) return col.getAttribute("data-stage");
        var id = col.id || "";
        if (id.indexOf("pipeline-col-") === 0) return id.slice("pipeline-col-".length);
        return "";
    }

    function postDrop(evt) {
        if (!evt || !evt.item) return;
        if (evt.to === evt.from && evt.oldIndex === evt.newIndex) return;
        var card = evt.item;
        var form = card.querySelector("form.stage-form");
        var select = card.querySelector("select[name=stage]");
        if (!form || !select) return;
        var stage = destStage(evt.to);
        if (!stage) return;
        var pos = form.querySelector("input[name=position]");
        if (!pos) {
            pos = document.createElement("input");
            pos.type = "hidden";
            pos.name = "position";
            form.appendChild(pos);
        }
        pos.value = String(evt.newIndex);
        select.value = stage;
        if (window.htmx) {
            window.htmx.trigger(select, "change");
        } else {
            form.submit();
        }
    }

    function bindLists() {
        if (typeof Sortable === "undefined") return;
        document.querySelectorAll(".pipeline-card-list").forEach(function (list) {
            if (Sortable.get(list)) return;
            Sortable.create(list, {
                group: "pipeline",
                animation: 150,
                draggable: ".pipeline-card",
                filter: "select, a, button, input, textarea, label",
                preventOnFilter: false,
                forceFallback: true,
                fallbackTolerance: 3,
                fallbackOnBody: true,
                delay: 200,
                delayOnTouchOnly: true,
                touchStartThreshold: 8,
                emptyInsertThreshold: 24,
                ghostClass: "sortable-ghost",
                chosenClass: "sortable-chosen",
                dragClass: "sortable-drag",
                onEnd: postDrop
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bindLists);
    } else {
        bindLists();
    }
    document.body.addEventListener("htmx:afterSwap", bindLists);
})();
