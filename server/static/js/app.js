// Main app entry point — tab switching + global init
import { getChart, resizeAll, disposeAll } from './charts.js';
import { switchSancaiSubTab, switchSancaiTimeRange } from './sancai.js';

// Expose to inline onclick handlers
window.switchTab = switchTab;
window.switchSancaiSubTab = switchSancaiSubTab;
window.switchSancaiTimeRange = switchSancaiTimeRange;
window.onDidaoSymbolChange = function () {
  import('./sancai.js').then(m => m.onDidaoSymbolChange());
};

// Retain legacy functions for non-sancai tabs
window.loadChart = loadChart;
window.loadTickChart = loadTickChart;
window.loadDepthAndFlow = loadDepthAndFlow;
window.switchChartMode = switchChartMode;
window.onPeriodChange = onPeriodChange;
window.renderMaToggles = renderMaToggles;
window.getActiveMaKeys = getActiveMaKeys;
window.runBacktest = runBacktest;
window.addCustomSymbols = addCustomSymbols;
window.loadSancai = loadSancaiLegacy;
window.loadStocks = loadStocks;
window.loadDashboard = loadDashboard;
window.onModeChange = onModeChange;
window.showTradeKline = showTradeKline;

// ============ Tab Switching ============
function switchTab(tab) {
  document.querySelectorAll('[id^="tab-"]').forEach(el => el.classList.add('hidden'));
  document.getElementById('tab-' + tab).classList.remove('hidden');
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  if (window.event && window.event.target) {
    window.event.target.classList.add('active');
  }

  if (tab === 'chart') loadChart();
  if (tab === 'sancai') {
    // Load sancai module dynamically to activate the tier pages
    import('./sancai.js').then(m => m.loadTiandao());
  }
  if (tab === 'stocks') loadStocks();
  if (tab === 'dashboard') loadDashboard();
}

// ============ Health Check ============
async function checkHealth() {
  try {
    const resp = await fetch('/api/health');
    const data = await resp.json();
    const el = document.getElementById('rust-status');
    if (el) {
      el.textContent = data.rust_core ? 'Rust 已加载' : 'Rust 未加载';
      el.style.background = data.rust_core ? '#238636' : '#da3633';
    }
  } catch (e) { console.error(e); }
}

// ============ Dashboard ============
async function loadDashboard() {
  try {
    const resp = await fetch('/api/sancai/status');
    const data = await resp.json();
    document.getElementById('mini-tiandao').innerHTML = '<span class="tag tag-' +
      (data.tiandao.assessment === '吉' ? 'ji' : data.tiandao.assessment === '凶' ? 'xiong' : 'ping') + '">' + data.tiandao.assessment + '</span>';
    document.getElementById('mini-didao').innerHTML = '<b>' + data.didao.filtered_count + '</b> 只合格';
    document.getElementById('mini-rendao').innerHTML = '<b>' + data.rendao.current_positions + '</b> 个持仓';
  } catch (e) { console.error(e); }
  document.getElementById('stat-equity').textContent = '1,000,000';
  document.getElementById('stat-return').textContent = '--';
  document.getElementById('stat-sharpe').textContent = '--';
  document.getElementById('stat-drawdown').textContent = '--';
}

// ============ Stale loadSancai (kept for dashboard mini view) ============
async function loadSancaiLegacy() {
  try {
    const resp = await fetch('/api/sancai/status');
    const data = await resp.json();
    const tiandao = data.tiandao;
    const verdictClass = 'tag tag-' + (tiandao.assessment === '吉' ? 'ji' : tiandao.assessment === '凶' ? 'xiong' : 'ping');
    document.getElementById('tiandao-verdict').innerHTML = '<span class="' + verdictClass + '" style="font-size:48px;">' + tiandao.assessment + '</span>';
    document.getElementById('tiandao-details').innerHTML = tiandao.details.map(d => '<div style="padding:4px 0;">' + d + '</div>').join('');
    document.getElementById('flow-tiandao').style.background = tiandao.assessment === '吉' ? '#1b3a1b' : tiandao.assessment === '凶' ? '#3a1b1b' : '#3a351b';
    const didao = data.didao;
    document.getElementById('didao-count').textContent = '筛选结果: ' + didao.filtered_count + ' 只合格';
    document.getElementById('didao-list').innerHTML = (didao.top_picks || []).map((s, i) =>
      '<div style="padding:6px 0;border-bottom:1px solid #21262d;">' +
      '<b>' + s.symbol + '</b> ' + s.name + ' | 评分:<b>' + s.score + '</b> | 最新:' + s.latest_price.toFixed(2) + '</div>'
    ).join('');
    const rendao = data.rendao;
    document.getElementById('rendao-positions').textContent = '当前持仓: ' + rendao.current_positions + ' | 今日信号: ' + rendao.today_signals;
    document.getElementById('rendao-actions').textContent = rendao.note || '';
  } catch (e) { console.error(e); }
}

