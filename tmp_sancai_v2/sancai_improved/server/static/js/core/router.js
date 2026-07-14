// ══════════════════════════════════════════════════════════════
// Router — hash-based view switching with lazy module loading
// ══════════════════════════════════════════════════════════════

const routes = {
  dashboard: { label: '仪表盘', loader: () => import('../modules/dashboard.js') },
  chart:     { label: 'K线图',   loader: () => import('../modules/chart.js') },
  backtest:  { label: '回测',    loader: () => import('../modules/backtest.js') },
  didao:     { label: '策略选股', loader: () => import('../modules/didao.js') },
  news:      { label: '消息追踪', loader: () => import('../modules/news.js') },
  strategies:{ label: '策略管理', loader: () => import('../modules/strategies.js') },
  lab:       { label: '策略实验室', loader: () => import('../modules/lab.js') },
  research:  { label: 'AI 研究',  loader: () => import('../modules/research.js') },
  evolution: { label: '进化实验室', loader: () => import('../modules/evolution.js') },
  stocks:    { label: '数据管理', loader: () => import('../modules/stocks.js') },
  admin:     { label: '系统配置', loader: () => import('../modules/admin.js') },
};

const mountedModules = {};
let current = null;

async function activate(route) {
  if (!routes[route]) route = 'dashboard';
  if (current === route) return;

  // Let the outgoing module tear down timers / listeners before we switch (Bug 30).
  const prev = current;
  mountedModules[prev]?.onHide?.();

  document.querySelectorAll('.rail-item[data-route]').forEach((el) => {
    el.classList.toggle('active', el.dataset.route === route);
    el.setAttribute('aria-current', el.dataset.route === route ? 'page' : 'false');
  });
  document.querySelectorAll('.view').forEach((el) => {
    el.classList.toggle('active', el.id === `view-${route}`);
  });

  const titleEl = document.getElementById('topbar-title-text');
  if (titleEl) titleEl.textContent = routes[route].label;

  current = route;
  history.replaceState(null, '', `#${route}`);

  const container = document.getElementById(`view-${route}`);
  if (!mountedModules[route]) {
    try {
      const mod = await routes[route].loader();
      await mod.mount(container);
      mountedModules[route] = mod;
    } catch (err) {
      console.error(`模块加载失败: ${route}`, err);
      container.innerHTML = `<div class="empty-state"><div class="empty-title">模块加载失败</div><div class="empty-hint">${err.message || err}</div></div>`;
    }
  } else {
    mountedModules[route]?.onShow?.();
  }
}

export function initRouter() {
  document.querySelectorAll('.rail-item[data-route]').forEach((el) => {
    el.addEventListener('click', () => activate(el.dataset.route));
  });
  window.addEventListener('hashchange', () => activate(location.hash.slice(1)));
  const initial = location.hash.slice(1) || 'dashboard';
  activate(initial);
}

export function navigate(route) { activate(route); }
