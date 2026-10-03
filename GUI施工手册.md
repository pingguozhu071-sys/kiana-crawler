# Kiana Vnext Plus · GUI 施工手册（v2.16.1）

> 面向对象：新会话/新模型接手 GUI 改动前的交接文档。
> 基线：HEAD `7a5ec17`（2026-08-30），行号均以该提交为准（后续改动会漂移，以函数/菜单路径为准）。
> 配套：`使用说明.txt`（面向用户的功能说明）、`python tools/v9_smoke.py`（GUI 冒烟）。

---

## 一、架构全景（三层，依赖方向单向）

```
launcher_v9.py  （打包主入口，KianaLauncher.spec 指向它；五页 UI，~1260 行）
   ├─ 从 launcher_v8.py 复用（v9:38 import）：
   │    CONFIG_FILE   —— launcher_config.json 路径（v8:40-45，含便携版分支）
   │    EngineBridge  —— UI↔引擎桥（v8:180-328，唯一"引擎运行器"，v9 零改动复用）
   │    _LineStream   —— print 捕获 → Qt 信号（v8:136-164）
   │    _QtLogHandler —— logging 捕获 → Qt 信号（v8:166-177）
   ├─ kiana_vnext_plus/wallpaper.py（底图管线，纯内存衍生，绝不写盘）
   └─ kiana_vnext_plus/config.py data_root()（便携版数据根）
launcher_v8.py  （legacy 壳 KianaV8 保留对照；只贡献上述件，不再打包）
```

**头等大事**：引擎**不是子进程**，是 `EngineBridge._run_engine` 在 **daemon 线程**里
跑 asyncio 事件循环、进程内直接调 `run_crawler.crawl()`（v8:218-288）。stdout/logging
被双路重定向为 Qt 信号回主线程。GUI 改动**禁止直接触碰引擎对象**（除了经
`EngineBridge` 的 stop/pause/resume 与 `_engine_ref` 只读引用）。

启动→回传数据流（改 GUI 必须按这条链对齐，缺一环 = 死键/静默失效）：

```
load_config() → win.config(dict) → 控件初值
win._start()(v9:986) 从控件现值组 cfg → engine.start(urls,cfg)
  → EngineBridge._run_engine: UI键→引擎键映射(v8:239-259)
      depth→crawl_depth / pages→max_pages / outdir→download_path
      resolution→project.config.video_settings.preferred_resolution
      subs→download_subtitles / thumb→download_thumbnail / info→write_info_json
      llm_enabled/key/api_base/model → run_crawler._maybe_llm_enhancer
  → run_crawler.crawl() → 引擎
进度回传：引擎写 stats.jsonl → _parse_progress(v9:1155) 读该文件（regex 兜底）
  → 更新 7 个统计卡(_stat_labels) + 进度条；日志：两条桥 → LogPage.append(彩色分级)
生命周期：started/finished(退出码 0/1/2/3) 信号 → 按钮置灰/恢复 + toast +
  退出码语义提示(v9:1121-1148：0完成/1停止/2崩溃/3加载失败)
```

---

## 二、五页结构（v9）

| 页 | 类 | 控件要点 |
|---|---|---|
| 首页 | `HomePage`(v9:101) | URL 多行(64px)、深度/页数 SpinBox、输出目录+浏览、Cookies+选择/清空、过滤白名单、**开关组 4 个**(下载视频/图片/音频/脱敏)、**画质下拉+字幕/封面/详情开关**(v2.16.1 新增, v9:165-181)、5 按钮、进度条+3 统计卡(已抓取/失败/每秒) |
| 日志 | `LogPage`(v9:254) | 过滤下拉(全部/INFO/成功/警告/错误)+彩色分级 QPlainTextEdit(上限 5000 行) |
| 数据 | `DataPage`(v9:313) | 4 统计卡(已抓取页面/失败页面/待处理/总计) |
| 任务 | `TasksPage`(v9:333) | 历史任务列表(文本行选中)+刷新/打开选中/删除选中 |
| 设置 | `SettingsPage`(v9:369) | 6 卡：**主题/强调色 · Cookies(与首页双向同步) · 打码 API 密钥 · LLM 智能增强(v2.16.1) · 出口代理状态 · 外观·底图** |