// ============ Stocks ============
async function loadStocks() {
  try {
    const resp = await fetch('/api/data/stocks');
    const data = await resp.json();
    const div = document.getElementById('stock-list');
    div.innerHTML = '<table class="table"><tr><th>代码</th><th>名称</th><th>日线数据</th><th>K线条数</th></tr>' +
      data.stocks.map(s => '<tr><td>' + s.symbol + '</td><td>' + s.name + '</td><td>' + (s.has_daily ? 'OK' : 'NO') + '</td><td>' + (s.daily_bars || 0) + '</td></tr>').join('') +
      '</table>';
  } catch (e) { console.error(e); }
}

// ============ K-line Chart (kept inline for compat) ============
let chartMode = 'kline';
let depthTimer = null;
let klineChart = null, kdjChart = null, tickChart = null;

function switchChartMode(mode) {
  chartMode = mode;
  document.getElementById('btn-tick').className = mode === 'tick' ? 'btn btn-info' : 'btn';
  document.getElementById('btn-kline').className = mode === 'kline' ? 'btn btn-info' : 'btn';
  document.getElementById('kline-chart').style.display = mode === 'kline' ? '' : 'none';
  document.getElementById('kdj-chart').style.display = mode === 'kline' ? '' : 'none';
  document.getElementById('tick-chart').style.display = mode === 'tick' ? '' : 'none';
  document.getElementById('tick-panels').style.display = mode === 'tick' ? '' : 'none';
  if (depthTimer) { clearInterval(depthTimer); depthTimer = null; }
  if (mode === 'tick') {
    loadTickChart();
    loadDepthAndFlow();
    depthTimer = setInterval(loadDepthAndFlow, 5000);
  } else {
    loadChart();
  }
}

function renderMaToggles() {
  const periods = getMaPeriods();
  const checked = periods.filter(p => p <= 144);
  const html = periods.map(p => {
    const isChecked = checked.includes(p);
    return '<label><input type="checkbox"' + (isChecked ? ' checked' : '') +
      ' onchange="loadChart()" id="ma-toggle-' + p + '"> MA' + p + '</label>';
  }).join('');
  document.getElementById('ma-toggles').innerHTML = html;
}

function onPeriodChange() {
  renderMaToggles();
  if (chartMode === 'tick') loadTickChart();
  else loadChart();
}

function getMaPeriods() {
  const period = document.getElementById('chart-period').value;
  if (period === 'daily') return [5, 13, 21, 34, 55, 144, 233, 623];
  return [34, 144, 233];
}

function getActiveMaKeys() {
  const periods = getMaPeriods();
  return periods.filter(p => document.getElementById('ma-toggle-' + p)?.checked);
}

