// ══════════════════════════════════════════════════════════════
// Backtest module
// ══════════════════════════════════════════════════════════════
import { api, poll } from '../core/api.js';
import { createStore, bindRender } from '../core/store.js';
import { h, fmtNum, fmtPct, pctClass, toast } from '../core/dom.js';
import { EmptyState, StatTile, Table } from '../components/ui.js';

const MODES = [
  { v: 'simple', label: 'KDJ超卖' }, { v: 'schools', label: '多学派共识' },
  { v: 'strict', label: '三才BP1' }, { v: 'strict_reverse', label: '追涨突破' },
  { v: 'chan_theory', label: '缠论' }, { v: 'ict', label: 'ICT' },
  { v: 'price_action', label: '价格行为' }, { v: 'wyckoff', label: '威科夫' },
  { v: 'morphology', label: '形态学' }, { v: 'gann', label: '江恩' },
  { v: 'wave_theory', label: '波浪' }, { v: 'dow_theory', label: '道氏' },
];
const SINGLE_SCHOOLS = new Set(['chan_theory', 'ict', 'price_action', 'wyckoff', 'morphology', 'gann', 'wave_theory', 'dow_theory']);
const PERIODS = [
  { v: 'daily', label: '日线' }, { v: '60min', label: '60分钟' }, { v: '30min', label: '30分钟' },
  { v: '15min', label: '15分钟' }, { v: '5min', label: '5分钟' }, { v: '1min', label: '1分钟' },
];

const store = createStore({
  symbols: ['000001', '600519'],
  period: 'daily',
  startDate: '2024-01-01',
  endDate: '',
  capital: 1000000,
  riskPct: 2,
  mode: 'simple',
  running: false,
  status: '',
  result: null,
  schoolParams: null,
  tuning: {},
  pollHandle: null,
});

let root;

export async function mount(container) {
  root = container;
  render(store.get());
  bindRender(store, render);
  loadSchoolParams();
}

async function loadSchoolParams() {
  try {
    const res = await api.get('/api/backtest/school-params');
    const params = res?.data || res;
    store.set({ schoolParams: params });
  } catch (e) { /* non-fatal */ }
}

// ── Actions ──

function addSymbol(raw) {
  const parts = raw.split(/[,\s]+/).map((s) => s.trim()).filter((s) => /^\d{6}$/.test(s));
  if (!parts.length) return;
  const symbols = [...new Set([...store.get().symbols, ...parts])];
  store.set({ symbols });
}
function removeSymbol(sym) {
  store.set({ symbols: store.get().symbols.filter((s) => s !== sym) });
}
function setMode(mode) { store.set({ mode, tuning: {} }); }

async function runBacktest() {
  const s = store.get();
  if (!s.symbols.length) { toast('请至少添加一只股票代码', { type: 'error' }); return; }
  store.set({ running: true, status: '提交回测任务…', result: null });
  try {
    const payload = {
      symbols: s.symbols,
      period: s.period,
      start_date: s.startDate || undefined,
      end_date: s.endDate || undefined,
      initial_capital: Number(s.capital),
      risk_per_trade: Number(s.riskPct) / 100,
      mode: s.mode,
      school_config: SINGLE_SCHOOLS.has(s.mode) && Object.keys(s.tuning).length ? s.tuning : undefined,
    };
    const res = await api.post('/api/backtest/run', payload);
    const taskId = res?.data?.task_id || res?.task_id;
    store.set({ status: `运行中… (任务 ${taskId})` });

    const { promise, cancel } = poll(
      () => api.get(`/api/backtest/${taskId}/status`),
      {
        intervalMs: 1800, maxMs: 240000,
        until: (r) => { const st = r?.data?.status || r?.status; return st === 'completed' || st === 'failed'; },
        onTick: (r) => { const st = r?.data?.status || r?.status; store.set({ status: st === 'running' ? '回测执行中…' : st }); },
      },
    );
    store.set({ pollHandle: cancel });
    const finalStatus = await promise;
    const finalSt = finalStatus?.data?.status || finalStatus?.status;
    if (finalSt === 'failed') {
      throw new Error(finalStatus?.data?.error || finalStatus?.error || '回测失败');
    }
    const rawResult = await api.get(`/api/backtest/${taskId}/result`);
    const result = rawResult?.data || rawResult;
    store.set({ running: false, result, status: '完成' });
    toast('回测完成', { type: 'success' });
  } catch (e) {
    store.set({ running: false, status: '' });
    toast(`回测失败: ${e.message}`, { type: 'error' });
  }
}

function cancelPoll() {
  store.get().pollHandle?.();
  store.set({ running: false, status: '已取消' });
}

function setTuningParam(school, key, value) {
  const tuning = { ...store.get().tuning };
  tuning[key] = value;
  store.set({ tuning });
}

// ── Render ──

function render(state) {
  root.replaceChildren(
    h('div', { class: 'dash-hero' }, [
      h('div', {}, [h('h1', {}, '回测系统'), h('div', { class: 'dash-sub' }, '多学派策略历史回测 · 批量验证 · 自主进化循环')]),
    ]),
    h('div', { class: 'bt-layout' }, [
      renderConfigCard(state),
      renderResultCard(state),
    ]),
  );
}

