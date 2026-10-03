# 架构文档（ARCHITECTURE）

> 适用版本：**四层结构与数据流仍然适用**；条目按当前迭代 v2.19.9 校正了规模类陈述
> （模块数、业务表数、schema 版本）。文档主体停在 v2.18.2 的叙述粒度，尚未逐条覆盖
> v2.19.x 的 SSRF 逐跳闸、租约/CAS 收口、脱敏边界（handler 挂载与导出副本）等变化。
> 现行事实以本快照内的源码与文档为准。
> 描述 Kiana Vnext Plus 的分层结构、核心链路、模块职责、数据结构与产物格式。

---

## 一、总览：四层结构

```
┌─────────────────────────────────────────────────────────────┐
│ ① 入口层                                                     │
│   launcher_v9.py（GUI，qfluentwidgets FluentWindow）         │
│   run_crawler.py（CLI，argparse）                            │
├─────────────────────────────────────────────────────────────┤
│ ② 桥接层                                                     │
│   launcher_v8.py → EngineBridge（GUI 与引擎线程的边界）       │
│   kiana_vnext_plus/cli.py（CLI 装配）                        │
├─────────────────────────────────────────────────────────────┤
│ ③ 引擎层（kiana_vnext_plus/，71 模块）                       │
│   主循环 → 任务队列 → 单页流水线 → 解析/下载/入队             │
├─────────────────────────────────────────────────────────────┤
│ ④ 支撑层                                                     │
│   并发控制 / 反爬防御 / 身份与凭据 / 限流 / 缓存 / 隐私脱敏   │
└─────────────────────────────────────────────────────────────┘
```

**并发模型（重要）**：**单进程 + asyncio 协程并发**，不是多线程也不是多进程。并发上限在**三级之间仲裁**——全局、按域、按出口——**取最小值**：每域默认 **20** 路，每出口默认 **10** 路，全局默认按机器自适应为 `max(8, min(32, 2 × 逻辑核数))`（除非显式配置）。线程只用于 `asyncio.to_thread` 卸载阻塞调用（如 cv2 图像处理、yt-dlp 解析）。这意味着：
- 引擎内任何**跨 `await` 的读-改-写**都是真竞态，必须用锁或 CAS；
- 引擎内任何**长阻塞同步调用**都会冻住整个循环（历史多次事故根源）。

---

## 二、核心链路（一次爬取任务的完整生命周期）

```
用户输入网址
   │
   ▼
[种子阶段] crawler.run()
   │  · URL 清洗去重（_clean_urls）
   │  · 种子类型分流：视频/音频/m3u8 → yt-dlp 直连队列
   │                   图片 → 图片通道；其他文件 → 文件通道
   │                   普通 URL → 正常爬取
   │  · RSS/sitemap 播种（可选）
   │  · LLM 链接打分（可选，默认关；分片并发 + 总预算超时）
   ▼
[入队] frontier.push(url, depth, priority, parent_hash)
   │  · 写 SQLite frontier 表（status='pending'）
   │  · parent_hash 记录血缘（哪个页面发现了它）
   ▼
┌─►[取批] frontier.pop_batch(50) ──────────── 租约式领取
│     · 回收超时租约（lease_expires < now → 回 pending）
│     · 领取时置 status='leased', leased_at, lease_expires
│     ▼
│  [单页流水线] PageProcessor.process_job(job)
│     ① 获取出口代理（身份捆绑开启 → 会话池；否则 exit_mgr）
│     ② 并发准入（concurrency.acquire_all：全局+分域+分出口三级）
│     ③ 拟人延迟（域被盾 → 额外 10-30s）+ robots Crawl-delay
│     ④ 限流令牌桶（全局+域名双档，429 指数退避）
│     ⑤ HTTP 磁盘缓存查询（命中即复用；过期走 304 协商）
│     ⑥ 抓取（engine_router：curl_cffi 主通道 → urllib 回退）
│     ⑦ 脱敏（sanitizer：手机号/邮箱/IP）
│     ⑧ 内容哈希 + SimHash 近似去重标记
│     ⑨ 防御评估（403/429 → sentinel/oracle；渲染兜底后重评）
│     ⑩ 解析（parser：正文/元数据/JSON-LD/站点规则）
│     ⑪ 质量闸（低分且短文本 → 拒收隔离，不落盘）
│     ⑫ 持久化 + 导出（extracted 表 / jsonl / csv / markdown）
│     ⑬ 入队（发现新链接、视频、图片、B站流、电商商品）
│     ⑭ CAS 打卡（mark_done_checked，租约被抢则丢弃结果）
│     ▼
│  [结果] 成功 → status='done'；失败 → 重试（指数退避+抖动）或 dead
└─────┘ 循环直到 pending+retry 清空
   │
   ▼
[收尾] finally
   · 等视频 worker 完成（最长 150s）
   · checkpoint 落盘 + stats.jsonl 最终快照
   · LLM 批量增强（可选，默认关）
   · graceful shutdown（恢复电源计划、关浏览器、落库）
```