async function loadChart() {
  const symbol = document.getElementById('chart-symbol').value;
  const period = document.getElementById('chart-period').value;
  try {
    const resp = await fetch('/api/data/stocks/' + symbol + '/kline?period=' + period + '&limit=300');
    const result = await resp.json();
    if (!result.data || result.data.length === 0) return;
    const dates = result.data.map(d => d.date);
    const ohlc = result.data.map(d => [d.open, d.close, d.low, d.high]);
    const volumes = result.data.map(d => d.volume);
    const mas = {};
    const activeKeys = getActiveMaKeys();
    activeKeys.forEach(k => {
      const key = 'ma_' + k;
      const vals = result.data.map(d => {
        const v = d[key];
        return (v === null || v === undefined) ? null : v;
      });
      if (vals.some(v => v !== null)) mas['MA' + k] = vals;
    });
    const maColors = {
      MA5: '#e6c300', MA13: '#f0883e', MA21: '#a371f7',
      MA34: '#58a6ff', MA55: '#3fb950', MA144: '#f85149',
      MA233: '#d2991d', MA623: '#8b949e'
    };
    if (!klineChart) klineChart = echarts.init(document.getElementById('kline-chart'));
    klineChart.setOption({
      backgroundColor: '#161b22',
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { data: ['K线', ...Object.keys(mas)], top: 0, textStyle: { color: '#8b949e', fontSize: 11 } },
      grid: [{ left: '8%', right: '8%', top: '8%', height: '65%' }, { left: '8%', right: '8%', top: '78%', height: '18%' }],
      xAxis: [{ type: 'category', data: dates, gridIndex: 0, axisLabel: { color: '#8b949e', fontSize: 10 } },
              { type: 'category', data: dates, gridIndex: 1, axisLabel: { show: false } }],
      yAxis: [{ type: 'value', gridIndex: 0, axisLabel: { color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } }, scale: true },
              { type: 'value', gridIndex: 1, axisLabel: { color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } } }],
      dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }, { type: 'slider', xAxisIndex: [0, 1], bottom: 0, height: 15 }],
      series: [
        { name: 'K线', type: 'candlestick', data: ohlc, itemStyle: { color: '#26a69a', color0: '#ef5350', borderColor: '#26a69a', borderColor0: '#ef5350' } },
        ...Object.entries(mas).map(([name, vals]) => ({
          name, type: 'line', data: vals, smooth: true, symbol: 'none',
          lineStyle: { width: 1.5, color: maColors[name] || '#8b949e' }
        })),
        { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes,
          itemStyle: { color: function (p) { return ohlc[p.dataIndex][1] >= ohlc[p.dataIndex][0] ? '#26a69a44' : '#ef535044'; } } }
      ]
    }, true);

    // KDJ
    const kdjK = result.data.map(d => d.kdj_k || null);
    const kdjD = result.data.map(d => d.kdj_d || null);
    const kdjJ = result.data.map(d => d.kdj_j || null);
    if (!kdjChart) kdjChart = echarts.init(document.getElementById('kdj-chart'));
    kdjChart.setOption({
      backgroundColor: '#161b22', tooltip: { trigger: 'axis' },
      legend: { data: ['K', 'D', 'J'], top: 0, textStyle: { color: '#8b949e', fontSize: 11 } },
      grid: { left: '8%', right: '8%', top: '15%', bottom: '10%' },
      xAxis: { type: 'category', data: dates, axisLabel: { show: false } },
      yAxis: { type: 'value', min: 0, max: 100, axisLabel: { color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } } },
      dataZoom: [{ type: 'inside' }],
      series: [
        { name: 'K', type: 'line', data: kdjK, smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#e6c300' } },
        { name: 'D', type: 'line', data: kdjD, smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#58a6ff' } },
        { name: 'J', type: 'line', data: kdjJ, smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#f0883e' } },
        { type: 'line', markLine: { silent: true, data: [{ yAxis: 20, label: { formatter: '超卖' } }, { yAxis: 80, label: { formatter: '超买' } }], lineStyle: { color: '#30363d', type: 'dashed' } }, data: [] }
      ]
    }, true);
  } catch (e) { console.error(e); }
}

async function loadTickChart() {
  const symbol = document.getElementById('chart-symbol').value;
  const period = document.getElementById('chart-period').value;
  const tickPeriod = (period === 'daily') ? '1min' : period;
  try {
    const resp = await fetch('/api/data/stocks/' + symbol + '/kline?period=' + tickPeriod + '&limit=240');
    const result = await resp.json();
    if (!result.data || result.data.length === 0) {
      document.getElementById('tick-chart').innerHTML = '<div class="loading">暂无分时数据</div>';
      return;
    }
    const today = new Date().toISOString().slice(0, 10);
    const bars = result.data.filter(d => String(d.date).startsWith(today));
    const useBars = bars.length > 10 ? bars : result.data.slice(-120);
    const times = useBars.map(d => {
      const t = String(d.date);
      return t.length >= 16 ? t.slice(11, 16) : t.slice(-8, -3);
    });
    const prices = useBars.map(d => d.close);
    const avgPrices = [];
    let sumPrice = 0, sumVol = 0;
    useBars.forEach(d => {
      sumPrice += d.close * (d.volume || 1);
      sumVol += (d.volume || 1);
      avgPrices.push(sumVol > 0 ? +(sumPrice / sumVol).toFixed(3) : null);
    });
    const prevClose = useBars[0]?.open || prices[0];
    const pctChange = prevClose > 0 ? ((prices[prices.length - 1] - prevClose) / prevClose * 100) : 0;
    const lineColor = pctChange >= 0 ? '#ef5350' : '#26a69a';
    const areaColor = pctChange >= 0 ? 'rgba(239,83,80,0.15)' : 'rgba(38,166,154,0.15)';
    if (!tickChart) tickChart = echarts.init(document.getElementById('tick-chart'));
    tickChart.setOption({
      backgroundColor: '#161b22',
      title: {
        text: symbol + ' 分时图',
        subtext: prices[prices.length - 1].toFixed(2) + '  ' + (pctChange >= 0 ? '+' : '') + pctChange.toFixed(2) + '%',
        left: 'center', top: 5, textStyle: { color: '#e6f1ff', fontSize: 16 }, subtextStyle: { color: lineColor, fontSize: 14 }
      },
      tooltip: { trigger: 'axis' },
      grid: { left: '8%', right: '8%', top: '18%', bottom: '10%' },
      xAxis: { type: 'category', data: times, boundaryGap: false, axisLabel: { color: '#8b949e', fontSize: 10 }, axisLine: { lineStyle: { color: '#30363d' } } },
      yAxis: [{ type: 'value', scale: true, splitNumber: 5, axisLabel: { color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } } },
              { type: 'value', scale: true, splitNumber: 5, axisLabel: { color: '#8b949e', formatter: '{value}%' }, splitLine: { show: false } }],
      dataZoom: [{ type: 'inside' }],
      series: [
        { name: '价格', type: 'line', data: prices, smooth: true, symbol: 'none', lineStyle: { width: 2, color: lineColor },
          areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: areaColor }, { offset: 1, color: 'rgba(0,0,0,0)' }]) } },
        { name: '均价', type: 'line', data: avgPrices, smooth: true, symbol: 'none', lineStyle: { width: 1, color: '#d2991d', type: 'dashed' } },
        { name: '昨收', type: 'line', data: [], symbol: 'none',
          markLine: { silent: true, symbol: 'none', data: [{ yAxis: prevClose, label: { formatter: '昨收 ' + prevClose.toFixed(2), color: '#8b949e', fontSize: 10 } }], lineStyle: { color: '#8b949e', type: 'dashed', width: 1 } } }
      ]
    }, true);
  } catch (e) { console.error(e); }
}

