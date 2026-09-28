/* For the page that embeds a DOGFOOD gallery: sizes each <iframe data-dogfood-embed> to its content.

     <iframe data-dogfood-embed src="PORTAL/embed/events/<slug>/gallery"
             width="100%" height="600" title="projects"></iframe>
     <script src="PORTAL/static/js/embed.js" defer></script>

   (PORTAL is the portal's base URL; the organizer's event page shows the snippet filled in.)

   A height message is accepted only from the iframe it is about: its origin must be the iframe
   src's origin, and its source that iframe's window. Anything else is ignored. Without this script
   the iframe keeps its height attribute. */
(function () {
  "use strict";
  var MAX = 10000;
  function frames() {
    return Array.prototype.slice.call(document.querySelectorAll("iframe[data-dogfood-embed]"));
  }
  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.type !== "dogfood-embed-height" || typeof data.height !== "number") return;
    frames().forEach(function (frame) {
      var origin;
      try { origin = new URL(frame.src, window.location.href).origin; } catch (e) { return; }
      if (event.origin !== origin || event.source !== frame.contentWindow) return;
      var height = Math.max(100, Math.min(MAX, Math.round(data.height)));
      frame.style.height = height + "px";
    });
  });
})();
