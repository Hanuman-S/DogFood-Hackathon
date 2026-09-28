/* Inside the embeddable gallery: tell the page that frames it how tall the content is, so its
   embed.js can size the iframe. Sends a number and nothing else; the receiving page checks where the
   message came from. Without a parent (the page opened on its own) it does nothing. */
(function () {
  "use strict";
  if (window.parent === window) return;
  var last = 0;
  function send() {
    var height = Math.ceil(document.documentElement.scrollHeight);
    if (height === last) return;
    last = height;
    window.parent.postMessage({ type: "dogfood-embed-height", height: height }, "*");
  }
  window.addEventListener("load", send);
  window.addEventListener("resize", send);
  if (window.ResizeObserver) new ResizeObserver(send).observe(document.body);
  send();
})();