async function loadDepthAndFlow() {
  if (document.getElementById('tick-panels').style.display === 'none') return;
  const symbol = document.getElementById('chart-symbol').value;
  const now = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  document.getElementById('depth-time').textContent = now;
  document.getElementById('flow-time').textContent = now;
  const [depthResp, ticksResp] = await Promise.allSettled([
    fetch('/api/data/stocks/' + symbol + '/depth'),
    fetch('/api/data/stocks/' + symbol + '/ticks?limit=100')
  ]);
  if (depthResp.status === 'fulfilled' && depthResp.value.ok) {
    renderDepth(await depthResp.value.json());
  } else {
    document.getElementById('depth-body').innerHTML = '<tr><td colspan="7"><div class="depth-error">盘口数据暂不可用</div></td></tr>';
  }
  if (ticksResp.status === 'fulfilled' && ticksResp.value.ok) {
    renderFlow(await ticksResp.value.json());
  } else {
    document.getElementById('flow-buy').textContent = '--';
    document.getElementById('flow-sell').textContent = '--';
    document.getElementById('flow-net').textContent = '--';
    document.getElementById('flow-net').style.color = '#c9d1d9';
    document.getElementById('flow-detail').innerHTML = '逐笔数据暂不可用';
    document.getElementById('flow-bar-buy').style.width = '50%';
    document.getElementById('flow-bar-sell').style.width = '50%';
    document.getElementById('flow-buy-pct').textContent = '买 --';
    document.getElementById('flow-sell-pct').textContent = '卖 --';
    document.getElementById('big-trades').innerHTML = '<p style="color:#8b949e;">非交易时段或无数据</p>';
  }
}

function renderDepth(data) {
  const sells = data.sells || [];
  const buys = data.buys || [];
  if (sells.length === 0 && buys.length === 0) {
    document.getElementById('depth-body').innerHTML = '<tr><td colspan="7"><div class="depth-error">暂无盘口数据</div></td></tr>';
    return;
  }
  const maxVol = Math.max(...sells.map(s => s.volume), ...buys.map(b => b.volume), 1);
  function volBar(vol, cls) {
    const pct = (vol / maxVol * 100).toFixed(0);
    return '<div class="vol-bar ' + cls + '" style="width:' + pct + '%;"></div>';
  }
  function formatVol(v) {
    if (v >= 10000) return (v / 10000).toFixed(1) + '万';
    if (v >= 1000) return (v / 1000).toFixed(1) + 'K';
    return v.toString();
  }
  const rows = [];
  const maxRows = Math.max(sells.length, buys.length);
  for (let i = 0; i < maxRows; i++) {
    const s = sells[i];
    const b = buys[i];
    rows.push('<tr>' +
      (s ? '<td style="color:#3fb950;">卖' + (sells.length - i) + '</td>' +
       '<td class="vol-cell">' + formatVol(s.volume) + volBar(s.volume, 'sell') + '</td>' +
       '<td style="color:#3fb950;">' + s.price.toFixed(2) + '</td>' :
       '<td></td><td></td><td></td>') +
      '<td class="price-cell">' + ((s || b) ? ((b || s).price.toFixed(2)) : '') + '</td>' +
      (b ? '<td style="color:#f85149;">' + b.price.toFixed(2) + '</td>' +
       '<td class="vol-cell">' + formatVol(b.volume) + volBar(b.volume, 'buy') + '</td>' +
       '<td style="color:#f85149;">买' + (i + 1) + '</td>' :
       '<td></td><td></td><td></td>') +
      '</tr>');
  }
  let spreadHtml = '';
  if (data.spread != null && data.spread_pct != null) {
    spreadHtml = '<tr style="border-top:1px solid #21262d;"><td colspan="3" style="text-align:right;font-size:10px;color:#8b949e;">价差</td>' +
      '<td class="price-cell" style="font-size:11px;">' + data.spread.toFixed(2) + ' (' + data.spread_pct + '%)</td>' +
      '<td colspan="3" style="font-size:10px;color:#8b949e;">昨收 ' + (data.prev_close || '--') + '</td></tr>';
  }
  document.getElementById('depth-body').innerHTML = rows.join('') + spreadHtml;
}

