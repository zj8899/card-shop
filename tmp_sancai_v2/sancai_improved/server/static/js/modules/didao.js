// Didao — Strategy Screener with full history + deep DD support
import { api, poll } from '../core/api.js';
import { toast, escapeHtml } from '../core/dom.js';

const SL = { chan_theory:'缠论', strict:'BP1', strict_reverse:'追涨', simple:'KDJ',
  schools:'8流派', ict:'ICT', price_action:'价行', wyckoff:'威科夫',
  morphology:'形态', gann:'江恩', wave_theory:'波浪', dow_theory:'道氏' };

let _scanPollCancel = null, _lastResults = [], _ddCounts = {}, _freqData = {};
let _ddFilter = 'all';  // 'all' | 'dd' | 'no_dd'
let _selectedSyms = {}; // {symbol: true} for batch DD
let _sortKey = 'price'; // 默认按价格升序
let _sortDir = 1;       // 1=asc, -1=desc

export async function mount(container) {
  container.innerHTML =
'<div class="dash-hero"><h1>策略选股</h1><div class="dash-sub">多学派全市场扫描 · 历史记录 · 深度尽调</div></div>'+

'<div class="card" style="margin-bottom:10px;padding:10px 14px">'+
  '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'+
    '<span style="font-size:18px">股票代码</span>'+
    '<input id="ds-sym" maxlength="6" placeholder="000001" style="width:90px;font-size:13px;padding:5px 8px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-hairline);border-radius:6px" onkeydown="if(event.key==\'Enter\')window._dq()">'+
    '<button class="btn btn-sm btn-primary" onclick="window._dq()">查询</button>'+
    '<span id="ds-val" style="display:none;margin-left:12px;font-size:13px"></span>'+
  '</div>'+
  '<div id="ds-concepts" style="display:none;margin-top:6px;flex-wrap:wrap;gap:4px;align-items:center"></div>'+
'</div>'+

'<div class="card" style="margin-bottom:10px;padding:10px 14px">'+
  '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px">'+
    '<b style="font-size:13px">🎯 策略扫描</b>'+
    '<select id="ds-mode" style="font-size:13px;padding:4px 6px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-hairline);border-radius:6px">'+
      Object.entries(SL).map(function(e){return '<option value="'+e[0]+'">'+e[1]+'</option>';}).join('')+
    '</select>'+
    '<select id="ds-quality" style="font-size:13px;padding:4px 6px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-hairline);border-radius:6px">'+
      '<option value="medium">标准(≥2元)</option><option value="high">高质量(≥5元)</option><option value="all">全部</option>'+
    '</select>'+
    '<button id="ds-go" class="btn btn-sm btn-primary" onclick="window._ds()">▶ 扫描</button>'+
    '<button id="ds-cancel" class="btn btn-sm" style="display:none;color:#ff4d6a" onclick="window._dsc()">■ 中断</button>'+
    '<span id="ds-status" style="font-size:13px;color:var(--text-secondary)"></span>'+
    '<span style="margin-left:auto;display:flex;align-items:center;gap:8px">'+
      '<span id="sched-scan-panel" style="display:flex;align-items:center;gap:4px;flex-wrap:wrap"></span>'+
      '<label style="display:flex;align-items:center;gap:3px;font-size:15px;color:var(--text-secondary);border-left:1px solid var(--border-hairline);padding-left:8px;cursor:pointer">'+
        '<input type="checkbox" id="sched-scan-toggle" onchange="window._toggleSchedScan(this)" style="width:12px;height:12px">'+
        '<span id="sched-scan-status">定时</span>'+
      '</label>'+
    '</span>'+
    '<span style="display:flex;gap:6px">'+
      '<select id="ds-hist" style="font-size:18px;padding:4px 6px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-hairline);border-radius:6px;max-width:300px" onchange="window._dh()">'+
        '<option value="">📅 历史扫描(加载中…)</option></select>'+
      '<select id="ds-dd" style="font-size:18px;padding:4px 6px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-hairline);border-radius:6px;max-width:300px" onchange="window._ddd()">'+
        '<option value="">🔬 尽调(加载中…)</option></select>'+
    '</span>'+
  '</div>'+
  '<div id="ds-results" style="font-size:18px"></div>'+
  '<div id="ds-dd-panel" style="display:none;margin-top:8px"></div>'+

  // ── 集合竞价解读看板 ──
  '<div class="card" style="margin-top:12px;padding:12px 14px">'+
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'+
      '<b style="font-size:13px">🔔 竞价策略解读</b>'+
      '<button class="btn btn-xs btn-primary" onclick="window._auctionLoad()">刷新竞价</button>'+
    '</div>'+
    '<div id="auction-board" style="font-size:12px;color:var(--text-secondary)">点击刷新获取昨日扫描×今日竞价解读</div>'+
  '</div>'+
'</div>';

  // 历史记录+尽调异步加载 (不阻塞页面)
  _loadHist().catch(function(e){ console.warn('loadHist err:', e); });
  _loadDD().catch(function(e){ console.warn('loadDD err:', e); });
  _loadUserStrategies().catch(function(e){ console.warn('loadUserStrat err:', e); });

  // 兜底: 3秒后再试一次
  setTimeout(function() {
    var sel = document.getElementById('ds-dd');
    if (sel && sel.options && sel.options.length <= 1) {
      _loadDD().catch(function(){});
    }
  }, 3000);
}

// 切换回tab时刷新
export function onShow() {
  _loadHist().catch(function(){});
  _loadDD().catch(function(){});
  _loadUserStrategies().catch(function(){});  // 切回时刷新自定义策略，新建后无需刷新整页
}

