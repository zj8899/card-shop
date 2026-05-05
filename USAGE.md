# 三才回测实盘 A 股交易系统 — 使用说明

## 项目概览

本系统是一个基于 FastAPI + ECharts 的 A 股量化交易平台，提供 **数据管理、K线图表、多策略回测、三才流程分析（天道/地道/人道）** 四大功能模块。后端使用 Python，核心指标计算通过 Rust（sancai_core，PyO3/maturin 绑定）加速，实时数据源来自 akshare（东方财富 API）。

**启动命令：**
```
cd D:\quant_code
python -m uvicorn server.main:app --host 0.0.0.0 --port 8000
```
浏览器打开 `http://localhost:8000` 即可使用。

---

## 系统架构

```
浏览器 (index.html + ECharts)
  │
  ├─ GET /                    → 静态 HTML 仪表盘
  ├─ GET /static/*            → JS/CSS 静态资源
  ├─ /api/data/*              → 行情数据接口
  ├─ /api/backtest/*          → 回测接口
  ├─ /api/signal/*            → 信号接口
  ├─ /api/sancai/*            → 三才分析接口 (旧)
  ├─ /api/sancai/tiandao/*    → 天道宏观择时
  ├─ /api/sancai/didao/*      → 地道选股评分
  ├─ /api/sancai/rendao/*     → 人道执行监控
  └─ /api/health              → 健康检查
```

**后端文件：**
| 文件 | 作用 |
|------|------|
| `server/main.py` | FastAPI 应用入口，注册 CORS、路由、静态文件 |
| `server/routers/data.py` | 股票列表、K线数据、盘口深度、逐笔成交 |
| `server/routers/backtest.py` | 回测任务创建/轮询/结果获取 |
| `server/routers/signal.py` | 信号生成（与 sancai_core 联动） |
| `server/routers/sancai.py` | 旧三才状态总览（仪表盘迷你卡片用） |
| `server/routers/sancai_layers.py` | **新三才分层 API**（15 个端点） |

**前端文件：**
| 文件 | 作用 |
|------|------|
| `server/static/index.html` | 主页面：导航栏 + 5 个标签页 + CSS |
| `server/static/js/api.js` | fetch 封装（超时 + 请求取消） |
| `server/static/js/charts.js` | ECharts 实例管理 + 暗色主题常量 |
| `server/static/js/timeline.js` | 时间轴组件（图表 + 事件列表双向联动） |
| `server/static/js/sancai.js` | 三才三页面数据加载与渲染 |
| `server/static/js/app.js` | 应用入口，标签页切换，所有页面逻辑 |

---

## 前端功能模块

### 1. 仪表盘 (Dashboard)

页面顶栏显示 Rust 核心加载状态和服务运行状态。

**功能区块：**
- **4 个统计卡片**：总资产 / 总收益率 / 夏普比率 / 最大回撤 — 运行回测后自动填充
- **三才执行状态（迷你）**：天道/地道/人道三列简要评估，数据来自 `/api/sancai/status`
- **最近交易信号**：回测产生的买卖信号列表，自动滚动

**联动关系：**
- 页面加载时自动调用 `checkHealth()` 和 `loadDashboard()`
- 仪表盘的三才迷你卡片 → 与旧 `sancai.py` 的 `/api/sancai/status` 联动
- 回测完成后 → 仪表盘统计数据自动更新

---

### 2. K线图表页 (Chart)

**核心功能：** 多周期 K 线 + 动态均线 + 分时图 + 五档盘口 + 资金流向

**控件说明：**
- **股票选择器**：10 只 A 股（平安银行、万科、格力、招行、茅台、平安、五粮液、海康、宁德时代、中芯国际）
- **周期选择器**：日线 / 120min / 60min / 30min / 15min / 5min / 1min — 切换周期时均线列表自动变化
- **MA 多选**：日线默认勾选 5/13/21/34/55/144 六条均线，分钟线默认 34/144/233；可任意组合
- **K线/分时图切换**：两个按钮互斥

**图表布局（K线模式）：**
```
┌─ K线图 (K线蜡烛 + 选中均线 + 成交量) ────────┐ 高度 ~500px
├─ KDJ 指标图 (K/D/J + 超买超卖参考线) ────────┘ 高度 ~180px
```

**图表布局（分时图模式）：**
```
┌─ 分时图 (价格线 + 均价线 + 昨收参考线) ──────┐ 高度 ~500px
├─ 五档盘口表格 (卖5→卖1 | 价格 | 买1→买5) ───┤ 含成交量柱
├─ 资金流向 (主动买/卖/净流入 + 买卖比例条) ───┤ 大单列表(≥100手)
└─ 5 秒自动刷新 (仅分时图模式下启用) ──────────┘
```

