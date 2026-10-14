// Classic script (no import/export): its declarations are globals.
function formatDate(d) {
  return d.toISOString().slice(0, 10);
}

function initLegacy() {
  var el = document.getElementById('today');
  el.textContent = formatDate(new Date());
  trackEvent('legacy-init');
}

window.addEventListener('load', initLegacy);
