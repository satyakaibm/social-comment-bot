(() => {
  const back = document.querySelector('[data-back-fallback]');
  if (!back) return;
  function goBack() {
    if (window.history.length > 1) window.history.back();
    else window.location.assign(back.dataset.backFallback);
  }
  document.addEventListener('keydown', event => {
    const target = event.target;
    if (event.defaultPrevented || event.key !== 'Backspace' || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    if (target instanceof Element && (target.closest('input, textarea, select, [role="textbox"]') || target.isContentEditable)) return;
    event.preventDefault();
    goBack();
  });
  document.addEventListener('click', event => {
    document.querySelectorAll('details.profile[open]').forEach(menu => {
      if (!menu.contains(event.target)) menu.removeAttribute('open');
    });
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') document.querySelectorAll('details.profile[open]').forEach(menu => {
      menu.removeAttribute('open');
      menu.querySelector('summary').focus();
    });
  });
})();
