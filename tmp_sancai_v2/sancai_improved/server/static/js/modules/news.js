// News Events — 立案/回访/结案 · 信源分级 · 资金对齐 · 时间轴
import { api } from '../core/api.js';
import { toast, escapeHtml } from '../core/dom.js';

var _allEvents = [], _selectedId = null;

const SOURCE_ICONS = { official_l1: '🛡', authority_media_l2: '📰', market_rumor_l3: '💬' };
const SOURCE_COLORS = { official_l1: '#f6465d', authority_media_l2: '#ffb020', market_rumor_l3: '#93a1b8' };
const VERDICT_COLORS = { positive: '#0ecb81', negative: '#f6465d', neutral: '#93a1b8', divergent: '#ffb020' };
const CKPT_LABELS = { 'T+30m': 'T+30分钟', 'T+2h': 'T+2小时', 'T+1d': 'T+1天' };
const CAT_LABELS = { official: '🏛️ 官媒', market: '📊 市场', sentiment: '🌐 舆情' };
const CAT_COLORS = { official: '#f6465d', market: '#3b9eff', sentiment: '#93a1b8' };
const OFFICIAL_TYPES = ['政策-监管','政策-利好','制裁-关税'];
const OFFICIAL_KW = ['证监会','财政部','央行','国务院','发改委','统计局','银保监会','交易所','上交所','深交所'];

function _getEventCategory(e) {
  var sl = e.source_level || '';
  var et = e.event_type || '';
  if (sl === 'official_l1' || OFFICIAL_TYPES.indexOf(et) >= 0) return 'official';
  var txt = (e.title||'') + ' ' + ((e.content||'') || '');
  for (var i=0; i<OFFICIAL_KW.length; i++) { if (txt.indexOf(OFFICIAL_KW[i]) >= 0) return 'official'; }
  if (sl === 'authority_media_l2') return 'market';
  return 'sentiment';
}

export async function mount(container) {
  container.innerHTML = buildHTML();
  await loadEvents();
}

export function onShow() { loadEvents(); }

// ── Data ──

async function loadEvents() {
  var el = document.getElementById('news-event-list');
  if (el) el.innerHTML = '<div style="padding:30px;text-align:center;color:var(--text-secondary)">⏳ 加载事件…</div>';
  try {
    var r = await api.get('/api/news/events?limit=50', { timeoutMs: 15000 });
    _allEvents = ((r || {}).data || {}).events || (r || {}).events || [];
    renderList();
  } catch(e) { if (el) el.innerHTML = '<div style="padding:30px;color:var(--accent-danger)">加载失败: ' + e.message + '</div>'; }
}

window._newsFilter = function(status) {
  var btns = document.querySelectorAll('.news-filter-btn');
  btns.forEach(function(b) { b.classList.remove('active'); });
  var target = document.querySelector('.news-filter-btn[data-status="' + status + '"]');
  if (target) target.classList.add('active');
  _activeStatus = status;
  renderFiltered();
};

window._newsFilterCat = function(cat) {
  var btns = document.querySelectorAll('.news-cat-btn');
  btns.forEach(function(b) { b.classList.remove('active'); });
  var target = document.querySelector('.news-cat-btn[data-cat="' + cat + '"]');
  if (target) target.classList.add('active');
  _activeCat = cat;
  renderFiltered();
};

var _activeStatus = '', _activeCat = '';

function renderFiltered() {
  var items = _allEvents;
  if (_activeStatus) {
    items = items.filter(function(e) { return e.status === _activeStatus; });
  }
  if (_activeCat) {
    items = items.filter(function(e) { return _getEventCategory(e) === _activeCat; });
  }
  renderList(items);
}

window._newsSelect = async function(eventId) {
  _selectedId = eventId;
  renderList();
  var detail = document.getElementById('news-detail-panel');
  detail.innerHTML = '<div style="padding:30px;color:var(--text-secondary)">⏳ 加载详情…</div>';
  try {
    var r = await api.get('/api/news/events/' + eventId, { timeoutMs: 15000 });
    var d = ((r || {}).data || r || {});
    renderDetail(d);
  } catch(e) { detail.innerHTML = '<div style="color:var(--accent-danger);padding:20px">加载失败: ' + e.message + '</div>'; }
};