**联动细节：**
- 切换股票 → `loadChart()` 或 `loadTickChart()` 重新请求 K 线数据
- 切换周期 → `onPeriodChange()` 先重建 MA 选择列表，再刷新图表
- 勾选/取消均线 → 直接触发 `loadChart()` 重建 series
- 分时图模式 → `depthTimer = setInterval(loadDepthAndFlow, 5000)` 每 5 秒拉盘口+逐笔数据
- 切换回 K 线或离开图表页 → `clearInterval(depthTimer)` 停止轮询
- **数据源**：K 线来自本地 parquet 文件（`data/raw/daily/` 和 `data/raw/minute/`），盘口和逐笔来自 akshare 实时接口

---

### 3. 回测页 (Backtest)

**配置参数：**
- **股票**：多选列表（Ctrl+点击多选）
- **周期**：日线 / 30 分钟
- **起止日期**：起始日期输入框
- **初始资金**：默认 100 万
- **模式**：`simple`（均线金叉）/ `strict`（三才 BP1 多条件）
- **单笔风险**：默认 2%

**执行流程：**
1. 点击"运行回测" → `POST /api/backtest/run`（参数 JSON）→ 获取 `task_id`
2. 前端每秒轮询 `GET /api/backtest/{task_id}/status`（最多 120 次）
3. 状态变为 `completed` → 调用 `GET /api/backtest/{task_id}/result` 获取完整结果
4. 结果渲染：权益曲线 (ECharts) + 交易记录表 (HTML) + 仪表盘统计更新

**数据联动：**
- 回测完成 → 交易记录表显示最近 50 笔（买入绿色/卖出红色标签 + 原因）
- 回测完成 → 仪表盘 4 个统计卡片更新
- 回测完成 → 仪表盘"最近交易信号"更新

---

### 4. 三才流程页 (Sancai) — **本次核心重构**

**页面入口：** 顶部导航栏点击"三才流程"

#### 4.1 子标签页导航

三个子页面各对应三才的一个层级：

| 子页面 | 含义 | 关注点 |
|--------|------|--------|
| 天道·择时 | 宏观市场时机 | 沪深300 趋势、PE 估值、宏观研报 |
| 地道·选股 | 个股多因子筛选 | 技术面评分、财务指标、研报评级 |
| 人道·执行 | 持仓监控与预警 | 模拟持仓聚合、预警面板 |

顶部还有一个 **时间范围选择器**：1 周 / 1 月 / 3 月 / 6 月，切换后所有数据重新加载。

#### 4.2 三才合一对齐信号

三个子页面共享一个顶栏 **三才合一信号条**：
- 天道=吉 且 地道=吉 且 人道=吉 → `三才合一·吉`（绿色渐变）
- 全部=凶 → `三才皆凶·空仓`（红色渐变）
- 其他 → `三才分歧·观望`（黄色渐变）

每次切换子标签页时都会调用 `/api/sancai/alignment` 刷新信号。

---

#### 4.3 天道页面布局与联动

```
┌─ 评估头部: 吉/凶/平 (大字) + 事件统计 ──────────────────┐
├─ 时间轴图表 (沪深300 折线 + 各层事件标记点 markPoints) ──┤ 高度 ~380px
├─ 事件列表 (按日期分组, 彩点+层标签+标题, 可滚动) ──────┘ 最大 220px
├─ ▼ 行情层: 三大指数表格 (收盘价/涨跌幅) ─────────────────┐
├─ ▶ 研报层: 宏观策略研报列表 ([机构] 标题) ────────────────┤ 折叠面板
├─ ▶ 基础数据层: PE/PB 分位数表格 ────────────────────────┤
└─ ▶ 板块层: 行业涨跌横向柱状图 ─────────────────────────┘
```

**事件颜色编码：**
- 蓝 `#58a6ff` = 行情（涨跌幅 >1.5% 标记）
- 绿 `#3fb950` = 研报
- 红 `#f85149` = 公告
- 紫 `#a371f7` = 基础数据（PE 极值标记）
- 金 `#d2991d` = (预留)新闻

**联动：**
- 点击时间轴图表上的事件标记点 → 下方事件列表自动滚动到对应条目并高亮 2 秒
- 折叠面板懒加载：默认仅行情层展开，点击展开时不会重复请求（数据已随 `timeline` 一次性加载）

