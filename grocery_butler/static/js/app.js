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

/**
 * Bind input handler for the recipe search/filter bar.
 */
function initRecipeSearch() {
    var searchInput = document.getElementById("recipe-search");
    if (!searchInput) {
        return;
    }

    searchInput.addEventListener("input", function () {
        var query = searchInput.value.toLowerCase().trim();
        var cards = document.querySelectorAll(".recipe-card");
        cards.forEach(function (card) {
            var name = card.getAttribute("data-name") || "";
            if (name.indexOf(query) !== -1) {
                card.classList.remove("hidden");
            } else {
                card.classList.add("hidden");
            }
        });
    });
}

/**
 * Bind click handler for the add ingredient row button on recipe form.
 */
function initAddIngredientRow() {
    var addBtn = document.getElementById("add-ingredient-row");
    if (!addBtn) {
        return;
    }

    addBtn.addEventListener("click", function () {
        var container = document.getElementById("ingredient-rows");
        if (!container) {
            return;
        }
        var rows = container.querySelectorAll(".ingredient-row");
        var idx = rows.length;
        var categorySelect = container.querySelector("select");
        var options = "";
        if (categorySelect) {
            var allOptions = categorySelect.querySelectorAll("option");
            allOptions.forEach(function (opt) {
                options += '<option value="' + opt.value + '">' + opt.textContent + "</option>";
            });
        }

        var html =
            '<div class="form-row ingredient-row" data-index="' + idx + '">' +
            '<div class="form-group">' +
            '<label for="ing_name_' + idx + '">Name</label>' +
            '<input type="text" id="ing_name_' + idx + '" name="ing_name_' + idx + '" placeholder="e.g. pasta">' +
            "</div>" +
            '<div class="form-group">' +
            '<label for="ing_qty_' + idx + '">Qty</label>' +
            '<input type="number" id="ing_qty_' + idx + '" name="ing_qty_' + idx + '" step="0.01" value="1">' +
            "</div>" +
            '<div class="form-group">' +
            '<label for="ing_unit_' + idx + '">Unit</label>' +
            '<input type="text" id="ing_unit_' + idx + '" name="ing_unit_' + idx + '" placeholder="e.g. lb">' +
            "</div>" +
            '<div class="form-group">' +
            '<label for="ing_category_' + idx + '">Category</label>' +
            '<select id="ing_category_' + idx + '" name="ing_category_' + idx + '">' + options + "</select>" +
            "</div>" +
            '<div class="form-group form-group-check">' +
            '<label for="ing_pantry_' + idx + '">' +
            '<input type="checkbox" id="ing_pantry_' + idx + '" name="ing_pantry_' + idx + '"> Pantry' +
            "</label></div></div>";

        container.insertAdjacentHTML("beforeend", html);
    });
}

/* Initialize on DOMContentLoaded */
document.addEventListener("DOMContentLoaded", function () {
    initStatusButtons();
    initSearchFilter();
    initFlashDismiss();
    initNavToggle();
    initRecipeSearch();
    initAddIngredientRow();
});
