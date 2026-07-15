// ══════════════════════════════════════════════════════════════
// Dashboard module
// ══════════════════════════════════════════════════════════════
import { api } from '../core/api.js';
import { createStore, bindRender } from '../core/store.js';
import { h, qs, fmtNum, fmtPct, fmtMoney, pctClass, escapeHtml, toast, announce } from '../core/dom.js';
import { EmptyState, SkeletonRows, StatTile } from '../components/ui.js';
import { navigate } from '../core/router.js';

const STRATEGY_LABELS = {
  chan_theory: '缠论', strict: 'BP1', strict_reverse: '追涨', simple: 'KDJ',
  ict: 'ICT', price_action: '价格行为', wyckoff: '威科夫', morphology: '形态学',
  gann: '江恩', wave_theory: '波浪', dow_theory: '道氏',
};

const store = createStore({
  focus: { loading: true, items: [] },
  news: { loading: true, items: [] },
  holdings: { loading: true, items: [], totalValue: 0, totalPnl: 0 },
  formOpen: false,
  editingSymbol: null,
  regime: null,   // V4 AI regime
  emotion: null,  // V4 emotion index
  quotes: { loading: false, data: {} },
  dataHealth: { loading: false, status: null, freshness: null },
  summary: null,
  _summaryTimer: 0,
  liveAccounts: null,
});

let root;
// Generation counters guard against race conditions when loadHoldings/loadQuotes
// are invoked from multiple places in rapid succession: a stale in-flight response
// is discarded if a newer load has started (see Bug 29).
let _holdingsGen = 0;
let _quotesGen = 0;

export async function mount(container) {
  root = container;
  render(store.get());
  bindRender(store, render);
  await Promise.all([loadFocus(), loadNews(), loadHoldings(), loadRegime(), loadDataHealth()]);
  loadQuotes();
  loadSummary();
  loadLiveAccounts();
}

export function onShow() {
  loadHoldings().then(() => loadQuotes());
  loadRegime();
  loadDataHealth();
  loadSummary();
}

// ── Data loaders ──

async function loadFocus() {
  store.set({ focus: { loading: true, items: [] } });
  try {
    const res = await api.get('/api/didao/screener/high-frequency-v2?days=7&min_count=3');
    const items = res?.data?.high_frequency || res?.high_frequency || [];
    store.set({ focus: { loading: false, items } });
  } catch (e) {
    store.set({ focus: { loading: false, items: [], error: e.message } });
  }
}

async function loadRegime() {
  try {
    const res = await api.get('/api/research/engines/regime?symbol=000001');
    store.set({ regime: res?.data });
  } catch (e) { store.set({ regime: null }); }
  try {
    const er = await api.get('/api/research/engines/emotion/index');
    store.set({ emotion: er?.data });
  } catch (e) { store.set({ emotion: null }); }
}

async function loadNews() {
  store.set({ news: { loading: true, items: [] } });
  try {
    const res = await api.get('/api/news/events?limit=5');
    const items = res?.events || [];
    store.set({ news: { loading: false, items } });
  } catch (e) {
    store.set({ news: { loading: false, items: [], error: e.message } });
  }
}

async function loadHoldings() {
  const gen = ++_holdingsGen;
  store.set((s) => ({ holdings: { ...s.holdings, loading: true } }));
  try {
    const res = await api.get('/api/holdings');
    if (gen !== _holdingsGen) return; // stale, discard
    const data = res?.data || {};
    store.set({ holdings: { loading: false, items: data.holdings || [], totalValue: data.total_value || 0, totalPnl: data.total_pnl || 0 } });
  } catch (e) {
    if (gen !== _holdingsGen) return; // stale, discard
    store.set((s) => ({ holdings: { ...s.holdings, loading: false, error: e.message } }));
  }
}

