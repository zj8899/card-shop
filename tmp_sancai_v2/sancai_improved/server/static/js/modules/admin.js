// ══════════════════════════════════════════════════════════════
// Admin / System config module
// ══════════════════════════════════════════════════════════════
import { api } from '../core/api.js';
import { createStore, bindRender } from '../core/store.js';
import { h, toast } from '../core/dom.js';
import { EmptyState, SkeletonRows } from '../components/ui.js';

const store = createStore({
  config: { loading: true, data: null },
  saving: false,
  testingFeishu: false,
  usage: { loading: true, data: null },
  usageDays: 30,
  feishuToggles: null,
  togglesSaving: false,
});

let root;
let usageChart;

export async function mount(container) {
  root = container;
  render(store.get());
  bindRender(store, (state) => { render(state); paintUsageChart(state); });
  await Promise.all([loadConfig(), loadUsage()]);
  loadFeishuToggles();
}

async function loadConfig() {
  store.set({ config: { loading: true, data: null } });
  try {
    const res = await api.get('/api/admin/config');
    store.set({ config: { loading: false, data: res?.data || res } });
  } catch (e) {
    store.set({ config: { loading: false, data: null, error: e.message } });
  }
}

async function loadUsage() {
  const days = store.get().usageDays;
  store.set({ usage: { loading: true, data: null } });
  try {
    const res = await api.get(`/api/admin/ai-usage?days=${days}`);
    store.set({ usage: { loading: false, data: res?.data || res } });
  } catch (e) {
    store.set({ usage: { loading: false, data: null, error: e.message } });
  }
}

function setUsageDays(days) { store.set({ usageDays: days }); loadUsage(); }

async function loadFeishuToggles() {
  try {
    const res = await api.get('/api/admin/feishu-toggles', { timeoutMs: 5000 });
    store.set({ feishuToggles: res?.data || res });
  } catch (e) { /* silent */ }
}

async function saveFeishuToggles() {
  const t = store.get().feishuToggles || {};
  store.set({ togglesSaving: true });
  try {
    const res = await api.post('/api/admin/feishu-toggles', t, { timeoutMs: 8000 });
    toast('飞书推送开关已保存', { type: 'success' });
  } catch (e) { toast('保存失败: ' + (e.message || e), { type: 'error' }); }
  store.set({ togglesSaving: false });
}

async function saveConfig(formEl) {
  const fd = new FormData(formEl);
  const payload = {
    ai: {
      deepseek_key: fd.get('deepseek_key') || '',
      deepseek_model: fd.get('deepseek_model') || '',
      doubao_key: fd.get('doubao_key') || '',
      doubao_model: fd.get('doubao_model') || '',
      anthropic_key: fd.get('anthropic_key') || '',
    },
    feishu: {
      enabled: fd.get('feishu_enabled') === 'on',
      webhook_url: fd.get('feishu_webhook') || '',
      poll_interval_minutes: Number(fd.get('feishu_interval')) || 5,
    },
    firecrawl: {
      enabled: fd.get('firecrawl_enabled') === 'on',
      api_key: fd.get('firecrawl_key') || '',
    },
  };
  store.set({ saving: true });
  try {
    await api.post('/api/admin/config', payload);
    toast('配置已保存', { type: 'success' });
    loadConfig();
  } catch (e) {
    toast(`保存失败: ${e.message}`, { type: 'error' });
  } finally {
    store.set({ saving: false });
  }
}

async function testFeishu() {
  store.set({ testingFeishu: true });
  try {
    await api.post('/api/admin/feishu-test', {});
    toast('飞书测试消息已发送', { type: 'success' });
  } catch (e) {
    toast(`测试失败: ${e.message}`, { type: 'error' });
  } finally {
    store.set({ testingFeishu: false });
  }
}

