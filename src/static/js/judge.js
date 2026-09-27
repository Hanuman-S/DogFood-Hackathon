/* Judge portal enhancements: Animated ASCII Donut Progress Gauge & Toggleable Rating Stars. */
(function () {
  "use strict";

  // --- Toggleable Rating Radios (allows unchecking a selected rating) ---
  document.querySelectorAll(".rating-btn input[type='radio']").forEach(function (radio) {
    if (radio.checked) {
      radio._wasChecked = true;
      var parent = radio.closest(".rating-btn");
      if (parent) parent.classList.add("active");
    }

    radio.addEventListener("mousedown", function () {
      radio._wasChecked = radio.checked;
    });

    radio.addEventListener("click", function () {
      var parent = radio.closest(".rating-btn");
      if (radio._wasChecked) {
        radio.checked = false;
        radio._wasChecked = false;
        if (parent) parent.classList.remove("active");
      } else {
        var group = document.querySelectorAll("input[name='" + radio.name + "']");
        group.forEach(function (r) {
          r._wasChecked = false;
          var p = r.closest(".rating-btn");
          if (p) p.classList.remove("active");
        });
        radio.checked = true;
        radio._wasChecked = true;
        if (parent) parent.classList.add("active");
      }
    });
  });

  // --- Animated 3D ASCII Progress Donut ---
  var el = document.querySelector("[data-progress-donut]");
  if (!el) return;

  var pct = parseFloat(el.getAttribute("data-progress") || "0");
  var W = 42, H = 20;              // Grid dimensions in characters
  var R1 = 1, R2 = 2, K2 = 5;      // Tube radius, ring radius, viewer distance
  var K1 = (W * K2 * 3) / (8 * (R1 + R2));
  var RAMP = ".,-~:;=!*#$@";
  var TWO_PI = Math.PI * 2;

  function render(A, B) {
    var chars = new Array(W * H).fill(" ");
    var depth = new Float32Array(W * H);
    var cA = Math.cos(A), sA = Math.sin(A), cB = Math.cos(B), sB = Math.sin(B);

    // Phi angle range depends on progress percentage (0% to 100%)
    // If pct == 0, show a light wireframe outline; if pct > 0, fill proportional to pct
    var maxPhi = pct <= 0 ? TWO_PI : (pct / 100) * TWO_PI;
    var phiStep = pct <= 0 ? 0.25 : 0.03;

    for (var theta = 0; theta < TWO_PI; theta += 0.08) {
      var ct = Math.cos(theta), st = Math.sin(theta);
      var circleX = R2 + R1 * ct, circleY = R1 * st;

      for (var phi = 0; phi < maxPhi; phi += phiStep) {
        var cp = Math.cos(phi), sp = Math.sin(phi);

        var x = circleX * (cB * cp + sA * sB * sp) - circleY * cA * sB;
        var y = circleX * (sB * cp - sA * cB * sp) + circleY * cA * cB;
        var invZ = 1 / (K2 + cA * circleX * sp + circleY * sA);

        var col = Math.floor(W / 2 + K1 * invZ * x);
        var row = Math.floor(H / 2 - K1 * invZ * y * 0.5);
        if (col < 0 || col >= W || row < 0 || row >= H) continue;

        var light = cp * ct * sB - cA * ct * sp - sA * st + cB * (cA * st - ct * sA * sp);
        var i = col + W * row;
        if (pct <= 0) {
          if (invZ > depth[i]) {
            depth[i] = invZ;
            chars[i] = ".";
          }
        } else if (light > 0 && invZ > depth[i]) {
          depth[i] = invZ;
          chars[i] = RAMP[Math.min(RAMP.length - 1, Math.floor(light * 8))];
        }
      }
    }

    var lines = [];
    for (var r = 0; r < H; r++) {
      lines.push(chars.slice(r * W, r * W + W).join(""));
    }
    return lines.join("\n");
  }

  var A = 0.9, B = 0.4;
  el.textContent = render(A, B);

  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  var last = 0;
  function tick(now) {
    if (now - last > 50) {
      last = now;
      A += 0.04;
      B += 0.02;
      el.textContent = render(A, B);
    }
    if (!document.hidden) window.requestAnimationFrame(tick);
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) window.requestAnimationFrame(tick);
  });
  window.requestAnimationFrame(tick);
})();
