// AI Research module — Market Regime, Emotion, Money Flow, AI Explain
import { api } from '../core/api.js';
import { createStore, bindRender } from '../core/store.js';
import { h, fmtNum, fmtPct, toast } from '../core/dom.js';
import { StatTile, EmptyState, SkeletonRows } from '../components/ui.js';

const store = createStore({
  regime: null, emotion: null, hotspots: [], moneyFlow: null,
  explain: null, review: null, mfSymbol: '000001',
  loading: { regime: true, emotion: true, hotspots: true, mf: false, explain: false, review: false },
});

let root;

export async function mount(container) {
  root = container;
  render(store.get());
  bindRender(store, render);
  await Promise.all([loadRegime(), loadEmotion(), loadHotspots()]);
}

export function onShow() { loadRegime(); loadEmotion(); }

// Data loaders
async function loadRegime() {
  try {
    const res = await api.get('/api/research/engines/regime?symbol=000001');
    store.set(s => ({ regime: res?.data, loading: { ...s.loading, regime: false } }));
  } catch (e) {
    store.set(s => ({ regime: null, loading: { ...s.loading, regime: false } }));
  }
}

async function loadEmotion() {
  try {
    const res = await api.get('/api/research/engines/emotion/index');
    store.set(s => ({ emotion: res?.data, loading: { ...s.loading, emotion: false } }));
  } catch (e) {
    store.set(s => ({ emotion: null, loading: { ...s.loading, emotion: false } }));
  }
}

async function loadHotspots() {
  try {
    const res = await api.get('/api/research/engines/emotion/hotspots');
    store.set(s => ({ hotspots: res?.data?.hotspots || [], loading: { ...s.loading, hotspots: false } }));
  } catch (e) {
    store.set(s => ({ hotspots: [], loading: { ...s.loading, hotspots: false } }));
  }
}

async function loadMoneyFlow(sym) {
  const symbol = sym || store.get().mfSymbol;
  store.set(s => ({ mfSymbol: symbol, loading: { ...s.loading, mf: true } }));
  try {
    const res = await api.get('/api/research/engines/money-flow/' + symbol);
    store.set(s => ({ moneyFlow: res?.data, loading: { ...s.loading, mf: false } }));
  } catch (e) {
    store.set(s => ({ moneyFlow: null, loading: { ...s.loading, mf: false } }));
  }
}

async function loadExplain() {
  // Shared symbol coupling (Bug 23): read the symbol charted by chart.js from a
  // simple global instead of a fragile cross-module DOM selector; fall back to '000001'.
  const sym = window._currentSymbol || '000001';
  store.set(s => ({ loading: { ...s.loading, explain: true } }));
  try {
    const res = await api.get('/api/research/engines/explain/' + sym);
    store.set(s => ({ explain: res?.data, loading: { ...s.loading, explain: false } }));
  } catch (e) {
    store.set(s => ({ explain: null, loading: { ...s.loading, explain: false } }));
  }
}

async function loadReview() {
  store.set(s => ({ loading: { ...s.loading, review: true } }));
  try {
    const res = await api.get('/api/review/latest');
    store.set(s => ({ review: res?.data, loading: { ...s.loading, review: false } }));
  } catch (e) {
    store.set(s => ({ review: null, loading: { ...s.loading, review: false } }));
  }
}

async function generateReview() {
  store.set(s => ({ loading: { ...s.loading, review: true } }));
  try {
    await api.post('/api/review/generate', {}, { timeoutMs: 30000 });
    await loadReview();
    toast('复盘报告已生成', { type: 'success' });
  } catch (e) {
    store.set(s => ({ loading: { ...s.loading, review: false } }));
    toast(e.message, { type: 'error' });
  }
}

// Render helpers
const icons = { '主升浪':'🚀','趋势行情':'📈','修复行情':'🔄','高位震荡':'📊','板块轮动':'🔀','机构行情':'🏦','游资行情':'⚡','情绪退潮':'⚠️','冰点行情':'❄️','一致高潮':'🔥' };
const riskCol = { 1:'#3fb950', 2:'#16b4ff', 3:'#ffb020', 4:'#f0883e', 5:'#ff4d6a' };
const phaseCol = { '启动':'#58a6ff','扩散':'#3fb950','一致':'#ffb020','高潮':'#f0883e','退潮':'#ff4d6a' };
const sigCol = { '拉升':'#3fb950','吸筹':'#16b4ff','洗盘':'#ffb020','出货':'#ff4d6a','炸板':'#ff4d6a','试盘':'#7c6bff','尾盘抢筹':'#f0883e','回流':'#3fb950' };

