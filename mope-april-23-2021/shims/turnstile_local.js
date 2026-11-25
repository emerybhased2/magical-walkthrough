/*
 * Local stand-in for Cloudflare Turnstile (challenges.cloudflare.com/turnstile/v0/api.js).
 *
 * The 2025 mope.io page only loads client.js from Turnstile's `onload=run` callback, and
 * client.js asks Turnstile for a token (turnstile.render(el, {sitekey, callback})) that it
 * sends to the game server in message 64. This shim exists solely for LOCAL testing of the
 * archived client: it provides the same API surface and returns an obviously fake token.
 * It performs no verification and must never be deployed in front of a public service.
 */
(function () {
  'use strict';
  var LOCAL_TOKEN = 'LOCAL-DEV-TURNSTILE-TOKEN';
  var counter = 0;

  window.turnstile = {
    render: function (element, options) {
      var id = 'local-turnstile-' + (++counter);
      setTimeout(function () {
        if (options && typeof options.callback === 'function') options.callback(LOCAL_TOKEN);
      }, 50);
      return id;
    },
    execute: function () {},
    reset: function () {},
    remove: function () {},
    getResponse: function () { return LOCAL_TOKEN; },
    isExpired: function () { return false; },
    ready: function (callback) { if (typeof callback === 'function') callback(); }
  };

  // Honour ?onload=<function>, like the real loader. The callback (e.g. run()) may be
  // defined later in the page than this async script, so wait for the DOM if needed.
  var src = (document.currentScript && document.currentScript.src) || '';
  var match = /[?&]onload=([\w$]+)/.exec(src);
  if (!match) return;
  var fire = function () {
    var fn = window[match[1]];
    if (typeof fn === 'function') fn();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fire);
  else setTimeout(fire, 0);
})();
