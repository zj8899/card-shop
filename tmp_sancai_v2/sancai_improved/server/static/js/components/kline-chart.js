// ══════════════════════════════════════════════════════════════
// K-line chart component (ECharts candlestick + MA + volume + KDJ)
// ══════════════════════════════════════════════════════════════

const MA_COLORS = { ma_5: '#ffb020', ma_10: '#16b4ff', ma_21: '#7c6bff', ma_55: '#00e2a8', ma_120: '#ff4d6a' };

export function renderKlineChart(el, records, { visibleMas = ['ma_5', 'ma_10', 'ma_21'] } = {}) {
  if (typeof echarts === 'undefined') { console.warn('ECharts not loaded'); return null; }
  const chart = echarts.getInstanceByDom(el) || echarts.init(el, null, { renderer: 'canvas' });
  const dates = records.map((r) => String(r.date).slice(0, 16));
  const ohlc = records.map((r) => [r.open, r.close, r.low, r.high]);
  const volumes = records.map((r, i) => ({ value: r.volume, itemStyle: { color: r.close >= r.open ? '#f6465d' : '#0ecb81' } }));

  const maSeries = visibleMas.map((key) => ({
    name: key.toUpperCase(),
    type: 'line',
    data: records.map((r) => r[key] ?? null),
    smooth: true,
    symbol: 'none',
    lineStyle: { width: 1.1, color: MA_COLORS[key] || '#93a1b8' },
    xAxisIndex: 0, yAxisIndex: 0,
  }));

  chart.setOption({
    backgroundColor: 'transparent',
    animation: false,
    grid: [
      { left: 56, right: 16, top: 16, height: '58%' },
      { left: 56, right: 16, top: '68%', height: '18%' },
    ],
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#17202f',
      borderColor: '#232e42',
      textStyle: { color: '#eef3fb', fontSize: 11, fontFamily: 'JetBrains Mono, monospace' },
      axisPointer: { type: 'cross', crossStyle: { color: '#5c6a84' } },
    },
    xAxis: [
      { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#232e42' } }, axisLabel: { color: '#5c6a84', fontSize: 10 }, splitLine: { show: false } },
      { type: 'category', gridIndex: 1, data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#232e42' } }, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    yAxis: [
      { scale: true, position: 'left', axisLine: { show: false }, axisLabel: { color: '#5c6a84', fontSize: 10 }, splitLine: { lineStyle: { color: '#1b2434' } } },
      { scale: true, gridIndex: 1, axisLine: { show: false }, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], top: '90%', height: 14, borderColor: '#232e42', fillerColor: 'rgba(0,226,168,0.1)', handleStyle: { color: '#16b4ff' }, textStyle: { color: '#5c6a84', fontSize: 9 } },
    ],
    series: [
      {
        name: 'K线', type: 'candlestick', data: ohlc,
        itemStyle: { color: '#f6465d', color0: '#0ecb81', borderColor: '#f6465d', borderColor0: '#0ecb81' },
      },
      ...maSeries,
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volumes },
    ],
  }, true);

  return chart;
}

export function renderKdjChart(el, records) {
  if (typeof echarts === 'undefined') { console.warn('ECharts not loaded'); return null; }
  const chart = echarts.getInstanceByDom(el) || echarts.init(el, null, { renderer: 'canvas' });
  const dates = records.map((r) => String(r.date).slice(0, 16));
  chart.setOption({
    backgroundColor: 'transparent',
    animation: false,
    grid: { left: 56, right: 16, top: 20, bottom: 24 },
    tooltip: { trigger: 'axis', backgroundColor: '#17202f', borderColor: '#232e42', textStyle: { color: '#eef3fb', fontSize: 11 } },
    legend: { data: ['K', 'D', 'J'], top: 0, right: 16, textStyle: { color: '#93a1b8', fontSize: 10 }, itemWidth: 10, itemHeight: 10 },
    xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: '#232e42' } }, axisLabel: { color: '#5c6a84', fontSize: 9 } },
    yAxis: { scale: true, axisLine: { show: false }, axisLabel: { color: '#5c6a84', fontSize: 9 }, splitLine: { lineStyle: { color: '#1b2434' } } },
    series: [
      { name: 'K', type: 'line', data: records.map((r) => r.kdj_k ?? null), symbol: 'none', lineStyle: { width: 1, color: '#ffb020' } },
      { name: 'D', type: 'line', data: records.map((r) => r.kdj_d ?? null), symbol: 'none', lineStyle: { width: 1, color: '#16b4ff' } },
      { name: 'J', type: 'line', data: records.map((r) => r.kdj_j ?? null), symbol: 'none', lineStyle: { width: 1, color: '#7c6bff' } },
    ],
  }, true);
  return chart;
}

export function renderTickChart(el, ticks) {
  if (typeof echarts === 'undefined') { console.warn('ECharts not loaded'); return null; }
  const chart = echarts.getInstanceByDom(el) || echarts.init(el, null, { renderer: 'canvas' });
  const times = ticks.map((t) => t.time);
  chart.setOption({
    backgroundColor: 'transparent',
    animation: false,
    grid: { left: 56, right: 16, top: 16, bottom: 24 },
    tooltip: { trigger: 'axis', backgroundColor: '#17202f', borderColor: '#232e42', textStyle: { color: '#eef3fb', fontSize: 11 } },
    xAxis: { type: 'category', data: times, axisLine: { lineStyle: { color: '#232e42' } }, axisLabel: { color: '#5c6a84', fontSize: 9 } },
    yAxis: { scale: true, axisLine: { show: false }, axisLabel: { color: '#5c6a84', fontSize: 9 }, splitLine: { lineStyle: { color: '#1b2434' } } },
    series: [{
      name: '价格', type: 'line', data: ticks.map((t) => t.price), symbol: 'none',
      lineStyle: { width: 1.4, color: '#16b4ff' },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: 'rgba(22,180,255,0.22)' }, { offset: 1, color: 'rgba(22,180,255,0)' }] } },
    }],
  }, true);
  return chart;
}

export function resizeChart(chart) { chart?.resize(); }