统计卡机制（坑 3 防线）：显式注册 `win._stat_labels`（v9:637-641，7 个 label），
**废弃 `findChildren` 扫描**（曾被坑：静默不更新）。新增统计卡必须显式登记。

---

## 三、改 GUI 的"三处对齐"模式（最重要）

任何新增首页/设置页控件，必须同轮改齐三处，否则键值到不了引擎：

1. **控件创建**：`HomePage.build` / `SettingsPage.build`；
2. **接线**：`KianaV9._wire`（v9:953-968，首页开关走 `checkedChanged→config.__setitem__`；
   设置页控件在 build 内 connect）；
3. **启动组装**：`_start` 的 cfg 字典（v9:1002-1017，首页控件现值）
   + `EngineBridge._run_engine` 的 engine_cfg 映射（v8:239-259，**v8 是必经之路**）
   + `run_crawler.crawl()` 对应消费（GlobalConfig 键 / project.config 合并）。

设置页两类保存模式：
- 即时保存（滑杆/下拉/开关）：`win._wp_update(key, value)`（写键→即存→防抖重渲染）；
- 按钮保存（密钥类/LLM）：专用 `_save_*` 函数（如 `_save_llm_settings` v9:836），
  toast 反馈——**LLM 卡、打码卡走这种**。

---

## 四、壁纸/底图系统（视觉改动最大雷区）

- 链路：`_wp_render`(主线程, v9:730) → `_wp_worker`(**worker 线程**跑 cv2, v9:752)
  → `wp_ready` Qt 信号回主线程（v9:763 转 QPixmap）→ `paintEvent` 铺满绘制
  （v9:698-710）+ 交叉溶解（QVariantAnimation 450ms）+ 自动取色 `extract_accent`
  （另起线程→`accent_ready` 信号→`setThemeColor`）。
- **红线（已加测试锁死，tests/test_v2161_dl.py::TestWallpaperOriginUntouched）**：
  `wallpaper.process/extract_accent` 只读源图、只出内存衍生图，**任何路径都不许写盘**；
  图片下载→落盘一律 `wb` 原始字节（断点续传只做 Range 搬运，零转码）。
- Qt 线程规则：worker 线程算完必须经 Signal 回主线程（QTimer 跨线程不触发，坑 5）。
- 配置键：`wp_mode(off/single/folder)/wp_path/wp_folder/wp_focus/wp_blur/wp_dim/
  wp_auto_dim/wp_accent_lock/wp_random_start`；外观卡控件在 `SettingsPage.build` 尾部
  （v9:480-561），注意布局末尾的 `addStretch(1)` 必须留着。

---

## 五、测试与冒烟体系（改完怎么验证）

| 工具 | 用途 | 怎么跑 |
|---|---|---|
| `tools/v9_smoke.py` | **主冒烟**：移屏外 show + 五页截图 + 6 项控件断言(含壁纸/取色异步轮询) | `QT_QPA_PLATFORM=offscreen python tools/v9_smoke.py` → 产物 `tests/assets/gui_shots/v9_*.png` + PASS/FAIL |
| `tools/v9_engine_smoke.py` | 重冒烟：GUI 按钮驱动真实爬 1 页，断言日志回传/统计卡跳动/停止语义 | 需网络；输出 STATE/CHECKS/ENGINE_SMOKE |
| `tools/v9_mini_smoke.py` | 死因诊断（example.com 快速失败判定） | 诊断用 |
| `tools/fluent_smoke.py` / `gui_smoke_shot.py` | 第 0 步路线验证 / v8 离屏五页 | 轻量后备 |
| `tests/test_phase5_gui.py` | 引擎侧单测（pause/resume、on_engine 回调链）——不实例化窗口 | `pytest tests/test_phase5_gui.py` |

**两个已知事项**：
1. 离屏(offscreen)截图下中文呈"口口口"方块——字体缺失问题，**不是 GUI 缺陷**；
   真实窗口渲染正常。视觉验收请真窗口跑或看 `gui_shots` 布局而非文字。
