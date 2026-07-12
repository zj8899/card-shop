// Sancai tier page loaders: Tiandao, Didao, Rendao
import { apiGet } from './api.js';
import { renderTimeline } from './timeline.js';

let currentSancaiSub = 'tiandao';
let sancaiTimeRange = 60;
let _quizQuestions = null;
let _userProfile = null;
let _screenerData = null;

// Expose to inline onclick handlers
window.switchSancaiSubTab = switchSancaiSubTab;
window.switchSancaiTimeRange = switchSancaiTimeRange;
window.submitQuiz = submitQuiz;
window.loadDidaoMindmap = loadDidaoMindmap;
window.loadDidaoScreener = loadDidaoScreener;
window.loadDidaoScreenerTab = loadDidaoScreenerTab;
window.onDidaoSymbolChange = onDidaoSymbolChange;

// === Sub-tab switching ===
export function switchSancaiSubTab(tier) {
  currentSancaiSub = tier;
  ['tiandao', 'didao', 'rendao'].forEach(t => {
    document.getElementById('sancai-' + t).classList.toggle('hidden', t !== tier);
  });
  document.querySelectorAll('#sancai-sub-nav .nav-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tier === tier);
  });

  if (tier === 'tiandao') loadTiandao();
  else if (tier === 'didao') loadDidao();
  else if (tier === 'rendao') loadRendao();

  loadAlignment();
}

export function switchSancaiTimeRange(days) {
  sancaiTimeRange = days;
  document.querySelectorAll('#sancai-range-bar .nav-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.days) === days);
  });
  if (currentSancaiSub === 'tiandao') loadTiandao();
  else if (currentSancaiSub === 'didao') loadDidao();
  else if (currentSancaiSub === 'rendao') loadRendao();
}

// ═══════════════════════════════════════════
// 天道 (Tiandao)
// ═══════════════════════════════════════════

export async function loadTiandao() {
  const days = sancaiTimeRange;
  try {
    // Fire the slow timeline fetch, but don't block sub-loaders
    const tlPromise = apiGet('/api/sancai/tiandao/timeline?days=' + days, 30000);

    // Fire all sub-loaders immediately — they render independently
    loadTiandaoMarket(days);
    loadTiandaoIndices(days);
    loadTiandaoResearch(days);
    loadTiandaoFundamental();
    loadTiandaoSectors();
    loadTiandaoSentiment();
    loadTiandaoMedia();
    loadTiandaoPolicy(days);

    // Now await timeline and render it
    const tl = await tlPromise;
    if (tl.status !== 'ok') { showError('tiandao'); return; }

    renderTimeline('tiandao-timeline-chart', 'tiandao-event-list', tl.data, {
      priceLabel: '沪深300',
      showCandlestick: false,
    });

    const events = tl.data.events || [];
    const bullCount = events.filter(e => e.layer === 'fundamental' || e.title.includes('低估')).length;
    const bearCount = events.filter(e => e.title.includes('跌') && e.importance >= 2).length;
    const assessment = bearCount > 3 ? '凶' : (bullCount >= 1 ? '吉' : '平');
    const assCls = assessment === '吉' ? 'tag-ji' : (assessment === '凶' ? 'tag-xiong' : 'tag-ping');
    document.getElementById('tiandao-assessment').innerHTML =
      '<span class="tag ' + assCls + '" style="font-size:36px;">' + assessment + '</span>';
    document.getElementById('tiandao-assessment-sub').textContent =
      '沪深300趋势 | PE分位数估值 | 宏观事件: ' + tl.data.event_count + '条';
  } catch (e) {
    console.error('Tiandao load error:', e);
    showError('tiandao');
  }
}

// ── Sentiment index ──
async function loadTiandaoSentiment() {
  try {
    const resp = await apiGet('/api/tiandao/sentiment', 20000);
    if (resp.status !== 'ok') return;
    const d = resp.data;
    const idx = d.sentiment_index;
    const color = d.phase_color || '#d2991d';
    document.getElementById('tiandao-sentiment-index').textContent = idx;
    document.getElementById('tiandao-sentiment-index').style.color = color;
    document.getElementById('tiandao-sentiment-label').textContent = d.temperature + ' · ' + d.phase;
    document.getElementById('tiandao-sentiment-phase').innerHTML =
      '<span style="color:' + color + ';">' + d.phase_description + '</span>';

    // Limit-up stats
    const lu = d.limit_up_stats || {};
    document.getElementById('zt-total').textContent = lu.total_limit_up || 0;
    document.getElementById('zt-dt').textContent = d.limit_down_count || 0;
    document.getElementById('zt-1b').textContent = lu.first_board || 0;
    document.getElementById('zt-2b').textContent = lu.second_board || 0;
    document.getElementById('zt-3b').textContent = lu.third_board || 0;
    document.getElementById('zt-4b').textContent = lu.fourth_board || 0;
    document.getElementById('zt-5b').textContent = lu.fifth_plus_board || 0;
    const high = (lu.third_board || 0) + (lu.fourth_board || 0) + (lu.fifth_plus_board || 0);
    document.getElementById('zt-high').textContent = high;
  } catch (e) { console.error('Sentiment error:', e); }
}