function paintUsageChart(state) {
  requestAnimationFrame(() => {
    const el = document.getElementById('ai-usage-canvas');
    if (!el || state.usage.loading || !state.usage.data) return;
    const rows = state.usage.data.daily || state.usage.data.rows || [];
    if (!Array.isArray(rows) || rows.length === 0) return;
    usageChart = echarts.getInstanceByDom(el) || echarts.init(el);
    usageChart.setOption({
      backgroundColor: 'transparent',
      grid: { left: 40, right: 16, top: 24, bottom: 30 },
      tooltip: { trigger: 'axis', backgroundColor: '#17202f', borderColor: '#232e42', textStyle: { color: '#eef3fb', fontSize: 11 } },
      legend: { top: 0, textStyle: { color: '#93a1b8', fontSize: 10 } },
      xAxis: { type: 'category', data: rows.map((r) => r.date), axisLine: { lineStyle: { color: '#232e42' } }, axisLabel: { color: '#5c6a84', fontSize: 9 } },
      yAxis: { axisLine: { show: false }, axisLabel: { color: '#5c6a84', fontSize: 9 }, splitLine: { lineStyle: { color: '#1b2434' } } },
      series: [
        { name: '调用次数', type: 'bar', data: rows.map((r) => r.count ?? r.calls ?? 0), itemStyle: { color: '#16b4ff', borderRadius: [3, 3, 0, 0] } },
      ],
    }, true);
  });
}

function render(state) {
  root.replaceChildren(
    h('div', { class: 'dash-hero' }, [
      h('div', {}, [h('h1', {}, '系统配置'), h('div', { class: 'dash-sub' }, 'AI 密钥 · 飞书推送 · 调用统计')]),
    ]),
    h('div', { class: 'admin-layout' }, [
      h('div', { style: 'display:flex;flex-direction:column;gap:16px;' }, [
        renderAIConfigCard(state),
        renderFeishuTogglesCard(state),
        renderFeishuCard(state),
        renderFirecrawlCard(state),
      ]),
      renderUsageCard(state),
    ]),
  );
}

function renderAIConfigCard(state) {
  const cfg = state.config.data;
  const env = cfg?.env_keys || {};
  const body = state.config.loading
    ? SkeletonRows(5)
    : h('form', { class: 'admin-form-grid', id: 'admin-config-form', onSubmit: (e) => { e.preventDefault(); saveConfig(e.target); } }, [
        field('DeepSeek API Key', h('input', { name: 'deepseek_key', type: 'password', placeholder: env.DEEPSEEK_API_KEY || 'sk-...' })),
        field('DeepSeek 模型', h('input', { name: 'deepseek_model', placeholder: 'deepseek-chat' })),
        field('豆包 API Key', h('input', { name: 'doubao_key', type: 'password', placeholder: env.DOUBAO_API_KEY || 'ark-...' })),
        field('豆包模型', h('input', { name: 'doubao_model', placeholder: 'doubao-seed-2-0-lite-260428' })),
        field('Anthropic API Key', h('input', { name: 'anthropic_key', type: 'password', placeholder: env.ANTHROPIC_API_KEY || 'sk-ant-...' })),
      ]);
  return h('div', { class: 'card' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:10px;' }, '🤖 AI 密钥配置'),
    body,
  ]);
}

function renderFeishuTogglesCard(state) {
  const t = state.feishuToggles || {};
  const toggle = (key, label) => h('label', { style: 'display:flex;align-items:center;gap:4px;font-size:12px;margin-bottom:4px;' }, [
    h('input', { type: 'checkbox', checked: t[key] ? 'checked' : undefined,
      onChange: (e) => store.set({ feishuToggles: { ...store.get().feishuToggles, [key]: e.target.checked } })
    }), label,
  ]);
  return h('div', { class: 'card' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:8px;' }, '📱 飞书推送开关'),
    h('div', { style: 'margin-bottom:8px;' }, [
      h('label', { style: 'display:flex;align-items:center;gap:4px;font-size:13px;margin-bottom:6px;font-weight:700;' }, [
        h('input', { type: 'checkbox', checked: t.enabled !== false ? 'checked' : undefined,
          onChange: (e) => store.set({ feishuToggles: { ...store.get().feishuToggles, enabled: e.target.checked } })
        }), '全局开启飞书推送',
      ]),
      toggle('daily_decision', '每日决策报告(14:52)'),
      toggle('auction_confirm', '竞价确认(9:26)'),
      toggle('daily_close', '每日收盘复盘'),
      toggle('evolution_report', '进化报告'),
      toggle('realtime_signal', '实时信号提醒'),
    ]),
    h('button', { class: 'btn btn-sm btn-primary', onClick: saveFeishuToggles, disabled: state.togglesSaving }, state.togglesSaving ? '保存中…' : '💾 保存开关'),
  ]);
}