async function loadQuotes() {
  const symbols = store.get().holdings.items.map((h) => h.symbol);
  if (!symbols.length) return;
  const gen = ++_quotesGen;
  store.set((s) => ({ quotes: { ...s.quotes, loading: true } }));
  try {
    const res = await api.get(`/api/data/quotes?symbols=${symbols.join(',')}`);
    if (gen !== _quotesGen) return; // stale, discard
    const data = res?.data?.quotes || {};
    store.set({ quotes: { loading: false, data } });
  } catch (e) {
    if (gen !== _quotesGen) return; // stale, discard
    store.set({ quotes: { loading: false, data: {}, error: e.message } });
  }
}

async function loadDataHealth() {
  store.set((s) => ({ dataHealth: { ...s.dataHealth, loading: true } }));
  try {
    const res = await api.get('/api/data/health');
    // response is { status, freshness, schema } at top level
    store.set({ dataHealth: { loading: false, status: res?.status || 'error', freshness: res?.freshness } });
  } catch (e) {
    store.set({ dataHealth: { loading: false, status: 'error', freshness: null } });
  }
}

// ── Actions ──

function toggleForm(open, symbol = null) {
  store.set({ formOpen: open ?? !store.get().formOpen, editingSymbol: symbol });
}

async function saveHolding(formEl) {
  const fd = new FormData(formEl);
  const payload = {
    symbol: fd.get('symbol')?.trim(),
    name: fd.get('name')?.trim() || '',
    quantity: Number(fd.get('quantity')) || 0,
    cost_price: Number(fd.get('cost_price')) || 0,
    strategy: fd.get('strategy') || 'chan_theory',
  };
  if (!payload.symbol || payload.symbol.length !== 6) {
    toast('请输入6位股票代码', { type: 'error' }); return;
  }
  const editing = store.get().editingSymbol;
  try {
    if (editing) {
      await api.put(`/api/holdings/${editing}`, payload);
      toast('已更新持仓', { type: 'success' });
    } else {
      await api.post('/api/holdings', payload);
      toast('已添加持仓', { type: 'success' });
    }
    toggleForm(false);
    loadHoldings();
  } catch (e) {
    toast(e.message, { type: 'error' });
  }
}

async function deleteHolding(symbol) {
  if (!confirm(`确认删除持仓 ${symbol}？`)) return;
  try {
    await api.del(`/api/holdings/${symbol}`);
    toast('已删除', { type: 'success' });
    loadHoldings();
  } catch (e) {
    toast(e.message, { type: 'error' });
  }
}

// ── Render ──