**数据流：**
1. `loadTiandao()` → `GET /api/sancai/tiandao/timeline?days=` 获取价格数据 + 聚合事件
2. 同时并行请求：`/tiandao/market`（行情表）、`/tiandao/research`（研报）、`/tiandao/fundamental`（PE/PB）、`/tiandao/sectors`（板块热力）

---

#### 4.4 地道页面布局与联动

```
┌─ 股票选择器 ────────────────────────────────────────┐
├─ 评估头部: 吉/凶/平 + 评分/100 + 最新价 ──────────────┤
├─ 时间轴图表 (个股 K 线蜡烛 + 各层事件标记) ──────────┤
├─ 事件列表 ────────────────────────────────────────┘
├─ ▼ 行情层: 最新价/开/高/低/量 ──────────────────────┐
├─ ▶ 研报层: 个股研报 ([机构] 评级 标题 目标价) ────────┤
├─ ▶ 基础数据层: 财务指标表 + 近期评级 + 主力资金 ──────┤
└─ ▶ 公告层: 公司公告列表 ───────────────────────────┘
```

**地道评分模型（满分 100）：**

| 因子 | 满分 | 规则 |
|------|------|------|
| 价格 > MA21 | +15 | 短期趋势 |
| 价格 > MA55 | +15 | 中期趋势 |
| MA21 > MA34 | +10 | 均线排列 |
| 近5日均量 > 近10日 | +10 | 放量确认 |
| ROE > 15% | +10 | 盈利能力（来自 akshare 财务摘要） |
| 净利润 > 0 | +5 | 盈利确认 |
| 近 3 个买入/增持评级 | +10 | 机构认可度 |
| 主力资金流入 ≥7 天 | +10 | 资金面（来自 akshare 资金流向） |
| 沪深300 PE 分位 <30% | +5 | 大盘估值环境 |

评分 ≥70 → 吉，评分 <35 → 凶，其余 → 平

**联动：**
- 切换地道子页面的股票选择器 → `onDidaoSymbolChange()` → 重新加载 `loadDidao()`
- 评分卡中的每一项因子都对应后端的一个 akshare 调用（带缓存，5分钟 TTL）

---

#### 4.5 人道页面布局与联动

```
┌─ 持仓概览: 模拟持仓股列表 + 信号 ────────────────┐
├─ 聚合时间轴 (多股事件合并) ──────────────────────┤
├─ 事件列表 ────────────────────────────────────┘
├─ ▼ 预警面板: MA144 跌破 / 评级下调 / 公告预警 ────┤
└─ ▶ 资金流向 ───────────────────────────────────┘
```

人道当前为模拟模式（无实盘持仓），使用配置文件中前 3 只股票作为模拟持仓，聚合它们的地道事件。

---

### 5. 数据管理页 (Stocks)

展示所有股票的数据状态表格：代码 / 名称 / 日线数据有无 / K 线条数。数据来自本地 parquet 文件的实际读取和统计。

---

## API 路由详解