// ── Media hot topics ──
async function loadTiandaoMedia() {
  try {
    const resp = await apiGet('/api/tiandao/media/hot-topics', 20000);
    if (resp.status !== 'ok') return;
    const topics = resp.data.topics || [];
    const html = topics.slice(0, 15).map(t => {
      const platformColor = t.platform === '东方财富热榜' ? '#f85149' :
                            t.platform === '同花顺概念' ? '#58a6ff' :
                            t.platform === '东财热帖' ? '#3fb950' : '#d2991d';
      const sentColor = t.sentiment_hint === '偏多' ? '#f85149' : t.sentiment_hint === '偏空' ? '#26a69a' : '#8b949e';
      return '<div style="padding:3px 0;border-bottom:1px solid #21262d;font-size:11px;">' +
        '<span style="color:' + platformColor + ';">[' + t.platform + ']</span> ' +
        escHtml(t.name || t.summary || t.symbol || '').substring(0, 60) +
        ' <span style="color:' + sentColor + ';font-size:10px;">' + t.sentiment_hint + '</span></div>';
    }).join('');
    document.getElementById('tiandao-media-topics').innerHTML = html || '<span style="color:#8b949e;">暂无舆论数据</span>';

    // Load summary
    loadTiandaoMediaSummary();
  } catch (e) { console.error('Media error:', e); }
}

async function loadTiandaoMediaSummary() {
  try {
    const resp = await apiGet('/api/tiandao/media/summary', 20000);
    if (resp.status !== 'ok') return;
    const d = resp.data;
    document.getElementById('tiandao-media-summary').innerHTML =
      '<strong style="color:#d2991d;">舆论分析:</strong> ' + escHtml(d.narrative) +
      ' | 偏多 ' + d.sentiment_stats.bull_count + ' / 偏空 ' + d.sentiment_stats.bear_count +
      '<br><span style="color:#58a6ff;">建议:</span> ' + escHtml(d.advice) +
      (d.hot_concepts && d.hot_concepts.length > 0 ?
        '<br><span style="color:#3fb950;">热门概念:</span> ' +
        d.hot_concepts.slice(0, 5).map(c => escHtml(c.name)).join(' · ') : '');
  } catch (e) { /* ignore */ }
}

// ── Policy announcements ──
async function loadTiandaoPolicy(days) {
  try {
    const resp = await apiGet('/api/tiandao/policy?days=' + days, 20000);
    if (resp.status !== 'ok') return;
    const items = resp.data.announcements || [];
    const html = items.length === 0
      ? '<span style="color:#8b949e;">暂无政策公告</span>'
      : items.slice(0, 15).map(a => {
          const dirColor = a.direction === '利多' ? '#f85149' : a.direction === '利空' ? '#26a69a' : '#8b949e';
          return '<div style="padding:4px 0;border-bottom:1px solid #21262d;font-size:11px;">' +
            '<span style="color:' + dirColor + ';">[' + a.direction + ']</span> ' +
            '<span class="tag tag-buy" style="font-size:10px;">' + escHtml(a.agency) + '</span> ' +
            escHtml(a.title) +
            ' <span style="color:#8b949e;font-size:10px;">' + (a.date || '') + '</span></div>';
        }).join('');
    document.getElementById('tiandao-policy-list').innerHTML = html;
  } catch (e) { /* ignore */ }
}

// ── Legacy tiandao sub-loaders ──
async function loadTiandaoMarket(days) {
  try {
    const resp = await apiGet('/api/sancai/tiandao/market?days=' + days, 30000);
    if (resp.status !== 'ok') return;
    const d = resp.data;
    const groups = {};
    Object.entries(d).forEach(([code, info]) => {
      const g = info.group || '其他';
      if (!groups[g]) groups[g] = [];
      groups[g].push({code, ...info});
    });
    let html = '';
    for (const [group, items] of Object.entries(groups)) {
      html += '<h4 style="font-size:12px;color:#58a6ff;margin:8px 0 4px;">' + escHtml(group) + '</h4>';
      const rows = items.map(item => {
        const pct = item.change_pct || 0;
        const color = pct >= 0 ? '#f85149' : '#26a69a';
        const sign = pct >= 0 ? '+' : '';
        return '<tr><td>' + item.name + '</td><td>' + (item.latest || 0).toFixed(2) + '</td>' +
          '<td style="color:' + color + ';">' + sign + pct.toFixed(2) + '%</td></tr>';
      }).join('');
      html += '<table class="table"><thead><tr><th>指数</th><th>收盘</th><th>涨跌</th></tr></thead><tbody>' + rows + '</tbody></table>';
    }
    document.getElementById('tiandao-market-table').innerHTML = html;
  } catch (e) { /* ignore */ }
}