function renderFlow(data) {
  const s = data.summary;
  if (!s) {
    document.getElementById('flow-buy').textContent = '--';
    document.getElementById('flow-sell').textContent = '--';
    document.getElementById('flow-net').textContent = '--';
    return;
  }
  const buyAmt = (s.buy_amount / 10000).toFixed(0);
  const sellAmt = (s.sell_amount / 10000).toFixed(0);
  const netAmt = (s.net_flow / 10000).toFixed(0);
  const netSign = s.net_flow >= 0 ? '+' : '';
  document.getElementById('flow-buy').textContent = buyAmt;
  document.getElementById('flow-sell').textContent = sellAmt;
  document.getElementById('flow-net').textContent = netSign + netAmt;
  document.getElementById('flow-net').style.color = s.net_flow >= 0 ? '#f85149' : '#3fb950';
  document.getElementById('flow-buy-pct').textContent = '买 ' + s.buy_pct + '%';
  document.getElementById('flow-sell-pct').textContent = '卖 ' + (100 - s.buy_pct) + '%';
  document.getElementById('flow-bar-buy').style.width = s.buy_pct + '%';
  document.getElementById('flow-bar-sell').style.width = (100 - s.buy_pct) + '%';
  document.getElementById('flow-detail').innerHTML =
    '<span>买 ' + s.buy_count + '笔</span>  <span>卖 ' + s.sell_count + '笔</span>  <span>共 ' + s.total_count + '笔</span>';
  const bigTrades = data.big_trades || [];
  if (bigTrades.length === 0) {
    document.getElementById('big-trades').innerHTML = '<p style="color:#8b949e;">暂无大单</p>';
  } else {
    const recentBig = bigTrades.slice(-15).reverse();
    document.getElementById('big-trades').innerHTML = recentBig.map(t => {
      const typeCls = t.type === '买盘' ? 'type-buy' : (t.type === '卖盘' ? 'type-sell' : 'type-neutral');
      const typeLabel = t.type || '中性';
      return '<div class="big-trade-row">' +
        '<span class="time">' + t.time + '</span>' +
        '<span class="price">' + t.price.toFixed(2) + '</span>' +
        '<span class="vol">' + t.volume + '手</span>' +
        '<span class="' + typeCls + '">' + typeLabel + '</span></div>';
    }).join('');
  }
}

// ============ Backtest ============
let equityChart = null;
let btKlineChart = null;

// Fine-tuning toggle definitions for each school
const SCHOOL_TOGGLES = {
  chan_theory: {
    toggles: [
      {key:'buy_point_1', label:'一买(背驰)', default:true},
      {key:'buy_point_2', label:'二买(回抽中枢)', default:true},
      {key:'buy_point_3', label:'三买(突破回踩)', default:true},
      {key:'sell_point_1', label:'一卖(顶背驰)', default:true},
      {key:'sell_point_2', label:'二卖(反弹受阻)', default:true},
      {key:'sell_point_3', label:'三卖(跌破反抽)', default:true},
    ]
  },
  ict: {
    toggles: [
      {key:'ob', label:'OB回踩', default:true},
      {key:'fvg', label:'FVG缺口', default:true},
      {key:'liquidity_ote', label:'流动性猎杀+OTE', default:true},
      {key:'breaker_block', label:'Breaker Block', default:true},
    ]
  },
  price_action: {
    toggles: [
      {key:'pin_bar', label:'Pin Bar', default:true},
      {key:'engulfing', label:'吞没形态', default:true},
      {key:'inside_bar', label:'Inside Bar突破', default:true},
      {key:'fakey', label:'Fakey假突破', default:true},
      {key:'sr_flip', label:'阻力/支撑互换', default:true},
      {key:'trendline', label:'趋势线', default:true},
    ]
  },
  wyckoff: {
    toggles: [
      {key:'spring', label:'Spring', default:true},
      {key:'sos', label:'SOS强势信号', default:true},
      {key:'lps', label:'LPS最后支撑', default:true},
      {key:'vsa_accumulation', label:'VSA吸筹', default:true},
      {key:'utad', label:'UTAD', default:true},
      {key:'sow', label:'SOW弱势信号', default:true},
      {key:'vsa_distribution', label:'VSA派发', default:true},
    ]
  },
  morphology: {
    toggles: [
      {key:'double_bottom', label:'W底', default:true},
      {key:'double_top', label:'M顶', default:true},
      {key:'head_shoulders_bottom', label:'头肩底', default:true},
      {key:'head_shoulders_top', label:'头肩顶', default:true},
      {key:'ascending_triangle', label:'上升三角', default:true},
      {key:'descending_triangle', label:'下降三角', default:true},
      {key:'bull_flag', label:'牛旗', default:true},
      {key:'bear_flag', label:'熊旗', default:true},
      {key:'cup_handle', label:'杯柄', default:true},
      {key:'box', label:'箱体', default:true},
    ]
  },
  gann: {
    toggles: [
      {key:'angle_support', label:'Gann角度线', default:true},
      {key:'retrace_levels', label:'回调/反弹位', default:true},
      {key:'time_cycles', label:'时间周期窗口', default:true},
      {key:'square_of_nine', label:'九方图', default:true},
    ]
  },
  wave_theory: {
    toggles: [
      {key:'impulse_w5', label:'推动浪W5', default:true},
      {key:'abc_correction', label:'ABC调整', default:true},
      {key:'fib_retrace', label:'斐波那契回撤', default:true},
      {key:'fib_extension', label:'斐波那契延伸', default:true},
    ]
  },
  dow_theory: {
    toggles: [
      {key:'primary_trend', label:'主趋势确认', default:true},
      {key:'accumulation_breakout', label:'吸筹/派发突破', default:true},
      {key:'secondary_pullback', label:'次级回调/反弹', default:true},
      {key:'trend_reversal', label:'趋势反转', default:true},
      {key:'participation_breakout', label:'公众参与突破', default:true},
    ]
  }
};