---

## 三、模块职责（按功能分组，71 模块）

### 3.1 主控与调度
| 模块 | 职责 |
|---|---|
| `crawler.py` | **主循环**（KianaCrawler.run）：种子处理、取批、任务派发、看门狗（page_timeout）、视频 worker、跨批延迟、收尾 |
| `frontier.py` | **任务队列 + 持久化**（FrontierDB）：SQLite、写队列批量 flusher、租约管理、血缘、死信落盘、**8 张队列业务表**（导出快照库是另一个 db 文件，见第四节） |
| `page_processor.py` | **单页流水线**（PageProcessor.process_job）：上文的 ①②-⑭ 全流程 |
| `main.py` / `cli.py` | 装配与 CLI 参数解析 |
| `redis_frontier.py` | 分布式队列备选实现（实验，默认不用） |

### 3.2 网络与协议
| 模块 | 职责 |
|---|---|
| `engine_router.py` | 抓取通道选择与回退 |
| `protocol_engine.py` | 协议层：headers 构建、解码、`safe_urlopen` 回退（协议/私网/重定向逐跳校验） |
| `response_adapter.py` | 统一响应对象（status/headers/text/too_big） |
| `session_pool.py` | 会话复用池（线程安全） |
| `url_utils.py` | URL 规范化、`is_private_url`（SSRF 闸）、`safe_urlopen` |
| `header_generator.py` | 请求头生成（含 API 头） |
| `proxy_fetcher.py` | 代理源拉取（默认关；私网拒、形态校验） |

### 3.3 解析与内容
| 模块 | 职责 |
|---|---|
| `parser.py` | 正文/元数据/JSON-LD 实体/SimHash/内容哈希 |
| `site_rules.py` + `rules/sites/` | **YAML 站点规则层**：列表页/详情字段/图片/翻页，逐段容错 |
| `ecommerce_parser.py` | 电商商品解析（淘宝/京东等） |
| `feed_source.py` | RSS/Atom 订阅源解析（零依赖正则，失败降级普通页面） |
| `robots_policy.py` | robots.txt 解析（含 Crawl-delay 尊重） |

### 3.4 媒体下载
| 模块 | 职责 |
|---|---|
| `universal_downloader.py` | **yt-dlp 通用通道**：格式链、PO Token 定位、cookies、流式分块写 |
| `m3u8_downloader.py` | HLS 分片下载 + ffmpeg 合并（GPU 编码器探测缓存） |
| `media_downloader.py` | 图片/文件下载（SSRF 闸） |
| `media_schema.py` | 统一媒体 schema（四平台 resolver 收口） |
| `video_resolver.py` | B站视频流解析（`__INITIAL_STATE__` 括号平衡扫描） |
| `douyin_resolver.py` / `douyin_abogus.py` | 抖音直链 + abogus 签名（自研实现，固定向量回归） |
| `kuaishou_resolver.py` / `xhs_resolver.py` / `music163_resolver.py` | 快手/小红书/网易云解析 |
| `comment_danmaku.py` | B站评论 + 弹幕（ASS 字幕） |

