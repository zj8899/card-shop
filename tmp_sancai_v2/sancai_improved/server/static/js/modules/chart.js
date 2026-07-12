// ══════════════════════════════════════════════════════════════
// Chart module — K-line, tick chart, order book depth, fund flow
// ══════════════════════════════════════════════════════════════
import { api } from '../core/api.js';
import { createStore, bindRender } from '../core/store.js';
import { h, fmtNum, fmtPct, fmtCompact, pctClass, debounce, toast } from '../core/dom.js';
import { EmptyState } from '../components/ui.js';
import { renderKlineChart, renderKdjChart, renderTickChart, resizeChart } from '../components/kline-chart.js';

const PERIODS = [
  { v: 'daily', label: '日线' }, { v: '120min', label: '120分' }, { v: '60min', label: '60分' },
  { v: '30min', label: '30分' }, { v: '15min', label: '15分' }, { v: '5min', label: '5分' }, { v: '1min', label: '1分' },
];

// ── Live quote helpers ──
function isTradingHours() {
  const now = new Date();
  const day = now.getDay();
  if (day === 0 || day === 6) return false;
  const t = now.getHours() * 100 + now.getMinutes();
  return t >= 930 && t <= 1500;
}
function getCode(symbol) {
  return String(symbol).replace(/^(sh|sz|bj)/, '');
}

let liveTimer = null;
// Held at module scope so onHide() can remove the same listener that mount() added
// (an anonymous debounce() result cannot otherwise be un-registered → leak, Bug 30).
let _resizeHandler = null;
const MA_OPTS = [
  { key: 'ma_5', label: 'MA5', color: '#ffb020' },
  { key: 'ma_10', label: 'MA10', color: '#16b4ff' },
  { key: 'ma_21', label: 'MA21', color: '#7c6bff' },
  { key: 'ma_55', label: 'MA55', color: '#00e2a8' },
  { key: 'ma_120', label: 'MA120', color: '#ff4d6a' },
];

const store = createStore({
  symbol: 'sh000001',
  period: 'daily',
  mode: 'kline', // kline | tick
  records: [],
  quote: null,
  loading: true,
  visibleMas: ['ma_5', 'ma_10', 'ma_21'],
  depth: null,
  ticks: [],
  liveMode: false,
  liveQuote: null,
  liveSource: null,
  multi: [
    { symbol: 'sh000016', name: '' }, { symbol: 'sz399006', name: '' },
    { symbol: 'sh000300', name: '' }, { symbol: 'sh000852', name: '' },
  ],
});

let root;
let klineChart, kdjChart, tickChart;
const multiCharts = [null, null, null, null];

export async function mount(container) {
  root = container;
  render(store.get());
  bindRender(store, (state) => { render(state); paintCharts(state); });
  _resizeHandler = debounce(() => {
    resizeChart(klineChart); resizeChart(kdjChart); resizeChart(tickChart);
    multiCharts.forEach(resizeChart);
  }, 200);
  window.addEventListener('resize', _resizeHandler);
  await loadSymbol(store.get().symbol);
  loadMultiCharts();
}

export function onShow() {
  resizeChart(klineChart); resizeChart(kdjChart);
}

// Cleanup when navigating away — stop the live-quote interval and drop the resize
// listener so they don't accumulate across mounts (Bug 30). Called by the router.
export function onHide() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  if (_resizeHandler) { window.removeEventListener('resize', _resizeHandler); _resizeHandler = null; }
}

// ── Data ──

async function loadSymbol(symbol) {
  store.set({ loading: true, symbol });
  // Shared symbol coupling (Bug 31/23): lab.js and research.js need the currently
  // charted symbol but live in other modules. No shared store exists in core/, so we
  // publish the bare 6-digit code on a simple global that those modules read.
  window._currentSymbol = getCode(symbol);
  try {
    const period = store.get().period;
    const res = await api.get(`/api/data/stocks/${symbol}/kline?period=${period}&limit=500`);
    const records = res?.data || [];
    const last = records[records.length - 1];
    const prev = records[records.length - 2];
    const quote = last ? {
      price: last.close, prevClose: prev?.close ?? last.open,
      change: prev ? last.close - prev.close : 0,
      changePct: prev ? ((last.close - prev.close) / prev.close) * 100 : 0,
      high: last.high, low: last.low, volume: last.volume, date: last.date,
    } : null;
    store.set({ loading: false, records, quote });
    loadDepthAndFlow(symbol);
    if (store.get().liveMode) fetchLiveQuote();
  } catch (e) {
    store.set({ loading: false, records: [], quote: null });
    toast(`加载失败: ${e.message}`, { type: 'error' });
  }
}

