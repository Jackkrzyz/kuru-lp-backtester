/* Reveal a page only after its initial local data requests have completed. */
(() => {
  const content = document.getElementById('page-content');
  const loading = document.getElementById('page-loading');
  const message = document.getElementById('page-loading-message');
  const retry = document.getElementById('page-loading-retry');
  const pending = new Set((content.dataset.loadParts || '').split(',').filter(Boolean));
  let revealed = false;
  window.PageLoading = {
    ready(part) {
      pending.delete(part);
      if (pending.size || revealed) return;
      revealed = true;
      content.hidden = false;
      loading.hidden = true;
      document.body.setAttribute('aria-busy', 'false');
      window.dispatchEvent(new Event('page-ready'));
    },
    error(text) {
      if (revealed) return;
      message.textContent = text;
      retry.hidden = false;
    },
  };
  retry.addEventListener('click', () => window.location.reload());
})();