function onModeChange() {
  const mode = document.getElementById('bt-mode').value;
  const panel = document.getElementById('bt-school-tuning');
  const togglesDiv = document.getElementById('bt-tuning-toggles');
  const st = SCHOOL_TOGGLES[mode];
  if (st) {
    panel.style.display = '';
    togglesDiv.innerHTML = st.toggles.map(t =>
      '<label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;color:#c9d1d9;">' +
      '<input type="checkbox" data-signal-key="' + t.key + '" ' + (t.default ? 'checked' : '') + '>' +
      t.label + '</label>'
    ).join('');
  } else {
    panel.style.display = 'none';
  }
}

function addCustomSymbols() {
  const input = document.getElementById('bt-custom-symbols');
  const sel = document.getElementById('bt-symbols');
  if (!input.value.trim()) return;
  const customs = input.value.split(',').map(s => s.trim()).filter(Boolean);
  customs.forEach(code => {
    // Check if already in list
    for (let i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === code) return;
    }
    const opt = document.createElement('option');
    opt.value = code;
    opt.textContent = code;
    opt.selected = true;
    sel.appendChild(opt);
  });
  input.value = '';
}

async function runBacktest() {
  const btn = document.getElementById('bt-run-btn');
  btn.disabled = true;
  btn.textContent = '运行中...';
  document.getElementById('bt-status').textContent = '正在计算...';
  const symbolsSel = document.getElementById('bt-symbols');
  const symbols = Array.from(symbolsSel.selectedOptions).map(o => o.value);
  if (symbols.length === 0) symbols.push(symbolsSel.options[0].value);
  const endDate = document.getElementById('bt-end').value.trim();
  const mode = document.getElementById('bt-mode').value;
  let schoolConfig = null;
  if (SCHOOL_TOGGLES[mode]) {
    schoolConfig = {};
    document.querySelectorAll('#bt-tuning-toggles input[type="checkbox"]').forEach(cb => {
      schoolConfig[cb.dataset.signalKey] = cb.checked;
    });
  }
  const req = {
    symbols, period: document.getElementById('bt-period').value,
    start_date: document.getElementById('bt-start').value,
    end_date: endDate || null,
    initial_capital: parseFloat(document.getElementById('bt-capital').value),
    risk_per_trade: parseFloat(document.getElementById('bt-risk').value) / 100,
    mode,
    school_config: schoolConfig,
  };
  try {
    const startResp = await fetch('/api/backtest/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(req)
    });
    const { task_id } = await startResp.json();
    let attempts = 0;
    while (attempts < 120) {
      await new Promise(r => setTimeout(r, 1000));
      const statusResp = await fetch('/api/backtest/' + task_id + '/status');
      const status = await statusResp.json();
      document.getElementById('bt-status').textContent = '状态: ' + status.status;
      if (status.status === 'completed') {
        document.getElementById('bt-status').textContent = '完成! ' + status.trade_count + ' 笔交易';
        const resultResp = await fetch('/api/backtest/' + task_id + '/result');
        const result = await resultResp.json();
        showBacktestResults(result);
        break;
      } else if (status.status === 'failed') {
        document.getElementById('bt-status').textContent = '失败: ' + (status.error || '');
        break;
      }
      attempts++;
    }
  } catch (e) {
    document.getElementById('bt-status').textContent = 'Error: ' + e.message;
  }
  btn.disabled = false;
  btn.textContent = '▶ 运行回测';
}