async function loadTiandaoIndices(days) {
  try {
    const resp = await apiGet('/api/sancai/tiandao/indices?days=' + Math.max(days, 90), 30000);
    if (resp.status !== 'ok') return;
    const d = resp.data;
    const indices = d.indices || [];
    const groups = {};
    indices.forEach(item => {
      const g = item.group || '其他';
      if (!groups[g]) groups[g] = [];
      groups[g].push(item);
    });
    let html = '<div style="border-top:1px solid #30363d;padding-top:12px;margin-top:8px;">';
    html += '<h4 style="font-size:13px;color:#d2991d;margin-bottom:8px;">均线排列 · 爻卦象 (MA34/144/233)</h4>';

    for (const [group, items] of Object.entries(groups)) {
      html += '<div style="margin-bottom:10px;">';
      html += '<div style="font-size:11px;color:#58a6ff;margin-bottom:4px;">' + escHtml(group) + '</div>';
      items.forEach(item => {
        const hasGua = item.hexagram && item.hexagram !== '—';
        let alignColor;
        if (item.alignment === '多头排列') { alignColor = '#f85149'; }
        else if (item.alignment === '空头排列') { alignColor = '#26a69a'; }
        else if (item.alignment === '多头交叉') { alignColor = '#e8c840'; }
        else if (item.alignment === '空头交叉') { alignColor = '#26a69a'; }
        else { alignColor = '#8b949e'; }
        const dirTag = item.direction === '吉' ? '<span class="tag tag-ji" style="font-size:10px;">吉</span>' :
                       item.direction === '凶' ? '<span class="tag tag-xiong" style="font-size:10px;">凶</span>' :
                       '<span class="tag tag-ping" style="font-size:10px;">平</span>';
        const guaInfo = hasGua ?
          '<span style="color:' + item.color + ';font-size:18px;vertical-align:middle;">' + item.symbol + '</span> ' +
          '<span style="color:#e6f1ff;">' + item.yao_ci + '</span>' :
          '<span style="color:#8b949e;">数据不足</span>';
        html += '<div style="display:flex;align-items:center;gap:8px;padding:4px 8px;border-bottom:1px solid #21262d;font-size:11px;">' +
          '<span style="min-width:80px;font-weight:bold;">' + escHtml(item.name) + '</span>' +
          '<span style="min-width:55px;text-align:right;color:#e6f1ff;">' + (item.price || 0).toFixed(1) + '</span>' +
          '<span style="min-width:55px;text-align:right;color:' + (item.change_pct >= 0 ? '#f85149' : '#26a69a') + ';">' + (item.change_pct >= 0 ? '+' : '') + (item.change_pct || 0).toFixed(2) + '%</span>' +
          '<span style="min-width:70px;color:' + alignColor + ';font-size:10px;">' + (item.alignment || '') + '</span>' +
          '<span style="flex:1;min-width:180px;">' + guaInfo + '</span>' +
          dirTag +
          '<span style="color:#8b949e;font-size:10px;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' + escHtml(item.advice || '') + '">' + escHtml(item.advice || '') + '</span>' +
          '</div>';
      });
      html += '</div>';
    }

    const overall = d.overall || {};
    html += '<div style="text-align:center;padding:12px;background:#1c2128;border-radius:6px;margin-top:8px;">';
    html += '<span style="color:#d2991d;font-size:14px;">天道总判: </span>';
    html += '<span style="font-size:24px;color:' + (overall.hexagram === '乾' ? '#f85149' : overall.hexagram === '坤' ? '#26a69a' : '#d2991d') + ';">' + (overall.symbol || '') + '</span> ';
    html += '<span style="color:#e6f1ff;font-size:14px;">' + escHtml(overall.yao_ci || '') + '</span> ';
    const overallTag = overall.assessment === '吉' ? '<span class="tag tag-ji">吉</span>' :
                       overall.assessment === '凶' ? '<span class="tag tag-xiong">凶</span>' :
                       '<span class="tag tag-ping">平</span>';
    html += overallTag;
    html += '<div style="color:#8b949e;font-size:11px;margin-top:4px;">' + escHtml(overall.meaning || '') + ' | ' + escHtml(overall.advice || '') + '</div>';
    html += '</div>';
    html += '</div>';

    document.getElementById('tiandao-market-summary').textContent =
      '多头' + d.multi_count + ' | 空头' + d.bear_count + ' | 震荡' + d.cross_count + ' | 共' + d.total + '指数';
    document.getElementById('tiandao-indices-grid').innerHTML = html;
  } catch (e) { console.error('loadTiandaoIndices error:', e); }
}

async function loadTiandaoResearch(days) {
  try {
    const resp = await apiGet('/api/sancai/tiandao/research?days=' + days, 30000);
    if (resp.status !== 'ok') return;
    const reports = resp.data.reports || [];
    const html = reports.length === 0
      ? '<p style="color:#8b949e;">暂无宏观研报</p>'
      : reports.slice(0, 10).map(r =>
          '<div style="padding:6px 0;border-bottom:1px solid #21262d;font-size:12px;">' +
          '<span style="color:#3fb950;">[' + escHtml(r.org) + ']</span> ' +
          escHtml(r.title) + '</div>'
        ).join('');
    document.getElementById('tiandao-research-list').innerHTML = html;
  } catch (e) { /* ignore */ }
}

