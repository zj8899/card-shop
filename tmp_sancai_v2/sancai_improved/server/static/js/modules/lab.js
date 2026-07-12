// ══════════════════════════════════════════════════════════════
// Strategy Lab module — factor combo → evaluate → rank → save
// ══════════════════════════════════════════════════════════════
import { api } from '../core/api.js';
import { createStore, bindRender } from '../core/store.js';
import { h, fmtNum, fmtPct, pctClass, toast } from '../core/dom.js';
import { EmptyState, SkeletonRows } from '../components/ui.js';

let selected = [];
let factorCatalog = [];

const store = createStore({ factors: [], results: [], loading: false, error: '', comboName: 'my_combo' });

let root;

export async function mount(container) {
  root = container;
  render(store.get());
  bindRender(store, render);
  await loadCatalog();
}

export function onShow() {}

async function loadCatalog() {
  try {
    const res = await api.get('/api/lab/factors');
    const cats = res?.data?.categories || {};
    const catalog = res?.data?.catalog || [];
    factorCatalog = catalog;
    // Build display list grouped by category
    const items = [];
    const catColors = { '技术因子': '#58a6ff', '微观结构': '#3fb950', '另类数据': '#d2991d', '情绪因子': '#a371f7' };
    for (const [cat, factors] of Object.entries(cats)) {
      items.push({ type: 'header', label: cat, color: catColors[cat] || '#8b949e' });
      for (const f of factors) {
        const isSel = selected.find(s => s.name === f.name);
        items.push({ type: 'factor', ...f, selected: !!isSel });
      }
    }
    store.set({ factors: items });
  } catch (e) { store.set({ factors: [], error: e.message }); }
}

function toggleFactor(name, label) {
  const idx = selected.findIndex(s => s.name === name);
  if (idx >= 0) { selected.splice(idx, 1); }
  else { if (selected.length < 8) selected.push({ name, label }); else { toast('最多选择8个因子', { type: 'warn' }); return; } }
  const cn = selected.map(s => s.name).slice(0, 3).join('_') || 'my_combo';
  store.set({ comboName: cn });
  // Re-render catalog with updated selection
  const items = [];
  const cats = {};
  for (const f of factorCatalog) { const cat = f.category_label || '其他'; if (!cats[cat]) cats[cat] = []; cats[cat].push(f); }
  const catColors = { '技术因子': '#58a6ff', '微观结构': '#3fb950', '另类数据': '#d2991d', '情绪因子': '#a371f7' };
  for (const [cat, factors] of Object.entries(cats)) {
    items.push({ type: 'header', label: cat, color: catColors[cat] || '#8b949e' });
    for (const f of factors) {
      const isSel = selected.find(s => s.name === f.name);
      items.push({ type: 'factor', ...f, selected: !!isSel });
    }
  }
  store.set({ factors: items });
}

async function runEval() {
  if (!selected.length) { toast('请先选择因子', { type: 'warn' }); return; }
  store.set({ loading: true, error: '' });
  try {
    // Shared symbol coupling (Bug 31): read the symbol charted by chart.js from a
    // simple global instead of a fragile cross-module DOM selector; fall back to '000001'.
    const sym = window._currentSymbol || '000001';
    const resp = await api.post('/api/lab/evaluate', {
      symbol: sym,
      factors_list: [selected.map(s => s.name)],
      metric: 'sharpe_ratio',
      top_n: 10 }, { timeoutMs: 60000 });
    const results = resp?.data?.results || [];
    store.set({ loading: false, results });
    if (!results.length) toast('评估完成，0个有效结果', { type: 'info' });
    else toast(`完成 ${results.length} 个组合评估`, { type: 'success' });
  } catch (e) { store.set({ loading: false, error: e.message }); toast(e.message, { type: 'error' }); }
}

async function saveCombo() {
  if (!selected.length) { toast('请先选择因子', { type: 'warn' }); return; }
  try {
    const resp = await api.post('/api/lab/save-strategy', {
      name: store.get().comboName, factors: selected.map(s => s.name),
      weights: {}, entry_threshold: 0.3, exit_threshold: -0.2 }, { timeoutMs: 15000 });
    toast('策略已保存: ' + resp?.data?.registry_key, { type: 'success' });
  } catch (e) { toast(e.message, { type: 'error' }); }
}