function showBacktestResults(result) {
  const m = result.metrics || {};
  document.getElementById('btm-return').innerHTML = '<span class="' + (m.total_return >= 0 ? 'stat-up' : 'stat-down') + '">' + (m.total_return || 0).toFixed(2) + '%</span>';
  document.getElementById('btm-winrate').textContent = (m.win_rate || 0).toFixed(1) + '%';
  document.getElementById('btm-winrate').style.color = (m.win_rate || 0) >= 50 ? '#3fb950' : '#f85149';
  document.getElementById('btm-sharpe').textContent = (m.sharpe_ratio || 0).toFixed(2);
  document.getElementById('btm-drawdown').innerHTML = '<span class="stat-down">' + (m.max_drawdown || 0).toFixed(2) + '%</span>';
  document.getElementById('btm-trades').textContent = m.total_trades || 0;
  document.getElementById('btm-profit-factor').textContent = (m.profit_factor || 0).toFixed(2);
  document.getElementById('btm-profit-factor').style.color = (m.profit_factor || 0) >= 1.5 ? '#3fb950' : '#c9d1d9';
  document.getElementById('btm-avg-win').textContent = (m.avg_win || 0).toFixed(0);
  document.getElementById('btm-avg-win').style.color = '#f85149';
  document.getElementById('btm-avg-loss').textContent = (m.avg_loss || 0).toFixed(0);
  document.getElementById('btm-avg-loss').style.color = '#26a69a';
  const eq = result.equity_curve || [];
  if (!equityChart) equityChart = echarts.init(document.getElementById('equity-chart'));
  equityChart.setOption({
    backgroundColor: '#161b22', tooltip: { trigger: 'axis' },
    grid: { left: '12%', right: '5%', top: '5%', bottom: '5%' },
    xAxis: { type: 'category', data: eq.map(e => e.date || ''), axisLabel: { color: '#8b949e', fontSize: 10, rotate: 45 } },
    yAxis: { type: 'value', axisLabel: { color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } } },
    series: [{ name: '权益', type: 'line', data: eq.map(e => e.equity), smooth: true, lineStyle: { color: '#58a6ff' }, areaStyle: { color: 'rgba(88,166,255,0.1)' }, symbol: 'none' }]
  }, true);
  const tbody = document.querySelector('#bt-trades-table tbody');
  tbody.innerHTML = '';
  (result.trades || []).slice(-50).reverse().forEach(t => {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>' + (t.date || '') + '</td><td>' + t.symbol + '</td><td><span class="tag tag-' + (t.side === 'buy' ? 'buy' : 'sell') + '">' + (t.side === 'buy' ? '买入' : '卖出') + '</span></td><td>' + (t.price || 0).toFixed(3) + '</td><td>' + t.quantity + '</td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;">' + (t.reason || '') + '</td>';
    tbody.appendChild(tr);
  });
  document.getElementById('stat-equity').textContent = (m.final_equity || 1000000).toLocaleString();
  document.getElementById('stat-return').innerHTML = '<span class="' + ((m.total_return || 0) >= 0 ? 'stat-up' : 'stat-down') + '">' + (m.total_return || 0).toFixed(2) + '%</span>';
  document.getElementById('stat-sharpe').textContent = (m.sharpe_ratio || 0).toFixed(2);
  document.getElementById('stat-drawdown').innerHTML = '<span class="stat-down">' + (m.max_drawdown || 0).toFixed(2) + '%</span>';
  const sigDiv = document.getElementById('recent-signals');
  if (result.trades && result.trades.length > 0) {
    sigDiv.innerHTML = result.trades.slice(-8).reverse().map(t =>
      '<div style="padding:4px 0;border-bottom:1px solid #21262d;font-size:12px;">' +
      '<span class="tag tag-' + (t.side === 'buy' ? 'buy' : 'sell') + '" style="margin-right:8px;">' + (t.side === 'buy' ? '买' : '卖') + '</span>' +
      t.symbol + ' @ ' + (t.price || 0).toFixed(2) + ' | ' + (t.reason || '') + '</div>'
    ).join('');
  }
  // Store result for K-line trade marking
  window._lastBacktestResult = result;
  const klineRow = document.getElementById('bt-kline-row');
  const klineSymbol = document.getElementById('bt-kline-symbol');
  const tradedSymbols = [...new Set((result.trades || []).map(t => t.symbol))];
  klineSymbol.innerHTML = tradedSymbols.map(s => '<option value="' + s + '">' + s + '</option>').join('');
  if (tradedSymbols.length > 0) {
    klineRow.hidden = false;
    klineSymbol.value = tradedSymbols[0];
    showTradeKline();
  } else {
    klineRow.hidden = true;
  }
}