### 3.5 反爬与隐身
| 模块 | 职责 |
|---|---|
| `defense_protocols.py` | **多层防御**：SentinelBrain（评分）/ ShieldSoldier（并发压制+恢复）/ AmeNoHabakiri（反制）/ OracleBrain；冷却落库与重启恢复 |
| `exit_manager.py` | 代理池：节点状态机（ACTIVE/COOLDOWN/MELTDOWN）、健康分、粘性会话、inflight 计数 |
| `solver_engine.py` | 隐身浏览器（patchright）求解/渲染，池管理 |
| `challenge_solver.py` / `slider_vision.py` / `captcha_solver_extended.py` | 验证码/滑块识别（含视觉） |
| `evasion_engine.py` / `evasion_v2.py` / `ultimate_evasion.py` | 反检测策略 |
| `stealth_v3.py` / `source_level_stealth.py` | 隐身脚本注入 |
| `fingerprint_consistency.py` | 指纹一致性（canvas/webgl 等） |
| `behavioral_biometrics.py` / `trajectory_engine.py` | 行为拟人（鼠标轨迹、对数正态延迟） |
| `injection_scripts.py` | 浏览器注入脚本集 |

### 3.6 并发与自适应
| 模块 | 职责 |
|---|---|
| `concurrency.py` | **三级信号量**（全局/分域/分出口）+ **min-wins 仲裁表**（多控制器诉求取最严值） |
| `rate_limiter.py` | 双档令牌桶 + 指数退避 + 速率边界探测 |
| `smart_adaptive.py` / `adaptive_v2.py` / `ultimate_core_v4.py` | 自适应调参（v2.18 起调参源唯一化，避免互相抵消） |

### 3.7 身份与凭据
| 模块 | 职责 |
|---|---|
| `identity.py` / `identity_session.py` | 身份会话（出口+cookies+指纹打包，封锁整包退役） |
| `cookie_armory.py` | Cookie 弹药库（Fernet 加密 + 健康分轮换 + 失败冷却） |
| `cookie_utils.py` | cookies 多文件解析（分号/换行/注释/去重） |
| `cookie_health.py` | 登录态自检（B站 nav API 等） |
| `privacy_store.py` | 敏感数据加密存储（DPAPI） |

### 3.8 智能增强
| 模块 | 职责 |
|---|---|
| `llm_client.py` | **LLM 兼容层**：四格式（openai/anthropic/gemini/ollama）、错误语义化、批处理 |
| `llm_enrich.py` | 离线批后处理（摘要/分类/关键词），**爬虫关键路径零 LLM 调用** |
| `link_scoring.py` | 链接优先级打分（规则层 + 可选 LLM） |

### 3.9 支撑
| 模块 | 职责 |
|---|---|
| `config.py` | 全局配置 + **`data_root()` 数据根统一入口**（便携模式） |
| `sanitizer.py` | 隐私脱敏（手机号/邮箱/IP，正则线性化） |
| `enhancements.py` / `p1_enhancements.py` | 导出（xlsx 按域分表/sqlite+FTS5）、数据校验、HttpCache、断点、去重、自动节流 |
| `wallpaper.py` | GUI 底图管线（读图/裁切/模糊/蒙层/取色） |
| `win32_native.py` | Windows 原生优化（电源三档、GC、IO 优先级） |
| `json_util.py` / `api_errors.py` | JSON 工具、平台语义异常 |
| `identity.py` | 工程标识 |

---

## 四、数据库结构（两个独立的 SQLite 库）

引擎的持久化分**两个互不相同的库文件**，两者的表数与迁移方式都不同：

| 库 | 建表位置 | 表 | 迁移 |
|---|---|---|---|
| **引擎队列库**（`frontier.db`） | `frontier.py` | **8 张业务表** | 由 `PRAGMA user_version` 驱动（`SCHEMA_VERSION = 6`，即 **schema v6**），幂等 ALTER |
| **导出快照库**（`data.sqlite`，由 `export --format sqlite` 按需生成） | `enhancements.py` | **2 张业务表**（`media` / `scrape`）+ **1 张 FTS5 虚拟表**（`scrape_fts`） | 无版本号；每次导出重建（快照语义） |

合计 **10 张业务表 + 1 张 FTS5 索引**。下文各表除 `media` / `scrape` / `scrape_fts` 外，均属**引擎队列库**。