function render(state) {
  const icons = { '主升浪':'🚀','趋势行情':'📈','修复行情':'🔄','高位震荡':'📊','板块轮动':'🔀','机构行情':'🏦','游资行情':'⚡','情绪退潮':'⚠️','冰点行情':'❄️','一致高潮':'🔥' };
  const riskCol = {1:'#3fb950',2:'#58a6ff',3:'#d2991d',4:'#f0883e',5:'#f85149'};
  const regimeCard = state.regime
    ? h('div', { class: 'card', style: 'margin-bottom:12px;border-left:3px solid ' + (riskCol[state.regime.risk_level]||'#8b949e') + ';' }, [
        h('div', { style: 'display:flex;align-items:center;gap:12px;flex-wrap:wrap;' }, [
          h('span', { style: 'font-size:22px;' }, icons[state.regime.label] || '📊'),
          h('div', {}, [
            h('div', { style: 'display:flex;align-items:center;gap:8px;' }, [
              h('b', { style: 'font-size:16px;color:var(--text-primary);' }, state.regime.label || '--'),
              h('span', { style: `font-size:12px;padding:2px 8px;border-radius:10px;color:${riskCol[state.regime.risk_level]||'#8b949e'};background:${riskCol[state.regime.risk_level]}15;border:1px solid ${riskCol[state.regime.risk_level]}30;` }, `风险 ${state.regime.risk_level}/5`),
              h('span', { style: 'font-size:12px;color:var(--text-secondary);' }, `置信 ${(state.regime.confidence*100).toFixed(0)}%`),
            ]),
          ]),
          h('div', { style: 'margin-left:auto;display:flex;gap:16px;align-items:center;' }, [
            h('div', { style: 'text-align:center;' }, [h('div', { style: 'font-size:18px;font-weight:800;' }, `${(state.regime.suggested_position_pct*100).toFixed(0)}%`), h('div', { style: 'font-size:11px;color:var(--text-secondary);' }, '建议仓位')]),
            state.emotion ? h('div', { style: 'text-align:center;' }, [h('div', { style: `font-size:18px;font-weight:800;color:${state.emotion.value>70?'var(--accent-down)':state.emotion.value>50?'var(--accent-warn)':'var(--accent-up)'};` }, state.emotion.value.toFixed(0)), h('div', { style: 'font-size:11px;color:var(--text-secondary);' }, state.emotion.label)]) : null,
            h('span', { style: 'font-size:12px;color:var(--text-secondary);' }, (state.regime.recommended_strategies||[]).slice(0,2).join(' · ') || '--'),
            h('span', { class: 'card-action', onClick: loadRegime }, '刷新'),
          ]),
        ]),
      ])
    : null;

  const s = state.summary;
  const summaryCards = s ? [
    renderSentimentCard(s.sentiment),
    renderDecisionCard(s.today_decision),
    renderRankingCard(s.strategy_ranking),
    renderEvolutionCard(s.latest_evolution),
    renderAuctionCard(s.today_auction),
    renderLiveAccountsCard(state),
  ] : [
    renderLiveAccountsCard(state),
  ];
  root.replaceChildren(
    regimeCard,
    h('div', { class: 'dash-hero' }, [
      h('div', {}, [
        h('h1', {}, '仪表盘'),
        h('div', { class: 'dash-sub' }, '多维度策略扫描总览 · 实时持仓 · 消息追踪'),
      ]),
      h('div', { class: 'dash-hero-actions' }, [
        renderHealthDot(state.dataHealth),
        h('button', { class: 'btn btn-outline', onClick: () => navigate('didao') }, '前往策略选股 →'),
        h('button', { class: 'btn btn-primary', onClick: () => navigate('backtest') }, '运行回测'),
      ]),
    ]),
    renderQuoteStrip(state.quotes),
    h('div', { class: 'dash-grid' }, [
      h('div', { style: 'display:flex;flex-direction:column;gap:16px;' }, [
        renderFocusCard(state.focus),
        renderNewsCard(state.news),
      ]),
      renderHoldingsCard(state.holdings, state),
    ]),
    ...summaryCards,
  );
}

function renderFocusCard(focus) {
  const body = focus.loading
    ? SkeletonRows(5)
    : focus.items.length === 0
      ? EmptyState({ title: '近7天暂无高频出现个股', hint: '策略扫描累计后将自动显示' })
      : h('div', {}, focus.items.slice(0, 10).map((item, i) => h('div', { class: `focus-row ${i < 3 ? 'top' : ''}` }, [
          h('div', { class: 'focus-rank' }, String(i + 1)),
          h('div', { class: 'focus-name' }, [
            item.name || item.symbol,
            h('span', { class: 'focus-sym' }, `  ${item.symbol}`),
          ]),
          h('div', { class: 'focus-count' }, `${item.count ?? item.appear_count ?? '-'}次`),
        ])));

  return h('div', { class: 'card' }, [
    h('div', { class: 'card-head' }, [
      h('div', { class: 'card-title' }, [h('span', { class: 'dot' }), '重点关注 · 近7天≥3次']),
      h('span', { class: 'card-action', onClick: () => loadFocus() }, '刷新'),
    ]),
    body,
  ]);
}

function renderNewsCard(news) {
  const body = news.loading
    ? SkeletonRows(3)
    : news.items.length === 0
      ? EmptyState({ title: '暂无消息案件' })
      : h('div', {}, news.items.map((c) => {
          const tierClass = c.source_tier === 1 ? 't1' : c.source_tier === 2 ? 't2' : 't3';
          return h('div', { class: 'news-row', onClick: () => navigate('news') }, [
            h('div', { class: `news-tier ${tierClass}` }),
            h('div', { class: 'news-body' }, [
              h('div', { class: 'news-title' }, c.title || c.headline || '(无标题)'),
              h('div', { class: 'news-meta' }, [
                c.status === 'watching' ? h('span', { class: 'badge badge-warn', style: 'margin-right:6px;' }, '观察中') : h('span', { class: 'badge badge-neutral', style: 'margin-right:6px;' }, '已结案'),
                c.created_at || '',
              ]),
            ]),
          ]);
        }));

  return h('div', { class: 'card' }, [
    h('div', { class: 'card-head' }, [
      h('div', { class: 'card-title' }, [h('span', { class: 'dot' }), '消息追踪 · 信源分级']),
      h('span', { class: 'card-action', onClick: () => navigate('news') }, '查看全部 →'),
    ]),
    body,
  ]);
}

