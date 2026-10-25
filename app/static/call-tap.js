// Fired alongside a real <a href="tel:..."> tap. The dial itself
// goes through the browser's native, reliable tel: link handling;
// this is just a fire-and-forget log of the tap, so it can't ever
// block or delay the actual call (sendBeacon queues and returns
// immediately, unlike a redirect the browser has to resolve first).
function logCallTap(url, csrfToken) {
    if (!navigator.sendBeacon) return;
    var fd = new FormData();
    fd.append("csrf_token", csrfToken);
    navigator.sendBeacon(url, fd);
}

// CSP script-src 'self' blocks inline onclick, so tel links carry
// data-call-tap="<log url>" data-csrf="<token>" instead.
document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a[data-call-tap]");
    if (!link) return;
    logCallTap(link.getAttribute("data-call-tap"), link.getAttribute("data-csrf") || "");
});
