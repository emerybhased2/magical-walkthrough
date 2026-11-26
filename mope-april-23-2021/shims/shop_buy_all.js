/*
 * Local addition to the archived 2025 shop: a "BUY ALL" entry in the shop's tab bar.
 *
 * Injected into index.html by the web gateway (rewrite "buy_all_button"); client.js is not changed.
 * The shop rebuilds its tab bar (#shop_div > ul) whenever a tab changes, so a MutationObserver
 * puts the entry back each time. Clicking it asks the local account server to buy every skin the
 * account does not own yet (POST /shop/buyAll, one all-or-nothing order; free while
 * ACCOUNT_INFINITE_CURRENCY is on), then closes and reopens the shop so the client reloads its
 * purchases and moves the skins to PURCHASES / WARDROBE.
 */
(function () {
  'use strict';
  var ID = 'localBuyAll';
  var busy = false;

  function label(text) {
    var el = document.getElementById(ID);
    if (el) el.textContent = text;
  }

  function reopenShop() {
    var close = document.getElementById('closebtn');
    if (close) close.click();
    setTimeout(function () {
      var open = document.getElementById('shopbutton3');
      if (open) open.click();
    }, 400);
  }

  function buyAll() {
    if (busy) return;
    var userId = localStorage.getItem('login_userId');
    var token = localStorage.getItem('login_passwordToken');
    if (!userId || !token) {
      label('LOG IN FIRST');
      return;
    }
    if (!window.confirm('Buy every skin in the shop that you do not own yet?')) return;
    busy = true;
    label('BUYING...');
    fetch('/shop/buyAll', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ userId: userId, token: token })
    }).then(function (r) { return r.json(); }).then(function (res) {
      busy = false;
      if (!res.success) {
        label('BUY ALL');
        window.alert(res.error || 'Buying all skins failed.');
        return;
      }
      label(res.bought ? 'BOUGHT ' + res.bought : 'ALL OWNED');
      var coins = document.getElementById('loginCoins');
      if (coins && typeof res.coins === 'number') coins.textContent = res.coins;
      if (res.bought) setTimeout(reopenShop, 600);
    }).catch(function () {
      busy = false;
      label('BUY ALL');
      window.alert('Could not reach the local account server.');
    });
  }

  function ensureEntry() {
    var bar = document.querySelector('#shop_div > ul');
    if (!bar || bar.querySelector('#' + ID)) return;
    var li = document.createElement('li');
    li.id = ID;
    li.className = 'title notselected';
    li.textContent = 'BUY ALL';
    li.title = 'Buy every skin you do not own yet';
    li.style.cssText = 'background:#e8a90c;color:#fff;border-radius:4px;cursor:pointer;font-weight:bold';
    li.onclick = function (e) { e.stopPropagation(); buyAll(); };
    var balance = bar.querySelector('.coins-balance');
    bar.insertBefore(li, balance || null);
  }

  new MutationObserver(ensureEntry).observe(document.documentElement, { childList: true, subtree: true });
})();