// ── stock query ──
window._dq = async function() {
  var s = document.getElementById('ds-sym').value.trim();
  if (!/^\d{6}$/.test(s)) { toast('请输入6位代码', {type:'error'}); return; }
  var el = document.getElementById('ds-val');
  el.style.display = ''; el.textContent = '查询中…';
  try {
    var r = await api.get('/api/didao/valuation/'+s);
    var v = (r && r.data) || r || {};
    el.innerHTML = '<b>'+s+'</b> '+ (v.name||'') +
      ' PE:'+(v.pe_ttm||'—')+' PB:'+(v.pb||'—')+' 市值:'+((v.market_cap_yi||0)/1).toFixed(1)+'亿';
    _loadStockDD(s);
    _loadStockConcepts(s);
  } catch(e) { el.textContent = '查询失败'; }
};

// ── scan ──
window._ds = async function() {
  var mode = document.getElementById('ds-mode').value;
  var qual = document.getElementById('ds-quality').value;
  var mp = qual==='high'?'5':qual==='all'?'0':'2';
  var es = qual!=='all';
  document.getElementById('ds-go').style.display='none';
  document.getElementById('ds-cancel').style.display='';
  document.getElementById('ds-status').textContent='提交中…';

  var params = 'mode='+mode+'&buy_type=&min_price='+mp+'&exclude_st='+es;
  try {
    var f = await api.get('/api/didao/screener/strategy-scan?'+params);
    var tid = (f&&f.data&&f.data.task_id) || (f&&f.task_id);
    if (tid==='cached') { _renderResults((f&&f.data)||f); _done(); return; }
    var p = poll(
      function(){return api.get('/api/didao/screener/strategy-scan/status?task_id='+tid);},
      {intervalMs:2000,maxMs:180000,until:function(r){var s=(r&&r.data&&r.data.status)||(r&&r.status)||'';return ['done','error','cancelled'].indexOf(s)>=0;},
       onTick:function(r){var s=(r&&r.data&&r.status)||(r&&r.status)||'';document.getElementById('ds-status').textContent=s;}});
    _scanPollCancel = p.cancel.bind(p);
    var final = await p.promise;
    var st = (final&&final.data&&final.data.status)||(final&&final.status);
    if (st==='error') throw new Error((final&&final.data&&final.data.error)||'扫描失败');
    _renderResults((final&&final.data&&final.data.result)||(final&&final.result));
  } catch(e) { toast(e.message,{type:'error'}); }
  _done();
};

function _done() {
  _scanPollCancel=null;
  document.getElementById('ds-go').style.display='';
  document.getElementById('ds-cancel').style.display='none';
  document.getElementById('ds-status').textContent='完成';
  _loadHist();
}

window._dsc = function() { if(_scanPollCancel) _scanPollCancel(); _done(); };

async function _loadDdCounts(symbols) {
  if (!symbols || !symbols.length) return;
  try {
    var r = await api.get('/api/didao/deep-dd/counts?symbols='+symbols.slice(0,300).join(',')+'&days=30', {timeoutMs:15000});
    var counts = (((r||{}).data||{}).counts) || ((r||{}).counts) || {};
    Object.assign(_ddCounts, counts);
  } catch(e) {}
}