window._newsFileEvents = async function() {
  var btn = document.getElementById('news-file-btn');
  if (btn) { btn.disabled = true; btn.textContent = '立案中…'; }
  try {
    var r = await api.post('/api/news/events/file?limit=10', {}, { timeoutMs: 30000 });
    var d = (r || {}).data || r || {};
    toast(d.message || '立案完成', { type: 'success' });
    loadEvents();
  } catch(e) { toast(e.message, { type: 'error' }); }
  if (btn) { btn.disabled = false; btn.textContent = '📋 拉取立案'; }
};

// ── 每日统计面板 (研究用) ──

var _statsLoaded = false;

window._newsToggleStats = function() {
  var panel = document.getElementById('news-stats-panel');
  if (!panel) return;
  var open = panel.style.display === 'none';
  panel.style.display = open ? '' : 'none';
  if (open && !_statsLoaded) _loadDailyStats();
};

async function _loadDailyStats() {
  var panel = document.getElementById('news-stats-panel');
  if (!panel) return;
  panel.innerHTML = '<div class="card" style="padding:20px;text-align:center;color:var(--text-secondary)">⏳ 加载统计…</div>';
  try {
    var sr = await api.get('/api/news/stats/daily', { timeoutMs: 15000 });
    var stats = (sr || {}).data || sr || {};
    var cr = await api.get('/api/news/correlation', { timeoutMs: 20000 });
    var corr = (cr || {}).data || cr || {};
    panel.innerHTML = _renderStats(stats, corr);
    _statsLoaded = true;
  } catch(e) {
    panel.innerHTML = '<div class="card" style="padding:20px;color:var(--accent-danger)">统计加载失败: ' + e.message + '</div>';
  }
}

function _bars(obj, colorFn) {
  var entries = Object.keys(obj || {}).map(function(k){ return [k, obj[k]]; })
    .filter(function(e){ return e[0] && e[1]; })
    .sort(function(a,b){ return b[1]-a[1]; });
  if (!entries.length) return '<div style="color:var(--text-tertiary);font-size:11px">无数据</div>';
  var max = entries[0][1];
  return entries.map(function(e){
    var pct = Math.round(e[1]/max*100);
    var c = colorFn ? colorFn(e[0]) : 'var(--brand-teal)';
    return '<div style="display:flex;align-items:center;gap:6px;margin:3px 0;font-size:12px">' +
      '<span style="width:88px;text-align:right;color:var(--text-secondary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + e[0] + '</span>' +
      '<div style="flex:1;height:14px;background:var(--bg-input);border-radius:3px;overflow:hidden"><div style="width:' + pct + '%;height:100%;background:' + c + '"></div></div>' +
      '<span style="width:28px;color:var(--text-primary);font-weight:600">' + e[1] + '</span>' +
    '</div>';
  }).join('');
}

function _sentColor(label) {
  if (String(label).indexOf('利好') >= 0 || String(label).indexOf('正') >= 0 || label === 'positive') return VERDICT_COLORS.positive;
  if (String(label).indexOf('利空') >= 0 || String(label).indexOf('负') >= 0 || label === 'negative') return VERDICT_COLORS.negative;
  return VERDICT_COLORS.neutral;
}