function renderHoldingsCard(holdings, state) {
  const cols = [
    { key: 'symbol', label: '代码' },
    { key: 'name', label: '名称' },
    { key: 'cost_price', label: '成本', align: 'right', fmt: (v) => fmtNum(v) },
    { key: 'price', label: '现价', align: 'right', fmt: (v) => fmtNum(v) },
    { key: 'quantity', label: '数量', align: 'right', fmt: (v) => fmtNum(v, 0) },
    { key: 'pnl_pct', label: '盈亏', align: 'right', fmt: (v) => h('span', { class: pctClass(v) }, fmtPct(v)) },
    { key: 'strategy', label: '策略', fmt: (v) => STRATEGY_LABELS[v] || v },
  ];

  const table = holdings.loading
    ? SkeletonRows(4)
    : holdings.items.length === 0
      ? EmptyState({ title: '暂无模拟持仓', hint: '点击右上角"添加"记录第一笔持仓' })
      : renderHoldingsTable(holdings.items, cols);

  return h('div', { class: 'card' }, [
    h('div', { class: 'card-head' }, [
      h('div', { class: 'card-title' }, [h('span', { class: 'dot' }), '模拟持仓']),
      h('button', { class: 'btn btn-sm btn-primary', onClick: () => toggleForm(true, null) }, '+ 添加'),
    ]),
    state.formOpen ? renderHoldingForm(state, holdings.items) : null,
    h('div', { class: 'scroll-y', style: 'max-height:340px;' }, table),
    !holdings.loading && holdings.items.length > 0
      ? h('div', { class: 'holdings-summary' }, [
          h('span', {}, ['总市值 ', h('b', {}, fmtMoney(holdings.totalValue))]),
          h('span', {}, ['总盈亏 ', h('b', { class: pctClass(holdings.totalPnl) }, fmtMoney(holdings.totalPnl))]),
        ])
      : null,
  ]);
}

function renderHoldingsTable(items, cols) {
  const table = h('table', { class: 'tbl tbl-compact' });
  table.append(h('thead', {}, h('tr', {}, [...cols.map((c) => h('th', { style: c.align === 'right' ? 'text-align:right' : '' }, c.label)), h('th')])));
  const tbody = h('tbody');
  items.forEach((row) => {
    const tr = h('tr', {}, cols.map((c) => {
      const raw = row[c.key];
      const val = c.fmt ? c.fmt(raw, row) : raw ?? '--';
      return h('td', { class: c.align === 'right' ? 'num' : '' }, val instanceof Node ? val : String(val));
    }));
    tr.append(h('td', {}, [
      h('button', { class: 'btn btn-xs btn-ghost', onClick: () => toggleForm(true, row.symbol) }, '编辑'),
      h('button', { class: 'btn btn-xs btn-ghost', style: 'color:var(--accent-danger)', onClick: () => deleteHolding(row.symbol) }, '删除'),
    ]));
    tbody.append(tr);
  });
  table.append(tbody);
  return h('div', { class: 'tbl-wrap' }, table);
}

