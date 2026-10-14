'use strict';
(() => {
  const search = document.getElementById('tool-search');
  const cards = Array.from(document.querySelectorAll('#tool-grid .tool-card'));
  const categories = Array.from(document.querySelectorAll('#category-tabs button'));
  const empty = document.getElementById('no-tools');
  const status = document.getElementById('search-status');
  let category = 'all';
  function filter() {
    const query = search.value.trim().toLowerCase();
    let visible = 0;
    for (const card of cards) {
      card.hidden = (category !== 'all' && card.dataset.category !== category) || !card.textContent.toLowerCase().includes(query);
      if (!card.hidden) visible++;
    }
    empty.hidden = visible !== 0;
    status.textContent = `${visible} ${visible === 1 ? 'tool' : 'tools'} found.`;
    for (const button of categories) {
      const active = button.dataset.category === category;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    }
  }
  search.addEventListener('input', filter);
  for (const button of categories) button.addEventListener('click', () => { category = button.dataset.category; filter(); });
  document.getElementById('clear-search').addEventListener('click', () => { search.value = ''; category = 'all'; filter(); search.focus(); });
  document.addEventListener('keydown', event => {
    if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !event.target.isContentEditable && !['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName)) {
      event.preventDefault(); search.focus();
    }
  });
})();
