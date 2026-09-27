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

  // Rubric editor: keep <span data-weight-total> equal to the sum of the [data-weight] inputs
  // (rows ticked for removal excluded), in thousandths so 33.334 + 33.333 + 33.333 is 100.
  var weightTotal = document.querySelector("[data-weight-total]");
  if (weightTotal) {
    var weights = document.querySelectorAll("[data-weight]");
    var removal = function (input) {
      return document.querySelector("[name='" + input.name.replace(/weight$/, "DELETE") + "']");
    };
    var recount = function () {
      var units = 0;
      weights.forEach(function (input) {
        var remove = removal(input);
        if (remove && remove.checked) return;
        var value = parseFloat(input.value);
        if (!isNaN(value)) units += Math.round(value * 1000);
      });
      weightTotal.textContent = String(units / 1000);
      weightTotal.classList.toggle("weight-total--off", units !== 100000);
    };
    weights.forEach(function (input) {
      input.addEventListener("input", recount);
      var remove = removal(input);
      if (remove) remove.addEventListener("change", recount);
    });
    recount();
  }

  // Live dashboards: <div data-refresh-url="...?partial=1" data-refresh-every="30"> replaces its
  // contents with a fresh render from the server. Without JavaScript the page is simply static.
  document.querySelectorAll("[data-refresh-url]").forEach(function (box) {
    var every = Math.max(10, parseInt(box.getAttribute("data-refresh-every"), 10) || 30) * 1000;
    var status = document.querySelector("[data-refresh-status]");
    var refresh = function () {
      if (document.hidden) return;
      fetch(box.getAttribute("data-refresh-url"), { credentials: "same-origin", headers: { "Accept": "text/html" } })
        .then(function (response) {
          if (!response.ok) throw new Error(response.status);
          return response.text();
        })
        .then(function (html) {
          box.innerHTML = html;
          if (status) status.textContent = " (updated " + new Date().toLocaleTimeString() + ")";
        })
        .catch(function () {
          if (status) status.textContent = " (could not refresh; showing the last update)";
        });
    };
    setInterval(refresh, every);
  });

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
