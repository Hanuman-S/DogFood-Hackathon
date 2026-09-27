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
  // A page with a big form (the project editor) asks before leaving with unsaved changes.
  if (document.querySelector("form[data-guard-unsaved]")) {
    window.addEventListener("beforeunload", function (e) {
      if (!dirty) return;
      e.preventDefault();
      e.returnValue = "";
    });
  }

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

  // Rubric editor (progressive: without JS it is a plain formset with one blank row).
  //  - each row's share of the score (weight / sum of the kept rows' weights), as you type;
  //  - "remove" hides the whole row at once (it is deleted on save), with an undo;
  //  - "+ add criterion" adds as many blank rows as you like before one save.
  var rubricRows = document.querySelector("[data-rubric-rows]");
  if (rubricRows) {
    var removeBox = function (row) { return row.querySelector("input[type=checkbox][name$='-DELETE']"); };
    var recount = function () {
      var rows = Array.prototype.slice.call(rubricRows.querySelectorAll("[data-rubric-row]"));
      var kept = rows.filter(function (row) { var box = removeBox(row); return !(box && box.checked); });
      var total = 0;
      kept.forEach(function (row) {
        var value = parseFloat((row.querySelector("[data-weight]") || {}).value);
        if (value > 0) total += value;
      });
      rows.forEach(function (row) {
        var share = row.querySelector("[data-weight-share]");
        var value = parseFloat((row.querySelector("[data-weight]") || {}).value);
        if (!share) return;
        share.textContent = (kept.indexOf(row) !== -1 && total > 0 && value > 0)
          ? (Math.round(value / total * 1000) / 10) + "%" : "\u2013";
      });
    };
    var removedBar = document.querySelector("[data-rubric-removed]");
    var removedText = document.querySelector("[data-rubric-removed-text]");
    var removed = [];
    var showRemoved = function () {
      if (!removedBar) return;
      removedBar.hidden = removed.length === 0;
      if (removed.length) {
        var last = removed[removed.length - 1].querySelector("input[name$='-label']");
        removedText.textContent = removed.length + " removed" +
          (last && last.value ? " (last: " + last.value + ")" : "") + ". saved only when you press save rubric.";
      }
    };
    var setRemoved = function (row, isRemoved) {
      row.hidden = isRemoved;
      var i = removed.indexOf(row);
      if (isRemoved && i === -1) removed.push(row);
      if (!isRemoved && i !== -1) removed.splice(i, 1);
      showRemoved();
      recount();
    };
    rubricRows.addEventListener("input", recount);
    rubricRows.addEventListener("change", function (e) {
      if (e.target.matches("input[type=checkbox][name$='-DELETE']")) {
        setRemoved(e.target.closest("[data-rubric-row]"), e.target.checked);
      }
    });
    var undo = document.querySelector("[data-rubric-undo]");
    if (undo) undo.addEventListener("click", function () {
      var row = removed[removed.length - 1];
      if (!row) return;
      removeBox(row).checked = false;
      setRemoved(row, false);
    });
    // Rows already marked for removal (a page shown again after an error) stay hidden.
    rubricRows.querySelectorAll("[data-rubric-row]").forEach(function (row) {
      var box = removeBox(row);
      if (box && box.checked) setRemoved(row, true);
    });
    var addButton = document.querySelector("[data-rubric-add]");
    var template = document.getElementById("rubric-new-row");
    var totalForms = document.querySelector("input[name$='-TOTAL_FORMS']");
    var maxForms = document.querySelector("input[name$='-MAX_NUM_FORMS']");
    if (addButton && template && totalForms) {
      addButton.hidden = false;
      addButton.addEventListener("click", function () {
        var index = parseInt(totalForms.value, 10);
        if (maxForms && index >= parseInt(maxForms.value, 10)) {
          addButton.disabled = true;
          addButton.textContent = "at most " + maxForms.value + " criteria";
          return;
        }
        rubricRows.insertAdjacentHTML("beforeend", template.innerHTML.replace(/__prefix__/g, index));
        totalForms.value = index + 1;
        var label = rubricRows.lastElementChild && rubricRows.lastElementChild.querySelector("input[name$='-label']");
        if (label) label.focus();
        recount();
      });
    }
    recount();
  }

  // Custom question forms: the "choices" box only matters for a single-choice question, so it
  // is shown only when that kind is picked (a server error on it keeps it visible).
  document.querySelectorAll("select[name='kind']").forEach(function (kind) {
    var form = kind.form;
    var choices = form && form.querySelector("[name='choices']");
    var field = choices && choices.closest(".field");
    if (!field) return;
    var sync = function () {
      field.hidden = kind.value !== "choice" && !field.classList.contains("field--invalid");
    };
    kind.addEventListener("change", function () { field.classList.remove("field--invalid"); sync(); });
    sync();
  });

  // Keep your place. A form post reloads the page, which used to land at the top every time.
  // Before a post, remember where you were; on the page that comes back, return there -- or, if
  // the post was refused, to the first error. Any message shown at the top is repeated in a
  // small toast, since you are no longer scrolled up to it. (sessionStorage: this tab only.)
  var PLACE = "dogfood:place";
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if ((f.getAttribute("method") || "").toLowerCase() !== "post" || f.hasAttribute("data-no-keep-place")) return;
    try {
      sessionStorage.setItem(PLACE, JSON.stringify({
        from: location.pathname, to: new URL(f.action, location.href).pathname,
        y: window.scrollY, t: Date.now(),
      }));
    } catch (err) { /* storage off: the page just starts at the top */ }
  });
  var place = null;
  try {
    place = JSON.parse(sessionStorage.getItem(PLACE) || "null");
    sessionStorage.removeItem(PLACE);
  } catch (err) { place = null; }
  if (place && Date.now() - place.t < 60000) {
    var here = location.pathname;
    var restore = function () {
      var problem = document.querySelector(".field--invalid, .msg--error");
      if (problem && (here === place.to || here === place.from)) {
        problem.scrollIntoView({ block: "center" });
      } else if (here === place.from && !location.hash) {
        window.scrollTo(0, place.y);
      } else if (here === place.from && location.hash) {
        window.scrollTo(0, place.y);  // the redirect's #section is close; the exact spot is better
      } else {
        return;  // a different page: start at its top as usual
      }
      var notes = document.querySelectorAll(".msgs .msg");
      if (notes.length && window.scrollY > 200) {
        var toast = document.createElement("div");
        toast.className = "toast";
        toast.setAttribute("role", "status");
        notes.forEach(function (n) {
          var line = document.createElement("p");
          line.className = n.className;
          line.textContent = n.textContent;
          toast.appendChild(line);
        });
        document.body.appendChild(toast);
        setTimeout(function () { toast.classList.add("toast--gone"); }, 4000);
        setTimeout(function () { toast.remove(); }, 4600);
      }
    };
    if (document.readyState === "complete") restore();
    else window.addEventListener("load", function () { requestAnimationFrame(restore); });
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