function _shortReason(r) {
  if (!r) return '';
  if (r.startsWith('BP1:')) return 'BP1 抄底';
  if (r.startsWith('追涨突破:')) return '追涨突破';
  if (r.startsWith('KDJ超卖')) return 'KDJ超卖';
  var m = r.match(/^(多流派共识买入\(\d+\/8\))/);
  if (m) return m[1];
  m = r.match(/^(.+?)[(:（]/);
  return m ? m[1] : r.slice(0, 14);
}

// 频次徽章：按模式拆分 tooltip + 多空方向标记
// BP1(strict)=空头排列抄底(底部) ; 追涨(strict_reverse)=多头排列趋势(向上)
var _BULLISH_MODES = { strict_reverse: 1 };
var _BEARISH_MODES = { strict: 1 };
function _freqBadge(f) {
  if (!f || !f.total_count) return '<span style="font-size:15px;color:var(--text-tertiary)">—</span>';
  var modes = f.modes || {};
  var keys = Object.keys(modes).sort(function(a, b) { return modes[b] - modes[a]; });
  var parts = keys.map(function(m) { return (SL[m] || m) + '×' + modes[m]; });
  var bull = 0, bear = 0;
  keys.forEach(function(m) {
    if (_BULLISH_MODES[m]) bull += modes[m];
    if (_BEARISH_MODES[m]) bear += modes[m];
  });
  var dir = bull > bear ? '▲' : bear > bull ? '▼' : '';
  var dirColor = bull > bear ? 'var(--market-up)' : bear > bull ? 'var(--brand-teal)' : 'var(--text-tertiary)';
  var tip = '最近出现: ' + (f.latest_date || '—')
    + (parts.length ? '\n' + parts.join(' · ') : '')
    + (dir ? '\n▲多头(追涨趋势)  ▼空头(BP1抄底)' : '');
  var color = f.total_count >= 3 ? 'var(--accent-danger)' : 'var(--text-tertiary)';
  return '<span title="' + tip + '" style="font-size:15px;color:' + color + '">🔥×' + f.total_count
    + (dir ? ' <span style="color:' + dirColor + '">' + dir + '</span>' : '') + '</span>';
}

async function _loadFrequencies(symbols) {
  if (!symbols || !symbols.length) return;
  try {
    var r = await api.get('/api/didao/screener/scan-frequency?symbols=' + symbols.slice(0,300).join(',') + '&days=10', {timeoutMs:15000});
    var f = (((r||{}).data||{}).frequencies) || ((r||{}).frequencies) || {};
    Object.assign(_freqData, f);
  } catch(e) { /* silent */ }
}

// ── DD filter ──
function _applyFilters() {
  _rerenderRows();
}

window._setDdFilter = function(f) {
  _ddFilter = (_ddFilter === f) ? 'all' : f;
  var btns = document.querySelectorAll('#ds-filter-bar .filter-btn');
  btns.forEach(function(b) {
    var v = b.getAttribute('data-f');
    b.style.background = (v === _ddFilter) ? 'var(--brand-cyan)' : 'var(--bg-sunken)';
    b.style.color = (v === _ddFilter) ? '#fff' : 'var(--text-secondary)';
  });
  _rerenderRows();
};

// ── Batch select ──
window._toggleAll = function(cb) {
  var cbs = document.querySelectorAll('#ds-rows-container .ds-cb');
  cbs.forEach(function(c) { c.checked = cb.checked; });
  _selectedSyms = {};
  if (cb.checked) {
    _lastResults.forEach(function(x) { _selectedSyms[x.symbol] = true; });
  }
  _updateBatchBtn();
};

window._toggleOne = function(sym, cb) {
  if (cb.checked) _selectedSyms[sym] = true; else delete _selectedSyms[sym];
  _updateBatchBtn();
};

function _updateBatchBtn() {
  var n = Object.keys(_selectedSyms).length;
  var btn = document.getElementById('ds-batch-dd');
  var selAll = document.getElementById('ds-sel-all');
  if (btn) {
    btn.textContent = n > 0 ? '🔬 一键尽调(' + n + ')' : '🔬 一键尽调';
    btn.style.display = n > 0 ? '' : 'none';
  }
  if (selAll) selAll.checked = (n > 0 && n === _lastResults.length);
}

window._batchDD = async function() {
  var selected = Object.keys(_selectedSyms);
  if (!selected.length) { toast('请先勾选股票', {type:'warn'}); return; }
  if (!confirm('将对选中的 ' + selected.length + ' 只股票进行 AI 深度尽调，预计耗时约 ' + (selected.length * 30) + ' 秒，是否继续？')) return;
  var panel = document.getElementById('ds-dd-panel');
  panel.style.display = '';
  for (var i = 0; i < selected.length; i++) {
    var sym = selected[i];
    var nm = '';
    for (var j = 0; j < _lastResults.length; j++) {
      if (_lastResults[j].symbol === sym) { nm = _lastResults[j].name || ''; break; }
    }
    panel.innerHTML = '<div style="padding:16px;color:var(--text-secondary);font-size:13px">⏳ 尽调进度: <b>' + (i+1) + '/' + selected.length + '</b> — ' + sym + ' ' + nm + '</div>';
    try {
      var r = await api.post('/api/ai/didao_deep_dd', { symbol: sym, name: nm }, { timeoutMs: 120000 });
    } catch(e) { /* continue next */ }
    if (i < selected.length - 1) await new Promise(function(resolve) { setTimeout(resolve, 2000); });
  }
  panel.innerHTML = '<div style="padding:16px;color:var(--brand-teal);font-size:13px">✅ 批量尽调完成: ' + selected.length + ' 只</div>';
  _loadDdCounts(selected);
  _selectedSyms = {};
  _updateBatchBtn();
  // refresh checkboxes
  var cbs = document.querySelectorAll('#ds-rows-container .ds-cb');
  cbs.forEach(function(c) { c.checked = false; });
  var sa = document.getElementById('ds-sel-all');
  if (sa) sa.checked = false;
  setTimeout(function() { _loadDD(); }, 3000);
};

// ── Sort ──
// Column config: [key, label, sortKey, textAlign]
var _SORT_COLS = [
  ['', '', null, ''],
  ['name', '名称', 'name', ''],
  ['symbol', '代码', 'symbol', ''],
  ['reason', '信号', 'reason', ''],
  ['price', '价格', 'price', 'right'],
  ['pct', '涨跌', 'change_pct', 'right'],
  ['amount', '成交额', 'amount_val', 'right'],
  ['turnover', '换手', 'turnover_rate', 'right'],
  ['vol5', '5日量', 'volume_ratio_5d', 'right'],
  ['freq', '频次', 'freq', 'center'],
  ['dd', '尽调', 'dd', 'center'],
  ['', '', null, ''],
];

function _sortVal(x, key) {
  switch (key) {
    case 'name': return (x.name || '').toLowerCase();
    case 'symbol': return x.symbol || '';
    case 'reason': return (_shortReason(x.reason||'').toLowerCase());
    case 'price': return x.price || 0;
    case 'change_pct': return x.change_pct || 0;
    case 'amount_val': return (x.amount_wan || 0) > 0 ? x.amount_wan : ((x.amount || 0) / 10000);
    case 'turnover_rate': return x.turnover_rate || 0;
    case 'volume_ratio_5d': return x.volume_ratio_5d || 0;
    case 'freq': var f = _freqData[x.symbol]; return f ? f.total_count : 0;
    case 'dd': return _ddCounts[x.symbol] || 0;
    default: return 0;
  }
}

window._sortBy = function(key) {
  if (_sortKey === key) { _sortDir = -_sortDir; }
  else { _sortKey = key; _sortDir = 1; }
  // Sort in place
  _lastResults.sort(function(a, b) {
    var va = _sortVal(a, _sortKey), vb = _sortVal(b, _sortKey);
    if (va < vb) return -_sortDir;
    if (va > vb) return _sortDir;
    return 0;
  });
  _renderSortHeaders();
  _rerenderRows();
};

function _sortArrow(key) {
  if (key !== _sortKey) return '';
  return _sortDir > 0 ? ' ▴' : ' ▾';
}

function _renderSortHeaders() {
  var header = document.getElementById('ds-col-header');
  if (!header) return;
  header.innerHTML = _SORT_COLS.map(function(c) {
    var k = c[2], align = c[3] ? 'text-align:'+c[3] : '';
    if (!k) return '<span style="width:'+(c[0]===''?'13px':'')+';flex-shrink:0"></span>';
    return '<span onclick="window._sortBy(\''+k+'\')" style="cursor:pointer;'+_colWidthStyle(c[0])+align+';flex-shrink:0;user-select:none;color:'+(k===_sortKey?'var(--brand-cyan)':'')+'">'+c[1]+'<span style="font-size:14px">'+_sortArrow(k)+'</span></span>';
  }).join('');
}

function _colWidthStyle(id) {
  var w = {'name':'78px','symbol':'56px','reason':'84px','price':'56px','pct':'50px',
    'amount':'60px','turnover':'52px','vol5':'52px','freq':'50px','dd':'48px'};
  return 'width:'+(w[id]||'48px')+';';
}

function _rerenderRows() {
  var container = document.getElementById('ds-rows-container');
  if (!container) return;
  var html = '';
  for (var i = 0; i < Math.min(_lastResults.length, 200); i++) {
    var x = _lastResults[i];
    if (!x) continue;
    var c = _ddCounts[x.symbol]||0;
    // DD filter
    if (_ddFilter === 'dd' && c === 0) continue;
    if (_ddFilter === 'no_dd' && c > 0) continue;
    var f = _freqData[x.symbol];
    var fcnt = f ? f.total_count : 0;
    var fdate = f ? f.latest_date : '';
    var freqHtml = _freqBadge(f);
    var ddHtml = c>0
      ? '<span style="font-size:15px;color:var(--brand-cyan);font-weight:'+(c>=3?'600':'400')+'">🔬 '+c+'</span>'
      : '<span style="font-size:15px;color:var(--text-tertiary)">—</span>';
    var reason = _shortReason(x.reason||'');
    var amt = (x.amount_wan || 0) > 0 ? x.amount_wan : ((x.amount || 0) / 10000);
    var amtStr = amt > 0 ? (amt >= 10000 ? (amt/10000).toFixed(1)+'亿' : amt.toFixed(0)+'万') : '—';
    var toRate = x.turnover_rate || 0;
    var trStr = toRate > 0 ? toRate.toFixed(1)+'%' : '—';
    var volR5 = x.volume_ratio_5d || 0;
    var vr5Str = volR5 > 0 ? volR5.toFixed(1)+'x' : '—';
    var vr5Color = volR5 >= 2 ? 'var(--accent-danger)' : volR5 >= 1.2 ? 'var(--market-up)' : 'var(--text-tertiary)';
    var escName = (x.name||'').replace(/'/g,"\\'").replace(/"/g,"&quot;");
    var safeName = escapeHtml(x.name||'');
    html +=
    '<div class="ds-row" data-sym="'+x.symbol+'" data-dd="'+c+'" style="display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--border-hairline);cursor:pointer;font-size:16px" onclick="document.getElementById(\'ds-sym\').value=\''+x.symbol+'\';window._dq()">'+
      '<input type="checkbox" class="ds-cb" style="width:13px;height:13px;flex-shrink:0;cursor:pointer;accent-color:var(--brand-cyan)" onclick="event.stopPropagation();window._toggleOne(\''+x.symbol+'\',this)" title="勾选尽调">'+
      '<span style="width:78px;overflow:hidden;white-space:nowrap;font-weight:600;flex-shrink:0" title="'+safeName+'">'+safeName+'</span>'+
      '<span style="width:56px;font-size:15px;color:var(--text-secondary);font-family:monospace;flex-shrink:0">'+x.symbol+'</span>'+
      '<span style="width:84px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:'+(reason.indexOf('抄底')>=0?'var(--brand-teal)':reason.indexOf('追涨')>=0?'var(--accent-danger)':'var(--brand-cyan)')+';flex-shrink:0;font-size:15px">'+reason+'</span>'+
      '<span style="width:56px;text-align:right;flex-shrink:0;color:'+((x.price||0)>0?'var(--market-up)':'var(--market-down)')+'">'+(x.price||0).toFixed(2)+'</span>'+
      '<span style="width:50px;text-align:right;flex-shrink:0;font-size:15px;color:'+((x.change_pct||0)>=0?'var(--market-up)':'var(--market-down)')+'">'+((x.change_pct||0)>=0?'+':'')+((x.change_pct||0)).toFixed(1)+'%</span>'+
      '<span style="width:60px;text-align:right;flex-shrink:0;font-size:15px;color:var(--text-secondary)" title="成交额">'+amtStr+'</span>'+
      '<span style="width:52px;text-align:right;flex-shrink:0;font-size:15px;color:'+(toRate>=5?'var(--accent-danger)':toRate>=1?'var(--market-up)':'var(--text-secondary)')+'" title="换手率">'+trStr+'</span>'+
      '<span style="width:52px;text-align:right;flex-shrink:0;font-size:15px;color:'+vr5Color+'" title="5日量比">'+vr5Str+'</span>'+
      '<span id="freq-'+x.symbol+'" style="width:50px;text-align:center;flex-shrink:0">'+freqHtml+'</span>'+
      '<span id="ddbadge-'+x.symbol+'" style="width:48px;text-align:center;flex-shrink:0">'+ddHtml+'</span>'+
      '<button class="btn btn-xs" style="flex-shrink:0;font-size:18px;padding:1px 4px" onclick="event.stopPropagation();window._genDD(\''+x.symbol+'\',\''+escName+'\')" title="生成尽调">🔬</button>'+
    '</div>';
  }
  container.innerHTML = html || '<div style="padding:20px;text-align:center;color:var(--text-secondary);font-size:18px">无匹配个股</div>';
  var cnt = document.getElementById('ds-filter-count');
  if (cnt) cnt.textContent = '扫描 ' + (_lastResults.length > 0 ? _lastResults.length : '—') + ' 只 · 匹配 ' + _lastResults.length + ' 只';
}

function _renderResults(r) {
  _lastResults = (r&&r.results)||[];
  _freqData = {}; _ddFilter = 'all'; _selectedSyms = {};
  var el = document.getElementById('ds-results');
  if (!_lastResults.length) { el.innerHTML='<div style="padding:20px;text-align:center;color:var(--text-secondary)">无匹配个股</div>'; return; }
  // 异步加载尽调计数 + 频次
  _loadDdCounts(_lastResults.slice(0,200).map(function(x){return x.symbol;}));
  _loadFrequencies(_lastResults.slice(0,200).map(function(x){return x.symbol;}));
  _renderTable(el, r);
}

function _renderTable(el, r) {
  // Build initial rows HTML
  var rowsHtml = '';
  for (var i=0; i<Math.min(_lastResults.length,200); i++) {
    var x = _lastResults[i];
    var c = _ddCounts[x.symbol]||0;
    var f = _freqData[x.symbol];
    var fcnt = f ? f.total_count : 0;
    var fdate = f ? f.latest_date : '';
    var freqHtml = _freqBadge(f);
    var ddHtml = c>0
      ? '<span style="font-size:15px;color:var(--brand-cyan);font-weight:'+(c>=3?'600':'400')+'">🔬 '+c+'</span>'
      : '<span style="font-size:15px;color:var(--text-tertiary)">—</span>';
    var reason = _shortReason(x.reason||'');
    var amt = (x.amount_wan || 0) > 0 ? x.amount_wan : ((x.amount || 0) / 10000);
    var amtStr = amt > 0 ? (amt >= 10000 ? (amt/10000).toFixed(1)+'亿' : amt.toFixed(0)+'万') : '—';
    var toRate = x.turnover_rate || 0;
    var trStr = toRate > 0 ? toRate.toFixed(1)+'%' : '—';
    var volR5 = x.volume_ratio_5d || 0;
    var vr5Str = volR5 > 0 ? volR5.toFixed(1)+'x' : '—';
    var vr5Color = volR5 >= 2 ? 'var(--accent-danger)' : volR5 >= 1.2 ? 'var(--market-up)' : 'var(--text-tertiary)';
    var escName = (x.name||'').replace(/'/g,"\\'").replace(/"/g,"&quot;");
    var safeName = escapeHtml(x.name||'');
    rowsHtml +=
    '<div class="ds-row" data-sym="'+x.symbol+'" data-dd="'+c+'" style="display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--border-hairline);cursor:pointer;font-size:16px" onclick="document.getElementById(\'ds-sym\').value=\''+x.symbol+'\';window._dq()">'+
      '<input type="checkbox" class="ds-cb" style="width:13px;height:13px;flex-shrink:0;cursor:pointer;accent-color:var(--brand-cyan)" onclick="event.stopPropagation();window._toggleOne(\''+x.symbol+'\',this)" title="勾选尽调">'+
      '<span style="width:78px;overflow:hidden;white-space:nowrap;font-weight:600;flex-shrink:0" title="'+safeName+'">'+safeName+'</span>'+
      '<span style="width:56px;font-size:15px;color:var(--text-secondary);font-family:monospace;flex-shrink:0">'+x.symbol+'</span>'+
      '<span style="width:84px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:'+(reason.indexOf('抄底')>=0?'var(--brand-teal)':reason.indexOf('追涨')>=0?'var(--accent-danger)':'var(--brand-cyan)')+';flex-shrink:0;font-size:15px">'+reason+'</span>'+
      '<span style="width:56px;text-align:right;flex-shrink:0;color:'+((x.price||0)>0?'var(--market-up)':'var(--market-down)')+'">'+(x.price||0).toFixed(2)+'</span>'+
      '<span style="width:50px;text-align:right;flex-shrink:0;font-size:15px;color:'+((x.change_pct||0)>=0?'var(--market-up)':'var(--market-down)')+'">'+((x.change_pct||0)>=0?'+':'')+((x.change_pct||0)).toFixed(1)+'%</span>'+
      '<span style="width:60px;text-align:right;flex-shrink:0;font-size:15px;color:var(--text-secondary)" title="成交额">'+amtStr+'</span>'+
      '<span style="width:52px;text-align:right;flex-shrink:0;font-size:15px;color:'+(toRate>=5?'var(--accent-danger)':toRate>=1?'var(--market-up)':'var(--text-secondary)')+'" title="换手率">'+trStr+'</span>'+
      '<span style="width:52px;text-align:right;flex-shrink:0;font-size:15px;color:'+vr5Color+'" title="5日量比">'+vr5Str+'</span>'+
      '<span id="freq-'+x.symbol+'" style="width:50px;text-align:center;flex-shrink:0">'+freqHtml+'</span>'+
      '<span id="ddbadge-'+x.symbol+'" style="width:48px;text-align:center;flex-shrink:0">'+ddHtml+'</span>'+
      '<button class="btn btn-xs" style="flex-shrink:0;font-size:18px;padding:1px 4px" onclick="event.stopPropagation();window._genDD(\''+x.symbol+'\',\''+escName+'\')" title="生成尽调">🔬</button>'+
    '</div>';
  }

  // Build sortable header HTML
  var headerHtml = _SORT_COLS.map(function(c) {
    var k = c[2], align = c[3] ? 'text-align:'+c[3] : '';
    if (!k) return '<span style="width:'+(c[0]===''?'13px':'')+';flex-shrink:0"></span>';
    return '<span onclick="window._sortBy(\''+k+'\')" style="cursor:pointer;'+_colWidthStyle(c[0])+align+';flex-shrink:0;user-select:none;color:'+(k===_sortKey?'var(--brand-cyan)':'var(--text-tertiary)')+'">'+c[1]+'<span style="font-size:14px">'+_sortArrow(k)+'</span></span>';
  }).join('');

  el.innerHTML =
    '<div id="ds-filter-bar" style="display:flex;align-items:center;gap:6px;padding:4px 6px;margin-bottom:2px;font-size:15px;border-bottom:1px solid var(--border-hairline);overflow-x:auto">'+
      '<input type="checkbox" id="ds-sel-all" style="width:13px;height:13px;cursor:pointer;accent-color:var(--brand-cyan)" onchange="window._toggleAll(this)" title="全选/取消全选">'+
      '<button class="filter-btn" data-f="no_dd" onclick="window._setDdFilter(\'no_dd\')" style="font-size:15px;padding:2px 6px;border:none;border-radius:4px;background:var(--bg-sunken);color:var(--text-secondary);cursor:pointer">⬜ 未尽调</button>'+
      '<button class="filter-btn" data-f="dd" onclick="window._setDdFilter(\'dd\')" style="font-size:15px;padding:2px 6px;border:none;border-radius:4px;background:var(--bg-sunken);color:var(--text-secondary);cursor:pointer">🔬 已尽调</button>'+
      '<button id="ds-batch-dd" class="btn btn-xs btn-primary" style="display:none;font-size:15px;padding:2px 8px" onclick="event.stopPropagation();window._batchDD()">🔬 一键尽调</button>'+
      '<span id="ds-filter-count" style="margin-left:auto;font-size:15px;color:var(--text-tertiary);white-space:nowrap">扫描 '+(r.scanned||'-')+' 只 · 匹配 '+_lastResults.length+' 只</span>'+
    '</div>'+
    '<div id="ds-col-header" style="display:flex;align-items:center;gap:10px;padding:6px 12px;font-size:15px;font-weight:600;border-bottom:1px solid var(--border-hairline);overflow-x:auto">'+
      headerHtml +
    '</div>'+
    '<div id="ds-rows-container" style="max-height:500px;overflow:auto">'+rowsHtml+'</div>';
}

// ── scan history ──
async function _loadUserStrategies() {
  // 把自定义策略追加到策略扫描下拉（内置 SL 保留在前）。mode=user_xxx，scanner 已支持。
  var sel = document.getElementById('ds-mode');
  if (!sel) return;
  try {
    var r = await api.get('/api/strategies/list', {timeoutMs:15000});
    var data = (r && r.data) || r || {};
    var users = data.user || [];
    // 先清掉上一轮追加的 user option（避免 onShow 重复），标记用 data-user
    Array.prototype.slice.call(sel.querySelectorAll('option[data-user="1"]')).forEach(function(o){ o.remove(); });
    users.forEach(function(s){
      var id = s.id || s.name;
      var opt = document.createElement('option');
      opt.value = id;
      opt.textContent = '⭐ ' + (s.name || id);
      opt.setAttribute('data-user', '1');
      sel.appendChild(opt);
    });
  } catch(e) { /* non-fatal */ }
}

async function _loadHist() {
  var sel = document.getElementById('ds-hist');
  try {
    var r = await api.get('/api/didao/screener/scan-history?days=60', {timeoutMs:20000});
    // API: {status:"ok", data:{history:[{date, modes:[{mode,matched,scanned}]}]}}
    var raw = (((r||{}).data||{}).history) || ((r||{}).history) || [];
    var flat = [];
    raw.forEach(function(d){ (d.modes||[]).forEach(function(m){ flat.push({date:d.date, mode:m.mode, matched:m.matched||0}); }); });
    var opts = '<option value="">📅 历史扫描 ('+flat.length+'条)</option>';
    flat.forEach(function(x){ opts += '<option value="'+x.date+'|'+x.mode+'">'+x.date+' · '+(SL[x.mode]||x.mode)+' ('+x.matched+'只)</option>'; });
    if (sel) sel.innerHTML = opts;
  } catch(e) { if (sel) sel.innerHTML = '<option value="">📅 加载失败</option>'; }
}

window._dh = async function() {
  var v = (document.getElementById('ds-hist').value||'');
  if (!v) { document.getElementById('ds-results').innerHTML=''; return; }
  var p = v.split('|');
  try {
    var r = await api.get('/api/didao/screener/scan-history/'+p[0]+'/'+p[1]);
    var d = (r&&r.data)||r||{};
    if (d.results) _renderResults(d);
  } catch(e) { toast(e.message,{type:'error'}); }
};

// ── deep DD ──
async function _loadDD() {
  var sel = document.getElementById('ds-dd');
  try {
    var r = await api.get('/api/didao/deep-dd/history', {timeoutMs:30000});
    var files = (((r||{}).data||{}).files) || ((r||{}).files) || [];
    var opts = '<option value="">🔬 尽调 ('+files.length+'个)</option>';
    files.forEach(function(f){ opts += '<option value="'+f.filename+'">'+(f.date_str||'')+' · '+(SL[f.mode]||f.mode)+' ('+(f.stocks||0)+'只)</option>'; });
    if (sel) sel.innerHTML = opts;
  } catch(e) { if (sel) sel.innerHTML = '<option value="">🔬 加载失败</option>'; }
}

window._ddd = async function() {
  var fn = (document.getElementById('ds-dd').value||'');
  if (!fn) { document.getElementById('ds-dd-panel').style.display='none'; return; }
  var panel = document.getElementById('ds-dd-panel');
  panel.style.display='';
  panel.innerHTML = '<div style="padding:20px;color:var(--text-secondary);font-size:13px">⏳ 加载尽调…</div>';
  try {
    var r = await api.get('/api/didao/deep-dd/load/'+encodeURIComponent(fn), {timeoutMs:30000});
    var d = (r&&r.data)||r||{};
    var stocks = d.stocks||d.results||[];
    var h = '<div style="font-weight:600;font-size:14px;margin-bottom:8px">🔬 尽调报告: '+fn+'</div>';
    if (d.comparison||d.summary) h += '<div style="background:var(--bg-sunken);padding:10px;border-radius:6px;margin-bottom:8px;font-size:18px;line-height:1.6;white-space:pre-wrap;max-height:300px;overflow-y:auto">'+(d.comparison||d.summary||'')+'</div>';
    h += '<div style="font-size:18px;color:var(--text-secondary);margin-bottom:4px">共 '+stocks.length+' 只标的</div>';
    stocks.slice(0,50).forEach(function(s){
      h += '<details style="margin-bottom:4px;border:1px solid var(--border-hairline);border-radius:6px;padding:6px 10px;background:var(--bg-sunken)">'+
        '<summary style="font-size:13px;cursor:pointer"><b>'+(s.symbol||'')+'</b> '+(s.name||'')+'</summary>'+
        '<div style="margin-top:4px;font-size:18px;line-height:1.6;white-space:pre-wrap">'+(s.analysis||s.content||s.text||'暂无')+'</div></details>';
    });
    panel.innerHTML = h;
  } catch(e) { panel.innerHTML = '<div style="color:#ff4d6a;font-size:13px;padding:20px">加载失败: '+e.message+'</div>'; }
};

// ── per-stock DD ──
async function _loadStockDD(sym) {
  try {
    var r = await api.get('/api/didao/deep-dd/stock/'+sym, {timeoutMs:15000});
    var items = (((r||{}).data||{}).items) || ((r||{}).items) || [];
    var el = document.getElementById('ds-val');
    if (items.length && el) {
      el.innerHTML += ' | <span style="color:var(--brand-teal)">尽调:'+items.length+'次</span>';
    }
    var panel = document.getElementById('ds-dd-panel');
    if (items.length && panel) {
      panel.style.display='';
      panel.innerHTML = '<div style="font-size:13px;font-weight:600;margin-bottom:4px">🔬 '+sym+' 尽调历史 ('+items.length+'次)</div>'+
        items.map(function(h){ return '<div style="margin-bottom:2px;font-size:18px">📅 '+(h.date||'')+' · '+(SL[h.mode]||h.mode||'')+
          ' · <a href="#" style="color:var(--brand-cyan)" onclick="document.getElementById(\'ds-dd\').value=\''+(h.filename||'').replace(/'/g,'\\\'')+'\';window._ddd();return false">打开报告</a></div>'; }).join('');
    }
  } catch(e) {}
}

// ── stock concepts ──
async function _loadStockConcepts(sym) {
  var el = document.getElementById('ds-concepts');
  if (!el) return;
  try {
    var r = await api.get('/api/data/stocks/' + sym + '/concepts');
    var concepts = ((r && r.data && r.data.concepts) || (r && r.concepts) || []);
    if (!concepts.length) { el.style.display = 'none'; return; }
    var chips = '<span style="font-size:16px;color:var(--text-secondary);margin-right:4px">概念:</span>';
    concepts.forEach(function(c) {
      chips += '<span class="chip" style="cursor:default">' + c + '</span>';
    });
    el.innerHTML = chips;
    el.style.display = 'flex';
  } catch(e) { el.style.display = 'none'; }
}

// ── Scheduled scan toggle ──
(function() {
  var _scheduledEnabled = true;
  var _scheduledSlots = [];

  function _loadScheduledConfig() {
    try {
      var saved = localStorage.getItem('_afternoonScanEnabled');
      if (saved !== null) _scheduledEnabled = (saved === 'true');
    } catch(e) {}

    api.get('/api/admin/config', {timeoutMs:10000}).then(function(r) {
      var ssc = ((r||{}).data||{}).scheduled_scans || (r||{}).scheduled_scans || {};
      _scheduledEnabled = ssc.enabled !== false;
      _scheduledSlots = ssc.slots || [];
      var cb = document.getElementById('sched-scan-toggle');
      if (cb) cb.checked = _scheduledEnabled;
      _updateStatus();
      _renderSlots();
    }).catch(function(e) {
      console.warn('Load scheduled config failed:', e);
      var cb = document.getElementById('sched-scan-toggle');
      if (cb) cb.checked = _scheduledEnabled;
      _updateStatus();
    });
  }

  function _renderSlots() {
    var panel = document.getElementById('sched-scan-panel');
    if (!panel) return;
    if (!_scheduledSlots.length) {
      panel.innerHTML = '<span style="font-size:15px;color:var(--text-tertiary)">⏰ 未配置</span>';
      return;
    }
    panel.innerHTML = _scheduledSlots.map(function(s, i) {
      var icon = s.time < '12:00' ? '🌅' : '🌇';
      var color = s.enabled ? 'var(--brand-teal)' : 'var(--text-tertiary)';
      return '<label title="'+(s.label||s.mode)+'" style="cursor:pointer;display:flex;align-items:center;gap:2px;padding:2px 6px;border-radius:4px;border:1px solid '+(s.enabled?'var(--brand-teal)':'var(--border-hairline)')+';margin:2px;font-size:15px">'+
        icon+' <span style="font-size:15px">'+s.time+'</span> <span style="color:'+color+';font-size:14px">'+(s.label||s.mode).slice(0,6)+'</span>'+
        '<input type="checkbox" '+(s.enabled?'checked':'')+' onchange="window._toggleSchedSlot('+i+',this)" style="width:10px;height:10px;cursor:pointer">'+
      '</label>';
    }).join('');
  }

  function _updateStatus() {
    var el = document.getElementById('sched-scan-status');
    if (el) {
      el.textContent = _scheduledEnabled ? 'ON' : 'OFF';
      el.style.color = _scheduledEnabled ? 'var(--brand-teal)' : 'var(--accent-danger)';
    }
  }

  window._toggleSchedSlot = function(index, cb) {
    fetch('/api/admin/config/scheduled-slot', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ slot_index: index, enabled: !!cb.checked }),
    }).then(function(r){ return r.json(); }).then(function(){ _renderSlots(); })
    .catch(function(e){ console.warn('Toggle slot failed:', e); cb.checked = !cb.checked; });
  };

  window._toggleSchedScan = function(cb) {
    _scheduledEnabled = !!cb.checked;
    try { localStorage.setItem('_afternoonScanEnabled', _scheduledEnabled); } catch(e) {}
    _updateStatus();
    fetch('/api/admin/config/afternoon-scan', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ enabled: _scheduledEnabled }),
    }).catch(function(e){ console.warn('Sync toggle failed:', e); });
  };

  setTimeout(_loadScheduledConfig, 3000);

  // ── AI 尽调生成 ──
  window._genDD = async function(sym, name) {
    var panel = document.getElementById('ds-dd-panel');
    panel.style.display = '';
    panel.innerHTML = '<div style="padding:20px;color:var(--text-secondary);font-size:13px">⏳ AI 正在对 <b>'+sym+' '+name+'</b> 进行 5 阶段深度尽调…<br><small>价格辩证 → BOM供应链 → 财务穿透 → 红队证伪 → 动态熔断</small></div>';

    try {
      var r = await api.post('/api/ai/didao_deep_dd', { symbol: sym, name: name }, { timeoutMs: 120000 });
      var report = (r && r.report) || '';
      if (!report) throw new Error('AI 未返回报告');

      // 将 Markdown 报告渲染为 HTML
      var html = report
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
        .replace(/\n### (.+)/g, '\n<h4 style="margin:12px 0 6px;color:var(--brand-cyan);font-size:14px">$1</h4>')
        .replace(/\n## (.+)/g, '\n<h3 style="margin:14px 0 8px;color:var(--brand-teal);font-size:15px">$1</h3>')
        .replace(/\n# (.+)/g, '\n<h2 style="margin:16px 0 10px;color:var(--text-primary);font-size:16px">$1</h2>')
        .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
        .replace(/\n- (.+)/g, '\n<li>$1</li>')
        .replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>')
        .replace(/\n\n/g, '<br><br>')
        .replace(/\n/g, '<br>');

      var source = (r && r.source) || '';
      panel.innerHTML = '<div style="font-weight:600;font-size:14px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center">'+
        '<span>🔬 '+sym+' '+name+' 尽调报告</span>'+
        '<span style="font-size:16px;color:var(--text-tertiary)">'+source+' 生成</span></div>'+
        '<div style="max-height:600px;overflow-y:auto;font-size:13px;line-height:1.7;padding:4px 0">'+html+'</div>'+
        '<div style="margin-top:8px;font-size:16px;color:var(--text-tertiary)"><a href="#" onclick="document.getElementById(\'ds-dd\').value=\'\';window._loadDD()" style="color:var(--brand-cyan)">← 返回尽调列表</a></div>';

      // 刷新尽调计数
      setTimeout(function(){ _loadStockDD(sym); }, 2000);
    } catch(e) {
      panel.innerHTML = '<div style="padding:20px;color:var(--accent-danger);font-size:13px">❌ 尽调生成失败: '+(e.message||'未知错误')+'</div>';
    }
  };
})();

// ══════════════════════════════════════════════════════════════
// 集合竞价解读
// ══════════════════════════════════════════════════════════════

window._auctionLoad = async function() {
  var el = document.getElementById('auction-board');
  if (!el) return;
  el.innerHTML = '<span style="color:var(--text-tertiary)">加载中…</span>';
  try {
    var r = await api.get('/api/didao/screener/auction-board', {timeoutMs:45000});
    var data = (r && r.data) || r || {};
    var boards = data.boards || [];
    if (!boards.length) {
      el.innerHTML = '<span style="color:var(--text-tertiary)">无昨日扫描数据，先跑一次策略扫描</span>';
      return;
    }
    var html = '<div style="font-size:11px;color:var(--text-tertiary);margin-bottom:6px">';
    html += '📅 扫描日期: ' + (data.scan_date || '?') + ' | 竞价日期: ' + (data.date || '?');
    html += ' | 数据源: ' + (data.data_source || '?') + '</div>';
    for (var b = 0; b < boards.length; b++) {
      var board = boards[b];
      html += '<div style="border:1px solid var(--border-hairline);border-radius:6px;padding:10px;margin-bottom:8px;background:var(--bg-input)">';
      html += '<b style="font-size:13px">' + (board.label || board.mode) + '</b>';
      html += ' <span style="font-size:11px;color:var(--text-tertiary)">' + board.count + '只选中</span>';
      html += ' | ✅<span style="color:var(--brand-teal)">' + (board.confirmed || 0) + '</span>';
      html += ' ❌<span style="color:var(--text-danger)">' + (board.denied || 0) + '</span>';
      html += ' <span style="font-size:10px;color:var(--text-tertiary)">' + (board.data_quality || '') + '</span>';
      var ints = board.interpretations || [];
      if (ints.length) {
        html += '<div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px">';
        for (var i = 0; i < ints.length; i++) {
          var x = ints[i];
          var cls = x.verdict === 'confirmed' ? 'color:var(--brand-teal);' : (x.verdict === 'denied' ? 'color:var(--text-danger);' : 'color:var(--text-secondary);');
          var sym = x.symbol || '';
          html += '<div style="font-size:11px;padding:4px 8px;border-radius:4px;background:var(--bg-sunken);border:1px solid var(--border-hairline);min-width:160px">';
          html += '<b>' + sym + '</b> ' + (x.name || '') + ' 价' + (x.price||'-') + ' 开' + (x.gap_pct>0?'+':'') + (x.gap_pct||0).toFixed(1) + '%';
          html += '<div style="' + cls + ';font-size:10px">' + (x.note || '') + '</div>';
          html += '</div>';
        }
        html += '</div>';
      }
      html += '</div>';
    }
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<span style="color:var(--text-danger)">加载失败: ' + (e.message||e) + '</span>';
  }
};
