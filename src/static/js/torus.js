/* A slowly turning ASCII torus for the login page -- DOGFOOD's "food".
 *
 * Plain projection maths: sample points on a torus, rotate them about two axes, project
 * onto a character grid with a z-buffer, and shade each cell by its surface normal against
 * a fixed light, using a 12-step brightness ramp.
 *
 * Runs only while the tab is visible, and renders one still frame for anyone who asked
 * their OS for reduced motion.
 */
(function () {
  "use strict";

  var el = document.querySelector("[data-torus]");
  if (!el) return;

  var W = 46, H = 22;              // grid size in characters
  var R1 = 1, R2 = 2, K2 = 5;      // tube radius, ring radius, viewer distance
  var K1 = (W * K2 * 3) / (8 * (R1 + R2));
  var RAMP = ".,-~:;=!*#$@";
  var TWO_PI = Math.PI * 2;

  function render(A, B) {
    var chars = new Array(W * H).fill(" ");
    var depth = new Float32Array(W * H);
    var cA = Math.cos(A), sA = Math.sin(A), cB = Math.cos(B), sB = Math.sin(B);

    for (var theta = 0; theta < TWO_PI; theta += 0.08) {
      var ct = Math.cos(theta), st = Math.sin(theta);
      var circleX = R2 + R1 * ct, circleY = R1 * st;

      for (var phi = 0; phi < TWO_PI; phi += 0.025) {
        var cp = Math.cos(phi), sp = Math.sin(phi);

        var x = circleX * (cB * cp + sA * sB * sp) - circleY * cA * sB;
        var y = circleX * (sB * cp - sA * cB * sp) + circleY * cA * cB;
        var invZ = 1 / (K2 + cA * circleX * sp + circleY * sA);

        var col = Math.floor(W / 2 + K1 * invZ * x);
        var row = Math.floor(H / 2 - K1 * invZ * y * 0.5); // characters are ~2x taller than wide
        if (col < 0 || col >= W || row < 0 || row >= H) continue;

        var light = cp * ct * sB - cA * ct * sp - sA * st + cB * (cA * st - ct * sA * sp);
        var i = col + W * row;
        if (light > 0 && invZ > depth[i]) {
          depth[i] = invZ;
          chars[i] = RAMP[Math.min(RAMP.length - 1, Math.floor(light * 8))];
        }
      }
    }

    var lines = [];
    for (var r = 0; r < H; r++) lines.push(chars.slice(r * W, r * W + W).join(""));
    return lines.join("\n");
  }

  var A = 0.9, B = 0.4;
  el.textContent = render(A, B);

  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  var last = 0;
  function tick(now) {
    if (now - last > 60) {           // ~16 fps is plenty for a CRT feel
      last = now;
      A += 0.035;
      B += 0.018;
      el.textContent = render(A, B);
    }
    if (!document.hidden) window.requestAnimationFrame(tick);
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) window.requestAnimationFrame(tick);
  });
  window.requestAnimationFrame(tick);
})();