function renderFeishuCard(state) {
  const cfg = state.config.data?.feishu || {};
  return h('div', { class: 'card' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:10px;' }, '📢 飞书 Webhook 配置'),
    field('飞书 Webhook URL', h('input', { name: 'feishu_webhook', form: 'admin-config-form', placeholder: 'https://open.feishu.cn/open-apis/bot/v2/hook/...', style: 'width:100%;' })),
    h('div', { style: 'display:flex;gap:8px;margin-top:12px;' }, [
      h('button', { class: 'btn btn-sm btn-primary', type: 'submit', form: 'admin-config-form', disabled: state.saving }, state.saving ? '保存中…' : '💾 保存配置'),
      h('button', { class: 'btn btn-sm', type: 'button', onClick: testFeishu, disabled: state.testingFeishu }, state.testingFeishu ? '测试中…' : '🧪 飞书测试'),
    ]),
  ]);
}

function renderUsageCard(state) {
  return h('div', { class: 'card' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:10px;' }, `📊 AI 调用统计 · 近${state.usageDays}天`),
    state.usage.loading
      ? SkeletonRows(3)
      : !state.usage.data
        ? EmptyState({ title: '暂无用量数据' })
        : h('div', { id: 'ai-usage-canvas', class: 'usage-chart-wrap' }),
    h('div', { class: 'usage-range-tabs' }, [7, 30, 90].map((d) => h('button', {
      class: `btn btn-xs ${state.usageDays === d ? 'btn-primary' : ''}`, onClick: () => setUsageDays(d),
    }, `${d}天`))),
  ]);
}

function renderFirecrawlCard(state) {
  var cfg = (state.config.data && state.config.data.firecrawl) || {};
  var env = (state.config.data && state.config.data.env_keys) || {};
  var enabled = cfg.enabled !== false;
  var hasKey = !!(cfg.api_key || '');
  return h('div', { class: 'card' }, [
    h('div', { class: 'card-title', style: 'margin-bottom:10px;' }, '🔥 Firecrawl 多源新闻抓取'),
    h('div', { style: 'font-size:11px;color:var(--text-secondary);margin-bottom:10px;line-height:1.5;' }, [
      '多源财经新闻抓取引擎。启用后，消息立案时自动从中国证券报/东方财富/证券时报/第一财经抓取A股新闻。',
      h('br'),
      '状态: ', h('b', { style: 'color:' + (enabled && hasKey ? 'var(--brand-teal)' : 'var(--accent-danger)') + ';' },
        enabled && hasKey ? '● 已启用 (' + (cfg.sources||[]).filter(function(s){return s.enabled!==false}).length + '个信源)' : '○ 未启用'),
      ' | ', h('a', { href: 'https://firecrawl.dev', target: '_blank', style: 'color:var(--brand-cyan);' }, '获取API Key'),
    ]),
    h('div', { class: 'admin-inline-row', style: 'margin-bottom:10px;' }, [
      h('label', {}, [
        h('input', { type: 'checkbox', name: 'firecrawl_enabled', form: 'admin-config-form', checked: enabled ? 'checked' : undefined }),
        ' 启用多源抓取'
      ]),
    ]),
    field('API Key', h('input', { name: 'firecrawl_key', form: 'admin-config-form', type: 'password',
      placeholder: hasKey ? 'fc-****' + (cfg.api_key||'').slice(-4) : 'fc-xxxxx...',
      style: 'width:320px;' })),
    (cfg.sources || []).length ? h('div', { style: 'margin-top:8px;font-size:10px;color:var(--text-tertiary);' },
      '信源: ' + cfg.sources.filter(function(s){return s.enabled!==false}).map(function(s){return s.name;}).join(', ')
    ) : null,
  ]);
}

function field(label, input) {
  return h('div', { class: 'field' }, [h('label', {}, label), input]);
}
