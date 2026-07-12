// ══════════════════════════════════════════════════════════════
// Strategies module — code viewer + AI-assisted editing
// ══════════════════════════════════════════════════════════════
import { api } from '../core/api.js';
import { createStore, bindRender } from '../core/store.js';
import { h, toast } from '../core/dom.js';
import { EmptyState, SkeletonRows } from '../components/ui.js';

const store = createStore({
  list: { loading: true, items: [] },
  selected: null,
  code: '',
  codeMeta: null,
  chat: [],
  chatInput: '',
  chatBusy: false,
  saving: false,
  creating: false,
  newName: '',
});

let root;

export async function mount(container) {
  root = container;
  render(store.get());
  bindRender(store, render);
  await loadList();
}

async function loadList() {
  store.set({ list: { loading: true, items: [] } });
  try {
    const res = await api.get('/api/strategies/list');
    // Handle both wrapped {status:"ok", data:{...}} and raw {...} formats
    const data = (res && res.data) || res || {};
    const builtin = data.builtin || [];
    const user = data.user || [];
    const items = [
      ...builtin.map(s => ({ ...s, _group: '内置策略' })),
      ...user.map(s => ({ ...s, _group: '自定义策略' })),
    ];
    store.set({ list: { loading: false, items } });
  } catch (e) {
    store.set({ list: { loading: false, items: [], error: e.message } });
  }
}

async function selectStrategy(name) {
  store.set({ selected: name, code: '', codeMeta: null, creating: false });
  try {
    const res = await api.get(`/api/strategies/source/${name}`);
    store.set({ code: res.code || '', codeMeta: res });
  } catch (e) {
    toast(`加载失败: ${e.message}`, { type: 'error' });
  }
}

function startNew() {
  // 空白+纯AI生成：进入新建模式，编辑器空白可写，保存按钮激活。
  // codeMeta.editable=true 是关键 — textarea 的 readonly 和保存按钮都靠它判定。
  store.set({
    creating: true, selected: null, newName: '',
    code: '',
    codeMeta: { editable: true, type: 'user', isNew: true },
    chat: [],
  });
}

async function saveStrategy() {
  const state = store.get();
  if (!state.codeMeta?.editable) { toast('内置策略不可编辑', { type: 'error' }); return; }
  // 新建时用用户输入的名字（优先 live DOM，回退 store）；编辑时用当前选中的策略名
  let name = state.selected;
  if (state.creating) {
    const nameInput = document.querySelector('.strat-name-input');
    name = ((nameInput ? nameInput.value : '') || state.newName || '').trim();
    if (!name) { toast('请先输入策略名', { type: 'error' }); return; }
  }
  store.set({ saving: true });
  try {
    const res = await api.post('/api/strategies/user', { name, code: state.code });
    const savedName = res?.name || res?.data?.name || name;
    toast('已保存', { type: 'success' });
    store.set({ saving: false, creating: false });
    await loadList();
    // 尝试选中刚保存的策略。registry key 来自策略类的 name 属性，可能与文件名不同，
    // 所以在刷新后的列表里按 id/name 匹配，匹配不到就只刷新列表（不强制选中，避免 404）。
    const items = store.get().list.items || [];
    const hit = items.find(it => it.category === 'user' &&
      (it.id === `user_${savedName}` || it.name === savedName || it.id === `user_${name}`));
    if (hit) await selectStrategy(hit.id || hit.name);
  } catch (e) {
    store.set({ saving: false });
    toast(`保存失败: ${e.message}`, { type: 'error' });
  }
}

function copyCode() {
  navigator.clipboard?.writeText(store.get().code || '');
  toast('已复制到剪贴板', { type: 'success' });
}

async function deleteStrategy(name) {
  if (!confirm(`确定删除策略 "${name}"？此操作不可撤销。`)) return;
  try {
    const res = await api.del(`/api/strategies/user/${name}`);
    toast(`已删除: ${res?.name || name}`, { type: 'success' });
    store.set({ selected: null, code: '', codeMeta: null });
    await loadList();
  } catch (e) {
    toast(`删除失败: ${e.message}`, { type: 'error' });
  }
}

async function askAI(message) {
  if (!message.trim()) return;
  const chat = [...store.get().chat, { role: 'user', text: message }];
  store.set({ chat, chatBusy: true });
  // Clear input via live DOM query (not store) to avoid re-render focus loss
  const _aiInput = document.querySelector('.strat-ai-input-row input');
  if (_aiInput) _aiInput.value = '';
  try {
    const res = await api.post('/api/ai/chat', {
      message, context: { strategy: store.get().selected, code: store.get().code },
    }, { timeoutMs: 60000 });
    const reply = res?.data?.reply || res?.reply || res?.message || '(无回复)';
    const suggestedCode = res?.data?.code || res?.code;
    store.set({ chat: [...chat, { role: 'assistant', text: reply }], chatBusy: false });
    if (suggestedCode) store.set({ code: suggestedCode });
  } catch (e) {
    store.set({ chat: [...chat, { role: 'assistant', text: `出错: ${e.message}` }], chatBusy: false });
  }
}

