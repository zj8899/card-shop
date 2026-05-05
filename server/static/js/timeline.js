// Unified timeline component: ECharts chart + HTML event list
import { getChart, DARK_AXIS, LAYER_COLORS } from './charts.js';

const LAYER_LABELS = {
  market: '行情', research: '研报', news: '新闻',
  fundamental: '基础数据', announcement: '公告',
};

export function renderTimeline(chartDomId, listDomId, data, options = {}) {
  const { priceLabel = '价格', showCandlestick = false } = options;

  // === Event list (HTML) ===
  renderEventList(listDomId, data.events || []);

  // === ECharts chart ===
  const chart = getChart(chartDomId);
  if (!chart) return;

  const pd = data.price_data;
  if (!pd || !pd.dates || pd.dates.length === 0) {
    chart.setOption({
      backgroundColor: '#161b22',
      title: { text: '暂无价格数据', left: 'center', top: 'center', textStyle: { color: '#8b949e' } },
    }, true);
    return;
  }

  const dates = pd.dates;
  const series = [];
  const legends = [];

  if (showCandlestick && pd.open && pd.high && pd.low) {
    const ohlc = dates.map((_, i) => [pd.open[i], pd.close[i], pd.low[i], pd.high[i]]);
    series.push({
      name: 'K线', type: 'candlestick', data: ohlc,
      itemStyle: { color: '#26a69a', color0: '#ef5350', borderColor: '#26a69a', borderColor0: '#ef5350' },
    });
    legends.push('K线');
  } else {
    series.push({
      name: priceLabel, type: 'line', data: pd.close, smooth: true, symbol: 'none',
      lineStyle: { width: 2, color: '#58a6ff' },
    });
    legends.push(priceLabel);
  }

  // Mark points for events
  const events = data.events || [];
  const layerMarkPoints = {};
  events.forEach(e => {
    if (!e.date) return;
    const idx = dates.indexOf(e.date);
    if (idx < 0) return;
    const layer = e.layer || 'market';
    if (!layerMarkPoints[layer]) {
      layerMarkPoints[layer] = { data: [] };
    }
    layerMarkPoints[layer].data.push({
      coord: [idx, pd.high ? pd.high[idx] : pd.close[idx]],
      value: (e.title || '').substring(0, 20),
      symbol: 'pin',
      symbolSize: 24,
      itemStyle: { color: LAYER_COLORS[layer] || '#8b949e' },
    });
  });

  // Add markPoint series per layer
  Object.entries(layerMarkPoints).forEach(([layer, mp]) => {
    series.push({
      name: LAYER_LABELS[layer] || layer,
      type: 'line',
      data: [],
      symbol: 'none',
      markPoint: { data: mp.data, symbolSize: 28, label: { fontSize: 9, color: '#c9d1d9' } },
    });
    legends.push(LAYER_LABELS[layer] || layer);
  });

  chart.setOption({
    backgroundColor: '#161b22',
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    legend: {
      data: legends, top: 0, textStyle: { color: '#8b949e', fontSize: 11 },
    },
    grid: { left: '8%', right: '8%', top: '12%', bottom: '10%' },
    xAxis: { type: 'category', data: dates, ...DARK_AXIS },
    yAxis: { type: 'value', scale: true, ...DARK_AXIS },
    dataZoom: [{ type: 'inside' }, { type: 'slider', bottom: 0, height: 15 }],
    series,
  }, true);

  // Click handler: chart event → scroll event list
  chart.off('click');
  chart.on('click', function (params) {
    if (params.componentType === 'markPoint' && params.data && params.data.value) {
      const title = params.data.value;
      const list = document.getElementById(listDomId);
      if (list) {
        const items = list.querySelectorAll('.timeline-event');
        items.forEach(item => {
          if (item.textContent.includes(title)) {
            item.scrollIntoView({ behavior: 'smooth', block: 'center' });
            item.style.background = '#1c2847';
            setTimeout(() => { item.style.background = ''; }, 2000);
          }
        });
      }
    }
  });
}

export function renderEventList(listDomId, events) {
  const list = document.getElementById(listDomId);
  if (!list) return;

  if (!events || events.length === 0) {
    list.innerHTML = '<div style="color:#8b949e;text-align:center;padding:30px;">暂无事件</div>';
    return;
  }

  // Group by date
  const grouped = {};
  events.forEach(e => {
    const d = e.date || '未知';
    if (!grouped[d]) grouped[d] = [];
    grouped[d].push(e);
  });

  const html = Object.entries(grouped)
    .sort(([a], [b]) => b.localeCompare(a))
    .map(([date, evts]) => {
      const items = evts.map(e => {
        const color = LAYER_COLORS[e.layer] || '#8b949e';
        const label = LAYER_LABELS[e.layer] || e.layer;
        return `<div class="timeline-event" style="display:flex;align-items:flex-start;gap:8px;padding:6px 8px;border-bottom:1px solid #161b22;cursor:pointer;font-size:12px;">
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-top:4px;flex-shrink:0;"></span>
          <span style="color:#8b949e;min-width:48px;flex-shrink:0;">[${label}]</span>
          <span style="flex:1;">${escHtml(e.title || '')}</span>
          ${e.importance >= 2 ? '<span style="color:#f85149;">!</span>' : ''}
        </div>`;
      }).join('');
      return `<div style="margin-bottom:8px;">
        <div style="font-size:12px;color:#58a6ff;padding:4px 0;border-bottom:1px solid #21262d;">${date}</div>
        ${items}
      </div>`;
    }).join('');

  list.innerHTML = html;
}

function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