async function showTradeKline() {
  const result = window._lastBacktestResult;
  if (!result) return;
  const symbol = document.getElementById('bt-kline-symbol').value;
  if (!symbol) return;
  const trades = (result.trades || []).filter(t => t.symbol === symbol);
  if (trades.length === 0) return;

  const period = document.getElementById('bt-period').value;
  const start = document.getElementById('bt-start').value;
  const end = document.getElementById('bt-end').value || '';
  let url = '/api/data/stocks/' + symbol + '/kline?period=' + period + '&limit=500';
  if (start) url += '&start=' + start;
  if (end) url += '&end=' + end;

  try {
    const resp = await fetch(url);
    const data = await resp.json();
    if (!data.dates || data.dates.length === 0) return;

    const dates = data.dates.map(d => String(d).substring(0, 10));
    const ohlc = data.ohlc || [];
    const vols = data.volumes || [];

    // Build buy/sell mark data arrays
    const buyMarks = [];  // [[dateStr, price], ...]
    const sellMarks = [];
    const buyReasons = [];
    const sellReasons = [];

    trades.forEach(t => {
      const tradeDate = String(t.date || '').substring(0, 10);
      // Find matching K-line bar index
      const idx = dates.indexOf(tradeDate);
      if (idx >= 0) {
        const bar = ohlc[idx] || [];
        if (t.side === 'buy') {
          buyMarks.push({ name: '买入', coord: [tradeDate, bar[3] || t.price], value: t.reason || '' });
          buyReasons.push(t.reason || '');
        } else {
          sellMarks.push({ name: '卖出', coord: [tradeDate, bar[2] || t.price], value: t.reason || '' });
          sellReasons.push(t.reason || '');
        }
      }
    });

    // Build MA series from data
    const maSeries = [];
    const maColors = {ma_5:'#e6c300', ma_13:'#f0883e', ma_21:'#a371f7', ma_34:'#58a6ff', ma_55:'#3fb950', ma_144:'#f85149', ma_233:'#d2991d', ma_623:'#8b949e'};
    Object.entries(maColors).forEach(([key, color]) => {
      if (data[key]) {
        maSeries.push({ name: key.replace('_','').toUpperCase(), type: 'line', data: data[key], smooth: true, lineStyle: { color, width: 1 }, symbol: 'none' });
      }
    });

    // Dispose old instance
    if (btKlineChart) { btKlineChart.dispose(); btKlineChart = null; }

    const dom = document.getElementById('bt-kline-chart');
    btKlineChart = echarts.init(dom);
    btKlineChart.setOption({
      backgroundColor: '#161b22',
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      grid: [
        { left: '8%', right: '8%', top: '5%', height: '55%' },
        { left: '8%', right: '8%', top: '68%', height: '15%' }
      ],
      xAxis: [
        { type: 'category', data: dates, gridIndex: 0, axisLabel: { color: '#8b949e', fontSize: 10, rotate: 45 } },
        { type: 'category', data: dates, gridIndex: 1, axisLabel: { show: false } }
      ],
      yAxis: [
        { type: 'value', gridIndex: 0, axisLabel: { color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } } },
        { type: 'value', gridIndex: 1, axisLabel: { color: '#8b949e', fontSize: 9 } }
      ],
      series: [
        { name: symbol, type: 'candlestick', xAxisIndex: 0, yAxisIndex: 0,
          data: ohlc.map((d, i) => [d[0], d[2], d[1], d[3]]),
          itemStyle: { color: '#26a69a', color0: '#ef5350', borderColor: '#26a69a', borderColor0: '#ef5350' }
        },
        ...maSeries,
        { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
          data: vols, itemStyle: { color: function(p) {
            const o = ohlc[p.dataIndex]; return o && o[0] <= o[1] ? '#26a69a' : '#ef5350';
          } }
        },
        { name: '买入', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0,
          data: buyMarks, symbol: 'triangle', symbolSize: 16, symbolRotate: 0,
          itemStyle: { color: '#26a69a' },
          tooltip: { formatter: function(p) { return '买入 ' + symbol + ' @' + (p.data.coord ? p.data.coord[1] : ''); } }
        },
        { name: '卖出', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0,
          data: sellMarks, symbol: 'triangle', symbolSize: 16, symbolRotate: 180,
          itemStyle: { color: '#ef5350' },
          tooltip: { formatter: function(p) { return '卖出 ' + symbol + ' @' + (p.data.coord ? p.data.coord[1] : ''); } }
        }
      ]
    }, true);
  } catch (e) {
    console.error('showTradeKline error:', e);
  }
}

// ============ Init ============
checkHealth();
loadDashboard();
renderMaToggles();

window.addEventListener('resize', () => {
  if (klineChart) klineChart.resize();
  if (kdjChart) kdjChart.resize();
  if (tickChart) tickChart.resize();
  if (equityChart) equityChart.resize();
  if (btKlineChart) btKlineChart.resize();
  resizeAll();
});
