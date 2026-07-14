// Evolution Lab — ML训练 · 策略蒸馏 · 自主循环 · 系统监控
import { api } from '../core/api.js';
import { createStore, bindRender } from '../core/store.js';
import { h, fmtNum, fmtPct, toast } from '../core/dom.js';

const MODES = [
  { v: 'simple', l: 'KDJ超卖' }, { v: 'strict', l: '三才BP1' },
  { v: 'strict_reverse', l: '追涨突破' }, { v: 'schools', l: '多学派共识' },
  { v: 'chan_theory', l: '缠论' }, { v: 'ict', l: 'ICT' },
  { v: 'price_action', l: '价格行为' }, { v: 'wyckoff', l: '威科夫' },
  { v: 'morphology', l: '形态学' }, { v: 'gann', l: '江恩' },
  { v: 'wave_theory', l: '波浪' }, { v: 'dow_theory', l: '道氏' },
];

const store = createStore({
  sysStats: null,
  _statsTimer: 0,
  // train
  trainSymbols: '000001,600519,000651,000858',
  trainDevice: 'gpu',
  trainHorizon: 5,
  trainBusy: false,
  trainResult: '',
  // evolve
  evolveMode: 'simple',
  evolveSymbols: '000001,600519',
  evolveGen: 2,
  evolveInjectML: true,
  evolveBusy: false,
  evolveResult: '',
  // loop
  loopSymbols: 10,
  loopGen: 2,
  loopMinutes: 30,
  loopBusy: false,
  loopStatus: null,
  // log
  log: [],
});

let root, _statsInt = 0;
const _B = (text, cls) => h('div', { class: 'ev-log-line ' + (cls || '') }, text);
function _log(text, cls) { store.set({ log: [...store.get().log, _B(text, cls)] }); }

function _sysBar(s) {
  if (!s) return h('div', { class: 'ev-sysbar' }, h('span', { style: 'color:var(--text-tertiary);font-size:12px;' }, '资源监控加载中…'));

  const cpuBar = h('div', { class: 'bar' }, h('div', { class: 'bar-fill', style: `width:${s.cpu_pct}%;background:${s.cpu_pct>80?'#ff6b81':s.cpu_pct>50?'#ffb347':'#2dd4bf'}` }));
  const memBar = h('div', { class: 'bar' }, h('div', { class: 'bar-fill', style: `width:${s.mem_pct}%;background:${s.mem_pct>90?'#ff6b81':s.mem_pct>70?'#ffb347':'#2dd4bf'}` }));
  const elms = [
    h('div', { class: 'ev-sys-chip' }, [h('span', {}, 'CPU'), h('span', { style: 'font-weight:700;' }, s.cpu_pct + '%'), cpuBar]),
    h('div', { class: 'ev-sys-chip' }, [h('span', {}, '内存'), h('span', { style: 'font-weight:700;' }, s.mem_used_gb + '/' + s.mem_total_gb + 'GB'), memBar]),
    h('div', { class: 'ev-sys-chip' }, [h('span', {}, '线程'), h('span', { style: 'font-weight:700;' }, s.process?.threads || '-')]),
  ];
  if (s.gpu && s.gpu.name) {
    const gBar = h('div', { class: 'bar' }, h('div', { class: 'bar-fill', style: `width:${s.gpu.gpu_util_pct}%;background:${s.gpu.gpu_util_pct>80?'#ff6b81':'#7ecfff'}` }));
    const mBar = h('div', { class: 'bar' }, h('div', { class: 'bar-fill', style: `width:${s.gpu.mem_pct}%;background:${s.gpu.mem_pct>90?'#ff6b81':'#7ecfff'}` }));
    elms.push(h('div', { class: 'ev-sys-chip' }, [h('span', {}, 'GPU'), h('span', { style: 'font-weight:700;' }, s.gpu.gpu_util_pct + '%'), gBar]));
    elms.push(h('div', { class: 'ev-sys-chip' }, [h('span', {}, '显存'), h('span', { style: 'font-weight:700;' }, s.gpu.mem_used_mb + '/' + s.gpu.mem_total_mb + 'MB'), mBar]));
    elms.push(h('div', { class: 'ev-sys-chip' }, [h('span', {}, s.gpu.temp_c + '°C')]));
  }
  elms.push(h('div', { class: 'ev-sys-chip', style: 'margin-left:auto;color:var(--text-tertiary);' }, s.ts?.slice(11, 19) || ''));
  return h('div', { class: 'ev-sysbar' }, elms);
}

async function loadSysStats() {
  try {
    const res = await api.get('/api/evolution/system-stats', { timeoutMs: 5000 });
    store.set({ sysStats: res?.data || res });
  } catch (e) { /* silent */ }
}

