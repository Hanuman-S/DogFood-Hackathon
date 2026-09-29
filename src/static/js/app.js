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

  // Are you sure? A form (or its submit button) with data-confirm="<what will happen>" asks first,
  // in a dialog drawn in the portal's own style (the browser's confirm() box ignores the theme).
  // "cancel" has the focus, so a stray Enter never confirms. Registered before every other submit
  // listener and in the capture phase, so a cancelled submit is invisible to them (the page stays
  // as it was). Without this file the form simply submits; the server still enforces every rule.
  var confirmDialog = null;
  var askFirst = function (message, actionLabel, onYes) {
    if (!confirmDialog) {
      confirmDialog = document.createElement("dialog");
      confirmDialog.className = "confirm";
      confirmDialog.setAttribute("aria-labelledby", "confirm-title");
      confirmDialog.innerHTML =
        '<p class="confirm__title" id="confirm-title">are you sure?</p>' +
        '<p class="confirm__text" data-confirm-text></p>' +
        '<div class="actions"><button class="btn btn--ghost" type="button" data-confirm-no>cancel</button>' +
        '<button class="btn btn--danger" type="button" data-confirm-yes></button></div>';
      document.body.appendChild(confirmDialog);
      confirmDialog.querySelector("[data-confirm-no]").addEventListener("click", function () { confirmDialog.close(); });
      // A click on the backdrop (outside the box) is a cancel.
      confirmDialog.addEventListener("click", function (e) { if (e.target === confirmDialog) confirmDialog.close(); });
    }
    confirmDialog.querySelector("[data-confirm-text]").textContent = message;
    var yes = confirmDialog.querySelector("[data-confirm-yes]");
    var fresh = yes.cloneNode(false);  // drops the previous question's listener
    fresh.textContent = actionLabel || "yes";
    yes.replaceWith(fresh);
    fresh.addEventListener("click", function () { confirmDialog.close(); onYes(); });
    confirmDialog.showModal();
    confirmDialog.querySelector("[data-confirm-no]").focus();
  };
  document.addEventListener("submit", function (e) {
    var form = e.target, submitter = e.submitter || null;
    var message = (submitter && submitter.getAttribute("data-confirm")) || form.getAttribute("data-confirm");
    if (!message || form._confirmed) return;
    if (typeof HTMLDialogElement !== "function") {
      if (!window.confirm(message)) { e.preventDefault(); e.stopImmediatePropagation(); }
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
    var label = submitter ? submitter.textContent.trim() : "yes";
    askFirst(message, label, function () {
      form._confirmed = true;
      if (form.requestSubmit) form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
      else form.submit();
      form._confirmed = false;
    });
  }, true);

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
  // The Clipboard API exists only on HTTPS (and localhost). A portal served over plain HTTP uses
  // the older copy command on a selection; if even that is refused, the text is left selected and
  // the button says how to finish.
  function initCopy(root) {
    root.querySelectorAll("[data-copy]").forEach(function (button) {
      button.hidden = false;
      var label = button.textContent;
      var say = function (text) {
        button.textContent = text;
        setTimeout(function () { button.textContent = label; }, 2000);
      };
      var selectOnly = function (target) {
        var range = document.createRange();
        range.selectNodeContents(target);
        var selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      };
      var fallback = function (target) {
        selectOnly(target);
        var done = false;
        try { done = document.execCommand("copy"); } catch (err) { done = false; }
        say(done ? "copied" : "press ctrl+c");
      };
      button.addEventListener("click", function () {
        var target = document.querySelector(button.getAttribute("data-copy"));
        if (!target) return;
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(target.textContent.trim())
            .then(function () { say("copied"); }, function () { fallback(target); });
        } else {
          fallback(target);
        }
      });
    });
  }
  initCopy(document);

  // Tabs: <nav data-tabs> of links to #panel ids, each panel [data-tab-panel]. Without this file
  // the links jump down a page that shows every panel. With it, one panel shows at a time. The
  // page opens on: the first tab holding a refused field; else the tab holding the #hash (a
  // redirect to #judges lands on judging); else the tab last used on this page (a post that
  // redirects without a hash comes back where it was); else the first. Tabs holding a refused
  // field are marked.
  document.querySelectorAll("[data-tabs]").forEach(function (nav, navIndex) {
    var links = Array.prototype.slice.call(nav.querySelectorAll("a[href^='#']"));
    var panels = links.map(function (a) { return document.getElementById(a.getAttribute("href").slice(1)); });
    if (panels.some(function (p) { return !p; })) return;
    var key = "dogfood:tab:" + location.pathname + ":" + navIndex;
    nav.setAttribute("role", "tablist");
    links.forEach(function (a, i) {
      a.setAttribute("role", "tab");
      a.id = a.id || panels[i].id + "-tab";
      a.setAttribute("aria-controls", panels[i].id);
      panels[i].setAttribute("role", "tabpanel");
      panels[i].setAttribute("aria-labelledby", a.id);
      if (panels[i].querySelector(".field--invalid, .msg--error")) a.classList.add("tabs__tab--err");
    });
    var next = document.querySelector("[data-tab-next]");
    var current = -1;
    var show = function (index, remember) {
      current = index;
      links.forEach(function (a, i) {
        var on = i === index;
        a.setAttribute("aria-selected", on ? "true" : "false");
        a.tabIndex = on ? 0 : -1;
        a.classList.toggle("tabs__tab--on", on);
        panels[i].hidden = !on;
      });
      if (next) next.hidden = index >= links.length - 1;
      if (remember) {
        try { sessionStorage.setItem(key, panels[index].id); } catch (err) { /* storage off */ }
      }
    };
    var indexOf = function (el) {
      for (var i = 0; i < panels.length; i++) if (el && panels[i].contains(el)) return i;
      return -1;
    };
    var fromHash = function () {
      var id = location.hash.slice(1);
      return id ? indexOf(document.getElementById(decodeURIComponent(id))) : -1;
    };
    var start = links.findIndex(function (a) { return a.classList.contains("tabs__tab--err"); });
    if (start < 0) start = fromHash();
    if (start < 0) {
      try { start = indexOf(document.getElementById(sessionStorage.getItem(key) || "")); } catch (err) { start = -1; }
    }
    show(Math.max(start, 0), false);
    // A link to a whole tab (#tab-deadlines) would have the browser jump past the tab bar to the
    // panel; keep the bar in view instead.
    if (start >= 0 && location.hash.slice(1) === panels[start].id) {
      requestAnimationFrame(function () { nav.scrollIntoView({ block: "start" }); });
    }
    links.forEach(function (a, i) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        show(i, true);
        history.replaceState(null, "", a.getAttribute("href"));
      });
      a.addEventListener("keydown", function (e) {
        var step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
        var to = step ? (i + step + links.length) % links.length : e.key === "Home" ? 0 : e.key === "End" ? links.length - 1 : -1;
        if (to < 0) return;
        e.preventDefault();
        show(to, true);
        links[to].focus();
      });
    });
    // An in-page link to a section on another tab (say, "extend judging") opens that tab.
    window.addEventListener("hashchange", function () {
      var i = fromHash();
      if (i < 0 || i === current) return;
      show(i, true);
      var target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
      if (target && target !== panels[i]) target.scrollIntoView({ block: "start" });
    });
    if (next) next.addEventListener("click", function () {
      if (current < links.length - 1) { show(current + 1, true); links[current].focus(); }
    });
  });

  // A checklist item that links to a form field ("description", still missing) puts the cursor
  // in that field, not just the page beside it.
  document.querySelectorAll(".checklist a[href^='#']").forEach(function (link) {
    link.addEventListener("click", function (e) {
      var field = document.getElementById(link.getAttribute("href").slice(1));
      if (!field || !field.focus) return;
      e.preventDefault();
      field.scrollIntoView({ block: "center" });
      field.focus({ preventScroll: true });
    });
  });

  // Date boxes: the browser's own calendar ignores the theme (a grey or white popup), so date
  // inputs get this one instead. The box becomes a plain text box that still takes yyyy-mm-dd
  // typed by hand (the format the server parses), with a calendar button beside it. "Today" is
  // the UTC date: every time in the portal is UTC. Without this file the native picker is used.
  var MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December"];
  var iso = function (d) {
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate());
  };
  var parseIso = function (s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec((s || "").trim());
    if (!m) return null;
    var d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return iso(d) === m[0] ? d : null;
  };
  var openCal = null;
  var closeCal = function (refocus) {
    if (!openCal) return;
    var c = openCal;
    openCal = null;
    c.pop.remove();
    c.button.setAttribute("aria-expanded", "false");
    if (refocus) c.input.focus();
  };
  var buildCal = function (input, button, wrap) {
    var chosen = parseIso(input.value);
    var today = parseIso(iso(new Date()));
    var shown = chosen || today;
    var focusDay = new Date(shown.getTime());
    var pop = document.createElement("div");
    pop.className = "cal";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "choose a date (UTC)");
    var render = function (focus) {
      var year = focusDay.getUTCFullYear(), month = focusDay.getUTCMonth();
      pop.textContent = "";
      var head = document.createElement("div");
      head.className = "cal__head";
      var prev = document.createElement("button");
      prev.type = "button"; prev.className = "cal__nav"; prev.textContent = "<"; prev.setAttribute("aria-label", "previous month");
      var title = document.createElement("span");
      title.className = "cal__title"; title.textContent = MONTHS[month] + " " + year;
      title.setAttribute("aria-live", "polite");
      var nextM = document.createElement("button");
      nextM.type = "button"; nextM.className = "cal__nav"; nextM.textContent = ">"; nextM.setAttribute("aria-label", "next month");
      prev.addEventListener("click", function () { moveMonth(-1, false); });
      nextM.addEventListener("click", function () { moveMonth(1, false); });
      head.appendChild(prev); head.appendChild(title); head.appendChild(nextM);
      pop.appendChild(head);
      var grid = document.createElement("div");
      grid.className = "cal__grid";
      grid.setAttribute("role", "grid");
      ["mo", "tu", "we", "th", "fr", "sa", "su"].forEach(function (d) {
        var h = document.createElement("span");
        h.className = "cal__dow"; h.textContent = d;
        grid.appendChild(h);
      });
      var first = new Date(Date.UTC(year, month, 1));
      var lead = (first.getUTCDay() + 6) % 7;  // weeks start on Monday
      for (var i = 0; i < lead; i++) grid.appendChild(document.createElement("span"));
      var days = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
      var focusEl = null;
      for (var day = 1; day <= days; day++) {
        var date = new Date(Date.UTC(year, month, day));
        var b = document.createElement("button");
        b.type = "button";
        b.className = "cal__day";
        b.textContent = day;
        b.setAttribute("data-date", iso(date));
        b.setAttribute("aria-label", day + " " + MONTHS[month] + " " + year);
        if (chosen && iso(date) === iso(chosen)) { b.classList.add("cal__day--chosen"); b.setAttribute("aria-pressed", "true"); }
        if (iso(date) === iso(today)) b.classList.add("cal__day--today");
        var isFocus = iso(date) === iso(focusDay);
        b.tabIndex = isFocus ? 0 : -1;
        if (isFocus) focusEl = b;
        grid.appendChild(b);
      }
      pop.appendChild(grid);
      var foot = document.createElement("div");
      foot.className = "cal__foot";
      var todayBtn = document.createElement("button");
      todayBtn.type = "button"; todayBtn.className = "btn btn--small btn--ghost"; todayBtn.textContent = "today";
      todayBtn.addEventListener("click", function () { pick(today); });
      var zone = document.createElement("span");
      zone.className = "faint"; zone.textContent = "UTC";
      foot.appendChild(todayBtn); foot.appendChild(zone);
      pop.appendChild(foot);
      if (focus && focusEl) focusEl.focus();
    };
    var moveMonth = function (step, focus) {
      var y = focusDay.getUTCFullYear(), m = focusDay.getUTCMonth() + step, d = focusDay.getUTCDate();
      var last = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
      focusDay = new Date(Date.UTC(y, m, Math.min(d, last)));
      render(focus);
    };
    var pick = function (date) {
      input.value = iso(date);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      closeCal(false);
      // The time is the usual thing left to fill in: go to it.
      var time = wrap.parentNode.querySelector("input[type=time]");
      (time && !time.value ? time : input).focus();
    };
    pop.addEventListener("click", function (e) {
      var day = e.target.closest("[data-date]");
      if (day) pick(parseIso(day.getAttribute("data-date")));
    });
    pop.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.preventDefault(); closeCal(true); return; }
      if (!e.target.hasAttribute("data-date")) return;
      var step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key];
      if (step) {
        e.preventDefault();
        var month = focusDay.getUTCMonth();
        focusDay = new Date(focusDay.getTime() + step * 86400000);
        if (focusDay.getUTCMonth() !== month) render(true);
        else {
          pop.querySelectorAll("[data-date]").forEach(function (b) { b.tabIndex = -1; });
          var el = pop.querySelector("[data-date='" + iso(focusDay) + "']");
          el.tabIndex = 0; el.focus();
        }
      } else if (e.key === "PageUp" || e.key === "PageDown") {
        e.preventDefault();
        moveMonth(e.key === "PageUp" ? -1 : 1, true);
      }
    });
    render(false);
    return pop;
  };
  var showDateProblem = function (input, bad) {
    var field = input.closest(".field");
    var note = field && field.querySelector("[data-date-problem]");
    input.classList.toggle("input--bad", bad);
    if (bad) input.setAttribute("aria-invalid", "true"); else input.removeAttribute("aria-invalid");
    if (!field) return;
    if (bad && !note) {
      note = document.createElement("p");
      note.className = "field__error";
      note.setAttribute("data-date-problem", "");
      note.textContent = "use a real date: yyyy-mm-dd";
      field.querySelector(".datetime").insertAdjacentElement("afterend", note);
    } else if (!bad && note) {
      note.remove();
    }
  };
  document.querySelectorAll(".datetime input[type=date]").forEach(function (input) {
    if (input.disabled) return;  // a locked date keeps the plain (disabled) box
    input.type = "text";
    input.setAttribute("inputmode", "numeric");
    input.setAttribute("placeholder", "yyyy-mm-dd");
    var wrap = document.createElement("span");
    wrap.className = "datepick";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    var button = document.createElement("button");
    button.type = "button";
    button.className = "datepick__btn";
    button.setAttribute("aria-label", "open calendar");
    button.setAttribute("aria-haspopup", "dialog");
    button.setAttribute("aria-expanded", "false");
    button.textContent = "▦";
    wrap.appendChild(button);
    button.addEventListener("click", function () {
      var wasThis = openCal && openCal.input === input;
      closeCal(false);
      if (wasThis) return;
      var pop = buildCal(input, button, wrap);
      wrap.appendChild(pop);
      openCal = { input: input, button: button, pop: pop };
      button.setAttribute("aria-expanded", "true");
      var focus = pop.querySelector(".cal__day[tabindex='0']");
      if (focus) focus.focus();
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown" && e.altKey) { e.preventDefault(); button.click(); }
    });
    // Typing: digits only, and the dashes put in for you ("20261028" -> "2026-10-28"). A dash is
    // added only before a digit that follows it, so backspace never gets stuck on one. Pasted
    // "2026/10/28" or "2026-10-28" keep their digits the same way.
    input.addEventListener("input", function () {
      var digits = input.value.replace(/\D/g, "").slice(0, 8);
      var shaped = digits.slice(0, 4) + (digits.length > 4 ? "-" + digits.slice(4, 6) : "") +
        (digits.length > 6 ? "-" + digits.slice(6, 8) : "");
      if (shaped !== input.value) input.value = shaped;
      showDateProblem(input, false);
    });
    // Leaving the box: a date that does not exist (2026-13-45, or half typed) is marked at once.
    // The server checks every date too; this only saves a round trip.
    input.addEventListener("blur", function () {
      showDateProblem(input, input.value !== "" && !parseIso(input.value));
    });
  });
  document.addEventListener("mousedown", function (e) {
    if (openCal && !openCal.pop.contains(e.target) && e.target !== openCal.button) closeCal(false);
  });
  document.addEventListener("focusin", function (e) {
    if (openCal && !openCal.pop.contains(e.target) && e.target !== openCal.button) closeCal(false);
  });
})();