async function loadTiandaoFundamental() {
  try {
    const resp = await apiGet('/api/sancai/tiandao/fundamental', 30000);
    if (resp.status !== 'ok') return;
    const d = resp.data;
    const rows = Object.entries(d).map(([name, info]) => {
      if (!info) return '';
      return '<tr><td>' + name + '</td>' +
        '<td>' + (info.pe_weighted || info.pe_median || '--') + '</td>' +
        '<td>' + (info.pe_pct != null ? info.pe_pct.toFixed(1) + '%' : '--') + '</td>' +
        '<td>' + (info.pb_weighted || info.pb_median || '--') + '</td>' +
        '<td>' + (info.pb_pct != null ? info.pb_pct.toFixed(1) + '%' : '--') + '</td></tr>';
    }).filter(Boolean).join('');
    document.getElementById('tiandao-fundamental-table').innerHTML =
      '<table class="table"><thead><tr><th>指数</th><th>PE</th><th>PE分位</th><th>PB</th><th>PB分位</th></tr></thead><tbody>' + rows + '</tbody></table>';
    document.getElementById('tiandao-fund-ts').textContent =
      d['沪深300'] ? '(' + d['沪深300'].date + ')' : '';
  } catch (e) { /* ignore */ }
}

async function loadTiandaoSectors() {
  try {
    const resp = await apiGet('/api/sancai/tiandao/sectors', 30000);
    if (resp.status !== 'ok') return;
    const raw = resp.data.raw || {};
    const entries = Object.entries(raw).slice(0, 30);
    const maxVal = Math.max(...entries.map(([, v]) => Math.abs(parseFloat(v) || 0)), 1);
    const html = entries.map(([name, val]) => {
      const num = parseFloat(val) || 0;
      const color = num >= 0 ? '#f85149' : '#26a69a';
      const pct = Math.min(Math.abs(num) / maxVal * 100, 100);
      return '<div style="display:flex;align-items:center;gap:4px;font-size:11px;padding:1px 0;">' +
        '<span style="min-width:70px;text-align:right;">' + escHtml(name) + '</span>' +
        '<span style="flex:1;height:10px;background:#21262d;border-radius:2px;overflow:hidden;">' +
        '<div style="width:' + pct + '%;height:100%;background:' + color + ';border-radius:2px;"></div></span>' +
        '<span style="color:' + color + ';min-width:50px;">' + (num >= 0 ? '+' : '') + num.toFixed(2) + '%</span></div>';
    }).join('');
    document.getElementById('tiandao-sectors-content').innerHTML = html;
  } catch (e) { /* ignore */ }
}

// ═══════════════════════════════════════════
// 地道 (Didao)
// ═══════════════════════════════════════════

export async function loadDidao() {
  const symbol = document.getElementById('didao-symbol')?.value || '000001';
  const days = sancaiTimeRange;
  try {
    // Fire score and timeline fetches in parallel
    const scorePromise = apiGet('/api/sancai/didao/score?symbol=' + symbol, 30000);
    const tlPromise = apiGet('/api/sancai/didao/timeline?symbol=' + symbol + '&days=' + days, 30000);

    // Fire sub-loaders immediately — they render independently
    loadDidaoResearch(symbol, days);
    loadDidaoFundamental(symbol);
    loadDidaoAnnouncements(symbol, days);
    loadDidaoMarketLayer(symbol, days);

    const scoreResp = await scorePromise;
    if (scoreResp.status === 'ok') {
      const sc = scoreResp.data;
      const assCls = sc.assessment === '吉' ? 'tag-ji' : (sc.assessment === '凶' ? 'tag-xiong' : 'tag-ping');
      document.getElementById('didao-assessment').innerHTML =
        '<span class="tag ' + assCls + '" style="font-size:36px;">' + sc.assessment + '</span>';
      document.getElementById('didao-assessment-sub').textContent =
        '评分: ' + sc.score + '/100 | 最新价: ' + sc.latest_price.toFixed(2);
      const gua = sc.gua || {};
      if (gua.hexagram) {
        document.getElementById('didao-gua-info').innerHTML =
          '<span style="font-size:20px;color:' + (gua.color || '#d2991d') + ';">' + (gua.symbol || '') + '</span> ' +
          '<span style="color:#e6f1ff;">' + escHtml(gua.yao_ci || '') + '</span> ' +
          '<span style="color:' + (gua.color || '#8b949e') + ';">' + escHtml(gua.hexagram || '') + '卦·' + escHtml(gua.nature || '') + '</span>' +
          '<div style="margin-top:4px;color:#8b949e;">' + escHtml(gua.yao_meaning || '') + '</div>' +
          '<div style="margin-top:2px;color:#58a6ff;">' + escHtml(gua.advice || '') + ' | ' + escHtml(gua.alignment || '') + '</div>';
      }
    }

    const tl = await tlPromise;
    if (tl.status === 'ok') {
      renderTimeline('didao-timeline-chart', 'didao-event-list', tl.data, {
        priceLabel: symbol,
        showCandlestick: true,
      });
    }
  } catch (e) {
    console.error('Didao load error:', e);
    showError('didao');
  }
}

export function onDidaoSymbolChange() {
  loadDidao();
}

// ── TOP-N Screener ──
export async function loadDidaoScreener() {
  const panel = document.getElementById('didao-screener-panel');
  panel.classList.remove('hidden');
  loadDidaoScreenerTab('by_turnover');
  loadDidaoHotConcepts();
}