function _renderStats(stats, corr) {
  var s = stats || {}, c = corr || {};
  var head = '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">' +
    '<div class="card-title"><span class="dot" style="background:var(--brand-cyan)"></span>📊 每日消息统计 · ' + (s.date || '—') + '</div>' +
    '<div style="font-size:12px;color:var(--text-secondary)">共 <b style="color:var(--text-primary)">' + (s.total_events || 0) + '</b> 条 · ' +
      '均可信度 <b style="color:var(--text-primary)">' + (s.avg_credibility || 0) + '</b> · ' +
      '均情绪 <b style="color:' + _sentColor(s.avg_sentiment >= 0 ? '正' : '负') + '">' + (s.avg_sentiment || 0) + '</b></div>' +
  '</div>';

  if (!s.total_events) {
    return '<div class="card" style="padding:16px">' + head +
      '<div style="color:var(--text-tertiary);font-size:12px;padding:12px 0">当日暂无消息事件。</div></div>';
  }

  function block(title, body) {
    return '<div style="flex:1;min-width:200px"><div style="font-size:12px;font-weight:600;color:var(--text-secondary);margin-bottom:6px">' + title + '</div>' + body + '</div>';
  }

  var topSyms = (s.top_symbols || []).slice(0, 12).map(function(t){
    return '<span style="display:inline-block;padding:2px 7px;margin:2px;border-radius:5px;background:var(--bg-input);font-size:12px">' +
      t.symbol + ' <b style="color:var(--brand-cyan)">' + t.count + '</b></span>';
  }).join('') || '<span style="color:var(--text-tertiary);font-size:11px">无</span>';

  var grid = '<div style="display:flex;flex-wrap:wrap;gap:18px">' +
    block('事件类型', _bars(s.by_type)) +
    block('情绪分布', _bars(s.by_sentiment, _sentColor)) +
    block('信源分级', _bars(s.by_source_level, function(k){ return SOURCE_COLORS[k] || 'var(--brand-teal)'; })) +
    block('概念热力 Top', _bars(s.by_concept)) +
  '</div>';

  var symBlock = '<div style="margin-top:12px"><div style="font-size:12px;font-weight:600;color:var(--text-secondary);margin-bottom:4px">🔥 热点个股</div>' + topSyms + '</div>';

  // 消息-策略关联
  var corrRows = (c.correlations || []).map(function(m){
    return '<div style="display:flex;align-items:center;gap:8px;font-size:12px;margin:3px 0">' +
      '<span style="width:120px;color:var(--text-primary)">' + m.mode + '</span>' +
      '<span style="color:var(--brand-cyan)">命中 ' + m.matched_count + '</span>' +
      '<span style="color:var(--text-tertiary)">/ 仅扫描 ' + m.scan_only_count + '</span>' +
    '</div>';
  }).join('') || '<div style="color:var(--text-tertiary);font-size:11px">当日无扫描-消息交集</div>';

  var corrBlock = '<div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border-hairline)">' +
    '<div style="font-size:12px;font-weight:600;color:var(--text-secondary);margin-bottom:6px">🔗 消息 × 策略关联</div>' +
    '<div style="font-size:12px;color:var(--text-secondary);margin-bottom:8px">' + (c.summary || '') + '</div>' +
    corrRows + '</div>';

  return '<div class="card" style="padding:16px">' + head + grid + symBlock + corrBlock + '</div>';
}

// ── Render ──

function renderList(filtered) {
  var items = filtered || _allEvents;
  var el = document.getElementById('news-event-list');
  if (!el) return;
  if (!items.length) { el.innerHTML = '<div style="padding:30px;text-align:center;color:var(--text-secondary)">暂无消息事件。点击「拉取立案」从华尔街见闻获取最新快讯。</div>'; return; }

  el.innerHTML = items.map(function(e) {
    var srcIcon = SOURCE_ICONS[e.source_level] || '💬';
    var srcColor = SOURCE_COLORS[e.source_level] || '#93a1b8';
    var cat = _getEventCategory(e);
    var catColor = CAT_COLORS[cat] || '#93a1b8';
    var isSel = e.id === _selectedId;
    var borderStyle = isSel ? 'border-left:3px solid var(--brand-cyan);' : ('border-left:3px solid ' + catColor + '44;');
    var statusBadge = e.status === 'concluded'
      ? '<span style="background:var(--accent-success-soft);color:var(--brand-teal);padding:1px 6px;border-radius:4px;font-size:9px">✓ 已结案</span>'
      : '<span style="background:var(--accent-warn-soft);color:var(--accent-warn);padding:1px 6px;border-radius:4px;font-size:9px">⏳ 观察中</span>';
    var srcCountBadge = (e.source_count || 1) > 1
      ? '<span style="background:var(--accent-ai-soft);color:var(--accent-ai);padding:1px 5px;border-radius:3px;font-size:9px">📋 ×' + e.source_count + '</span>'
      : '';

    return '<div class="news-event-item" style="' + borderStyle + 'cursor:pointer;padding:8px 12px;border-bottom:1px solid var(--border-hairline)" onclick="window._newsSelect(' + e.id + ')">' +
      '<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">' +
        '<span style="background:' + catColor + '22;color:' + catColor + ';padding:1px 5px;border-radius:3px;font-size:9px">' + CAT_LABELS[cat] + '</span>' +
        '<span style="background:' + srcColor + '22;color:' + srcColor + ';padding:1px 5px;border-radius:3px;font-size:9px">' + srcIcon + ' ' + escapeHtml(e.source_label || '') + '</span>' +
        '<span style="font-size:9px;color:var(--text-tertiary);font-family:var(--font-mono)">#' + escapeHtml(e.case_no || '') + '</span>' +
        statusBadge + srcCountBadge +
        '<span style="margin-left:auto;font-size:9px;color:var(--text-tertiary)">' + (e.time || e.published_at || '').slice(5,16) + '</span>' +
      '</div>' +
      '<div style="font-size:13px;font-weight:600">' + escapeHtml(e.title || '') + '</div>' +
      '<div style="font-size:10px;color:var(--text-secondary);margin-top:2px">' +
        (e.event_type ? '<span>📋 ' + escapeHtml(e.event_type) + '</span> · ' : '') +
        (e.cap_alignment ? '<span style="color:var(--accent-warn)">💰 ' + escapeHtml(e.cap_alignment) + '</span> · ' : '') +
        (e.concepts || []).map(function(c) { return escapeHtml(c); }).slice(0, 3).join(' · ') +
      '</div>' +
    '</div>';
  }).join('');
}