function emotionColor(val) {
  if (val > 70) return '#ff4d6a';
  if (val > 50) return '#ffb020';
  return '#0ecb81';
}

function mfScoreColor(val) {
  if (val > 60) return '#0ecb81';
  if (val > 40) return '#ffb020';
  return '#ff4d6a';
}

function render(state) {
  if (!root) return;
  var s = state;
  root.innerHTML = '';

  var rows = [];

  // Row 1: Regime + Emotion + Risk
  var row1 = h('div', { class: 'research-row' });

  // Regime Card
  var regimeCard = h('div', { class: 'card', style: 'flex:1.5' });
  regimeCard.appendChild(h('div', { class: 'card-head' }, [
    h('div', { class: 'card-title' }, [h('span', { class: 'dot', style: 'background:var(--brand-teal)' }), '🌐 市场状态']),
    h('span', { class: 'card-action', onClick: loadRegime }, '刷新'),
  ]));

  if (s.loading.regime) {
    regimeCard.appendChild(SkeletonRows(4));
  } else if (!s.regime) {
    regimeCard.appendChild(EmptyState({ title: '加载失败' }));
  } else {
    var rb = h('div', { style: 'text-align:center;padding:12px 0;' });
    rb.appendChild(h('div', { style: 'font-size:48px;margin-bottom:4px;' }, icons[s.regime.label] || '📊'));
    rb.appendChild(h('div', { style: 'font-size:24px;font-weight:700;' }, s.regime.label));
    rb.appendChild(h('div', { style: 'font-size:13px;color:var(--text-secondary);margin-bottom:8px;' }, '置信度 ' + (s.regime.confidence * 100).toFixed(0) + '%'));
    var statRow = h('div', { style: 'display:flex;justify-content:center;gap:20px;' });
    statRow.appendChild(StatTile({ label: '建议仓位', value: (s.regime.suggested_position_pct * 100).toFixed(0) + '%' }));
    statRow.appendChild(StatTile({ label: '风险', value: s.regime.risk_level + '/5' }));
    rb.appendChild(statRow);
    rb.appendChild(h('div', { style: 'font-size:12px;color:var(--text-secondary);margin-top:8px;' }, '推荐: ' + ((s.regime.recommended_strategies || []).slice(0, 3).join(' · ') || '观望')));
    regimeCard.appendChild(rb);
  }
  row1.appendChild(regimeCard);

  // Emotion Card
  var emoCard = h('div', { class: 'card', style: 'flex:1' });
  emoCard.appendChild(h('div', { class: 'card-head' }, [
    h('div', { class: 'card-title' }, [h('span', { class: 'dot', style: 'background:var(--accent-warn)' }), '🌡️ 情绪']),
    h('span', { class: 'card-action', onClick: loadEmotion }, '刷新'),
  ]));
  if (s.loading.emotion) {
    emoCard.appendChild(SkeletonRows(3));
  } else if (!s.emotion) {
    emoCard.appendChild(EmptyState({ title: '加载失败' }));
  } else {
    var eb = h('div', { style: 'text-align:center;padding:16px 0;' });
    eb.appendChild(h('div', { style: 'font-size:42px;font-weight:800;color:' + emotionColor(s.emotion.value) + ';' }, s.emotion.value.toFixed(0)));
    eb.appendChild(h('div', { style: 'font-size:14px;' }, s.emotion.label));
    var compRow = h('div', { style: 'margin-top:8px;display:flex;gap:8px;justify-content:center;font-size:12px;color:var(--text-secondary);' });
    var comps = s.emotion.components || {};
    Object.entries(comps).slice(0, 4).forEach(function(kv) {
      compRow.appendChild(h('span', {}, kv[0] + ': ' + kv[1].toFixed(0)));
    });
    eb.appendChild(compRow);
    emoCard.appendChild(eb);
  }
  row1.appendChild(emoCard);

  // Risk Card
  var riskCard = h('div', { class: 'card', style: 'flex:1' });
  riskCard.appendChild(h('div', { class: 'card-head' }, [
    h('div', { class: 'card-title' }, [h('span', { class: 'dot', style: 'background:var(--accent-danger)' }), '⚠️ 风险']),
  ]));
  var rlist = [];
  if (s.regime && s.regime.risk_level >= 4) rlist.push({ c: '#ff4d6a', t: '高风险 ' + s.regime.risk_level + '/5 - ' + s.regime.label });
  else if (s.regime && s.regime.risk_level === 3) rlist.push({ c: '#ffb020', t: '中等风险 ' + s.regime.risk_level + '/5 - ' + s.regime.label });
  else if (s.regime) rlist.push({ c: '#0ecb81', t: '低风险 ' + s.regime.risk_level + '/5 - ' + s.regime.label });
  if (s.emotion && s.emotion.label === '极度贪婪') rlist.push({ c: '#ff4d6a', t: '情绪过热 - 警惕回调' });
  if (s.emotion && s.emotion.label === '极度恐慌') rlist.push({ c: '#ffb020', t: '极度恐慌 - 关注超跌' });
  if (!rlist.length) rlist.push({ c: 'var(--text-secondary)', t: '风险可控' });
  rlist.forEach(function(r) {
    riskCard.appendChild(h('div', { style: 'padding:6px 10px;margin-bottom:4px;border-radius:6px;border-left:3px solid ' + r.c + ';font-size:13px;background:var(--bg-input);' }, r.t));
  });
  row1.appendChild(riskCard);

  rows.push(row1);

  // Row 2: Hotspots + Money Flow
  var row2 = h('div', { class: 'research-row' });

  // Hotspots
  var hotspotCard = h('div', { class: 'card', style: 'flex:1' });
  hotspotCard.appendChild(h('div', { class: 'card-head' }, [
    h('div', { class: 'card-title' }, [h('span', { class: 'dot', style: 'background:var(--accent-ai)' }), '🔥 热点生命周期']),
    h('span', { class: 'card-action', onClick: loadHotspots }, '刷新'),
  ]));

  var hotspotBody = h('div', { class: 'scroll-y', style: 'max-height:280px;' });
  if (s.loading.hotspots) {
    hotspotBody.appendChild(SkeletonRows(5));
  } else if (!s.hotspots.length) {
    hotspotBody.appendChild(EmptyState({ title: '暂无活跃热点' }));
  } else {
    var tbl = h('table', { class: 'tbl tbl-compact' });
    var thead = h('thead', {});
    thead.appendChild(h('tr', {}, ['概念', '阶段', '强度', '持续', '涨停', '龙头'].map(function(l) { return h('th', {}, l); })));
    tbl.appendChild(thead);
    var tbody = h('tbody', {});
    s.hotspots.slice(0, 12).forEach(function(hs) {
      var tr = h('tr', {});
      tr.appendChild(h('td', { style: 'font-weight:600;' }, hs.concept));
      tr.appendChild(h('td', { style: 'color:' + (phaseCol[hs.phase] || '#8b949e') + ';' }, hs.phase));
      tr.appendChild(h('td', { class: 'num' }, (hs.strength_score || 0).toFixed(0)));
      tr.appendChild(h('td', { class: 'num' }, (hs.duration_days || 1) + 'd'));
      tr.appendChild(h('td', { class: 'num' }, String(hs.daily_limit_up_count || 0)));
      tr.appendChild(h('td', {}, hs.leading_stock_name || hs.leading_stock || '--'));
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    hotspotBody.appendChild(tbl);
  }
  hotspotCard.appendChild(hotspotBody);
  row2.appendChild(hotspotCard);

  // Money Flow
  var mfCard = h('div', { class: 'card', style: 'flex:0.8' });
  mfCard.appendChild(h('div', { class: 'card-head' }, [
    h('div', { class: 'card-title' }, [h('span', { class: 'dot', style: 'background:var(--accent-info)' }), '💰 资金行为']),
  ]));
  var btns = h('div', { style: 'display:flex;gap:4px;margin-bottom:8px;' });
  ['000001', '600519', '300750'].forEach(function(sym) {
    var isActive = store.get().mfSymbol === sym;
    btns.appendChild(h('button', {
      class: 'btn btn-xs ' + (isActive ? 'btn-primary' : 'btn-ghost'),
      onClick: function() { loadMoneyFlow(sym); },
      style: 'font-size:12px;'
    }, sym));
  });
  mfCard.appendChild(btns);

  if (s.loading.mf) {
    mfCard.appendChild(SkeletonRows(4));
  } else if (!s.moneyFlow) {
    mfCard.appendChild(EmptyState({ title: '点击股票代码查看' }));
  } else {
    var mfBody = h('div', {});
    var mfHead = h('div', { style: 'font-size:14px;font-weight:700;margin-bottom:4px;' });
    mfHead.appendChild(h('span', {}, s.moneyFlow.current_phase));
    mfHead.appendChild(h('span', { style: 'float:right;color:' + mfScoreColor(s.moneyFlow.money_flow_score) + ';' }, '健康度 ' + s.moneyFlow.money_flow_score + '/100'));
    mfBody.appendChild(mfHead);
    mfBody.appendChild(h('div', { style: 'font-size:12px;color:var(--text-secondary);margin-bottom:6px;' }, (s.moneyFlow.phase_sequence || []).slice(-8).join(' → ')));
    (s.moneyFlow.recent_signals || []).slice(-6).reverse().forEach(function(sig) {
      var sc = sigCol[sig.behavior] || '#8b949e';
      mfBody.appendChild(h('div', { style: 'font-size:12px;padding:2px 6px;margin-bottom:2px;border-left:3px solid ' + sc + ';' }, sig.behavior + ' ' + sig.date + ' · ' + (sig.description || '').slice(0, 40)));
    });
    mfCard.appendChild(mfBody);
  }
  row2.appendChild(mfCard);

  rows.push(row2);

  // Row 3: AI Explain + Review
  var row3 = h('div', { class: 'research-row' });

  // AI Explain
  var explainCard = h('div', { class: 'card', style: 'flex:1' });
  explainCard.appendChild(h('div', { class: 'card-head' }, [
    h('div', { class: 'card-title' }, [h('span', { class: 'dot', style: 'background:var(--brand-violet)' }), '🤖 AI 分析']),
    h('span', { class: 'card-action', onClick: loadExplain }, '刷新'),
  ]));
  if (s.loading.explain) {
    explainCard.appendChild(SkeletonRows(4));
  } else if (!s.explain) {
    var ebDiv = h('div', { style: 'text-align:center;padding:20px;' });
    ebDiv.appendChild(h('button', { class: 'btn btn-primary', onClick: loadExplain }, '▶ 开始分析'));
    explainCard.appendChild(ebDiv);
  } else {
    var xb = h('div', {});
    var actionColors = { '买入': '#f6465d', '卖出/回避': '#0ecb81', '持有/观望': '#ffb020' };
    xb.appendChild(h('div', {
      style: 'font-size:16px;font-weight:700;margin-bottom:6px;color:' + (actionColors[s.explain.action] || '#ffb020') + ';'
    }, '综合评分 ' + s.explain.composite_score + ' · ' + s.explain.action + ' · ' + s.explain.risk_level + '风险'));

    var reasonGrid = h('div', { style: 'display:grid;grid-template-columns:1fr 1fr;gap:3px 8px;font-size:12px;line-height:1.5;' });
    var reasons = s.explain.reasons || {};
    reasonGrid.appendChild(h('div', {}, '① ' + (reasons.market || '--').slice(0, 60)));
    reasonGrid.appendChild(h('div', {}, '② ' + (reasons.sector || '--').slice(0, 60)));
    reasonGrid.appendChild(h('div', {}, '③ ' + (reasons.capital || '--').slice(0, 60)));
    reasonGrid.appendChild(h('div', {}, '④ ' + (reasons.technical || '--').slice(0, 60)));
    reasonGrid.appendChild(h('div', {}, '⑤ ' + (reasons.event || '--').slice(0, 60)));
    xb.appendChild(reasonGrid);

    if ((s.explain.risks || []).length) {
      xb.appendChild(h('div', { style: 'margin-top:6px;font-size:12px;color:var(--accent-danger);' }, '⚠ ' + (s.explain.risks || []).join(' · ')));
    }
    explainCard.appendChild(xb);
  }
  row3.appendChild(explainCard);

  // Review Card
  var reviewCard = h('div', { class: 'card', style: 'flex:0.8' });
  reviewCard.appendChild(h('div', { class: 'card-head' }, [
    h('div', { class: 'card-title' }, [h('span', { class: 'dot', style: 'background:var(--brand-teal)' }), '📋 AI 日报']),
    h('div', { style: 'display:flex;gap:4px;' }, [
      h('span', { class: 'card-action', onClick: loadReview }, '刷新'),
      h('span', { class: 'card-action', onClick: generateReview, style: 'color:var(--market-up);' }, '生成'),
    ]),
  ]));
  if (s.loading.review) {
    reviewCard.appendChild(SkeletonRows(6));
  } else if (!s.review || !s.review.content) {
    reviewCard.appendChild(EmptyState({ title: '点击"生成"创建复盘', hint: s.review?.message || '' }));
  } else {
    reviewCard.appendChild(h('div', { class: 'scroll-y', style: 'max-height:360px;font-size:12px;line-height:1.6;white-space:pre-wrap;' }, s.review.content.slice(0, 3000)));
  }
  row3.appendChild(reviewCard);

  rows.push(row3);

  // Assemble all rows
  var grid = h('div', { class: 'research-grid' });
  rows.forEach(function(r) { grid.appendChild(r); });
  root.appendChild(grid);
}