function renderHoldingForm(state, items) {
  const editing = state.editingSymbol ? items.find((i) => i.symbol === state.editingSymbol) : null;
  const form = h('form', { class: 'holding-form-grid', onSubmit: (e) => { e.preventDefault(); saveHolding(e.target); } }, [
    field('代码', h('input', { name: 'symbol', maxlength: 6, placeholder: '000001', value: editing?.symbol || '', disabled: !!editing, style: 'width:90px;' })),
    field('名称', h('input', { name: 'name', placeholder: '平安银行', value: editing?.name || '', style: 'width:100px;' })),
    field('数量', h('input', { name: 'quantity', type: 'number', min: 100, step: 100, placeholder: '100', value: editing?.quantity || '', style: 'width:80px;' })),
    field('成本', h('input', { name: 'cost_price', type: 'number', step: 0.01, placeholder: '12.50', value: editing?.cost_price || '', style: 'width:85px;' })),
    field('策略', selectStrategy(editing?.strategy)),
    h('div', { style: 'display:flex;gap:6px;' }, [
      h('button', { class: 'btn btn-sm btn-primary', type: 'submit' }, '保存'),
      h('button', { class: 'btn btn-sm btn-ghost', type: 'button', onClick: () => toggleForm(false) }, '取消'),
    ]),
  ]);
  return form;
}

// ── Quote strip (live prices for holdings) ──

function renderQuoteStrip(quotes) {
  const entries = Object.entries(quotes.data);
  if (!entries.length) return null;

  const chips = entries.map(([symbol, q]) => {
    const dir = q.change_pct > 0 ? 'up' : q.change_pct < 0 ? 'down' : 'flat';
    const sign = q.change_pct > 0 ? '+' : '';
    return h('div', { class: `quote-chip ${dir}` }, [
      h('div', { class: 'quote-chip-sym' }, symbol),
      h('div', { class: 'quote-chip-name' }, q.name || symbol),
      h('div', { class: 'quote-chip-price' }, fmtNum(q.price)),
      h('div', { class: `quote-chip-change ${dir}` }, `${sign}${(q.change_pct ?? 0).toFixed(2)}%`),
    ]);
  });

  return h('div', { class: 'quote-strip' }, [
    h('div', { class: 'quote-strip-label' }, '实时行情'),
    h('span', { class: 'quote-strip-action', onClick: () => loadQuotes() }, '刷新'),
    h('div', { class: 'quote-strip-row' }, chips),
  ]);
}

// ── Data health indicator ──

function renderHealthDot(health) {
  let cls, label, title;
  if (health.loading) {
    cls = 'health-dot--loading';
    label = '...';
    title = '数据健康检查中...';
  } else if (health.status === 'ok') {
    cls = 'health-dot--ok';
    label = '数据正常';
    const fresh = health.freshness?.fresh_symbols ?? '--';
    const total = health.freshness?.total_symbols ?? '--';
    const latest = health.freshness?.latest_date || '--';
    title = `新鲜度: ${fresh}/${total} · 最新: ${latest}`;
  } else if (health.status === 'warning') {
    cls = 'health-dot--warn';
    label = '数据延迟';
    const stale = health.freshness?.stale_symbols ?? '?';
    title = `${stale} 只股票数据过期`;
  } else {
    cls = 'health-dot--err';
    label = '数据异常';
    title = '数据健康检查失败';
  }

  return h('span', { class: `health-dot ${cls}`, title, 'aria-label': title }, [
    h('span', { class: 'health-dot-dot' }),
    h('span', { class: 'health-dot-label' }, label),
  ]);
}

function field(label, input) {
  return h('div', { class: 'field' }, [h('label', {}, label), input]);
}

function selectStrategy(selected) {
  const sel = h('select', { name: 'strategy', style: 'width:110px;' },
    Object.entries(STRATEGY_LABELS).map(([v, label]) => h('option', { value: v, selected: v === selected ? 'selected' : undefined }, label)));
  return sel;
}

// ── 仪表盘: 决策管线+进化+竞价摘要 (30s轮询) ──

async function loadSummary() {
  try {
    const res = await api.get('/api/dashboard/summary', { timeoutMs: 8000 });
    store.set({ summary: res?.data || res });
  } catch (e) { /* silent */ }
  if (store.get()._summaryTimer) clearInterval(store.get()._summaryTimer);
  const timer = setInterval(loadSummary, 30000);
  store.set({ _summaryTimer: timer });
}

async function loadLiveAccounts() {
  try {
    const res = await api.get('/api/dashboard/live-positions', { timeoutMs: 8000 });
    store.set({ liveAccounts: res?.data || res });
  } catch (e) { /* silent */ }
}

