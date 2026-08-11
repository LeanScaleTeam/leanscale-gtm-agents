/* ============================================================
   LEANSCALE MODERN DESIGN STANDARD · behaviors  (v1)
   Drop-in, dependency-free. Powers two things every LeanScale
   surface uses:
     1. .reveal   -> fade/rise elements in on scroll
     2. .nav-links a (hash links) -> sticky-nav scroll-spy (.on)
   Also hides broken favicon <img>s so logo rows never show a
   busted-image glyph. Safe to include on any page; each feature
   no-ops if its markup isn't present.
   ============================================================ */
(function () {
  function reveal() {
    var els = document.querySelectorAll('.reveal');
    if (!els.length) return;
    if (!('IntersectionObserver' in window)) {
      els.forEach(function (e) { e.classList.add('in'); });
      return;
    }
    var io = new IntersectionObserver(function (en) {
      en.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
      });
    }, { rootMargin: '0px 0px -6% 0px', threshold: 0.06 });
    els.forEach(function (e) { io.observe(e); });
  }

  function scrollspy() {
    var links = Array.prototype.slice.call(document.querySelectorAll('.nav-links a[href^="#"]'));
    if (!links.length) return;
    var secs = links.map(function (a) { return document.querySelector(a.getAttribute('href')); });
    function onScroll() {
      var y = window.scrollY + 140, cur = 0;
      secs.forEach(function (s, i) { if (s && s.offsetTop <= y) cur = i; });
      links.forEach(function (a, i) { a.classList.toggle('on', i === cur); });
    }
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  function hideBrokenLogos() {
    setTimeout(function () {
      document.querySelectorAll('img').forEach(function (img) {
        var hide = function () { img.style.visibility = 'hidden'; };
        img.addEventListener('error', hide);
        if (img.complete && img.naturalWidth === 0) hide();
      });
    }, 200);
  }

  document.addEventListener('DOMContentLoaded', function () {
    reveal();
    scrollspy();
    hideBrokenLogos();
  });
})();