function render(state) {
  if (!root) return;
  root.innerHTML = '';
  root.appendChild(
    h('div', { class: 'lab-layout' }, [
      // Left: factor selector
      h('div', { class: 'lab-left card' }, [
        h('div', { class: 'card-head' }, [
          h('div', { class: 'card-title' }, [h('span', { class: 'dot', style: 'background:var(--brand-teal)' }), '因子组合器']),
          h('span', { class: 'card-action', onClick: loadCatalog }, '刷新'),
        ]),
        ...(() => {
          const els = []; let curHeader = null;
          for (const item of state.factors) {
            if (item.type === 'header') {
              curHeader = h('div', { style: `font-size:12px;font-weight:700;color:${item.color};margin:8px 0 4px;` }, item.label);
              els.push(curHeader);
            } else {
              els.push(h('span', {
                class: `chip ${item.selected ? 'chip-active' : ''}${item.needs_pipeline ? ' chip-dim' : ''}`,
                style: (item.selected ? `border-color:${item.color || '#58a6ff'};color:${item.color || '#58a6ff'};` : '') + (item.needs_pipeline ? 'opacity:0.55;' : ''),
                title: item.needs_pipeline ? `${item.label} — 需数据管道支持，无数据时因子值为0` : item.desc || '',
                onClick: () => toggleFactor(item.name, item.label),
              }, item.label || item.name));
            }
          }
          if (!state.factors.length) els.push(EmptyState({ title: '加载因子目录失败', hint: state.error || '检查 /api/lab/factors' }));
          return [h('div', { class: 'chip-wrap' }, els)];
        })(),
        selected.length ? h('div', { style: 'margin-top:12px;font-size:13px;color:var(--text-secondary);' }, [
          h('b', { style: 'color:var(--text-primary)' }, `已选 ${selected.length} 个: `),
          ...selected.map(s => h('span', { class: 'chip chip-active', style: 'margin:2px;' }, s.label)),
        ]) : null,
        h('div', { style: 'margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;' }, [
          h('input', { value: state.comboName, placeholder: '策略名', style: 'width:130px;font-size:12px;padding:6px 10px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-light);border-radius:6px;',
            onInput: (e) => store.set({ comboName: e.target.value }) }),
          h('button', { class: 'btn btn-primary', disabled: state.loading || !selected.length, onClick: runEval }, state.loading ? '评估中…' : '▶ 批量评估'),
          h('button', { class: 'btn btn-outline', disabled: !selected.length, onClick: saveCombo }, '保存策略'),
        ]),
      ]),
      // Right: results table
      h('div', { class: 'lab-right card' }, [
        h('div', { class: 'card-head' }, [
          h('div', { class: 'card-title' }, [h('span', { class: 'dot', style: 'background:var(--accent-ai)' }), '评估结果']),
          state.results.length ? h('span', { style: 'font-size:13px;color:var(--text-secondary);' }, `${state.results.length} 组合`) : null,
        ]),
        state.loading ? SkeletonRows(5) :
        !state.results.length ? EmptyState({ title: '选择因子后点击"批量评估"', hint: '系统将对因子组合进行回测评估并排序' }) :
        h('div', { class: 'scroll-y', style: 'max-height:60vh;' },
          h('table', { class: 'tbl tbl-compact' }, [
            h('thead', {}, h('tr', {}, ['排名', '因子', 'IC', '收益率', '胜率', '夏普', '回撤', '交易'].map(l => h('th', {}, l)))),
            h('tbody', {}, state.results.map((r, i) => {
              const retColor = r.total_return >= 0 ? 'var(--market-up)' : 'var(--market-down)';
              const ddColor = r.max_drawdown > -0.1 ? 'var(--market-down)' : r.max_drawdown > -0.2 ? 'var(--accent-warn)' : 'var(--market-up)';
              return h('tr', {}, [
                h('td', {}, String(i + 1)),
                h('td', { style: 'max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;', title: (r.factors||[]).join(', ') }, (r.factors||[]).slice(0, 4).join('+')),
                h('td', { class: 'num' }, (r.ic||0).toFixed(3)),
                h('td', { class: 'num', style: `color:${retColor}` }, fmtPct(r.total_return*100, 1)),
                h('td', { class: 'num' }, fmtPct(r.win_rate*100, 0)),
                h('td', { class: 'num' }, fmtNum(r.sharpe_ratio||0, 2)),
                h('td', { class: 'num', style: `color:${ddColor}` }, fmtPct(r.max_drawdown*100, 1)),
                h('td', { class: 'num' }, String(r.n_trades||0)),
              ]);
            })),
          ])
        ),
      ]),
    ])
  );
}
