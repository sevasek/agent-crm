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