async function loadDepthAndFlow(symbol) {
  try {
    const depth = await api.get(`/api/data/stocks/${symbol}/depth`, { timeoutMs: 12000 });
    store.set({ depth });
  } catch (e) {
    store.set({ depth: null });
  }
}

// ── Live quote ──

async function fetchLiveQuote() {
  const state = store.get();
  try {
    // 保留 sh/sz 前缀，后端 _lookup_tencent_result 会正确处理
    const sym = state.symbol;
    const res = await api.get(`/api/data/quote/${sym}`, { timeoutMs: 8000 });
    if (res && res.status === 'ok' && res.data) {
      const q = res.data;
      store.set({
        liveQuote: {
          price: q.price,
          prevClose: q.last_close,
          change: q.change_amt,
          changePct: q.change_pct,
          high: q.high,
          low: q.low,
          volume: q.amount_wan || 0,
          turnoverPct: q.turnover_pct,
          name: q.name,
          date: new Date().toISOString().slice(0, 10),
        },
        liveSource: res.source || 'tencent',
      });
    }
  } catch (e) {
    // silent fail — keep showing current data
  }
}

function startLiveRefresh() {
  stopLiveRefresh();
  fetchLiveQuote();
  liveTimer = setInterval(() => {
    if (isTradingHours()) fetchLiveQuote();
  }, 30000);
}

function stopLiveRefresh() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
}

function toggleLive() {
  const cur = store.get();
  const next = !cur.liveMode;
  store.set({ liveMode: next });
  if (next) {
    startLiveRefresh();
  } else {
    stopLiveRefresh();
    store.set({ liveQuote: null, liveSource: null });
  }
}

async function loadTicks(symbol) {
  try {
    const res = await api.get(`/api/data/stocks/${symbol}/ticks`, { timeoutMs: 12000 });
    store.set({ ticks: res?.data || res?.ticks || [] });
  } catch (e) {
    store.set({ ticks: [] });
  }
}

async function loadMultiCharts() {
  const multi = store.get().multi;
  for (let i = 0; i < multi.length; i++) {
    try {
      const res = await api.get(`/api/data/stocks/${multi[i].symbol}/kline?period=daily&limit=180`);
      const records = res?.data || [];
      requestAnimationFrame(() => {
        const el = document.getElementById(`mc-chart-${i}`);
        if (el) multiCharts[i] = renderKlineChart(el, records, { visibleMas: ['ma_21'] });
      });
    } catch (e) { /* silent per-cell failure */ }
  }
}

// ── Actions ──

function setPeriod(period) { store.set({ period }); loadSymbol(store.get().symbol); }
function setMode(mode) {
  store.set({ mode });
  if (mode === 'tick') loadTicks(store.get().symbol);
}
function toggleMa(key) {
  const cur = store.get().visibleMas;
  const next = cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key];
  store.set({ visibleMas: next });
}

function paintCharts(state) {
  requestAnimationFrame(() => {
    const klineEl = document.getElementById('kline-canvas');
    const kdjEl = document.getElementById('kdj-canvas');
    const tickEl = document.getElementById('tick-canvas');
    if (state.mode === 'kline' && klineEl && state.records.length) {
      klineChart = renderKlineChart(klineEl, state.records, { visibleMas: state.visibleMas });
      if (kdjEl) kdjChart = renderKdjChart(kdjEl, state.records);
    }
    if (state.mode === 'tick' && tickEl && state.ticks.length) {
      tickChart = renderTickChart(tickEl, state.ticks);
    }
  });
}

