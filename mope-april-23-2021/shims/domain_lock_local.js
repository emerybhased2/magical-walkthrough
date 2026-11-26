/*
 * Lets the archived 2025 client run on http://localhost:<port>/ (no hosts-file entry).
 *
 * The obfuscator's domain guard at the top of client.js (a0_0xab04ee.run(), rebuilt L83) reads
 * document.domain (falling back to location.hostname) and blanks the page unless the name ends in
 * .mope.io or one of five sister domains. This page-level shim, injected by the web gateway before
 * client.js loads, gives `document` its own `domain` property naming a host the guard accepts.
 * client.js itself stays byte-for-byte the archived file. Nothing else in the client reads
 * document.domain. Local use only.
 */
(function () {
  'use strict';
  var host = location.hostname;
  if (/(^|\.)mope\.io$/.test(host)) return;   // already accepted (local.mope.io)
  try {
    Object.defineProperty(document, 'domain', {
      configurable: true,
      enumerable: true,   // the guard finds 'domain' with for...in; a hidden own property would hide it
      get: function () { return 'localhost.mope.io'; },
      set: function () {}
    });
  } catch (e) { /* the guard will blank the page; serve under local.mope.io instead */ }
})();
