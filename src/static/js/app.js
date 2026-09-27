/* Small progressive enhancements. Every page works without this file. */
(function () {
  "use strict";

  // Tag suggestions: <button data-tag-add="python"> appends to <input data-tags>
  var tagInput = document.querySelector("[data-tags]");
  document.querySelectorAll("[data-tag-add]").forEach(function (button) {
    button.hidden = false;
    button.addEventListener("click", function () {
      if (!tagInput) return;
      var tag = button.getAttribute("data-tag-add");
      var current = tagInput.value.split(",").map(function (t) { return t.trim().toLowerCase(); })
        .filter(function (t) { return t; });
      if (current.indexOf(tag) === -1) current.push(tag);
      tagInput.value = current.join(", ");
      tagInput.focus();
    });
  });

  // Countdowns: <span data-countdown="<close ISO>" data-now="<server now ISO>">.
  // Time left is computed against the server's clock (offset from the page's render time),
  // so a participant whose laptop clock is wrong still sees the real deadline.
  var countdowns = document.querySelectorAll("[data-countdown]");
  if (countdowns.length) {
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    var tick = function () {
      countdowns.forEach(function (el) {
        var close = Date.parse(el.getAttribute("data-countdown"));
        var serverNow = Date.parse(el.getAttribute("data-now"));
        if (!el._offset) el._offset = serverNow - Date.now();
        var left = Math.floor((close - (Date.now() + el._offset)) / 1000);
        if (left <= 0) { el.textContent = "closed"; el.classList.add("countdown--closed"); return; }
        var d = Math.floor(left / 86400), h = Math.floor(left % 86400 / 3600),
            m = Math.floor(left % 3600 / 60), s = left % 60;
        el.textContent = "closes in " + (d ? d + "d " : "") + pad(h) + ":" + pad(m) + ":" + pad(s);
        el.classList.toggle("countdown--soon", left < 3600);
      });
    };
    tick();
    setInterval(tick, 1000);
  }

  // [ copy ] buttons: <button data-copy="#target-id">
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.hidden = false;
    button.addEventListener("click", function () {
      var target = document.querySelector(button.getAttribute("data-copy"));
      if (!target || !navigator.clipboard) return;
      navigator.clipboard.writeText(target.textContent.trim()).then(function () {
        var label = button.textContent;
        button.textContent = "copied";
        setTimeout(function () { button.textContent = label; }, 1500);
      });
    });
  });
})();
