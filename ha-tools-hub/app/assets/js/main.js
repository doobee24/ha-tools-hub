// ════════════════════════════════════════════════════════════════
//  HA Tools Hub — main.js
//  Shared nav, addon proxy helpers, theme utilities.
//
//  HA-Tools-Hub always runs as a HA addon via ingress.
//  All HA data flows through server.py using SUPERVISOR_TOKEN.
//  No WebSocket. No user token. No connection UI. No cog.
//
//  Copyright © 2026 HA Tools Hub. All rights reserved.
// ════════════════════════════════════════════════════════════════

// ── HAConn ───────────────────────────────────────────────────────
// Addon proxy helpers. Always in addon/ingress mode.
var HAConn = (function () {
  'use strict';

  // Derive the ingress base URL from the current pathname.
  // Ingress URL: /api/hassio_ingress/<TOKEN>/pages/xxx.html
  // Returns:     https://ha.local:8123/api/hassio_ingress/<TOKEN>/
  function addonBase() {
    var parts  = location.pathname.split('/');
    var apiIdx = parts.indexOf('api', 1);
    if (apiIdx >= 0 && parts[apiIdx + 1] === 'hassio_ingress') {
      return location.origin + '/' + parts.slice(1, apiIdx + 3).join('/') + '/';
    }
    // Fallback: strip last two segments (filename + pages dir)
    return location.origin + location.pathname.replace(/\/[^/]*\/[^/]*$/, '/');
  }

  // Proxy any HA Core REST endpoint via server.py (SUPERVISOR_TOKEN).
  // path: e.g. '/api/states'
  async function addonFetch(path) {
    var r = await fetch(addonBase() + 'api/proxy?path=' + encodeURIComponent(path), { credentials: 'include' });
    if (!r.ok) throw new Error('proxy HTTP ' + r.status);
    var d = await r.json();
    if (!d.ok) throw new Error(d.error || 'proxy error');
    return d.data;
  }

  // Proxy HA registry commands via server.py.
  // type: 'entities' | 'devices' | 'areas' | 'floors'
  async function addonRegistry(type) {
    var r = await fetch(addonBase() + 'api/registry?type=' + encodeURIComponent(type), { credentials: 'include' });
    if (!r.ok) throw new Error('registry HTTP ' + r.status);
    var d = await r.json();
    if (!d.ok) throw new Error(d.error || 'registry error');
    return d.result;
  }

  // Run an allowlisted HA WebSocket command via server.py.
  // type: e.g. 'config_entries/list', 'get_states', 'system_health/info'
  async function addonWsCmd(type) {
    try {
      var r = await fetch(addonBase() + 'api/ws-command?type=' + encodeURIComponent(type), { credentials: 'include' });
      if (!r.ok) return { ok: false, result: null };
      return await r.json();
    } catch (e) {
      return { ok: false, result: null };
    }
  }

  return {
    addonBase:     addonBase,
    addonFetch:    addonFetch,
    addonRegistry: addonRegistry,
    addonWsCmd:    addonWsCmd,
    // Shims — keep pages working without errors during transition
    addonMode:     true,
    state:         'addon',
    url:           '',
    token:         '',
    send:          function () { return Promise.resolve({ ok: false, result: null, error: 'addon-mode' }); },
    connect:       function () {},
    disconnect:    function () {},
    onStateChange: function () {},
    whenConnected: function (cb) { if (typeof cb === 'function') cb(); },
  };
}());

// ── HubSettings ──────────────────────────────────────────────────
var HubSettings = (function () {
  var KEY      = 'hub_settings_v2';
  var DEFAULTS = { autoPopulate: true, devMode: false };
  function load() {
    try { return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(KEY) || '{}')); }
    catch (e) { return Object.assign({}, DEFAULTS); }
  }
  function save(s) { try { localStorage.setItem(KEY, JSON.stringify(s)); } catch (e) {} }
  function get(k) { return load()[k]; }
  function set(k, v) { var s = load(); s[k] = v; save(s); }
  return { get: get, set: set, load: load, save: save };
}());

