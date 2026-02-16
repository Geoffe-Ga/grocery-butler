/* Grocery Butler — vanilla JS */

/**
 * Update an inventory item's status via AJAX POST.
 * @param {string} ingredient - The ingredient identifier.
 * @param {string} newStatus - The new status value (on_hand, low, out).
 * @returns {Promise<Object>} The parsed JSON response.
 */
function updateInventoryStatus(ingredient, newStatus) {
    return fetch("/inventory/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ingredient: ingredient, status: newStatus }),
    })
        .then(function (response) {
            return response.json().then(function (data) {
                return { ok: response.ok, data: data };
            });
        })
        .then(function (result) {
            if (!result.ok) {
                throw new Error(result.data.error || "Update failed");
            }
            return result.data;
        });
}

/**
 * Bind click handlers to all status toggle buttons.
 */
function initStatusButtons() {
    var groups = document.querySelectorAll(".status-buttons");
    groups.forEach(function (group) {
        var ingredient = group.getAttribute("data-ingredient");
        var buttons = group.querySelectorAll(".btn-status");
        buttons.forEach(function (btn) {
            btn.addEventListener("click", function () {
                var newStatus = btn.getAttribute("data-status");
                updateInventoryStatus(ingredient, newStatus)
                    .then(function () {
                        /* Remove active from all buttons in this group */
                        buttons.forEach(function (b) {
                            b.classList.remove("active");
                        });
                        btn.classList.add("active");
                    })
                    .catch(function (err) {
                        window.alert("Error: " + err.message);
                    });
            });
        });
    });
}

/**
 * Bind input handler for the inventory search/filter bar.
 */
function initSearchFilter() {
    var searchInput = document.getElementById("inventory-search");
    if (!searchInput) {
        return;
    }

    searchInput.addEventListener("input", function () {
        var query = searchInput.value.toLowerCase().trim();
        var cards = document.querySelectorAll(".inventory-card");
        cards.forEach(function (card) {
            var name = card.getAttribute("data-name") || "";
            var category = card.getAttribute("data-category") || "";
            var ingredient = card.getAttribute("data-ingredient") || "";
            var match =
                name.indexOf(query) !== -1 ||
                category.indexOf(query) !== -1 ||
                ingredient.indexOf(query) !== -1;
            if (match) {
                card.classList.remove("hidden");
            } else {
                card.classList.add("hidden");
            }
        });
    });
}

/**
 * Auto-dismiss flash messages after a delay.
 */
function initFlashDismiss() {
    var flashes = document.querySelectorAll(".flash");
    flashes.forEach(function (flash) {
        /* Auto-dismiss after 5 seconds */
        var timer = setTimeout(function () {
            flash.style.display = "none";
        }, 5000);

        /* Close button immediately dismisses */
        var closeBtn = flash.querySelector(".flash-close");
        if (closeBtn) {
            closeBtn.addEventListener("click", function () {
                clearTimeout(timer);
                flash.style.display = "none";
            });
        }
    });
}

/**
 * Toggle mobile navigation menu.
 */
function initNavToggle() {
    var toggle = document.querySelector(".nav-toggle");
    var links = document.querySelector(".nav-links");
    if (!toggle || !links) {
        return;
    }

    toggle.addEventListener("click", function () {
        var expanded = toggle.getAttribute("aria-expanded") === "true";
        toggle.setAttribute("aria-expanded", String(!expanded));
        links.classList.toggle("open");
    });
}

/* Initialize on DOMContentLoaded */
document.addEventListener("DOMContentLoaded", function () {
    initStatusButtons();
    initSearchFilter();
    initFlashDismiss();
    initNavToggle();
});
