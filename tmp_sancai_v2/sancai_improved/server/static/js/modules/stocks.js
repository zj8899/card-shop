// Data management module — V2 feature-complete rewrite for V3 shell
import { api, poll } from '../core/api.js';
import { toast, escapeHtml } from '../core/dom.js';

const PERIODS = ['1min','5min','15min','30min','60min','daily','weekly','monthly'];
const PLABELS = {'1min':'1分钟','5min':'5分钟','15min':'15分钟','30min':'30分钟','60min':'60分钟','daily':'日线','weekly':'周线','monthly':'月线'};
const DPERIODS = [['daily','日线'],['60min','60分钟'],['30min','30分钟'],['15min','15分钟'],['5min','5分钟'],['1min','1分钟']];

let _allStocks = [];

export async function mount(container) {
  container.innerHTML = buildHTML();
  await Promise.all([loadStocks(), loadConcepts()]);
}

export function onShow() { loadStocks(); }

// Expose to onclick handlers in innerHTML
window._stocksGoChart = function(sym) {
  var input = document.querySelector('[id$="-symbol"]');
  if (input) input.value = sym;
  // Try chart module's own symbol setter
  if (typeof window._setChartSymbol === 'function') window._setChartSymbol(sym);
  // Navigate to chart tab
  var rail = document.querySelector('.rail-item[data-route="chart"]');
  if (rail) rail.click();
};

window._stocksTypeChange = function() {
  loadConcepts();
  loadStocks();
};

window._stocksConceptChange = function() { loadStocks(); };

window._stocksSearch = function() {
  var q = (document.getElementById('stocks-search') || {}).value || '';
  q = q.toLowerCase().trim();
  var filtered = _allStocks;
  if (q) filtered = _allStocks.filter(function(s) {
    return s.symbol.indexOf(q) >= 0 ||
           (s.name || '').toLowerCase().indexOf(q) >= 0 ||
           (s.pinyin || '').toLowerCase().indexOf(q) >= 0;
  });
  renderTable(filtered);
};

window._stocksDownload = async function() {
  var period = (document.getElementById('dl-period') || {}).value || 'daily';
  var statusEl = document.getElementById('dl-status');
  var btn = document.getElementById('dl-btn');
  if (btn) { btn.disabled = true; btn.textContent = '更新中…'; }
  try {
    var res = await api.post('/api/data/download', {period: period, max_symbols: 0}, {timeoutMs: 30000});
    if (res && res.status === 'busy') {
      toast('已有下载任务运行中', {type:'warn'});
      if (btn) { btn.disabled = false; btn.textContent = '▶ 补齐缺口'; }
      return;
    }
    var taskId = (res && res.task_id) || (res && res.data && res.data.task_id);
    if (taskId) {
      var p = poll(function() { return api.get('/api/data/download/status?task_id=' + taskId); },
        {intervalMs: 3000, maxMs: 7200000, until: function(r) { return !(r && r.running); },
         onTick: function(r) { if (statusEl) statusEl.textContent = (r && r.message) || ''; }});
      try {
        await p.promise;
      } catch(e) {
        // poll timeout/cancel — still show result from last status
      }
    }
    // Read final status for accurate result
    var finalStatus = await api.get('/api/data/download/status').catch(function(){ return {}; });
    var updated = (finalStatus && finalStatus.updated) || 0;
    var bars = (finalStatus && finalStatus.bars_added) || 0;
    if (statusEl) statusEl.textContent = '更新完成: ' + updated + ' 只有缺口/已补齐, ' + bars + ' 条K线';
    toast('更新完成: ' + updated + ' 只更新, ' + bars + ' 条K线', {type:'success'});
    loadStocks();
  } catch(e) { toast((e && e.message) || '更新失败', {type:'error'}); }
  if (btn) { btn.disabled = false; btn.textContent = '▶ 补齐缺口'; }
};

window._stocksZipUpload = function(file) {
  if (!file) return;
  var statusEl = document.getElementById('zip-status');
  if (statusEl) statusEl.textContent = '上传中…';
  var fd = new FormData();
  fd.append('file', file);
  fetch('/api/data/import/zip', {method:'POST', body:fd}).then(function(r) { return r.json(); }).then(async function(res) {
    var taskId = (res && res.task_id) || (res && res.data && res.data.task_id);
    if (taskId) {
      var p = poll(function() { return api.get('/api/data/import/status?task_id=' + taskId); },
        {intervalMs: 2000, maxMs: 1800000,
         until: function(r) { return (r && r.status) === 'done' || (r && r.status) === 'error'; },
         onTick: function(r) { if (statusEl) statusEl.textContent = (r && r.message) || (r && r.status) || ''; }});
      await p.promise;
    }
    if (statusEl) statusEl.textContent = '导入完成';
    toast('导入完成', {type:'success'});
    loadStocks();
  }).catch(function(e) {
    if (statusEl) statusEl.textContent = '';
    toast((e && e.message) || '导入失败', {type:'error'});
  });
};