export async function loadDidaoScreenerTab(tab) {
  // Update active button
  document.querySelectorAll('#didao-screener-panel .nav-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.stab === tab ||
      (tab === 'by_turnover' && b.dataset.stab === 'turnover') ||
      (tab === 'by_turnover_rate' && b.dataset.stab === 'rate') ||
      (tab === 'by_gain' && b.dataset.stab === 'gain') ||
      (tab === 'hot_tagged' && b.dataset.stab === 'hot'));
  });

  try {
    const resp = await apiGet('/api/didao/screener/top-stocks?top_n=10', 20000);
    if (resp.status !== 'ok') return;
    _screenerData = resp.data;
    const stocks = resp.data[tab] || [];
    const labelMap = {
      by_turnover: '成交额 TOP10',
      by_turnover_rate: '换手率 TOP10',
      by_gain: '涨幅 TOP10',
      hot_tagged: '热点标的 TOP10',
    };
    const html = '<table class="table"><thead><tr><th>#</th><th>代码</th><th>名称</th><th>最新价</th><th>涨跌幅</th><th>成交额(万)</th><th>换手率</th></tr></thead><tbody>' +
      stocks.map((s, i) => {
        const gainColor = s.change_pct >= 0 ? '#f85149' : '#26a69a';
        const sign = s.change_pct >= 0 ? '+' : '';
        return '<tr><td>' + (i + 1) + '</td><td>' + s.symbol + '</td><td>' + escHtml(s.name) + '</td>' +
          '<td>' + (s.price || 0).toFixed(2) + '</td>' +
          '<td style="color:' + gainColor + ';">' + sign + (s.change_pct || 0).toFixed(2) + '%</td>' +
          '<td>' + ((s.amount || 0) / 10000).toFixed(0) + '</td>' +
          '<td>' + (s.turnover_rate || 0).toFixed(2) + '%</td></tr>';
      }).join('') +
      '</tbody></table>';
    document.getElementById('didao-screener-table').innerHTML =
      '<h4 style="font-size:12px;color:#58a6ff;margin-bottom:8px;">' + (labelMap[tab] || tab) + '</h4>' + html;

    // Intersection tip
    const intersection = resp.data.intersection || [];
    if (intersection.length > 0) {
      document.getElementById('didao-screener-table').innerHTML +=
        '<div style="margin-top:8px;padding:6px;background:#1b3a1b;border-radius:4px;font-size:11px;">' +
        '<span style="color:#3fb950;">多维度交集:</span> ' +
        intersection.map(s => s.symbol + ' ' + s.name).join(' · ') + '</div>';
    }
  } catch (e) { console.error('Screener error:', e); }
}

async function loadDidaoHotConcepts() {
  try {
    const resp = await apiGet('/api/didao/screener/hot-concepts?top_n=20', 20000);
    if (resp.status !== 'ok') return;
    const concepts = resp.data.concepts || [];
    const keywords = resp.data.hot_keywords || [];
    const html = concepts.slice(0, 10).map(c => {
      const color = c.change_pct >= 0 ? '#f85149' : '#26a69a';
      const sign = c.change_pct >= 0 ? '+' : '';
      return '<span class="tag" style="margin:2px;background:#1c2128;color:' + color + ';">' +
        escHtml(c.name) + ' ' + sign + c.change_pct.toFixed(2) + '%</span>';
    }).join('');
    document.getElementById('didao-hot-concepts').innerHTML =
      '<div style="font-size:12px;color:#58a6ff;margin-bottom:4px;">热点概念</div>' + html +
      (keywords.length > 0 ? '<br><span style="font-size:10px;color:#8b949e;">关键词: ' +
        keywords.slice(0, 10).map(k => k[0]).join(' · ') + '</span>' : '');
  } catch (e) { console.error('Hot concepts error:', e); }
}

// ── Mindmap ──
export async function loadDidaoMindmap() {
  const panel = document.getElementById('didao-mindmap-panel');
  panel.classList.remove('hidden');
  const symbol = document.getElementById('didao-symbol')?.value || '000001';

  try {
    const resp = await apiGet('/api/didao/mindmap/' + symbol, 20000);
    if (resp.status !== 'ok') return;
    const treeData = resp.data;

    const dom = document.getElementById('didao-mindmap-chart');
    if (!dom) return;
    dom.style.display = '';

    // Dispose existing chart instance
    const existingInstance = echarts.getInstanceByDom(dom);
    if (existingInstance) existingInstance.dispose();

    const chart = echarts.init(dom);
    chart.setOption({
      backgroundColor: '#161b22',
      tooltip: { trigger: 'item', triggerOn: 'mousemove' },
      series: [{
        type: 'tree',
        data: [treeData],
        left: '5%',
        right: '15%',
        top: '5%',
        bottom: '5%',
        symbolSize: 10,
        orient: 'LR',
        label: {
          position: 'right',
          verticalAlign: 'middle',
          align: 'left',
          fontSize: 11,
          color: '#c9d1d9',
        },
        leaves: {
          label: {
            position: 'right',
            verticalAlign: 'middle',
            align: 'left',
            fontSize: 10,
            color: '#8b949e',
          },
        },
        expandAndCollapse: true,
        animationDuration: 550,
        animationDurationUpdate: 750,
      }],
    });
  } catch (e) { console.error('Mindmap error:', e); }
}