function renderConfigCard(state) {
  return h('div', { class: 'card' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:12px;' }, '回测配置'),
    h('div', { class: 'field', style: 'margin-bottom:12px;' }, [
      h('label', {}, '股票池'),
      h('div', { class: 'symbol-tag-input' }, state.symbols.map((sym) => h('span', { class: 'symbol-tag' }, [
        sym, h('button', { onClick: () => removeSymbol(sym) }, '✕'),
      ]))),
      h('input', {
        placeholder: '输入6位代码后回车添加，如 000001, 000002', style: 'width:100%;',
        onKeydown: (e) => { if (e.key === 'Enter') { addSymbol(e.target.value); e.target.value = ''; } },
      }),
    ]),
    h('div', { class: 'bt-field-row', style: 'margin-bottom:12px;' }, [
      selectField('周期', PERIODS.map((p) => [p.v, p.label]), state.period, (v) => store.set({ period: v })),
      inputField('起始', 'date', state.startDate, (v) => store.set({ startDate: v })),
      inputField('截止', 'date', state.endDate, (v) => store.set({ endDate: v }), '留空=至今'),
    ]),
    h('div', { class: 'bt-field-row', style: 'margin-bottom:12px;' }, [
      inputField('初始资金', 'number', state.capital, (v) => store.set({ capital: v })),
      inputField('单笔风险%', 'number', state.riskPct, (v) => store.set({ riskPct: v })),
    ]),
    h('div', { class: 'field', style: 'margin-bottom:8px;' }, [
      h('label', {}, '策略模式'),
      h('div', { class: 'chip-select', style: 'margin-top:6px;' }, MODES.map((m) => h('button', {
        class: `chip ${state.mode === m.v ? 'active' : ''}`, onClick: () => setMode(m.v),
      }, m.label))),
    ]),
    SINGLE_SCHOOLS.has(state.mode) && state.schoolParams?.[state.mode] ? renderTuningPanel(state) : null,
    h('div', { style: 'display:flex;gap:8px;align-items:center;margin-top:14px;flex-wrap:wrap;' }, [
      state.running
        ? h('button', { class: 'btn btn-danger', onClick: cancelPoll }, '■ 取消')
        : h('button', { class: 'btn btn-primary', onClick: runBacktest }, '▶ 运行回测'),
      h('span', { style: 'font-size:12px;color:var(--text-secondary);' }, state.status),
    ]),
  ]);
}

function renderTuningPanel(state) {
  const schoolCfg = state.schoolParams[state.mode];
  const params = schoolCfg?.params || {};
  return h('div', { class: 'bt-tuning' }, [
    h('div', { style: 'font-size:11px;color:var(--text-tertiary);margin-bottom:6px;font-weight:600;' }, `${schoolCfg.name || state.mode} · 参数微调`),
    ...Object.entries(params).map(([key, spec]) => renderSlider(state.mode, key, spec, state.tuning[key])),
  ]);
}

function renderSlider(school, key, spec, current) {
  const value = current ?? spec.default ?? spec.min;
  const valEl = h('span', { class: 'bt-slider-val' }, String(value));
  const input = h('input', {
    type: 'range', min: spec.min, max: spec.max, step: spec.step || 1, value,
    onInput: (e) => { valEl.textContent = e.target.value; setTuningParam(school, key, Number(e.target.value)); },
  });
  return h('div', { class: 'bt-slider-row' }, [h('label', {}, spec.label || key), input, valEl]);
}

function renderResultCard(state) {
  return h('div', { class: 'card' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:12px;' }, '回测结果'),
    state.result ? renderMetrics(state.result) : EmptyState({ title: '暂无结果', hint: '配置左侧参数后点击"运行回测"' }),
    state.result?.trades?.length ? renderTradesTable(state.result.trades) : null,
  ]);
}

function renderMetrics(result) {
  const m = result.metrics || {};
  return h('div', { class: 'bt-metrics-grid' }, [
    StatTile({ label: '总收益率', value: fmtPct(m.total_return_pct ?? m.total_return * 100) }),
    StatTile({ label: '胜率', value: fmtPct(m.win_rate_pct ?? m.win_rate * 100) }),
    StatTile({ label: '夏普比率', value: fmtNum(m.sharpe_ratio, 2) }),
    StatTile({ label: '最大回撤', value: fmtPct(m.max_drawdown_pct ?? m.max_drawdown * 100) }),
    StatTile({ label: '交易次数', value: fmtNum(result.trade_count ?? result.trades?.length ?? m.trade_count, 0) }),
    StatTile({ label: '盈亏比', value: fmtNum(m.profit_factor, 2) }),
  ]);
}

function renderTradesTable(trades) {
  return h('div', { style: 'margin-top:14px;' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:8px;' }, `交易明细 (${trades.length})`),
    h('div', { class: 'scroll-y', style: 'max-height:320px;' },
      Table({
        columns: [
          { key: 'symbol', label: '代码' },
          { key: 'entry_date', label: '入场日期' },
          { key: 'exit_date', label: '出场日期' },
          { key: 'entry_price', label: '入场价', align: 'right', fmt: (v) => fmtNum(v) },
          { key: 'exit_price', label: '出场价', align: 'right', fmt: (v) => fmtNum(v) },
          { key: 'pnl_pct', label: '收益率', align: 'right', fmt: (v) => h('span', { class: pctClass(v) }, fmtPct(v)) },
        ],
        rows: trades,
        compact: true,
      })),
  ]);
}

function selectField(label, options, value, onChange) {
  return h('div', { class: 'field' }, [
    h('label', {}, label),
    h('select', { onChange: (e) => onChange(e.target.value) }, options.map(([v, l]) => h('option', { value: v, selected: v === value ? 'selected' : undefined }, l))),
  ]);
}
function inputField(label, type, value, onChange, placeholder = '') {
  return h('div', { class: 'field' }, [
    h('label', {}, label),
    h('input', { type, value: value ?? '', placeholder, style: 'width:130px;', onChange: (e) => onChange(e.target.value) }),
  ]);
}
