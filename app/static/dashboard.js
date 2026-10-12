/* Dashboard behaviour. It lives here and not in on...= attributes so that the
   Content-Security-Policy of the dashboard can forbid inline script entirely. */
(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var el = event.target;
    // Embed code fields: one click selects the whole snippet.
    if (el.matches("input[data-select]")) {
      el.select();
      return;
    }
    // "Copy" buttons copy the value of the field right before them.
    var button = el.closest("button[data-copy]");
    if (button && button.previousElementSibling) {
      navigator.clipboard.writeText(button.previousElementSibling.value);
      button.textContent = "Copied! ✅";
    }
  });

  // Forms with data-confirm ask first. The message is read as data and shown as
  // plain text, so a banner title or a username can never become script.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.dataset.confirm !== undefined && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
    }
  });
})();
