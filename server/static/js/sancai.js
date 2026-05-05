// Sancai tier page loaders: Tiandao, Didao, Rendao
import { apiGet } from './api.js';
import { renderTimeline } from './timeline.js';

let currentSancaiSub = 'tiandao';
let sancaiTimeRange = 60;

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

// === Tiandao (天道) ===
export async function loadTiandao() {
  const days = sancaiTimeRange;
  try {
    const tl = await apiGet('/api/sancai/tiandao/timeline?days=' + days, 30000);
    if (tl.status !== 'ok') { showError('tiandao'); return; }

    // Timeline
    renderTimeline('tiandao-timeline-chart', 'tiandao-event-list', tl.data, {
      priceLabel: '沪深300',
      showCandlestick: false,
    });

    // Assessment header
    const events = tl.data.events || [];
    const bullCount = events.filter(e => e.layer === 'fundamental' || e.title.includes('低估')).length;
    const bearCount = events.filter(e => e.title.includes('跌') && e.importance >= 2).length;
    const assessment = bearCount > 3 ? '凶' : (bullCount >= 1 ? '吉' : '平');
    const assCls = assessment === '吉' ? 'tag-ji' : (assessment === '凶' ? 'tag-xiong' : 'tag-ping');
    document.getElementById('tiandao-assessment').innerHTML =
      '<span class="tag ' + assCls + '" style="font-size:36px;">' + assessment + '</span>';
    document.getElementById('tiandao-assessment-sub').textContent =
      '沪深300趋势 | PE分位数估值 | 宏观事件: ' + tl.data.event_count + '条';

    // Load market layer
    loadTiandaoMarket(days);
    // Load research layer
    loadTiandaoResearch(days);
    // Load fundamental layer
    loadTiandaoFundamental();
    // Load sectors
    loadTiandaoSectors();
  } catch (e) {
    console.error('Tiandao load error:', e);
    showError('tiandao');
  }
}

async function loadTiandaoMarket(days) {
  try {
    const resp = await apiGet('/api/sancai/tiandao/market?days=' + days, 30000);
    if (resp.status !== 'ok') return;
    const d = resp.data;
    const rows = Object.entries(d).map(([code, info]) => {
      const pct = info.change_pct || 0;
      const color = pct >= 0 ? '#f85149' : '#26a69a';
      const sign = pct >= 0 ? '+' : '';
      return '<tr><td>' + info.name + '</td><td>' + (info.latest || 0).toFixed(2) + '</td>' +
        '<td style="color:' + color + ';">' + sign + pct.toFixed(2) + '%</td>' +
        '<td>' + (code === 'sh000300' ? '沪深300' : code === 'sh000001' ? '上证' : '深证') + '</td></tr>';
    }).join('');
    document.getElementById('tiandao-market-table').innerHTML =
      '<table class="table"><thead><tr><th>指数</th><th>收盘</th><th>涨跌</th><th>类型</th></tr></thead><tbody>' + rows + '</tbody></table>';
  } catch (e) { /* ignore */ }
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
    // Show first 30 sector items
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

// === Didao (地道) ===
export async function loadDidao() {
  const symbol = document.getElementById('didao-symbol')?.value || '000001';
  const days = sancaiTimeRange;
  try {
    // Load score
    const scoreResp = await apiGet('/api/sancai/didao/score?symbol=' + symbol, 30000);
    if (scoreResp.status === 'ok') {
      const sc = scoreResp.data;
      const assCls = sc.assessment === '吉' ? 'tag-ji' : (sc.assessment === '凶' ? 'tag-xiong' : 'tag-ping');
      document.getElementById('didao-assessment').innerHTML =
        '<span class="tag ' + assCls + '" style="font-size:36px;">' + sc.assessment + '</span>';
      document.getElementById('didao-assessment-sub').textContent =
        '评分: ' + sc.score + '/100 | 最新价: ' + sc.latest_price.toFixed(2);
    }

    // Timeline
    const tl = await apiGet('/api/sancai/didao/timeline?symbol=' + symbol + '&days=' + days, 30000);
    if (tl.status === 'ok') {
      renderTimeline('didao-timeline-chart', 'didao-event-list', tl.data, {
        priceLabel: symbol,
        showCandlestick: true,
      });
    }

    // Research layer
    loadDidaoResearch(symbol, days);
    // Fundamental layer
    loadDidaoFundamental(symbol);
    // Announcements layer
    loadDidaoAnnouncements(symbol, days);
    // Market layer detail
    loadDidaoMarketLayer(symbol, days);
  } catch (e) {
    console.error('Didao load error:', e);
    showError('didao');
  }
}

export function onDidaoSymbolChange() {
  loadDidao();
}

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

    // Key indicators
    const keys = ['基本每股收益', '加权平均净资产收益率', '营业收入', '归属于母公司所有者的净利润'];
    const rows = keys.map(k => {
      const v = fundamentals[k];
      return '<tr><td>' + k + '</td><td>' + (v?.latest != null ? v.latest.toFixed(4) : '--') + '</td></tr>';
    }).join('');
    document.getElementById('didao-fundamental-table').innerHTML =
      '<table class="table"><thead><tr><th>指标</th><th>最新值</th></tr></thead><tbody>' + rows + '</tbody></table>';

    // Recent ratings
    const ratings = fd.ratings || [];
    document.getElementById('didao-ratings-list').innerHTML = ratings.length === 0
      ? '' : '<h4 style="font-size:12px;color:#58a6ff;margin:12px 0 4px;">近期评级</h4>' +
        ratings.slice(0, 5).map(r =>
          '<span class="tag tag-buy" style="margin:2px;">' + escHtml(r.rating) + '</span> '
        ).join('');

    // Fund flow
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

// === Rendao (人道) ===
export async function loadRendao() {
  try {
    const tl = await apiGet('/api/sancai/rendao/timeline', 30000);
    if (tl.status === 'ok') {
      document.getElementById('rendao-positions-list').innerHTML =
        (tl.data.positions || []).map(s =>
          '<span class="tag tag-buy" style="margin:2px;">' + s + '</span>'
        ).join(' ') + ' <span style="color:#8b949e;font-size:11px;">(模拟持仓)</span>';

      // Render mini timeline
      renderTimeline('rendao-timeline-chart', 'rendao-event-list', {
        price_data: null, // No single price line for multi-stock
        events: tl.data.events || [],
      }, { priceLabel: '持仓聚合', showCandlestick: false });

      document.getElementById('rendao-assessment').innerHTML =
        '<span style="font-size:24px;color:#d2991d;">模拟监控中</span>';
    }
  } catch (e) {
    console.error('Rendao load error:', e);
    showError('rendao');
  }
}

// === Alignment (三才合一) ===
async function loadAlignment() {
  try {
    const resp = await apiGet('/api/sancai/alignment', 30000);
    if (resp.status !== 'ok') return;
    const bar = document.getElementById('sancai-alignment-bar');
    if (!bar) return;
    bar.style.display = '';
    const a = resp.data;
    bar.textContent = a.signal;
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

// === Helpers ===
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
