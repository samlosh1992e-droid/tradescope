/* TradeScope — animations partagées */
(function () {
  'use strict';

  var doc = document;
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- Règles CSS injectées (ripple) ----
  var css = '.btn-ripple{position:absolute;border-radius:50%;pointer-events:none;' +
    'background:rgba(255,255,255,.45);transform:scale(0);opacity:.9;' +
    'animation:ts-ripple .7s var(--ease, ease) forwards;}' +
    '@keyframes ts-ripple{to{transform:scale(3.2);opacity:0}}';
  var st = doc.createElement('style');
  st.textContent = css;
  doc.head.appendChild(st);

  // ---- Header : état au scroll ----
  var header = doc.querySelector('header.site');
  function onScroll() {
    if (header) { if (window.scrollY > 8) header.classList.add('scrolled'); else header.classList.remove('scrolled'); }
    // Barre de progression
    var h = doc.documentElement;
    var max = h.scrollHeight - h.clientHeight;
    var pb = doc.getElementById('progress-bar');
    if (pb) pb.style.width = (max > 0 ? (window.scrollY / max) * 100 : 0) + '%';
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // ---- Barre de progression (injectée) ----
  (function () {
    var pb = doc.createElement('div');
    pb.id = 'progress-bar';
    doc.body.appendChild(pb);
  })();

  // ---- Apparition au scroll ----
  function revealInit() {
    var els = Array.prototype.slice.call(doc.querySelectorAll(
      '.panel:not(.animate-up):not(.animate-pop), .reason-block, ' +
      '.reasons li:not(#reasons li), table.plan tbody tr, .mode-card, ' +
      '.alert, .verdict-banner, .trigger-box, .tag, .chip, .trade-head'
    ));
    if (reduceMotion || !('IntersectionObserver' in window)) {
      els.forEach(function (el) { el.classList.add('rv-in'); });
      return;
    }
    els.forEach(function (el, i) {
      el.classList.add('rv');
      if (el.className.indexOf('reason-block') !== -1) el.classList.add(i % 2 ? 'slide-l' : 'slide-r');
      el.style.setProperty('--rv-i', String(Math.min(i % 10, 9)));
    });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('rv-in'); io.unobserve(e.target); }
      });
    }, { threshold: 0.08, rootMargin: '0px 0px -36px 0px' });
    els.forEach(function (el) { io.observe(el); });
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', revealInit);
  else revealInit();

  // ---- Compteurs animés (stats admin) ----
  function animateCount(el) {
    var target = parseFloat(el.textContent.replace(/[^0-9.,]/g, '').replace(',', '.')) || 0;
    var start = 0;
    var dur = 900;
    var t0 = null;
    function step(ts) {
      if (!t0) t0 = ts;
      var p = Math.min((ts - t0) / dur, 1);
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(start + (target - start) * eased).toLocaleString('fr-FR');
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }
  function countersInit() {
    doc.querySelectorAll('[data-count]').forEach(function (el) {
      var shown = false;
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting && !shown) { shown = true; animateCount(el); io.unobserve(el); }
        });
      }, { threshold: 0.5 });
      io.observe(el);
    });
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', countersInit);
  else countersInit();

  // ---- Ripple sur les boutons ----
  doc.addEventListener('click', function (e) {
    var btn = e.target.closest('.btn');
    if (!btn || reduceMotion) return;
    var rect = btn.getBoundingClientRect();
    var d = Math.max(rect.width, rect.height);
    var span = doc.createElement('span');
    span.className = 'btn-ripple';
    span.style.width = span.style.height = d + 'px';
    span.style.left = (e.clientX - rect.left - d / 2) + 'px';
    span.style.top = (e.clientY - rect.top - d / 2) + 'px';
    btn.appendChild(span);
    setTimeout(function () { span.remove(); }, 700);
  });

  // ---- Spot lumineux suiveur de souris ----
  function spotlightInit() {
    if (reduceMotion || !window.matchMedia('(hover: hover)').matches) return;
    var sel = doc.querySelectorAll('.panel, .mode-card, .bias-banner, .reason-block, .trade-card, .hero');
    sel.forEach(function (el) {
      if (el.__spot) return;
      el.__spot = true;
      el.addEventListener('mousemove', function (e) {
        var r = el.getBoundingClientRect();
        el.style.setProperty('--px', (e.clientX - r.left) + 'px');
        el.style.setProperty('--py', (e.clientY - r.top) + 'px');
      });
      el.addEventListener('mouseleave', function () {
        el.style.setProperty('--px', '50%');
        el.style.setProperty('--py', '50%');
      });
    });
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', spotlightInit);
  else spotlightInit();

  // ---- Scroll doux pour ancres internes ----
  doc.querySelectorAll('a[href^="#"]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      var id = a.getAttribute('href');
      if (id.length < 2) return;
      var target = doc.querySelector(id);
      if (!target) return;
      e.preventDefault();
      var top = target.getBoundingClientRect().top + (window.pageYOffset || 0) - 70;
      window.scrollTo({ top: top, behavior: reduceMotion ? 'auto' : 'smooth' });
    });
  });
})();