async function runTrain() {
  const s = store.get();
  if (s.trainBusy) return;
  const syms = s.trainSymbols.split(/[,;\s]+/).filter(Boolean);
  if (!syms.length) { toast('请输入股票代码', { type: 'error' }); return; }
  store.set({ trainBusy: true, trainResult: '' });
  _log('[训练] 启动…', 'info');
  try {
    const res = await api.post('/api/evolution/train', {
      symbols: syms, device: s.trainDevice, label_horizon: Number(s.trainHorizon),
      window_size: 252, save_model: true,
    }, { timeoutMs: 120000 });
    const d = res?.data || res;
    _log('[训练] 完成 IC=' + d.mean_ic + ' IR=' + d.ic_ir + ' 行数=' + d.n_rows, '');
    _log('[训练] 模型 ' + d.model_path, 'info');
    const imp = d.feature_importances || {};
    const top = Object.entries(imp).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([k, v]) => `${k}:${v.toFixed(4)}`).join('  ');
    _log('[训练] 特征重要性 Top5: ' + top, '');
    store.set({ trainResult: `IC=${d.mean_ic} IR=${d.ic_ir} 模型已保存` });
  } catch (e) { _log('[训练] 失败: ' + (e.message || e), 'err'); }
  store.set({ trainBusy: false });
}

async function runEvolve() {
  const s = store.get();
  if (s.evolveBusy) return;
  const syms = s.evolveSymbols.split(/[,;\s]+/).filter(Boolean);
  if (!syms.length) { toast('请输入股票代码', { type: 'error' }); return; }
  store.set({ evolveBusy: true, evolveResult: '' });
  _log('[蒸馏] 模式=' + s.evolveMode + ' 代数=' + s.evolveGen + ' 注入ML=' + s.evolveInjectML, 'info');
  try {
    const res = await api.post('/api/evolution/evolve/' + s.evolveMode, {
      mode: s.evolveMode, symbols: syms, generations: Number(s.evolveGen),
      inject_ml: s.evolveInjectML,
    }, { timeoutMs: 300000 });
    const d = res?.data || res;
    const genList = d.generations || [];
    _log('[蒸馏] 完成 ' + genList.length + ' 代 最佳得分=' + d.best_composite, '');
    for (const g of genList) _log('  Gen' + g.generation + ' 得分=' + g.composite_score + ' 胜率=' + g.avg_win_rate + '% 收益=' + g.avg_return + '%', '');
    store.set({ evolveResult: `最佳得分=${d.best_composite} 策略${d.strategy_saved?'已':'未'}保存` });
  } catch (e) { _log('[蒸馏] 失败: ' + (e.message || e), 'err'); }
  store.set({ evolveBusy: false });
}

async function runLoop(action) {
  const s = store.get();
  if (action === 'stop') {
    try { await api.post('/api/evolution/auto-loop/stop', {}); _log('[循环] 停止请求已发送', 'warn'); }
    catch (e) { _log('[循环] 停止失败: ' + (e.message || e), 'err'); }
    return;
  }
  if (s.loopBusy) return;
  store.set({ loopBusy: true, loopStatus: null });
  _log('[循环] 启动 周期抽' + s.loopSymbols + '票 代数' + s.loopGen + ' 时限' + s.loopMinutes + '分', 'info');
  try {
    await api.post('/api/evolution/auto-loop/start', {
      symbol_count: Number(s.loopSymbols), generations: Number(s.loopGen),
      deadline_minutes: Number(s.loopMinutes),
    }, { timeoutMs: 15000 });
    _log('[循环] 已启动, 开始轮询…', '');
    // 轮询直到完成或停止
    let cnt = 0;
    const int = setInterval(async () => {
      try {
        const r = await api.get('/api/evolution/auto-loop/status', { timeoutMs: 5000 });
        const d = r?.data || r;
        store.set({ loopStatus: d });
        if (++cnt % 4 === 0) _log('[循环] 状态=' + d.phase + ' 周期=' + (d.cycle_number || 0) + ' 运行=' + (d.running ? '是' : '否'), '');
        if (!d.running) { clearInterval(int); store.set({ loopBusy: false }); _log('[循环] 已结束', 'info'); }
      } catch (e) { clearInterval(int); store.set({ loopBusy: false }); }
    }, 5000);
    store.set({ _loopPollId: int });
  } catch (e) { _log('[循环] 启动失败: ' + (e.message || e), 'err'); store.set({ loopBusy: false }); }
}

// ── render ──