function renderDetail(d) {
  var ev = d.event || {};
  var ckpts = d.checkpoints || [];
  var verdict = d.verdict;
  var sources = d.sources;

  var srcIcon = SOURCE_ICONS[ev.source_level] || '💬';
  var srcColor = SOURCE_COLORS[ev.source_level] || '#93a1b8';

  var html = '' +
  // Header
  '<div style="margin-bottom:12px">' +
    '<div style="font-size:11px;color:var(--text-tertiary);font-family:var(--font-mono)">#' + escapeHtml(ev.case_no || '') + ' · ' + escapeHtml(ev.published_at || '') + '</div>' +
    '<div style="font-size:16px;font-weight:700;margin:6px 0">' + escapeHtml(ev.title || '') + '</div>' +
    '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">' +
      '<span style="background:' + srcColor + '22;color:' + srcColor + ';padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600">' + srcIcon + ' ' + escapeHtml(ev.source_label || '') + '</span>' +
      '<span style="font-size:10px;color:var(--text-secondary)">判定依据: ' + escapeHtml(ev.source_rule || '') + '</span>' +
      (ev.status === 'concluded' ? '<span style="background:var(--accent-success-soft);color:var(--brand-teal);padding:2px 8px;border-radius:4px;font-size:11px">✓ 已结案</span>' : '<span style="background:var(--accent-warn-soft);color:var(--accent-warn);padding:2px 8px;border-radius:4px;font-size:11px">⏳ 观察中</span>') +
    '</div>' +
    // Multi-source indicator
    (sources && sources.count > 1 ? '<div style="margin-top:4px;padding:6px 10px;background:var(--accent-ai-soft);border-radius:6px;font-size:10px;color:var(--accent-ai)">📋 此事件被 <b>' + sources.count + ' 个信源</b>报道 (可信度+' + ((sources.count-1)*3) + '): ' +
      (sources.items || []).slice(0,5).map(function(s){ return '<span style="margin:0 4px">' + escapeHtml(s.source_name||'') + '</span>'; }).join('·') + '</div>' : '') +
  '</div>' +

  // 5W1H card
  '<div style="background:var(--bg-sunken);padding:10px;border-radius:8px;margin-bottom:10px">' +
    '<div style="font-size:10px;color:var(--text-tertiary);margin-bottom:4px">📝 5W1H 分析</div>' +
    '<div style="font-size:12px;line-height:1.6">' + escapeHtml(ev.summary_5w1h || '暂无') + '</div>' +
    '<div style="margin-top:4px;display:flex;gap:12px;font-size:11px">' +
      '<span>📋 ' + escapeHtml(ev.event_type || '未知') + (ev.event_subtype ? '-' + escapeHtml(ev.event_subtype) : '') + '</span>' +
      '<span>💥 ' + ((ev.event_impact || 0) >= 0 ? '+' : '') + (ev.event_impact || 0).toFixed(0) + '</span>' +
      '<span>😊 ' + escapeHtml(ev.sentiment_label || '中性') + ' (' + (ev.sentiment_score || 0).toFixed(2) + ')</span>' +
      '<span>🎯 可信度: ' + (ev.credibility || 0).toFixed(0) + '</span>' +
    '</div>' +
  '</div>' +

  // Stocks + Concepts
  '<div style="display:flex;gap:16px;margin-bottom:10px;font-size:11px">' +
    ((ev.stock_symbols || []).length ? '<div><b>关联个股</b>: ' + (ev.stock_symbols || []).slice(0, 10).map(function(s) {
      return '<span style="cursor:pointer;color:var(--brand-cyan);margin:0 4px" onclick="var el=document.querySelector(\'[id\\$=\\\"-symbol\\\"]\');if(el){el.value=\'' + s + '\';var rail=document.querySelector(\'.rail-item[data-route=\\\"chart\\\"]\');if(rail)rail.click()}">' + escapeHtml(s) + '</span>';
    }).join('') + '</div>' : '') +
    ((ev.concepts || []).length ? '<div><b>关联概念</b>: ' + (ev.concepts || []).map(function(c) { return escapeHtml(c); }).join(', ') + '</div>' : '') +
  '</div>' +

  // Capital alignment snapshot
  (ev.cap_alignment_type ? '<div style="background:var(--bg-sunken);padding:8px 10px;border-radius:6px;margin-bottom:10px;font-size:11px">💰 <b>资金对齐快照</b>: ' + escapeHtml(ev.cap_alignment_type) + ' (评分: ' + (ev.cap_alignment_score || 0).toFixed(2) + ')</div>' : '') +

  // Checkpoint timeline
  '<div style="margin-bottom:12px">' +
    '<div style="font-size:12px;font-weight:700;margin-bottom:6px">📊 市场真实反馈回访</div>' +
    '<div style="display:flex;gap:8px">' +
      ckpts.map(function(c) {
        var done = c.completed;
        var pct = c.avg_pct_change || 0;
        var color = done ? (pct > 0.3 ? '#0ecb81' : pct < -0.3 ? '#f6465d' : '#93a1b8') : 'var(--text-tertiary)';
        var label = CKPT_LABELS[c.checkpoint] || c.checkpoint;
        var capNote = c.cap_alignment ? '<div style="font-size:9px;color:var(--accent-warn)">' + escapeHtml(c.cap_alignment) + '</div>' : '';
        return '<div style="flex:1;text-align:center;padding:8px 4px;border-radius:8px;border:2px ' + (done ? 'solid' : 'dashed') + ' ' + color + ';background:' + (done ? color + '10' : 'transparent') + ';opacity:' + (done ? '1' : '0.5') + '">' +
          '<div style="font-size:10px;color:var(--text-secondary)">' + label + '</div>' +
          '<div style="font-size:18px;font-weight:800;color:' + color + ';margin:4px 0">' + (done ? (pct > 0 ? '+' : '') + pct.toFixed(2) + '%' : '--') + '</div>' +
          '<div style="font-size:9px;color:var(--text-secondary)">' + (done ? (c.up_count || 0) + '↑ ' + (c.down_count || 0) + '↓ ' + (c.flat_count || 0) + '—' : '待回访') + '</div>' +
          capNote +
        '</div>';
      }).join('') +
    '</div>' +
  '</div>' +

  // Checkpoint details
  ckpts.filter(function(c) { return c.completed; }).map(function(c) {
    var label = CKPT_LABELS[c.checkpoint] || c.checkpoint;
    return '<div style="background:var(--bg-sunken);padding:8px 10px;border-radius:6px;margin-bottom:6px;font-size:11px">' +
      '<div style="font-weight:600;margin-bottom:4px">' + label + ' 回访结果</div>' +
      '<div style="line-height:1.6">' + escapeHtml(c.verdict_text || '') + '</div>' +
      (c.cap_phase ? '<div style="margin-top:4px;color:var(--accent-warn)">💰 资金: ' + escapeHtml(c.cap_phase) + ' (健康度 ' + (c.cap_score || 50).toFixed(0) + ')</div>' : '') +
      '<div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px">' +
        (c.per_stock || []).slice(0, 10).map(function(s) {
          var pc = s.pct_change || 0;
          return '<span style="font-size:10px;padding:1px 5px;border-radius:3px;color:' + (pc > 0 ? '#0ecb81' : pc < 0 ? '#f6465d' : '#93a1b8') + ';border:1px solid ' + (pc > 0 ? '#0ecb81' : pc < 0 ? '#f6465d' : '#93a1b8') + '33">' + escapeHtml(s.symbol) + ' ' + (pc > 0 ? '+' : '') + pc.toFixed(2) + '%</span>';
        }).join('') +
      '</div>' +
    '</div>';
  }).join('') +

  // Conclusion
  (verdict ? '<div style="background:var(--bg-sunken);padding:10px;border-radius:8px;border-left:3px solid var(--brand-teal);margin-top:8px">' +
    '<div style="font-size:13px;font-weight:700;color:var(--brand-teal);margin-bottom:4px">📋 综合辩证结论</div>' +
    '<div style="font-size:11px;line-height:1.6">' + escapeHtml(verdict.verdict_summary || '') + '</div>' +
    '<div style="font-size:11px;color:var(--text-secondary);margin-top:4px">' + escapeHtml(verdict.market_reaction || '') + '</div>' +
    '<div style="display:flex;gap:12px;margin-top:6px;font-size:11px">' +
      '<span>⚠️ ' + escapeHtml(verdict.risk_notes || '') + '</span>' +
      '<span style="color:var(--brand-cyan)">💡 ' + escapeHtml(verdict.recommendation || '') + '</span>' +
    '</div>' +
    '<div style="margin-top:4px;font-size:9px;color:var(--text-tertiary)">T+30m: ' + ((verdict.t30m_pct || 0) >= 0 ? '+' : '') + (verdict.t30m_pct || 0).toFixed(2) + '% → T+2h: ' + ((verdict.t2h_pct || 0) >= 0 ? '+' : '') + (verdict.t2h_pct || 0).toFixed(2) + '% → T+1d: ' + ((verdict.t1d_pct || 0) >= 0 ? '+' : '') + (verdict.t1d_pct || 0).toFixed(2) + '%</div>' +
  '</div>' : '') +

  '';

  document.getElementById('news-detail-panel').innerHTML = html;
}

