const isChinese = document.documentElement.lang === 'zh-CN';
const menu = document.querySelector('.menu-toggle');
const navigation = document.querySelector('#navigation');
function closeMenu() { navigation?.classList.remove('open'); menu?.setAttribute('aria-expanded', 'false'); }
menu?.addEventListener('click', () => { const open = menu.getAttribute('aria-expanded') !== 'true'; menu.setAttribute('aria-expanded', String(open)); navigation.classList.toggle('open', open); });
navigation?.querySelectorAll('a').forEach(a => a.addEventListener('click', closeMenu));
document.addEventListener('keydown', e => { if(e.key === 'Escape' && menu?.getAttribute('aria-expanded') === 'true') { closeMenu(); menu.focus(); } });
const tabs = [...document.querySelectorAll('[data-preview]')];
const previewPanels = [...document.querySelectorAll('#product .preview-panel')];
const previewNavigation = [...document.querySelectorAll('.demo-full-navigation [data-nav]')];
function selectPreview(key, focusTab = false) {
  const panel = document.getElementById(`preview-${key}`);
  if (!panel) return;
  const selectedTab = tabs.find(tab => tab.dataset.preview === key);
  previewPanels.forEach(item => { item.hidden = item !== panel; });
  tabs.forEach(item => {
    const active = item === selectedTab;
    item.setAttribute('aria-selected', String(active));
    item.tabIndex = active || (!selectedTab && item === tabs[0]) ? 0 : -1;
  });
  previewNavigation.forEach(item => {
    const active = item.dataset.nav === key;
    item.classList.toggle('active', active);
    if (active) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  document.getElementById('preview-label').textContent = panel.querySelector('h2').textContent;
  const icon = panel.querySelector('h2 svg');
  const targetIcon = document.getElementById('preview-icon');
  if (targetIcon) targetIcon.replaceChildren(...(icon ? [icon.cloneNode(true)] : []));
  if (focusTab) selectedTab?.focus();
}
previewNavigation.forEach(item => item.addEventListener('click', () => selectPreview(item.dataset.nav)));
document.querySelectorAll('[data-demo-open]').forEach(item => item.addEventListener('click', () => {
  selectPreview(item.dataset.demoOpen);
  document.querySelector(`[data-nav="${item.dataset.demoOpen}"]`)?.focus({preventScroll: true});
}));
tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => selectPreview(tab.dataset.preview));
  tab.addEventListener('keydown', event => {
    let next;
    if(event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    if(event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
    if(event.key === 'Home') next = 0;
    if(event.key === 'End') next = tabs.length - 1;
    if(next !== undefined) { event.preventDefault(); selectPreview(tabs[next].dataset.preview, true); }
  });
});
document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
  const code = document.getElementById(button.dataset.copy);
  const status = document.getElementById('copy-status');
  try {
    await navigator.clipboard.writeText(code.textContent);
    status.textContent = isChinese ? '命令已复制' : 'Command copied';
    button.setAttribute('aria-label', status.textContent);
    button.style.color = 'var(--blue)';
    setTimeout(() => { button.style.color = ''; button.setAttribute('aria-label', isChinese ? '复制命令' : 'Copy command'); status.textContent = ''; }, 1800);
  } catch {
    const selection = window.getSelection(); const range = document.createRange(); range.selectNodeContents(code); selection.removeAllRanges(); selection.addRange(range);
    status.textContent = isChinese ? '已选中命令，请手动复制' : 'Command selected. Copy it manually.';
  }
}));

// Keep the current section when switching document language.
document.querySelectorAll("[data-doc-language]").forEach(link => {
  const update = () => { link.hash = window.location.hash; };
  update();
  window.addEventListener("hashchange", update);
});

// Application filters mirror the console controls, using sample rows only.
function filterDemoApplications() {
  const query = document.getElementById('demo-app-search')?.value.trim().toLowerCase() || '';
  const region = document.getElementById('demo-app-region')?.value || 'all';
  const rows = [...document.querySelectorAll('[data-demo-app]')];
  let count = 0;
  rows.forEach(row => { row.hidden = !([row.cells[0].textContent, row.cells[2].textContent].join(' ').toLowerCase().includes(query) && (region === 'all' || row.dataset.region === region)); if (!row.hidden) count++; });
  const countLabel = document.getElementById('demo-app-count');
  if (countLabel) countLabel.textContent = String(count);
  const empty = document.getElementById('demo-app-empty');
  if (empty) empty.hidden = count > 0;
}
['demo-app-search', 'demo-app-region', 'demo-app-status'].forEach(id => document.getElementById(id)?.addEventListener('input', filterDemoApplications));
if (previewPanels.length) selectPreview('apps');