// ── Legacy didao sub-loaders ──
async function loadDidaoMarketLayer(symbol, days) {
  try {
    const resp = await apiGet('/api/sancai/didao/market?symbol=' + symbol + '&days=' + Math.min(days, 60), 20000);
    if (resp.status !== 'ok') return;
    const data = resp.data.data || [];
    const latest = data[data.length - 1];
    if (latest) {
      document.getElementById('didao-market-summary').innerHTML =
        '最新: ' + latest.close.toFixed(2) +
        ' | 开: ' + latest.open.toFixed(2) +
        ' | 高: ' + latest.high.toFixed(2) +
        ' | 低: ' + latest.low.toFixed(2) +
        ' | 量: ' + (latest.volume / 10000).toFixed(1) + '万手';
    }
  } catch (e) { /* ignore */ }
}

async function loadDidaoResearch(symbol, days) {
  try {
    const resp = await apiGet('/api/sancai/didao/research?symbol=' + symbol + '&days=' + days, 30000);
    if (resp.status !== 'ok') return;
    const reports = resp.data.reports || [];
    const html = reports.length === 0
      ? '<p style="color:#8b949e;">暂无个股研报</p>'
      : reports.map(r =>
          '<div style="padding:6px 0;border-bottom:1px solid #21262d;font-size:12px;">' +
          '<span style="color:#3fb950;">[' + escHtml(r.org) + ']</span> ' +
          '<span class="tag tag-buy" style="margin:0 4px;">' + escHtml(r.rating) + '</span> ' +
          escHtml(r.title) +
          (r.target_price ? ' <span style="color:#8b949e;">目标' + escHtml(r.target_price) + '</span>' : '') +
          '</div>'
        ).join('');
    document.getElementById('didao-research-list').innerHTML = html;
  } catch (e) { /* ignore */ }
}

async function loadDidaoFundamental(symbol) {
  try {
    const resp = await apiGet('/api/sancai/didao/fundamental?symbol=' + symbol, 30000);
    if (resp.status !== 'ok') return;
    const fd = resp.data;
    const fundamentals = fd.fundamentals || {};
    const keys = ['基本每股收益', '加权平均净资产收益率', '营业收入', '归属于母公司所有者的净利润'];
    const rows = keys.map(k => {
      const v = fundamentals[k];
      return '<tr><td>' + k + '</td><td>' + (v?.latest != null ? v.latest.toFixed(4) : '--') + '</td></tr>';
    }).join('');
    document.getElementById('didao-fundamental-table').innerHTML =
      '<table class="table"><thead><tr><th>指标</th><th>最新值</th></tr></thead><tbody>' + rows + '</tbody></table>';

    const ratings = fd.ratings || [];
    document.getElementById('didao-ratings-list').innerHTML = ratings.length === 0
      ? '' : '<h4 style="font-size:12px;color:#58a6ff;margin:12px 0 4px;">近期评级</h4>' +
        ratings.slice(0, 5).map(r =>
          '<span class="tag tag-buy" style="margin:2px;">' + escHtml(r.rating) + '</span> '
        ).join('');

    const flow = fd.fund_flow;
    if (flow) {
      document.getElementById('didao-flow-summary').innerHTML =
        '近10日主力净流入: <span style="color:' + (flow.recent_net >= 0 ? '#f85149' : '#26a69a') + ';">' +
        (flow.recent_net / 10000).toFixed(1) + '万</span> | ' +
        '流入天数: ' + flow.positive_days + '/10';
    }
  } catch (e) { /* ignore */ }
}

async function loadDidaoAnnouncements(symbol, days) {
  try {
    const resp = await apiGet('/api/sancai/didao/announcements?symbol=' + symbol + '&days=' + days, 30000);
    if (resp.status !== 'ok') return;
    const announcements = resp.data.announcements || [];
    const html = announcements.length === 0
      ? '<p style="color:#8b949e;">' + (resp.data.note || '暂无公告') + '</p>'
      : announcements.map(a => {
          const vals = Object.values(a).filter(v => v).slice(0, 3);
          return '<div style="padding:4px 0;border-bottom:1px solid #21262d;font-size:12px;">' +
            escHtml(vals.join(' | ')) + '</div>';
        }).join('');
    document.getElementById('didao-announcements-list').innerHTML = html;
  } catch (e) { /* ignore */ }
}

// ═══════════════════════════════════════════
// 人道 (Rendao)
// ═══════════════════════════════════════════