### frontier —— 任务队列（核心表）
| 列 | 说明 |
|---|---|
| `url_hash` (PK) | URL 哈希（去重键） |
| `normalized_url` / `domain` | 规范化 URL / 域名 |
| `depth` / `priority` | 爬取深度 / 优先级（越小越优先） |
| `status` | `pending` / `leased` / `retry` / `done` / `dead` |
| `scheduled_at` | 下次可出队时间（重试退避用） |
| `retry_count` / `max_retries` | 重试计数与上限 |
| `leased_at` / `lease_expires` / `worker_id` | **租约**：领取时间 / 到期时间（心跳续期，v2.18 新增列）/ 领取者 |
| `parent_hash` | **血缘**：发现本 URL 的父页面（任务树溯源） |
| `created_at` | 创建时间 |

索引：`idx_status(status, scheduled_at)`、`idx_frontier_domain_status(domain, status)`

### pages —— 页面抓取记录
`url_hash`(PK) / `status_code` / `content_length` / `fetch_time` / `headers` / `content_hash` / `simhash` / `duplicate_of`（近似重复指向）

### extracted —— 结构化提取结果
`url_hash`(PK) / `data_json`（完整提取数据 JSON）

### errors —— 错误档案
`url_hash` / `error_type` / `error_message` / `timestamp` / `platform`（平台名）/ `code`（平台错误码）—— 支持 `GROUP BY platform, code` 审计

### video_downloads —— 视频下载队列
`video_url`(PK) / `domain` / `status` / `file_path` / `progress` / `fail_count` / `created_at` / `file_size`
索引：`idx_video_status(status, domain)`

### media / scrape —— 导出快照表（属**导出快照库**，与上文各表不同库）
导出 SQLite 时的载体（`media` 媒体记录、`scrape` 页面正文）；配套 **`scrape_fts`（FTS5 虚拟表，trigram 分词，支持中文全文检索）**，每次导出 rebuild（快照语义）。该库由 `export --format sqlite` 生成，与引擎队列库 `frontier.db` 是两个独立文件，因此不计入引擎队列库的 8 张业务表。

### cooldowns —— 域名冷却持久化
`domain`(PK) / `until_epoch` / `tier` / `updated_at` —— 重启后恢复风控状态（不再一重启就忘光）

### settings —— 键值配置
`key`(PK) / `value`

### accounts —— Cookie 弹药库
`site` + `name`(复合 PK) / `state_blob`（**Fernet 加密**，明文 cookies 绝不入库）/ `health_score` / `cooldown_until` / `success_count` / `fail_count` / `updated_at`

---

## 五、数据流与并发控制要点

### 5.1 三级并发仲裁（v2.18 重构）
```
生效并发 = min(所有控制器的诉求)
   ├─ default（配置基线）
   ├─ adaptive_v2（多因子调优）
   ├─ autoscale（AutoscaledPool）
   └─ defense（风控熔断，RED=5 最低——天然最高优先）
```
- 分域同规则（`_domain_requests`）；
- 控制器停止/防御恢复 → **归还诉求并重算**（v2.18 修复：此前残留低值永久钳死并发）。

### 5.2 租约与 CAS（防重复劳动）
```
pop_batch 领取 → leased_at/lease_expires 写入
处理期间     → 每 60s 心跳续租（heartbeat_lease，CAS 校验 leased_at）
完成打卡     → mark_done_checked(url_hash, leased_at) 原子 CAS
              · rowcount>0 → 结果被采纳
              · rowcount=0 → 租约已易主，丢弃本结果（避免双份媒体/导出）
```

### 5.3 缓存与增量
- **HTTP 磁盘缓存**（HttpCache）：TTL 内直接复用；过期后持 ETag/Last-Modified 发条件请求，304 → 复用旧内容跳过重解析（真增量）。

### 5.4 写路径（不阻塞主循环）
业务代码 `await frontier.mark_done(...)` 等只入内存队列 → 独立 flusher 协程批量 `executemany` 落库；失败批次落 `frontier.db.deadletter.jsonl`（不静默丢数据）。

---

## 六、产物目录结构