### `/api/data/*` — 行情数据

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/data/stocks` | GET | 所有股票及其数据状态 |
| `/api/data/stocks/{symbol}/kline` | GET | K 线数据（含 MA + KDJ 指标） |
| `/api/data/stocks/{symbol}/depth` | GET | 5 档盘口（实时，akshare） |
| `/api/data/stocks/{symbol}/ticks` | GET | 逐笔成交分类（实时，akshare） |
| `/api/data/gaps` | GET | 数据缺口检查 |

### `/api/backtest/*` — 回测

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/backtest/run` | POST | 创建回测任务 → 返回 task_id |
| `/api/backtest/{task_id}/status` | GET | 轮询状态（running/completed/failed） |
| `/api/backtest/{task_id}/result` | GET | 获取完整结果（权益曲线+交易记录） |

### `/api/sancai/*` — 旧三才总览

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/sancai/status` | GET | 仪表盘用的简要三才状态 |

### `/api/sancai/tiandao/*` — 天道宏观择时

| 端点 | 方法 | 缓存 | 说明 |
|------|------|------|------|
| `/api/sancai/tiandao/timeline` | GET | 2min | **核心**：价格数据 + 聚合事件，驱动时间轴 |
| `/api/sancai/tiandao/market` | GET | 2min | 三大指数日线数据 |
| `/api/sancai/tiandao/fundamental` | GET | 10min | 沪深300/上证50/中证500 PE/PB 分位数 |
| `/api/sancai/tiandao/research` | GET | 30min | 宏观策略研报（按关键词过滤） |
| `/api/sancai/tiandao/sectors` | GET | 5min | 行业板块涨跌数据 |

### `/api/sancai/didao/*` — 地道选股

| 端点 | 方法 | 缓存 | 说明 |
|------|------|------|------|
| `/api/sancai/didao/timeline` | GET | 2min | **核心**：个股 5 层事件 + 价格数据 |
| `/api/sancai/didao/market` | GET | — | 个股 K 线 + MA + KDJ（本地文件+rust 核心） |
| `/api/sancai/didao/fundamental` | GET | 10min | 财务指标 + 评级历史 + 资金流向摘要 |
| `/api/sancai/didao/research` | GET | 30min | 带目标价的个股研报 |
| `/api/sancai/didao/announcements` | GET | 30min | 公司公告 |
| `/api/sancai/didao/news` | GET | 15min | 公司公告事件（备选数据源） |
| `/api/sancai/didao/score` | GET | — | 多因子综合评分 |

### `/api/sancai/rendao/*` — 人道执行

| 端点 | 方法 | 缓存 | 说明 |
|------|------|------|------|
| `/api/sancai/rendao/timeline` | GET | — | 持仓股聚合事件 |
| `/api/sancai/rendao/flow` | GET | 2min | 个股资金流向 |

### `/api/sancai/*` — 跨层

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/sancai/alignment` | GET | 三才合一对齐信号 |

### 缓存机制

所有涉及 akshare 调用的端点均带模块级内存缓存（`_CACHE` 字典），TTL 根据数据变化频率设置：
- 行情数据：2 分钟
- 资金流向：2 分钟
- 基本面/PE：10 分钟
- 研报/公告：30 分钟

缓存命中时返回 `"source": "cache"`，命中 akshare 时返回 `"source": "akshare"`。

---

## 前端 JavaScript 模块联动

```
app.js (入口)
  ├── 导入 charts.js ── 管理所有 ECharts 实例
  ├── 导入 sancai.js ── 三才页面加载器
  │     ├── 导入 api.js ── fetch 封装
  │     └── 导入 timeline.js ── 时间轴组件
  │           └── 导入 charts.js ── ECharts 实例
  └── 暴露到 window ── 供 HTML onclick 调用
```

**全局函数暴露（`window.*`）：**
- `switchTab(tab)` — 5 个主标签页切换
- `switchSancaiSubTab(tier)` — 三才子标签页切换
- `switchSancaiTimeRange(days)` — 三才时间范围切换
- `onDidaoSymbolChange()` — 地道股票切换
- `loadChart()` / `loadTickChart()` — 图表页刷新
- `switchChartMode()` / `onPeriodChange()` — 图表页控制
- `runBacktest()` / `loadDashboard()` / `loadStocks()` — 其他页面

**模块加载方式：** `<script type="module" src="/static/js/app.js">`，浏览器原生 ES Module 支持。

---

## 数据目录结构

```
D:\quant_code\
├── server/                  # FastAPI 后端
│   ├── main.py              # 入口
│   ├── static/              # 前端
│   │   ├── index.html       # 主页面
│   │   └── js/              # JS 模块
│   └── routers/             # API 路由
├── data/
│   ├── raw/
│   │   ├── daily/           # 日线 parquet 文件 (000001.parquet ...)
│   │   └── minute/          # 分钟线 parquet (1min/, 5min/, ...)
│   └── metadata.db          # 数据目录 SQLite
├── config/
│   └── defaults.yaml        # 股票池配置 (universe)
└── sancai_core/             # Rust 核心库 (PyO3)
```

---

## 已知限制与注意事项

1. **akshare 延迟**：所有实时 akshare 调用（盘口、逐笔、指数、研报、财务、资金流向）需要 3-10 秒。单线程 uvicorn 在处理这些请求期间会阻塞其他请求。生产环境建议使用 gunicorn（Linux）或增加 uvicorn workers（非 Windows）。
2. **分时图轮询**：打开分时图后浏览器每 5 秒拉取盘口+逐笔数据，离开分时图或切换到其他标签页时会自动停止。如果服务器正处理这些慢请求，其他标签页可能加载变慢。
3. **人道页面**：当前为模拟模式，持仓使用配置文件前 3 只股票。实盘仓位需要与券商 API 对接。
4. **新闻层**：a kshare 的 `stock_notice_report` 和 `stock_individual_notice_report` 接口不稳定，地道的新闻和公告层可能返回空数据。
5. **缓存**：所有端点使用内存缓存（进程级），重启服务器后缓存清空。如需持久化缓存可改用 Redis。