// ── Render ──

function render(state) {
  root.replaceChildren(
    renderToolbar(state),
    (state.quote || state.liveQuote) ? renderQuoteStrip(state) : null,
    renderOverlayPanel(state),
    renderChartCard(state),
    state.mode === 'tick' ? renderTickPanels(state) : null,
    renderMultiChartCard(state),
  );
}

function renderToolbar(state) {
  return h('div', { class: 'chart-toolbar' }, [
    h('label', { style: 'font-size:12px;color:var(--text-secondary);' }, '股票代码'),
    h('input', {
      class: 'chart-symbol-input', value: state.symbol, placeholder: '输入代码，如 sh600519',
      onKeydown: (e) => { if (e.key === 'Enter') loadSymbol(e.target.value.trim()); },
    }),
    h('button', { class: 'btn btn-sm', onClick: () => loadSymbol(document.querySelector('.chart-symbol-input').value.trim()) }, '刷新'),
    h('div', { class: 'seg' }, PERIODS.map((p) => h('button', {
      class: `seg-btn ${state.period === p.v ? 'active' : ''}`, onClick: () => setPeriod(p.v),
    }, p.label))),
    h('button', {
      class: `btn btn-sm live-toggle ${state.liveMode ? 'live-on' : ''}`,
      onClick: () => toggleLive(),
      title: state.liveMode ? '关闭实时行情' : '开启实时行情',
    }, [
      state.liveMode ? h('span', { class: 'live-dot' }) : null,
      '实时',
    ]),
    h('div', { class: 'grow' }),
    h('div', { class: 'seg' }, [
      h('button', { class: `seg-btn ${state.mode === 'tick' ? 'active' : ''}`, onClick: () => setMode('tick') }, '分时图'),
      h('button', { class: `seg-btn ${state.mode === 'kline' ? 'active' : ''}`, onClick: () => setMode('kline') }, 'K线图'),
    ]),
  ]);
}

function renderQuoteStrip(state) {
  const useLive = state.liveMode && state.liveQuote;
  const q = useLive ? state.liveQuote : state.quote;
  if (!q) return null;
  const sourceLabel = useLive ? '腾讯实时' : '日线收盘';
  const sourceStyle = useLive
    ? 'color:var(--market-up);font-weight:600;'
    : 'color:var(--text-tertiary);';
  const cls = pctClass(q.changePct);
  return h('div', { class: 'chart-quote-strip' }, [
    h('div', { class: `q-price ${cls}` }, fmtNum(q.price)),
    h('div', { class: `q-change ${cls}` }, `${q.change >= 0 ? '+' : ''}${fmtNum(q.change)} (${fmtPct(q.changePct)})`),
    h('div', { class: 'q-meta' }, [
      h('span', {}, ['最高 ', h('b', {}, fmtNum(q.high))]),
      h('span', {}, ['最低 ', h('b', {}, fmtNum(q.low))]),
      h('span', {}, ['成交量 ', h('b', {}, fmtCompact(q.volume))]),
      h('span', {}, ['代码 ', h('b', {}, state.symbol)]),
      h('span', { class: 'q-source', style: sourceStyle }, sourceLabel),
    ]),
  ]);
}

function renderOverlayPanel(state) {
  return h('details', { class: 'overlay-panel' }, [
    h('summary', {}, '📐 均线叠加'),
    h('div', { class: 'overlay-grid' }, MA_OPTS.map((m) => h('label', {}, [
      h('input', {
        type: 'checkbox', checked: state.visibleMas.includes(m.key) ? 'checked' : undefined,
        onChange: () => toggleMa(m.key),
      }),
      h('span', { style: `color:${m.color}` }, m.label),
    ]))),
  ]);
}