export function onHide() {
  const timer = store.get()._summaryTimer;
  if (timer) { clearInterval(timer); store.set({ _summaryTimer: 0 }); }
}

function renderDecisionCard(d) {
  if (!d || !d.has_plan) {
    return h('div', { class: 'card', style: 'padding:14px;' }, [h('div', { class: 'card-title' }, '📊 今日决策'), h('span', { style: 'color:var(--text-tertiary);font-size:12px;' }, '等待 14:30 决策管线触发')]);
  }
  const rows = (d.orders || []).map((o) =>
    h('div', { style: 'font-size:11px;padding:3px 0;' }, `${o.symbol} ${o.name||''} ${o.shares}股 ¥${(o.cost||0).toFixed(0)}`));
  return h('div', { class: 'card', style: 'padding:14px;' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:6px;' }, `📊 今日决策 ${d.date||''}`),
    h('div', { style: 'font-size:11px;color:var(--text-secondary);margin-bottom:6px;' }, `候选${d.candidates}→新闻${d.after_news}→概念${d.after_concept}→下单${d.order_count}票 ¥${(d.total_amount||0).toFixed(0)} ${d.elapsed_s?'耗时'+d.elapsed_s+'s':''}`),
    ...rows.slice(0, 5),
    d.order_count > 5 ? h('div', { style: 'font-size:10px;color:var(--text-tertiary);' }, `...还有 ${d.order_count-5} 票`) : null,
  ]);
}

function renderRankingCard(ranking) {
  if (!ranking || !ranking.length) {
    return h('div', { class: 'card', style: 'padding:14px;' }, [h('div', { class: 'card-title' }, '🏆 策略实盘业绩'), h('span', { style: 'color:var(--text-tertiary);font-size:12px;' }, '尚无实盘数据，积累交易后自动显示')]);
  }
  const rows = ranking.slice(0, 8).map((r, i) =>
    h('div', { style: `display:flex;justify-content:space-between;font-size:11px;padding:3px 0;border-bottom:1px solid var(--border-hairline);${r.return_pct>0?'color:var(--brand-teal)':'color:var(--text-danger)'}` }, [
      h('span', {}, `${i+1}. ${r.name}`),
      h('span', {}, `${r.return_pct>0?'+':''}${(r.return_pct||0).toFixed(1)}% | 胜率${(r.win_rate||0).toFixed(0)}% | ${r.total_trades}笔`),
    ]));
  return h('div', { class: 'card', style: 'padding:14px;' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:6px;' }, '🏆 策略实盘业绩(30日)'),
    ...rows,
  ]);
}

function renderEvolutionCard(evo) {
  if (!evo || !evo.has_evolution) {
    return h('div', { class: 'card', style: 'padding:14px;' }, [h('div', { class: 'card-title' }, '🧬 最新进化'), h('span', { style: 'color:var(--text-tertiary);font-size:12px;' }, '收盘后自动蒸馏进化')]);
  }
  const strats = (evo.new_strategies || []).slice(0, 3);
  return h('div', { class: 'card', style: 'padding:14px;' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:6px;' }, '🧬 最新进化'),
    strats.length ? h('div', { style: 'font-size:11px;' }, strats.map((s) => h('span', { class: 'badge badge-ai', style: 'margin:2px;' }, s))) :
      h('span', { style: 'color:var(--text-tertiary);font-size:11px;' }, '等待首次蒸馏'),
  ]);
}

function renderAuctionCard(auc) {
  if (!auc || auc.total === 0) {
    return h('div', { class: 'card', style: 'padding:14px;' }, [h('div', { class: 'card-title' }, '🔔 竞价验证'), h('span', { style: 'color:var(--text-tertiary);font-size:12px;' }, '次日竞价后自动显示')]);
  }
  return h('div', { class: 'card', style: 'padding:14px;' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:6px;' }, `🔔 竞价验证(${auc.date||''})`),
    h('div', { style: 'font-size:11px;' }, `✅ 确认 ${auc.confirmed}  ❌ 否认 ${auc.denied}  → 中性 ${auc.neutral} | 确认率 ${auc.confirm_rate}%`),
  ]);
}

