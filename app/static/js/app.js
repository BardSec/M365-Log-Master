/* M365 Log Master – client-side helpers */

// Live UTC clock in navbar
(function () {
  function tick() {
    const el = document.getElementById('clock');
    if (el) {
      el.textContent = new Date().toUTCString().replace(' GMT', ' UTC');
    }
  }
  tick();
  setInterval(tick, 1000);
})();

// Auto-submit search form on Enter within any input
document.addEventListener('DOMContentLoaded', function () {
  const form = document.getElementById('searchForm');
  if (form) {
    form.querySelectorAll('input').forEach(function (input) {
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          form.submit();
        }
      });
    });
  }
});