// ── Global helpers ────────────────────────────────────────────────
function escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── Nav injection ─────────────────────────────────────────────────
(function () {
  var LINKS = [
    { href: 'index.html',                  label: '🏠 Home'        },
    { href: 'system.html',                 label: '🧠 System'      },
    { href: 'config-manager.html',         label: '⚙️ Config'      },
    { href: 'automation-manager.html',     label: '⚡ Automations' },
    { href: 'device-explorer.html',        label: '🔭 Devices'     },
    { href: 'health-score.html',           label: '❤️ Health'      },
    { href: 'dashboard-card-library.html', label: '📚 Cards'       },
  ];

  function buildNav() {
    var nav = document.querySelector('.hub-nav');
    if (!nav || nav.children.length > 0) return;
    var cur = location.pathname.split('/').pop() || 'index.html';

    var html = '<a href="index.html" class="hub-nav-logo">'
      + '<span>HA Tools Hub</span>'
      + '</a>'
      + '<div class="hub-nav-links">'
      + LINKS.map(function (l) {
          return '<a href="' + l.href + '"' + (l.href === cur ? ' class="active"' : '') + '>' + l.label + '</a>';
        }).join('')
      + '</div>'
      + '<button id="hub-hamburger" aria-label="Menu" onclick="hubToggleMobile()"'
      + ' style="display:none;margin-left:auto;padding:5px 10px;border:1px solid var(--border);'
      + 'border-radius:6px;background:transparent;color:var(--text2);font-size:18px;cursor:pointer;">☰</button>';

    nav.innerHTML = html;
  }

  function buildMobileMenu() {
    if (document.getElementById('hub-mobile-menu')) return;
    var cur  = location.pathname.split('/').pop() || 'index.html';
    var menu = document.createElement('div');
    menu.id  = 'hub-mobile-menu';
    menu.style.cssText = 'display:none;position:fixed;inset:0;background:var(--surface);z-index:500;'
      + 'flex-direction:column;padding:20px;gap:4px;overflow-y:auto;';
    menu.innerHTML = '<div style="display:flex;justify-content:flex-end;margin-bottom:16px;">'
      + '<button onclick="hubToggleMobile()" style="border:none;background:none;'
      + 'color:var(--text2);font-size:24px;cursor:pointer;">✕</button></div>'
      + LINKS.map(function (l) {
          var active = l.href === cur;
          return '<a href="' + l.href + '" onclick="hubToggleMobile()" style="display:block;padding:12px 16px;'
            + 'border-radius:8px;text-decoration:none;font-size:15px;font-weight:600;'
            + 'color:' + (active ? 'var(--accent)' : 'var(--text1)') + ';'
            + 'background:' + (active ? 'rgba(74,144,217,0.1)' : 'transparent') + ';">' + l.label + '</a>';
        }).join('');
    document.body.appendChild(menu);
  }

  function checkWidth() {
    var btn   = document.getElementById('hub-hamburger');
    var links = document.querySelector('.hub-nav-links');
    if (!btn) return;
    if (window.innerWidth <= 768) {
      btn.style.display   = 'flex';
      if (links) links.style.display = 'none';
    } else {
      btn.style.display   = 'none';
      if (links) links.style.display = '';
      var menu = document.getElementById('hub-mobile-menu');
      if (menu) menu.style.display = 'none';
    }
  }

  // Globals used by inline onclick handlers
  window.hubToggleMobile = function () {
    var menu = document.getElementById('hub-mobile-menu');
    if (!menu) return;
    var open = menu.style.display === 'flex';
    menu.style.display = open ? 'none' : 'flex';
    var btn = document.getElementById('hub-hamburger');
    if (btn) btn.textContent = open ? '☰' : '✕';
  };

  window.hubToggleSidebar = function () {
    var sel = ['.sa-sidebar','.cm-sidebar','.am-sidebar','.de-sidebar','.hs-sidebar','.hub-sidebar'];
    var sb  = null;
    for (var i = 0; i < sel.length; i++) { sb = document.querySelector(sel[i]); if (sb) break; }
    if (!sb) return;
    var hidden = sb.classList.toggle('sidebar-hidden');
    var btn = document.getElementById('sidebar-toggle-btn');
    if (btn) btn.textContent = hidden ? '⊞' : '⊟';
  };

  function init() {
    buildNav();
    buildMobileMenu();
    checkWidth();
    var t;
    window.addEventListener('resize', function () { clearTimeout(t); t = setTimeout(checkWidth, 80); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        var m = document.getElementById('hub-mobile-menu');
        if (m) m.style.display = 'none';
        var b = document.getElementById('hub-hamburger');
        if (b) b.textContent = '☰';
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    setTimeout(init, 0);
  }
}());