function renderSentimentCard(sent) {
  if (!sent || !sent.sentiment) {
    return h('div', { class: 'card', style: 'padding:14px;' }, [
      h('div', { class: 'card-title' }, '🌡 市场温度计'),
      h('span', { style: 'color:var(--text-tertiary);font-size:12px;' }, '等待 14:30 管线产出日报'),
    ]);
  }
  var s = sent.sentiment;
  var adv = sent.advice || {};
  var cross = sent.cross_signals || [];
  var color = s.label === '偏多' ? 'color:var(--brand-teal)' : (s.label === '偏空' ? 'color:var(--text-danger)' : 'color:var(--text-warning)');
  var strategyStats = sent.strategy_stats || {};
  var bullEntries = Object.entries(strategyStats).filter(function(e){ return e[1].type === 'bull'; });
  var bearEntries = Object.entries(strategyStats).filter(function(e){ return e[1].type === 'bear'; });

  return h('div', { class: 'card', style: 'padding:14px;' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:4px;' }, `🌡 市场温度计 ${sent.date||''}`),
    h('div', { style: `font-size:18px;font-weight:700;${color};margin:4px 0;` }, `${s.label} (${s.score||'?'}/100)`),
    h('div', { style: 'font-size:11px;color:var(--text-secondary);margin-bottom:6px;' }, s.summary || ''),
    h('div', { style: 'font-size:10px;color:var(--text-tertiary);margin-bottom:4px;' },
      `多头: ${s.bull_count||0}票 ${s.bull_trend==='up'?'↑':s.bull_trend==='down'?'↓':'→'} | 空头: ${s.bear_count||0}票 ${s.bear_trend==='up'?'↑':s.bear_trend==='down'?'↓':'→'}`),
    cross.length ? h('div', { style: 'font-size:11px;margin-bottom:4px;' }, [
      h('b', {}, `🎯 高置信(${cross.length}只): `),
      cross.slice(0, 4).map(function(c){ return h('span', {style:'margin-right:6px;'}, c.symbol + (c.name?' ':'') + (c.name||'')); }),
    ]) : null,
    h('div', { style: 'font-size:10px;color:var(--text-secondary);margin-top:4px;' },
      `建议: ${adv.position||'观望'} | 单票≤${adv.max_single_pct||15}% | ` +
      '优先: ' + ((adv.priority_strategies||[]).slice(0,2).join(', ') || '—')),
  ]);
}

function renderLiveAccountsCard(state) {
  const la = state.liveAccounts;
  const accounts = la?.accounts || [];
  if (!la || !accounts.length) {
    return h('div', { class: 'card', style: 'padding:14px;' }, [
      h('div', { class: 'card-title' }, '💼 策略实盘账户'),
      h('span', { style: 'color:var(--text-tertiary);font-size:12px;' }, '14:30管线启动后自动创建账户, 积累数据后显示持仓和交易记录'),
    ]);
  }
  const rows = accounts.slice(0, 10).map((a) => {
    const posList = (a.positions || []).map((p) => `${p.symbol} ${p.shares}股 @${(p.avg_cost||0).toFixed(2)}`);
    const tradeList = (a.trades || []).slice(0, 5).map((t) => `${t.date} ${t.side} ${t.symbol} ${t.shares}股 @${(t.price||0).toFixed(2)}`);
    return h('details', { style: 'margin-bottom:6px;font-size:11px;' }, [
      h('summary', {}, `${a.label || a.account?.id || '?'}  权益¥${(a.account?.total_equity||0).toFixed(0)}  持仓${posList.length}笔  交易${(a.trades||[]).length}笔`),
      posList.length ? h('div', { style: 'margin:4px 0;color:var(--brand-teal);' }, '持仓: ' + posList.join(' | ')) : null,
      tradeList.length ? h('div', { style: 'color:var(--text-secondary);' }, tradeList.join('<br>')) : null,
    ]);
  });
  return h('div', { class: 'card', style: 'padding:14px;' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:8px;' }, `💼 策略实盘账户(${accounts.length}个)`),
    ...rows,
  ]);
}
