// Pipeline kanban. SortableJS is an enhancement on top of the stage <select>.
// A drop onto another column sets that select and fires change, so htmx posts
// the card's existing form (CSRF, next=pipeline) to POST /deals/{id}/stage.
// The response is the same out-of-band column swap the menu already uses.
//
// Touch: TOUCH_DELAY_MS (180) and delayOnTouchOnly. A finger that moves
// TOUCH_START_THRESHOLD_PX (8) during the delay cancels the drag, so scrolling
// the board does not grab a card immediately. forceFallback is required:
// Sortable's native HTML5 drag path overwrites the threshold to 1px and then
// divides by devicePixelRatio, which cancels the delay on the first move.
// Mouse drags are not delayed.
//
// Order inside a column is DOM-only for this page view. It is not posted and
// not saved. There is no deals.position column.
(function () {
    var TOUCH_DELAY_MS = 180;
    var TOUCH_START_THRESHOLD_PX = 8;

    function stageOf(list) {
        return (list && list.getAttribute("data-stage")) || "";
    }

    function restore(evt) {
        var parent = evt.from;
        var card = evt.item;
        if (!parent || !card || card.parentNode === parent) return;
        var index = evt.oldIndex;
        var ref = (typeof index === "number" && index >= 0) ? parent.children[index] : null;
        if (ref) parent.insertBefore(card, ref);
        else parent.appendChild(card);
    }

    function onEnd(evt) {
        var card = evt.item;
        if (!card) return;
        if (evt.from === evt.to) return;
        var nextStage = stageOf(evt.to);
        var select = card.querySelector('select[name="stage"]');
        restore(evt);
        if (!select || !nextStage || select.value === nextStage) return;
        select.value = nextStage;
        select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    function initBoard() {
        if (typeof Sortable === "undefined") return;
        document.querySelectorAll(".pipeline-cards").forEach(function (list) {
            if (list.getAttribute("data-sortable-bound") === "1") return;
            list.setAttribute("data-sortable-bound", "1");
            Sortable.create(list, {
                group: "pipeline-deals",
                draggable: ".pipeline-card",
                animation: 150,
                delay: TOUCH_DELAY_MS,
                delayOnTouchOnly: true,
                touchStartThreshold: TOUCH_START_THRESHOLD_PX,
                forceFallback: true,
                fallbackOnBody: true,
                emptyInsertThreshold: 40,
                ghostClass: "pipeline-card-ghost",
                chosenClass: "pipeline-card-chosen",
                dragClass: "pipeline-card-drag",
                filter: "a, button, input, textarea, select, option, label",
                preventOnFilter: false,
                onEnd: onEnd
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initBoard);
    } else {
        initBoard();
    }
    document.addEventListener("htmx:afterOnLoad", initBoard);
})();