function render(state) {
  root.replaceChildren(
    h('div', { class: 'dash-hero' }, [
      h('div', {}, [h('h1', {}, '策略管理'), h('div', { class: 'dash-sub' }, '策略代码查看 · AI 辅助改写')]),
    ]),
    h('div', { class: 'strat-layout' }, [
      renderListPane(state),
      renderEditorPane(state),
    ]),
  );
}

function renderListPane(state) {
  // Group items by _group
  const groups = {};
  for (const item of state.list.items) {
    const g = item._group || '其他';
    if (!groups[g]) groups[g] = [];
    groups[g].push(item);
  }

  const children = state.list.loading
    ? [SkeletonRows(8)]
    : !state.list.items.length
      ? [EmptyState({ title: '暂无策略' })]
      : Object.entries(groups).map(([groupName, items]) =>
          h('div', {}, [
            h('div', { style: 'font-size:11px;font-weight:700;color:var(--text-secondary);padding:6px 4px 2px;' }, groupName),
            ...items.map((item) => {
              const key = item.id || item.name;
              const label = item.name || item.label || item.id;
              const isUser = item.category === 'user' || item.type === 'user';
              return h('div', {
                class: `strat-list-item ${state.selected === key ? 'active' : ''}`,
                onClick: () => selectStrategy(key),
              }, [
                h('span', {}, label),
                isUser ? h('span', { class: 'badge badge-ai', style: 'margin-left:auto;font-size:10px;' }, '自定义') : null,
                isUser ? h('button', {
                  class: 'btn btn-xs', style: 'margin-left:4px;color:var(--text-danger);',
                  onClick: (e) => { e.stopPropagation(); deleteStrategy(key); },
                  title: '删除策略',
                }, '🗑') : null,
              ]);
            }),
          ])
        );

  return h('div', { class: 'strat-list-pane' }, [
    h('div', { class: 'card' }, [
      h('div', { style: 'display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;' }, [
        h('div', { class: 'card-title', style: 'margin:0;' }, '📁 策略列表'),
        h('button', { class: 'btn btn-xs btn-primary', onClick: startNew, title: '新建自定义策略' }, '+ 新建'),
      ]),
      ...children,
    ]),
  ]);
}

function renderEditorPane(state) {
  const titleEl = state.creating
    ? h('input', {
        class: 'strat-name-input', placeholder: '输入策略名，如 my_ma_cross',
        value: state.newName,
        // 用 onChange(失焦时)持久化，避免每次按键 store.set 触发全量 re-render 丢焦点。
        // 保存时另从 live DOM 直读，不依赖失焦时机。
        onChange: (e) => store.set({ newName: e.target.value }),
      })
    : h('div', { class: 'card-title' }, state.selected || '📝 代码视窗');

  return h('div', { class: 'strat-editor-pane' }, [
    h('div', { class: 'card', style: 'flex:1;display:flex;flex-direction:column;gap:10px;' }, [
      h('div', { style: 'display:flex;justify-content:space-between;align-items:center;gap:8px;' }, [
        titleEl,
        h('div', { style: 'display:flex;gap:6px;flex-shrink:0;' }, [
          h('button', { class: 'btn btn-xs', onClick: copyCode }, '📋 复制'),
          state.codeMeta?.editable
            ? h('button', { class: 'btn btn-xs btn-primary', onClick: saveStrategy, disabled: state.saving }, state.saving ? '保存中…' : '💾 保存')
            : null,
        ]),
      ]),
      h('textarea', {
        class: 'strat-code-editor', spellcheck: 'false',
        readonly: state.codeMeta?.editable ? undefined : 'readonly',
        placeholder: state.creating
          ? '在下方 AI 助手描述需求生成策略代码（例："写一个MA20上穿买入、跌破5%止损的策略"），或直接手写。'
          : '点击左侧策略名称查看代码',
        value: state.code,
        onInput: (e) => store.set({ code: e.target.value }),
      }),
      renderAiPanel(state),
    ]),
  ]);
}

function renderAiPanel(state) {
  return h('div', { class: 'strat-ai-panel' }, [
    h('div', { class: 'strat-ai-panel-head' }, '✦ AI 策略助手'),
    h('div', { class: 'strat-ai-chat' }, state.chat.length
      ? state.chat.map((m) => h('div', { class: `strat-ai-msg ${m.role}` }, [m.role === 'user' ? '你: ' : 'AI: ', m.text]))
      : h('span', { style: 'color:var(--text-tertiary);' }, '输入需求，AI 帮你改策略代码。例："加一个跌破MA21止损"')),
    h('div', { class: 'strat-ai-input-row' }, [
      h('input', {
        placeholder: '描述需要修改的内容…',
        onKeydown: (e) => { if (e.key === 'Enter') askAI(e.target.value); },
      }),
      h('button', {
        class: 'btn btn-xs btn-primary', disabled: state.chatBusy,
        onClick: () => { const input = document.querySelector('.strat-ai-input-row input'); if (input) askAI(input.value); },
      }, '发送'),
    ]),
  ]);
}
