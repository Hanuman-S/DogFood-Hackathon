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

  // Unsaved input: any change to a form field marks the page dirty until that form is submitted.
  // Automatic refreshes and reloads never run over it.
  var dirty = false;
  document.addEventListener("input", function (e) { if (e.target.form) dirty = true; });
  document.addEventListener("change", function (e) { if (e.target.form) dirty = true; });
  document.addEventListener("submit", function () { dirty = false; });

  // Countdowns: <span data-countdown="<close ISO>" data-now="<server now ISO>">.
  // Time left is computed against the server's clock (offset from the page's render time),
  // so a participant whose laptop clock is wrong still sees the real deadline. When one reaches
  // zero the page reloads once, so the server says what is true now (an extension may have moved
  // the close); with unsaved input it shows the [data-countdown-notice] beside it instead.
  var pad = function (n) { return (n < 10 ? "0" : "") + n; };
  var reloadedAtZero = false;
  var tickCountdown = function (el) {
    if (!el.isConnected) return false;
    var close = Date.parse(el.getAttribute("data-countdown"));
    if (el._offset === undefined) el._offset = Date.parse(el.getAttribute("data-now")) - Date.now();
    var left = Math.floor((close - (Date.now() + el._offset)) / 1000);
    if (left <= 0) {
      el.textContent = "closed";
      el.classList.add("countdown--closed");
      if (el._wasOpen) {  // it closed while this page was open (not: it was already closed)
        el._wasOpen = false;
        var notice = el.nextElementSibling;
        if (dirty || reloadedAtZero) {
          if (notice && notice.hasAttribute("data-countdown-notice")) notice.hidden = false;
        } else {
          reloadedAtZero = true;
          location.reload();
        }
      }
      return true;
    }
    el._wasOpen = true;
    var d = Math.floor(left / 86400), h = Math.floor(left % 86400 / 3600),
        m = Math.floor(left % 3600 / 60), sec = left % 60;
    el.textContent = "closes in " + (d ? d + "d " : "") + pad(h) + ":" + pad(m) + ":" + pad(sec);
    el.classList.toggle("countdown--soon", left < 3600);
    return true;
  };
  var countdowns = [];
  var initCountdowns = function (root) {
    root.querySelectorAll("[data-countdown]").forEach(function (el) {
      countdowns.push(el);
      tickCountdown(el);
    });
  };
  initCountdowns(document);
  setInterval(function () {
    countdowns = countdowns.filter(tickCountdown);  // drops ones a refresh replaced
  }, 1000);

  // Rubric editor: show each row's share of the score (weight / sum of the weights of the rows
  // being kept) in its <span data-weight-share>, as the organizer types. Weights are relative.
  var weights = document.querySelectorAll("[data-weight]");
  if (weights.length) {
    var removal = function (input) {
      return document.querySelector("[name='" + input.name.replace(/weight$/, "DELETE") + "']");
    };
    var shareOf = function (input) {
      var row = input.closest("tr");
      return row ? row.querySelector("[data-weight-share]") : null;
    };
    var recount = function () {
      var total = 0;
      weights.forEach(function (input) {
        var remove = removal(input);
        var value = parseFloat(input.value);
        if ((!remove || !remove.checked) && value > 0) total += value;
      });
      weights.forEach(function (input) {
        var share = shareOf(input);
        if (!share) return;
        var remove = removal(input);
        var value = parseFloat(input.value);
        share.textContent = (total > 0 && value > 0 && !(remove && remove.checked))
          ? (Math.round(value / total * 1000) / 10) + "%" : "\u2013";
      });
    };
    weights.forEach(function (input) {
      input.addEventListener("input", recount);
      var remove = removal(input);
      if (remove) remove.addEventListener("change", recount);
    });
    recount();
  }

  // Live pages: <div data-refresh-url="...?partial=1" data-refresh-every="30"> replaces its
  // contents with a fresh render from the server, every N seconds and on coming back to the tab
  // or the page. Without JavaScript the page is simply static. A tick is skipped while someone is
  // working in the box (focus inside it, a <details> open) or has unsaved input anywhere, so a
  // refresh never throws away what they were doing. If the answer is not the partial (the
  // session expired and the guard redirected to the login page, or an error), the page is
  // reloaded once so the portal's own guard decides what to show.
  var boxes = [];
  var gaveUp = false;
  document.querySelectorAll("[data-refresh-url]").forEach(function (box) {
    var status = box.parentNode.querySelector("[data-refresh-status]");
    var busy = function () {
      return dirty || box.contains(document.activeElement) || box.querySelector("details[open]");
    };
    var refresh = function () {
      if (document.hidden || gaveUp || busy()) return;
      fetch(box.getAttribute("data-refresh-url"), { credentials: "same-origin", headers: { "Accept": "text/html" } })
        .then(function (response) {
          var type = response.headers.get("Content-Type") || "";
          if (response.redirected || !response.ok || type.indexOf("text/html") !== 0) {
            gaveUp = true;
            location.reload();
            return null;
          }
          return response.text();
        })
        .then(function (html) {
          if (html === null || busy()) return;  // started typing while it loaded: keep theirs
          box.innerHTML = html;
          initCountdowns(box);
          initCopy(box);
          if (status) status.textContent = " (updated " + new Date().toLocaleTimeString() + ")";
        })
        .catch(function () {
          if (status) status.textContent = " (could not refresh; showing the last update)";
        });
    };
    var every = Math.max(10, parseInt(box.getAttribute("data-refresh-every"), 10) || 30) * 1000;
    setInterval(refresh, every);
    // Coming back to the tab (say, after assigning judges in another one) refreshes at once.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) refresh();
    });
    boxes.push(refresh);
  });

  // Back / Forward can show this page from the browser's memory, as it was. Bring it up to date:
  // refresh the live boxes, or reload a page that has none (unless it holds unsaved input).
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    if (boxes.length) boxes.forEach(function (refresh) { refresh(); });
    else if (!dirty) location.reload();
  });

  // [ copy ] buttons: <button data-copy="#target-id">
  function initCopy(root) {
    root.querySelectorAll("[data-copy]").forEach(function (button) {
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
  }
  initCopy(document);
})();