function buildHTML() {
  return '' +
  '<div class="dash-hero"><div><h1>消息追踪</h1><div class="dash-sub">立案存档 · 信源分级 · 三阶段回访 · AI辩证结案</div></div>' +
    '<div style="display:flex;gap:6px">' +
      '<span class="news-filter-btn active" data-status="" style="font-size:11px;padding:3px 10px;border-radius:6px;cursor:pointer;background:var(--bg-input);color:var(--text-primary)" onclick="window._newsFilter(\'\')">全部</span>' +
      '<span class="news-filter-btn" data-status="watching" style="font-size:11px;padding:3px 10px;border-radius:6px;cursor:pointer;background:var(--bg-input);color:var(--text-primary)" onclick="window._newsFilter(\'watching\')">观察中</span>' +
      '<span class="news-filter-btn" data-status="concluded" style="font-size:11px;padding:3px 10px;border-radius:6px;cursor:pointer;background:var(--bg-input);color:var(--text-primary)" onclick="window._newsFilter(\'concluded\')">已结案</span>' +
      '<button id="news-file-btn" class="btn btn-sm btn-primary" onclick="window._newsFileEvents()" style="font-size:11px">📋 拉取立案</button>' +
      '<button id="news-stats-btn" class="btn btn-sm" onclick="window._newsToggleStats()" style="font-size:11px;background:var(--bg-input);color:var(--text-primary)">📊 每日统计</button>' +
    '<div style="margin-top:6px;display:flex;gap:6px">' +
      '<span class="news-cat-btn active" data-cat="" style="font-size:10px;padding:2px 8px;border-radius:6px;cursor:pointer;background:var(--bg-input);color:var(--text-primary)" onclick="window._newsFilterCat(\'\')">🔖 全部</span>' +
      '<span class="news-cat-btn" data-cat="official" style="font-size:10px;padding:2px 8px;border-radius:6px;cursor:pointer;background:var(--bg-input);color:'+CAT_COLORS.official+'" onclick="window._newsFilterCat(\'official\')">🏛️ 官媒/政策</span>' +
      '<span class="news-cat-btn" data-cat="market" style="font-size:10px;padding:2px 8px;border-radius:6px;cursor:pointer;background:var(--bg-input);color:'+CAT_COLORS.market+'" onclick="window._newsFilterCat(\'market\')">📊 市场/公司</span>' +
      '<span class="news-cat-btn" data-cat="sentiment" style="font-size:10px;padding:2px 8px;border-radius:6px;cursor:pointer;background:var(--bg-input);color:'+CAT_COLORS.sentiment+'" onclick="window._newsFilterCat(\'sentiment\')">🌐 舆情/社会</span>' +
    '</div>' +
    '</div>' +
  '</div>' +

  '<div id="news-stats-panel" style="display:none;margin-bottom:12px"></div>' +

  '<div style="display:grid;grid-template-columns:1fr 1.2fr;gap:12px;height:calc(100vh - 140px)">' +
    '<div class="card" style="overflow-y:auto">' +
      '<div class="card-head"><div class="card-title"><span class="dot" style="background:var(--brand-teal)"></span>📡 事件列表</div></div>' +
      '<div id="news-event-list">加载中…</div>' +
    '</div>' +
    '<div class="card" style="overflow-y:auto" id="news-detail-panel">' +
      '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-tertiary);font-size:13px">← 从左侧选择事件查看详情 + 回访记录</div>' +
    '</div>' +
  '</div>';
}
