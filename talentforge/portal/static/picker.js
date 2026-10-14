/* Template + colour picker: highlights the chosen card and recolours every thumbnail live. */
(function () {
  "use strict";
  document.querySelectorAll(".tpl-picker").forEach(function (picker) {
    function recolour(hex) {
      picker.querySelectorAll(".paper").forEach(function (p) { p.style.setProperty("--rc", hex); });
    }
    picker.querySelectorAll('input[name="template"]').forEach(function (r) {
      r.addEventListener("change", function () {
        picker.querySelectorAll(".tpl-card").forEach(function (c) {
          c.classList.toggle("on", c.contains(r) && r.checked);
        });
      });
    });
    picker.querySelectorAll('input[name="accent"]').forEach(function (r) {
      r.addEventListener("change", function () { if (r.checked) recolour(r.value); });
    });
  });
})();