window._stocksRefresh = function() { loadStocks(); };

window._stocksDelete = async function(symbol, name) {
  if (!confirm('确认删除 ' + symbol + ' ' + (name||'') + ' 的所有数据文件？\n\n包括每日K线、分钟数据及扫描历史记录。')) return;
  try {
    var resp = await fetch('/api/data/stocks/' + symbol, {method:'DELETE'});
    var d = await resp.json();
    if (d.status === 'ok') { toast('已删除'); loadStocks(); }
    else toast(d.message || '删除失败', {type:'error'});
  } catch(e) { toast((e && e.message) || '删除失败', {type:'error'}); }
};

// ── Data loaders ──

async function loadStocks() {
  var concept = (document.getElementById('stocks-concept-filter') || {}).value || '';
  var url = '/api/data/stocks' + (concept ? '?concept=' + encodeURIComponent(concept) : '');
  try {
    var res = await api.get(url);
    _allStocks = (res && res.stocks) || [];
    window._stocksSearch();
  } catch(e) { _allStocks = []; window._stocksSearch(); }
}

async function loadConcepts() {
  try {
    var res = await api.get('/api/data/concepts', {timeoutMs: 8000});
    var concepts = (res && res.concepts) || [];
    var typeSel = document.getElementById('stocks-type-filter');
    var nameSel = document.getElementById('stocks-concept-filter');
    var type = typeSel ? typeSel.value : '';
    if (nameSel) {
      nameSel.innerHTML = '<option value="">全部股票</option>' +
        concepts.filter(function(c) { return !type || c.type === type; }).slice(0, 200).map(function(c) {
          return '<option value="' + (c.name||'') + '">' + (c.name||'') + ' (' + (c.stock_count||0) + ')</option>';
        }).join('');
    }
  } catch(e) { /* concept index may not exist yet */ }
}

// ── Render helpers ──

function fmtCell(st, period) {
  var b = st[period + '_bars'] || 0;
  if (b === 0) return '<span style="color:var(--text-tertiary)">—</span>';
  if (b >= 1048576) return '<span style="color:#00e2a8">' + (b / 1048576).toFixed(1) + 'M</span>';
  if (b >= 1024) return '<span style="color:#00e2a8">' + Math.round(b / 1024) + 'K</span>';
  return '<span style="color:#00e2a8">' + b + 'B</span>';
}

function fmtTotal(b) {
  if (!b || b === 0) return '<span style="color:var(--text-tertiary)">—</span>';
  if (b >= 1073741824) return (b / 1073741824).toFixed(1) + 'G';
  if (b >= 1048576) return (b / 1048576).toFixed(1) + 'M';
  if (b >= 1024) return Math.round(b / 1024) + 'K';
  return b + 'B';
}