```
<输出目录>/<任务名>/
├── data/                       # 结构化数据（按域分文件）
│   ├── <domain>_<时间戳>.jsonl  # 每行一条记录（主产物）
│   ├── <domain>_<时间戳>.csv    # 同数据 CSV（UTF-8 + 首行 BOM）
│   └── <domain>_<时间戳>.md     # 正文 Markdown 快照（长文本页）
├── extracted/                  # 每页完整数据的 JSON 副本（文件名=URL哈希）
├── videos/                     # 媒体产物
│   ├── <平台>/<标题>.<ext>      # yt-dlp 下载（含 .info.json 元数据）
│   └── <平台>/*.mp4|*.ts        # m3u8 合并产物
├── images/                     # 图片产物（按页归目录，可选 OCR）
├── frontier.db                 # 本任务的任务队列 + 抓取记录（SQLite）
├── stats.jsonl                 # 结构化进度快照（每批一行 + 收尾 final 行）
├── checkpoint.json             # 断点统计参照
└── 导出文件（按需生成）
    ├── data.xlsx             # Excel（按域分 sheet + 汇总页）
    └── data.sqlite           # SQLite 快照（2 张业务表 + scrape_fts 全文索引）
```

**stats.jsonl 单行字段**：`ts` / `done` / `failed` / `pending` / `total` / `batch` / `videos` / `images` / `bytes`（收尾行额外 `final: true`）。GUI 与 `tools/dashboard.py` 消费此文件。

---

## 七、GUI 侧架构（launcher_v9.py）

```
模块导入
  ├─ from launcher_v8 import CONFIG_FILE, EngineBridge   ← 硬依赖，v8 不可删
  └─ from kiana_vnext_plus import wallpaper, config

KianaV9(FluentWindow)
  __init__（只跑一次）
    ├─ 签名标签（右下角，随强调色）
    ├─ EngineBridge（GUI ↔ 引擎线程边界：信号/日志/进度）
    ├─ 五个页面：HomePage / LogPage / DataPage / TasksPage / SettingsPage
    │     └─ 均继承 _Page（ScrollArea 包裹，v2.18.2 起；内容超出可滚动）
    ├─ addSubInterface × 5（导航注册）
    └─ _apply_qfluent_theme()（主题 + 面板玻璃 QSS，读 panel_alpha）
  事件
    ├─ resizeEvent → _sig_place（签名定位）+ 底图防抖重渲染
    ├─ eventFilter（Show/Resize/Activate）→ 签名重定位
    └─ event(_WpReadyEvent) → 底图/取色 worker 结果分发（postEvent，线程安全）
  底图系统（wallpaper.py）
    _wp_render（防抖250ms）→ 后台线程 process() → postEvent 回主线程
    → QPixmap + 交叉溶解（限帧30fps）+ 自动取色（同色守卫）
  面板玻璃
    panel_alpha（40-95，默认65）→ _panel_qss() 生成渐变玻璃 QSS
    → 应用于 logp/datap/taskp 全部卡片 + home 三张统计卡
```

**性能红线**：引擎桥与页面构建**必须只在 `__init__` 执行一次**。历史事故：该段曾被误缩进进
`_sig_place`，导致窗口每次 Show/Resize 都重建整个界面（单次 1.2s，事件循环停顿 7.6s）。
`tools/gui_perf_probe.py` 是这条红线的回归门禁。

---

## 八、扩展点（怎么加东西）

| 想做的事 | 改哪里 |
|---|---|
| 加站点规则 | `rules/sites/<name>.yaml`（字段选择器/列表/翻页），零代码 |
| 加平台解析器 | `kiana_vnext_plus/<平台>_resolver.py` + 在 `page_processor._enqueue_and_render` 接线 + `media_schema` 收口 |
| 加配置项 | `config.py` 默认值 + GUI「设置」页控件 + `run_crawler.py` 参数 |
| 加导出格式 | `enhancements.py`（参考 `export_xlsx` / `export_sqlite`） |
| 加防御策略 | `defense_protocols.py`（Brain 子类 + `_apply_tier` 接线） |
| 加页面 | `launcher_v9.py` 新建 `_Page` 子类 + `addSubInterface`（注意：只在 `__init__` 调用） |
