// Categories lists only failed and broken tests. On a run without any, Allure shows a bare "There are no items",
// which reads like a broken page. This replaces it with a deliberate empty state, after checking the report's own
// data: "all passing" only when categories.json really has no failures, not when the status filters hide them.
(function () {
  let failures = null;                                   // failed + broken results, once categories.json is loaded

  const count = node => (node.children || []).reduce((n, child) => n + (child.children ? count(child) : 1), 0);
  fetch('data/categories.json')
    .then(res => res.json())
    .then(tree => { failures = count(tree); update(); })
    .catch(() => {});                                    // no data: leave Allure's own empty state alone

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  function emptyState(allPass) {
    const box = el('div', 'qa-empty');
    const badge = el('span', 'qa-empty__badge' + (allPass ? '' : ' qa-empty__badge_neutral'));
    badge.innerHTML = allPass
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 6h16M7 12h10M10 18h4"/></svg>';
    box.append(badge);
    if (allPass) {
      box.append(el('h3', '', 'All Tests Passing – No Categorized Failures'));
      box.append(el('p', '', 'The Categories view groups failing tests by defect type: product defects and test defects. '
                             + 'Every test in this run passed, so nothing needs categorizing.'));
    } else {
      box.append(el('h3', '', 'No Failures Match the Current Filters'));
      box.append(el('p', '', 'This run has ' + failures + ' failed or broken ' + (failures === 1 ? 'test' : 'tests')
                             + ', hidden by the status filters above. Turn those statuses back on to see them here.'));
    }
    const cta = el('a', 'qa-empty__cta', allPass ? 'View Passing Suites →' : 'View All Suites →');
    cta.href = '#suites';
    box.append(cta);
    return box;
  }

  function update() {
    const onCategories = location.hash.startsWith('#categories');
    const allPass = onCategories && failures === 0;
    document.body.classList.toggle('qa-categories-clear', allPass);
    if (!onCategories || failures === null) return;

    const empty = document.querySelector('.tree__empty');
    if (empty && !empty.dataset.qa) {
      empty.dataset.qa = 'done';
      empty.replaceChildren(emptyState(allPass));
    }
    const right = document.querySelector('.side-by-side__right .empty-view__message');
    if (right && !right.dataset.qa) {
      right.dataset.qa = 'done';
      right.textContent = 'Select a failure category on the left to inspect error traces, logs, and stack details.';
    }
  }

  new MutationObserver(update).observe(document.body, { childList: true, subtree: true });
  window.addEventListener('hashchange', update);
  update();
})();