function renderTable(stocks) {
  var countEl = document.getElementById('stock-count');
  var bodyEl = document.getElementById('stock-tbody');
  if (countEl) countEl.textContent = '共 ' + stocks.length + ' 只';
  if (!bodyEl) return;
  if (!stocks.length) {
    bodyEl.innerHTML = '<tr><td colspan="12" style="text-align:center;padding:24px;color:var(--text-secondary)">无匹配股票</td></tr>';
    return;
  }
  bodyEl.innerHTML = stocks.slice(0, 500).map(function(s) {
    var cells = '';
    cells += '<td style="font-weight:600;cursor:pointer" onclick="window._stocksGoChart(\'' + s.symbol + '\')">' + s.symbol + '</td>';
    cells += '<td style="max-width:100px;overflow:hidden;white-space:nowrap">' + escapeHtml(s.name || '') + '</td>';
    for (var i = 0; i < PERIODS.length; i++) cells += '<td style="text-align:center">' + fmtCell(s, PERIODS[i]) + '</td>';
    cells += '<td style="text-align:right;font-size:12px">' + fmtTotal(s.total_size || 0) + '</td>';
    cells += '<td style="font-size:12px;color:var(--text-secondary);white-space:nowrap">' + (s.last_updated || '—') + '</td>';
    // escape for JS-string context (single quotes) then HTML-attribute context so a
    // crafted name cannot break out of the onclick handler (XSS, stocks.js scan bug).
    var escName = escapeHtml((s.name || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'"));
    cells += '<td><button class="btn btn-xs" style="color:#ff4d6a;font-size:11px;padding:1px 5px" onclick="event.stopPropagation();window._stocksDelete(\'' + s.symbol + '\',\'' + escName + '\')">✕</button></td>';
    return '<tr>' + cells + '</tr>';
  }).join('');
}

function buildHTML() {
  var thCells = PERIODS.map(function(p) { return '<th style="text-align:center;min-width:56px;font-size:12px">' + PLABELS[p] + '</th>'; }).join('');
  var dlOptions = DPERIODS.map(function(d) { return '<option value="' + d[0] + '">' + d[1] + '</option>'; }).join('');

  return '' +
  '<div class="dash-hero"><div><h1>数据管理</h1><div class="dash-sub">股票数据总览 · 按周期查看覆盖度 · 增量下载 · 批量导入</div></div></div>' +

  '<div class="card" style="margin-bottom:12px;padding:10px 14px">' +
    '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">' +
      '<select id="stocks-type-filter" onchange="window._stocksTypeChange()" style="width:90px;font-size:12px;padding:5px 8px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-hairline);border-radius:6px">' +
        '<option value="">全部分类</option><option value="industry">行业</option><option value="concept">概念</option><option value="region">地域</option>' +
      '</select>' +
      '<select id="stocks-concept-filter" onchange="window._stocksConceptChange()" style="flex:1;min-width:160px;max-width:280px;font-size:12px;padding:5px 8px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-hairline);border-radius:6px">' +
        '<option value="">全部股票</option>' +
      '</select>' +
      '<input id="stocks-search" type="text" placeholder="搜索代码/名称/拼音…" oninput="window._stocksSearch()" style="width:180px;font-size:12px;padding:5px 10px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-hairline);border-radius:6px">' +
      '<span id="stock-count" style="font-size:13px;color:var(--text-secondary);white-space:nowrap">共 0 只</span>' +
      '<span style="margin-left:auto;font-size:13px;color:var(--brand-cyan);cursor:pointer" onclick="window._stocksRefresh()">刷新</span>' +
    '</div>' +
  '</div>' +

  '<div class="card" style="margin-bottom:12px">' +
    '<div style="overflow-x:auto;max-height:55vh">' +
      '<table class="tbl tbl-compact" style="font-size:13px;min-width:980px" id="stocks-data-table">' +
        '<thead><tr>' +
          '<th style="min-width:72px">代码</th><th style="min-width:90px">名称</th>' +
          thCells +
          '<th style="text-align:right;min-width:60px">大小</th><th style="min-width:80px">更新日期</th><th></th>' +
        '</tr></thead>' +
        '<tbody id="stock-tbody"><tr><td colspan="12" style="text-align:center;padding:24px;color:var(--text-secondary)">加载中…</td></tr></tbody>' +
      '</table>' +
    '</div>' +
  '</div>' +

  '<div class="card" style="margin-bottom:12px;padding:12px 14px">' +
    '<h3 style="font-size:13px;margin-bottom:10px">📥 增量数据更新（补齐缺口）</h3>' +
    '<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">' +
      '<select id="dl-period" style="width:100px;font-size:12px;padding:5px 8px;background:var(--bg-input);color:var(--text-primary);border:1px solid var(--border-hairline);border-radius:6px">' + dlOptions + '</select>' +
      '<button id="dl-btn" class="btn btn-primary btn-sm" onclick="window._stocksDownload()">▶ 补齐缺口</button>' +
      '<span id="dl-status" style="font-size:13px;color:var(--text-secondary)"></span>' +
    '</div>' +
    '<div style="font-size:12px;color:var(--text-tertiary);margin-top:8px">💡 增量更新只下载有数据缺口的股票，已是最新的跳过。如需全量重下，先删除 data/raw/ 下文件。</div>' +
    '<div style="font-size:12px;color:var(--text-tertiary);margin-top:4px">⏰ 自动更新: 每日 9:00 / 11:30 / 15:00（需开启飞书监控）</div>' +
  '</div>' +

  '<div class="card" style="padding:12px 14px">' +
    '<h3 style="font-size:13px;margin-bottom:10px">📦 ZIP / RAR / 7Z 文件导入</h3>' +
    '<div style="border:2px dashed var(--border-hairline);border-radius:10px;padding:24px;text-align:center;cursor:pointer" ' +
      'onclick="document.getElementById(\'zip-file-input\').click()" ' +
      'ondragover="event.preventDefault();this.classList.add(\'drag-over\')" ' +
      'ondragleave="this.classList.remove(\'drag-over\')" ' +
      'ondrop="event.preventDefault();this.classList.remove(\'drag-over\');window._stocksZipUpload(event.dataTransfer.files[0])">' +
      '<div style="font-size:32px;margin-bottom:8px">📦</div>' +
      '<div style="font-size:13px">拖拽 ZIP / RAR / 7Z 到此处，或点击选择</div>' +
      '<div style="font-size:13px;color:var(--text-tertiary);margin-top:4px">支持包含 CSV / Parquet 的压缩包，大文件后台处理</div>' +
    '</div>' +
    '<input id="zip-file-input" type="file" accept=".zip,.rar,.7z" style="display:none" onchange="window._stocksZipUpload(this.files[0])">' +
    '<div id="zip-status" style="margin-top:8px;font-size:13px;color:var(--text-secondary)"></div>' +
  '</div>';
}