2. 冒烟断言是"控件存在性+关键行为"，不校验像素——改视觉后截图需人工对比。

---

## 六、已知坑位（从源码注释提炼，改时避开）

1. 统计卡：`findChildren` 扫描曾静默不更新 → 必须显式注册 `_stat_labels`；
2. connect 目标必须在窗口已建（`_wire` 在页面 build 之后调用）；
3. worker 线程→UI 必须经 Qt 信号；`QPropertyAnimation`/`QVariantAnimation` 跨线程不触发；
4. cookie 双入口（首页+设置页）双向同步保留（`_sync_cookie_to_settings` /
   `_on_cookie_settings`，坑 9）；
5. 底图重渲染防抖 250ms（resizeEvent 会触发，坑 11）；
6. `_on_finished` 里 `self.badge = None` 是遗留占位（badge 实际在 home 页）——勿引用；
7. 玻璃分级 QSS：`_apply_qfluent_theme`（v9:914-933）只给 logp/datap/taskp 的
   CardWidget 与首页 3 统计卡设背景；设置页卡片走 Fluent 默认主题（新卡无需 QSS）；
8. 停止语义：`EngineBridge.stop` 是软停止（`_should_stop`），暂停是"批间不取批"；
9. 退出码：0 完成 / 1 主动停止 / 2 引擎崩溃 / 3 引擎加载失败（GUI 提示文案 v9:1129-1136）。

---

## 七、v2.16.1 新增件速览（本手册写成之前刚加的功能）

- **首页画质下拉 + 字幕/封面/详情开关**：三处对齐已完成
  （HomePage.build v9:165-181 / `_wire` 开关组 / `_start` cfg + v8 engine_cfg + run_crawler
  `download_subtitles` 等键 + crawler `_quality_for` 消费）；画质档位值：
  highest/2160/1440/1080/720/480（2160/1440 下拉对应 `bestvideo[...]+bestaudio` 音画组合）。
- **设置页 LLM 卡**（v9:438-470）：SwitchButton(默认关) + API 地址 + 模型名 + Key 密码框 +
  月度预算 SpinBox + 保存按钮（`_save_llm_settings(enabled,key,base,model,budget)`）；
  键 `llm_enabled/llm_key/llm_api_base/llm_model/llm_budget_month`；
  v8 `_load_config` 已收窄旧清洗（不再 pop 这 5 键）；默认关=零开销
  （`run_crawler._maybe_llm_enhancer` 键不齐不构建）。隐私扫描器已覆盖 llm_key 明文检查。
- **首页"协议合规"开关**（v2.17）：`sw_robots`（初始 `cfg.get("robots_respect", False)`），
  同内容脱敏同款调动链（`_wire` 开关组 → `_start` cfg → v8 engine_cfg `robots_respect`
  → run_crawler gcfg → `crawler._robots_respect` 链接入队闸（robots_policy 按域缓存判定）。
- **高级键（无 GUI 卡，走配置文件）**：`cdp_attach/cdp_port`（实验，CDP 接管既有浏览器）、
  `fingerprint_update_enabled`（在线指纹更新）、`sitemap_discover`（sitemap 播种）。
- **代理状态卡/隐私扫描按钮**（v2.16 M3/M5 先例）：只读展示 + `_run_priv_scan` 子进程
  跑 tools/privacy_scanner.py。

---

## 八、环境/常驻文件

- 配置：`%LOCALAPPDATA%\KianaVnextPlus\launcher_config.json`（便携版
  `KIANA_PORTABLE=1` 时在 exe 同级 `KianaData/`，v8:40-45）；
- 底图源文件与统计卡等运行期无其他 GUI 状态文件；日志在现场会话内（不进配置）。
- 图标：`assets/icon.ico`（只读打包，禁止重编码）；spec: `KianaLauncher.spec` 入口
  launcher_v9.py，`collect_submodules('kiana_vnext_plus')` 自动收包内新模块，
  **GUI 新模块无需改 spec**（规则层/Chromium/ffmpeg 例外，见 spec 内注释）。