export async function loadRendao() {
  // Fire profile check and timeline in parallel
  const profilePromise = apiGet('/api/rendao/quiz/profile', 10000);
  const tlPromise = apiGet('/api/sancai/rendao/timeline', 30000);

  // Process profile
  try {
    const profileResp = await profilePromise;
    if (profileResp.status === 'ok' && profileResp.has_profile) {
      _userProfile = profileResp;
      document.getElementById('rendao-quiz-section').classList.add('hidden');
      document.getElementById('rendao-profile-section').classList.remove('hidden');
      loadRendaoProfile(profileResp);
      loadRendaoPlan();
    } else {
      document.getElementById('rendao-quiz-section').classList.remove('hidden');
      document.getElementById('rendao-profile-section').classList.add('hidden');
      loadRendaoQuiz();
    }
  } catch (e) {
    console.error('Profile check error:', e);
    document.getElementById('rendao-quiz-section').classList.remove('hidden');
    loadRendaoQuiz();
  }

  // Process timeline (runs concurrently with profile)
  try {
    const tl = await tlPromise;
    if (tl.status === 'ok') {
      document.getElementById('rendao-positions-list').innerHTML =
        (tl.data.positions || []).map(s =>
          '<span class="tag tag-buy" style="margin:2px;">' + s + '</span>'
        ).join(' ') + ' <span style="color:#8b949e;font-size:11px;">(模拟持仓)</span>';
      renderTimeline('rendao-timeline-chart', 'rendao-event-list', {
        price_data: null,
        events: tl.data.events || [],
      }, { priceLabel: '持仓聚合', showCandlestick: false });
      document.getElementById('rendao-assessment').innerHTML =
        '<span style="font-size:24px;color:#d2991d;">模拟监控中</span>';
      const gua = tl.data.gua || {};
      if (gua.hexagram) {
        const dirTag = gua.direction === '吉' ? '<span class="tag tag-ji" style="font-size:10px;">吉</span>' :
                       gua.direction === '凶' ? '<span class="tag tag-xiong" style="font-size:10px;">凶</span>' :
                       '<span class="tag tag-ping" style="font-size:10px;">平</span>';
        document.getElementById('rendao-gua-info').innerHTML =
          '<span style="font-size:20px;color:' + (gua.color || '#3fb950') + ';">' + (gua.symbol || '') + '</span> ' +
          '<span style="color:#e6f1ff;">' + escHtml(gua.yao_ci || '') + '</span> ' +
          '<span style="color:' + (gua.color || '#8b949e') + ';">' + escHtml(gua.hexagram || '') + '卦·' + escHtml(gua.nature || '') + '</span>' +
          ' ' + dirTag +
          '<div style="margin-top:4px;color:#8b949e;">' + escHtml(gua.yao_meaning || '') + '</div>' +
          '<div style="margin-top:2px;color:#58a6ff;">' + escHtml(gua.advice || '') + ' | ' + escHtml(gua.detail || '') + '</div>';
      }
    }
  } catch (e) {
    console.error('Rendao load error:', e);
    showError('rendao');
  }
}

// ── Quiz ──
async function loadRendaoQuiz() {
  try {
    const resp = await apiGet('/api/rendao/quiz', 10000);
    if (resp.status !== 'ok') return;
    _quizQuestions = resp.questions || [];
    const html = _quizQuestions.map((q, qi) => {
      const optionsHtml = q.options.map((opt, oi) =>
        '<label style="display:block;padding:4px 8px;margin:2px 0;cursor:pointer;border-radius:4px;" onmouseover="this.style.background=\'#1c2128\'" onmouseout="this.style.background=\'transparent\'">' +
        '<input type="radio" name="quiz-q' + q.id + '" value="' + oi + '" style="margin-right:8px;">' +
        escHtml(opt.label) + '</label>'
      ).join('');
      return '<div style="margin-bottom:12px;padding:8px;background:#0d1117;border-radius:6px;">' +
        '<div style="color:#e6f1ff;font-weight:bold;margin-bottom:4px;">' + q.id + '. ' + escHtml(q.question) + '</div>' +
        optionsHtml + '</div>';
    }).join('');
    document.getElementById('rendao-quiz-questions').innerHTML = html;
  } catch (e) {
    console.error('Quiz load error:', e);
    document.getElementById('rendao-quiz-questions').innerHTML =
      '<span style="color:#f85149;">问卷加载失败</span>';
  }
}

async function submitQuiz() {
  if (!_quizQuestions) return;
  const answers = [];
  let allAnswered = true;
  _quizQuestions.forEach(q => {
    const selected = document.querySelector('input[name="quiz-q' + q.id + '"]:checked');
    if (selected) {
      answers.push({ question_id: q.id, selected_option: parseInt(selected.value) });
    } else {
      allAnswered = false;
    }
  });

  if (!allAnswered) {
    document.getElementById('rendao-quiz-result').innerHTML =
      '<span style="color:#f85149;">请回答全部问题</span>';
    return;
  }

  try {
    const postResp = await fetch('/api/rendao/quiz/submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers }),
    });
    const data = await postResp.json();

    if (data.status === 'ok') {
      document.getElementById('rendao-quiz-result').innerHTML =
        '<span style="color:#3fb950;">测评完成! 类型: ' + data.label + '</span>';
      // Reload rendao
      setTimeout(() => loadRendao(), 500);
    } else {
      document.getElementById('rendao-quiz-result').innerHTML =
        '<span style="color:#f85149;">提交失败: ' + (data.message || '') + '</span>';
    }
  } catch (e) {
    document.getElementById('rendao-quiz-result').innerHTML =
      '<span style="color:#f85149;">提交失败: ' + e.message + '</span>';
  }
}

function loadRendaoProfile(profile) {
  document.getElementById('rendao-profile-label').textContent = profile.label || '--';
  document.getElementById('rendao-profile-desc').textContent = profile.description || '--';
}