function render(state) {
  root.replaceChildren(
    h('div', { class: 'dash-hero' }, [
      h('div', {}, [h('h1', {}, '🧬 进化实验室'), h('div', { class: 'dash-sub' }, 'ML 模型训练 · 策略蒸馏 · 自主进化循环')]),
    ]),
    _sysBar(state.sysStats),
    h('div', { class: 'ev-grid' }, [
      // ── 训练卡 ──
      h('div', { class: 'ev-card' }, [
        h('div', { class: 'ev-card-title' }, '🤖 训练 ML 模型'),
        h('div', { class: 'ev-field' }, [h('label', {}, '股票代码(逗号分隔)'), h('input', { value: state.trainSymbols, onChange: (e) => store.set({ trainSymbols: e.target.value }) })]),
        h('div', { class: 'ev-field-row' }, [
          h('div', { class: 'ev-field' }, [h('label', {}, '设备'), h('select', { value: state.trainDevice, onChange: (e) => store.set({ trainDevice: e.target.value }) }, [h('option', { value: 'cpu' }, 'CPU'), h('option', { value: 'gpu' }, 'GPU')])]),
          h('div', { class: 'ev-field' }, [h('label', {}, '标签窗口'), h('input', { type: 'number', value: state.trainHorizon, min: 1, max: 20, onChange: (e) => store.set({ trainHorizon: e.target.value }) })]),
        ]),
        h('button', { class: 'btn btn-primary btn-sm', onClick: runTrain, disabled: state.trainBusy, style: 'margin-top:8px;' }, state.trainBusy ? '训练中…' : '▶ 开始训练'),
        state.trainResult ? h('div', { class: 'ev-result' }, h('span', { class: 'highlight' }, state.trainResult)) : null,
      ]),
      // ── 蒸馏卡 ──
      h('div', { class: 'ev-card' }, [
        h('div', { class: 'ev-card-title' }, '🧪 策略蒸馏'),
        h('div', { class: 'ev-field' }, [h('label', {}, '策略模式'), h('select', { value: state.evolveMode, onChange: (e) => store.set({ evolveMode: e.target.value }) }, MODES.map((m) => h('option', { value: m.v }, m.l)))]),
        h('div', { class: 'ev-field' }, [h('label', {}, '回测股票'), h('input', { value: state.evolveSymbols, onChange: (e) => store.set({ evolveSymbols: e.target.value }) })]),
        h('div', { class: 'ev-field-row' }, [
          h('div', { class: 'ev-field' }, [h('label', {}, '代数'), h('input', { type: 'number', value: state.evolveGen, min: 1, max: 5, onChange: (e) => store.set({ evolveGen: e.target.value }) })]),
        ]),
        h('div', { class: 'ev-check' }, [h('input', { type: 'checkbox', checked: state.evolveInjectML, onChange: (e) => store.set({ evolveInjectML: e.target.checked }) }), '注入 ML 特征重要性']),
        h('button', { class: 'btn btn-primary btn-sm', onClick: runEvolve, disabled: state.evolveBusy, style: 'margin-top:8px;' }, state.evolveBusy ? '蒸馏中…' : '▶ 开始蒸馏'),
        state.evolveResult ? h('div', { class: 'ev-result' }, h('span', { class: 'highlight' }, state.evolveResult)) : null,
      ]),
      // ── 循环卡 ──
      h('div', { class: 'ev-card' }, [
        h('div', { class: 'ev-card-title' }, '🔄 自主进化循环'),
        h('div', { class: 'ev-field-row' }, [
          h('div', { class: 'ev-field' }, [h('label', {}, '每周期股票数'), h('input', { type: 'number', value: state.loopSymbols, min: 5, max: 100, onChange: (e) => store.set({ loopSymbols: e.target.value }) })]),
          h('div', { class: 'ev-field' }, [h('label', {}, '代数'), h('input', { type: 'number', value: state.loopGen, min: 1, max: 5, onChange: (e) => store.set({ loopGen: e.target.value }) })]),
          h('div', { class: 'ev-field' }, [h('label', {}, '时限(分)'), h('input', { type: 'number', value: state.loopMinutes, min: 5, max: 480, onChange: (e) => store.set({ loopMinutes: e.target.value }) })]),
        ]),
        h('div', { style: 'display:flex;gap:8px;margin-top:8px;align-items:center;' }, [
          h('button', { class: 'btn btn-primary btn-sm', onClick: () => runLoop('start'), disabled: state.loopBusy }, state.loopBusy ? '运行中…' : '▶ 启动循环'),
          h('button', { class: 'btn btn-sm', onClick: () => runLoop('stop') }, '■ 停止'),
        ]),
        state.loopStatus ? h('div', { class: 'ev-progress' }, `状态: ${state.loopStatus.phase||'-'} | 周期: ${state.loopStatus.cycle_number||0} | 运行: ${state.loopStatus.running?'是':'否'}`) : null,
      ]),
    ]),
    // ── 日志卡 ──
    h('div', { class: 'card', style: 'padding:12px;' }, [
      h('div', { class: 'card-title', style: 'margin-bottom:8px;' }, '📋 运行日志'),
      h('div', { class: 'ev-log' }, state.log.length ? state.log.slice(-60) : h('span', { style: 'color:var(--text-tertiary);' }, '点击训练/蒸馏/循环按钮开始…')),
    ]),
  );
}

// ── lifecycle ──

export async function mount(container) {
  root = container;
  render(store.get());
  bindRender(store, render);
  loadSysStats();
  _statsInt = setInterval(loadSysStats, 3000);
}

export function onShow() {
  loadSysStats();
}

export function onHide() {
  if (_statsInt) { clearInterval(_statsInt); _statsInt = 0; }
  const int = store.get()._loopPollId;
  if (int) { clearInterval(int); }
}
