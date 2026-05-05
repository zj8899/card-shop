// ECharts instance manager
const instances = {};

export function getChart(domId) {
  const dom = document.getElementById(domId);
  if (!dom) return null;
  if (!instances[domId]) {
    instances[domId] = echarts.init(dom);
  }
  return instances[domId];
}

export function disposeChart(domId) {
  if (instances[domId]) {
    instances[domId].dispose();
    delete instances[domId];
  }
}

export function disposeAll() {
  Object.keys(instances).forEach(k => {
    instances[k].dispose();
    delete instances[k];
  });
}

export function resizeAll() {
  Object.values(instances).forEach(c => c.resize());
}

// Common ECharts option defaults
export const DARK_THEME = {
  backgroundColor: '#161b22',
  textStyle: { color: '#c9d1d9' },
};

export const DARK_AXIS = {
  axisLabel: { color: '#8b949e', fontSize: 10 },
  splitLine: { lineStyle: { color: '#21262d' } },
};

export const LAYER_COLORS = {
  market: '#58a6ff',
  research: '#3fb950',
  news: '#d2991d',
  fundamental: '#a371f7',
  announcement: '#f85149',
};
