/* Shared helpers: fetch with CSRF, toasts, modals, theme, formatting. */
(function () {
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';

  async function api(url, data, opts = {}) {
    const init = {
      method: opts.method || (data === undefined ? 'GET' : 'POST'),
      headers: { 'X-CSRFToken': csrf(), Accept: 'application/json' },
      credentials: 'same-origin',
      keepalive: !!opts.keepalive,
    };
    if (data !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(data);
    }
    const res = await fetch(url, init);
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (!res.ok && !opts.allowError) {
      const err = new Error((body && body.error) || `Request failed (${res.status})`);
      err.status = res.status; err.body = body;
      throw err;
    }
    return body || {};
  }

  function toast(message, type = 'info', timeout = 5000) {
    const stack = document.getElementById('flash-stack');
    if (!stack) return;
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.setAttribute('role', 'status');
    const span = document.createElement('span');
    span.textContent = message;
    const close = document.createElement('button');
    close.className = 'close'; close.innerHTML = '&times;'; close.onclick = () => el.remove();
    el.append(span, close);
    stack.appendChild(el);
    if (timeout) setTimeout(() => el.remove(), timeout);
  }

  function modal({ title, body, icon, confirmText = 'Confirm', cancelText = 'Cancel', danger = false, input = null, size = '' }) {
    return new Promise((resolve) => {
      const bd = document.createElement('div');
      bd.className = 'modal-backdrop';
      bd.innerHTML = `
        <div class="modal ${size}" role="dialog" aria-modal="true">
          <div class="modal-head">${icon ? `<div class="status-icon ${danger ? 'bad' : 'ok'}"><i data-lucide="${icon}"></i></div>` : ''}<h3></h3></div>
          <div class="modal-body"></div>
          <div class="modal-foot">
            ${cancelText ? `<button class="btn" data-a="cancel">${cancelText}</button>` : ''}
            <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-a="ok">${confirmText}</button>
          </div>
        </div>`;
      bd.querySelector('h3').textContent = title || '';
      const bodyEl = bd.querySelector('.modal-body');
      if (body instanceof Node) bodyEl.appendChild(body); else bodyEl.innerHTML = body || '';
      let field = null;
      if (input) {
        field = document.createElement(input.multiline ? 'textarea' : 'input');
        field.className = 'input mt-1';
        field.placeholder = input.placeholder || '';
        field.value = input.value || '';
        if (input.type) field.type = input.type;
        bodyEl.appendChild(field);
      }
      const done = (v) => { bd.remove(); document.removeEventListener('keydown', onKey); resolve(v); };
      const onKey = (e) => { if (e.key === 'Escape' && cancelText) done(null); };
      document.addEventListener('keydown', onKey);
      bd.addEventListener('click', (e) => {
        const a = e.target.closest('[data-a]')?.dataset.a;
        if (a === 'cancel') done(null);
        if (a === 'ok') done(field ? field.value : true);
        if (e.target === bd && cancelText) done(null);
      });
      document.body.appendChild(bd);
      icons();
      (field || bd.querySelector('[data-a="ok"]')).focus();
    });
  }

  function icons() { if (window.lucide) window.lucide.createIcons(); }

  function toggleTheme() {
    const root = document.documentElement;
    const current = root.getAttribute('data-theme') ||
      (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = current === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch (e) { /* storage unavailable */ }
  }

  const fmt = {
    clock(sec) {
      sec = Math.max(0, Math.floor(sec));
      const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
      const mm = String(m).padStart(2, '0'), ss = String(s).padStart(2, '0');
      return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    },
    time(iso) { return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); },
    ago(sec) {
      if (sec == null) return '—';
      if (sec < 10) return 'just now';
      if (sec < 60) return `${sec}s ago`;
      if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
      return `${Math.floor(sec / 3600)}h ago`;
    },
  };

  const riskColor = (risk) => ({ low: 'var(--risk-low)', medium: 'var(--risk-medium)', high: 'var(--risk-high)', critical: 'var(--risk-critical)' })[risk] || 'var(--risk-low)';

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }

  function lightbox(src) {
    const el = document.createElement('div');
    el.className = 'lightbox';
    el.innerHTML = `<img alt="Evidence">`;
    el.querySelector('img').src = src;
    el.onclick = () => el.remove();
    document.body.appendChild(el);
  }

  document.addEventListener('click', (e) => {
    const z = e.target.closest('[data-zoom]');
    if (z) { e.preventDefault(); lightbox(z.dataset.zoom || z.src || z.href); }
    const c = e.target.closest('[data-confirm]');
    if (c && !c.dataset.confirmed) {
      e.preventDefault();
      modal({ title: c.dataset.confirm, body: c.dataset.confirmBody || '', danger: c.classList.contains('btn-danger'), icon: 'alert-triangle' }).then((ok) => {
        if (!ok) return;
        c.dataset.confirmed = '1';
        if (c.form && c.type === 'submit') c.form.requestSubmit(c); else c.click();
      });
    }
  });

  document.addEventListener('DOMContentLoaded', () => {
    icons();
    document.querySelectorAll('.flash-stack .toast').forEach((t) => setTimeout(() => t.remove(), 7000));
  });

  window.App = { api, toast, modal, icons, toggleTheme, fmt, riskColor, escapeHtml, lightbox };
})();
