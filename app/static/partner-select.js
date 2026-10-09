// Searchable partner picker for the deal form. Loaded only from
// deal_form.html. CSP is script-src 'self', so this cannot be inline.
// The native <select name="partner_id"> stays in the form and is what
// posts. Without this script (or without JavaScript) that select still
// submits. Edit-deal has no #partner_id; this no-ops there.
(function () {
    var select = document.getElementById("partner_id");
    if (!select || select.tagName !== "SELECT") return;
    if (typeof TomSelect !== "function" || select.tomselect) return;
    var picker = new TomSelect(select, {
        create: false,
        allowEmptyOption: false,
        maxOptions: null,
        closeAfterSelect: true,
        refreshThrottle: 0
    });
    // A pending search refresh can reopen the list after Enter. Cancel it
    // and close so the chosen partner stays selected.
    picker.on("item_add", function () {
        if (picker.refreshTimeout) {
            window.clearTimeout(picker.refreshTimeout);
            picker.refreshTimeout = null;
        }
        picker.setTextboxValue("");
        picker.close();
    });
})();