// ── Position plan ──
async function loadRendaoPlan() {
  try {
    const resp = await apiGet('/api/rendao/plan/position', 15000);
    if (resp.status !== 'ok') return;
    const plan = resp.plan;
    document.getElementById('rendao-plan-content').innerHTML =
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">' +
      '<div class="stat-item"><div class="stat-value">' + plan.total_position_pct + '%</div><div class="stat-label">目标仓位</div></div>' +
      '<div class="stat-item"><div class="stat-value">' + plan.max_single_pct + '%</div><div class="stat-label">单票上限</div></div>' +
      '<div class="stat-item"><div class="stat-value">' + plan.stop_loss_pct + '%</div><div class="stat-label">止损线</div></div>' +
      '<div class="stat-item"><div class="stat-value">' + plan.take_profit_pct + '%</div><div class="stat-label">止盈线</div></div>' +
      '<div class="stat-item"><div class="stat-value">' + plan.hold_days + '</div><div class="stat-label">持有周期</div></div>' +
      '<div class="stat-item"><div class="stat-value">' + plan.max_positions + '</div><div class="stat-label">最大持仓数</div></div>' +
      '</div>' +
      '<div style="margin-top:8px;font-size:11px;color:#8b949e;">' +
      '<div>加仓: ' + plan.add_position_rule + '</div>' +
      '<div>减仓: ' + plan.reduce_rule + '</div>' +
      '<div>清仓: ' + plan.clear_rule + '</div></div>';

    // Load pre-market plan
    loadRendaoPreMarket();
  } catch (e) { console.error('Plan error:', e); }
}

async function loadRendaoPreMarket() {
  const today = new Date().toISOString().slice(0, 10);
  try {
    const resp = await fetch('/api/rendao/plan/pre-market', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: today }),
    });
    const data = await resp.json();
    if (data.status !== 'ok') return;
    document.getElementById('rendao-premarket-date').textContent = '(' + data.date + ')';
    const tiandaoColor = data.tiandao.direction === '吉' ? '#3fb950' : data.tiandao.direction === '凶' ? '#f85149' : '#d2991d';
    document.getElementById('rendao-premarket-content').innerHTML =
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px;">' +
      '<div><strong>天道方向:</strong> <span style="color:' + tiandaoColor + ';">' + data.tiandao.direction + ' ' + data.tiandao.yao_ci + '</span></div>' +
      '<div><strong>操作策略:</strong> <span style="color:#58a6ff;">' + data.action + '</span></div>' +
      '<div style="grid-column:1/-1;"><strong>建议:</strong> ' + data.suggested_action + '</div>' +
      '<div style="grid-column:1/-1;"><strong>关键规则:</strong> ' +
        '加仓: ' + data.key_levels.add_rule + ' | 减仓: ' + data.key_levels.reduce_rule + '</div>' +
      (data.top_watchlist && data.top_watchlist.length > 0 ?
        '<div style="grid-column:1/-1;"><strong>重点关注:</strong> ' +
        data.top_watchlist.map(s => s.symbol + '(' + s.score + '分)').join(' · ') + '</div>' : '') +
      '</div>';
  } catch (e) { console.error('Pre-market error:', e); }
}

// ═══════════════════════════════════════════
// 三才合一 (Alignment)
// ═══════════════════════════════════════════

async function loadAlignment() {
  try {
    const resp = await apiGet('/api/sancai/alignment', 30000);
    if (resp.status !== 'ok') return;
    const bar = document.getElementById('sancai-alignment-bar');
    if (!bar) return;
    bar.style.display = '';
    const a = resp.data;
    const guaInfo = a.gua || {};
    const guaLine = [
      (guaInfo.tiandao && guaInfo.tiandao.symbol ? guaInfo.tiandao.symbol + '天道·' + (guaInfo.tiandao.yao_ci || '') : ''),
      (guaInfo.didao && guaInfo.didao.symbol ? guaInfo.didao.symbol + '地道·' + (guaInfo.didao.yao_ci || '') : ''),
      (guaInfo.rendao && guaInfo.rendao.symbol ? guaInfo.rendao.symbol + '人道·' + (guaInfo.rendao.yao_ci || '') : ''),
    ].filter(Boolean).join(' | ');
    bar.innerHTML = '<div style="font-size:16px;">' + a.signal + '</div>' +
      (guaLine ? '<div style="font-size:11px;margin-top:2px;opacity:0.8;">' + guaLine + '</div>' : '');
    if (a.aligned) {
      bar.style.background = 'linear-gradient(135deg, #1b3a1b, #0d3320)';
      bar.style.color = '#3fb950';
    } else if (a.signal.includes('凶')) {
      bar.style.background = 'linear-gradient(135deg, #3a1b1b, #330d0d)';
      bar.style.color = '#f85149';
    } else {
      bar.style.background = 'linear-gradient(135deg, #3a351b, #332d0d)';
      bar.style.color = '#d2991d';
    }
  } catch (e) { /* ignore */ }
}

// ═══════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════

function showError(tier) {
  const el = document.getElementById('sancai-' + tier);
  if (!el) return;
  const contentEl = el.querySelector('.sancai-error');
  if (contentEl) {
    contentEl.innerHTML = '<div style="text-align:center;color:#f85149;padding:40px;">数据加载失败，请检查服务器连接</div>';
  }
}

function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