function renderChartCard(state) {
  if (state.loading) {
    return h('div', { class: 'card' }, h('div', { class: 'chart-canvas-lg', style: 'display:flex;align-items:center;justify-content:center;color:var(--text-tertiary);' }, '加载K线数据…'));
  }
  if (!state.records.length) {
    return h('div', { class: 'card' }, EmptyState({ title: '暂无K线数据', hint: '请检查代码是否正确，或前往数据管理下载' }));
  }
  return h('div', { class: 'card' }, [
    h('div', { id: 'kline-canvas', class: 'chart-canvas-lg', style: state.mode === 'kline' ? '' : 'display:none;', role: 'img', 'aria-label': 'K线图' }),
    h('div', { id: 'tick-canvas', class: 'chart-canvas-lg', style: state.mode === 'tick' ? '' : 'display:none;', role: 'img', 'aria-label': '分时图' }),
    state.mode === 'kline' ? h('div', { id: 'kdj-canvas', class: 'chart-canvas-md', role: 'img', 'aria-label': 'KDJ指标' }) : null,
  ]);
}

function renderTickPanels(state) {
  return h('div', { class: 'grid grid-2', style: 'margin-top:12px;' }, [
    renderDepthCard(state.depth),
    renderFlowCard(state.depth),
  ]);
}

function renderDepthCard(depth) {
  if (!depth) return h('div', { class: 'card' }, [h('div', { class: 'card-title' }, '5档盘口'), EmptyState({ title: '暂无盘口数据' })]);
  const maxVol = Math.max(...[...(depth.sells || []), ...(depth.buys || [])].map((x) => x.volume), 1);
  const rows = [
    ...(depth.sells || []).map((s) => depthRow(s, 'sell', maxVol)),
    h('tr', {}, h('td', { colspan: 3, style: `text-align:center;padding:6px 0;font-weight:700;color:${pctClass(depth.spread_pct) === 'up' ? 'var(--market-up)' : 'var(--market-down)'};` }, fmtNum(depth.buys?.[0]?.price))),
    ...(depth.buys || []).map((b) => depthRow(b, 'buy', maxVol)),
  ];
  return h('div', { class: 'card' }, [
    h('div', { class: 'card-title' }, '5档盘口'),
    h('table', { class: 'depth-table' }, h('tbody', {}, rows)),
  ]);
}

function depthRow(level, side, maxVol) {
  const pct = (level.volume / maxVol) * 100;
  return h('tr', {}, [
    h('td', { class: side === 'sell' ? 'down' : 'up' }, fmtNum(level.price)),
    h('td', { class: 'depth-bar-cell' }, [h('div', { class: `depth-bar ${side}`, style: `width:${pct}%` }), h('span', { style: 'position:relative;' }, fmtCompact(level.volume))]),
  ]);
}

function renderFlowCard(depth) {
  return h('div', { class: 'card' }, [
    h('div', { class: 'card-title' }, '资金流向'),
    h('div', { class: 'flow-stats' }, [
      flowStat('主动买入(万)', depth?.total_amount ? fmtCompact(depth.total_amount) : '--', 'up'),
      flowStat('主动卖出(万)', '--', 'down'),
      flowStat('净流入(万)', '--', 'flat'),
    ]),
    EmptyState({ title: '大单成交', hint: '实时数据接入后展示 ≥100手 大单' }),
  ]);
}

function flowStat(label, value, cls) {
  return h('div', { class: 'flow-stat-item' }, [
    h('div', { class: `stat-value ${cls}` }, value),
    h('div', { class: 'stat-label' }, label),
  ]);
}

function renderMultiChartCard(state) {
  return h('details', { class: 'card', style: 'margin-top:16px;' }, [
    h('summary', { style: 'font-size:13px;font-weight:600;cursor:pointer;' }, '📊 多图表对比'),
    h('div', { class: 'multi-chart-grid', style: 'margin-top:12px;' }, state.multi.map((m, i) => h('div', { class: 'multi-chart-cell' }, [
      h('div', { class: 'mc-head' }, [
        h('input', { value: m.symbol, onChange: (e) => updateMultiSymbol(i, e.target.value) }),
      ]),
      h('div', { id: `mc-chart-${i}`, style: 'height:220px;' }),
    ]))),
  ]);
}

function updateMultiSymbol(i, symbol) {
  const multi = [...store.get().multi];
  multi[i] = { ...multi[i], symbol };
  store.set({ multi });
  loadMultiCharts();
}
