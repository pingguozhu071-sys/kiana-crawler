# GUI 开发指南

> 面向要修改这套桌面界面的开发者。技术内容以当前 `launcher_v9.py` 为准——
> **行号会随改动漂移，请以函数名与控件名为准**。
> 相关：`使用说明.txt`（面向使用者的功能说明）、`python tools/v9_smoke.py`（界面冒烟）。

---

## 一、架构全景（三层，依赖方向单向）

```
launcher_v9.py  （GUI 主入口，KianaLauncher.spec 指向它；五页 UI）
   ├─ 从 launcher_v8.py 复用：
   │    CONFIG_FILE   —— launcher_config.json 路径（含便携版分支）
   │    EngineBridge  —— UI↔引擎桥（唯一的"引擎运行器"，v9 原样复用）
   │    _LineStream   —— print 捕获 → Qt 信号
   │    _QtLogHandler —— logging 捕获 → Qt 信号
   ├─ kiana_vnext_plus/wallpaper.py（底图管线，纯内存衍生，绝不写盘）
   └─ kiana_vnext_plus/config.py data_root()（便携版数据根）
launcher_v8.py  （legacy 壳保留对照；只贡献上述 6 个符号，不再打包）
```

**最关键的一点**：引擎**不是子进程** —— `EngineBridge._run_engine` 在 **daemon 线程**里跑
一个独立的 asyncio 事件循环，进程内直接调 `run_crawler.crawl()`；stdout 与 logging 双路
重定向为 Qt 信号回主线程。

因此 GUI 层**禁止直接触碰引擎对象**（唯一例外：经 `EngineBridge` 的 stop/pause/resume，
以及 `_engine_ref` 只读引用）。

### 启动 → 回传的数据流（改 GUI 必须按这条链对齐，缺一环就是死键/静默失效）

```
load_config() → win.config(dict) → 控件初值
win._start() 从控件现值组 cfg → engine.start(urls, cfg)
  → EngineBridge._run_engine: UI 键 → 引擎键 映射
      depth→crawl_depth / pages→max_pages / outdir→download_path
      resolution→project.config.video_settings.preferred_resolution
      subs→download_subtitles / thumb→download_thumbnail / info→write_info_json
      llm_enabled/key/api_base/model → run_crawler 的 LLM 增强构建
  → run_crawler.crawl() → 引擎
进度回传：引擎写 stats.jsonl → _parse_progress 读该文件（带正则兜底）
  → 更新 _stat_labels 里的统计卡 + 进度条
  日志：两条桥 → LogPage.append（彩色分级）
生命周期：started / finished(退出码) 信号 → 按钮置灰或恢复 + 提示
```

---

## 二、五页结构

| 页 | 类 | 控件要点 |
|---|---|---|
| 首页 | `HomePage` | URL 多行(64px)、深度/页数 SpinBox、输出目录+浏览、Cookies+选择/清空、过滤白名单、开关组 4 个（下载视频/图片/音频/脱敏）、画质下拉 + 字幕/封面/详情开关、5 按钮、进度条 + 3 统计卡 |
| 日志 | `LogPage` | 过滤下拉（全部/INFO/成功/警告/错误）+ 彩色分级 QPlainTextEdit（上限 5000 行） |
| 数据 | `DataPage` | 4 统计卡（已抓取页面/失败页面/待处理/总计） |
| 任务 | `TasksPage` | 历史任务列表 + 刷新/打开选中/删除选中 |
| 设置 | `SettingsPage` | 6 卡：主题/强调色 · Cookies（与首页双向同步）· 打码 API 密钥 · LLM 智能增强 · 出口代理状态 · 外观·底图 |

**统计卡的硬约定**：必须在 `win._stat_labels` 里**显式注册**。
早期实现用 `findChildren` 扫描控件来定位统计卡，结果"界面接好了但数字不更新"——
这类静默失效没法靠肉眼发现，所以改为显式登记。**新增统计卡必须一并登记**。

---

## 三、改 GUI 的"三处对齐"（最重要）

新增首页/设置页控件时，必须**同轮改齐三处**，否则键值到不了引擎：

1. **控件创建**：`HomePage.build` / `SettingsPage.build`
2. **接线**：`KianaV9._wire`（首页开关走 `checkedChanged → config.__setitem__`；
   设置页控件在 build 内 `connect`）
3. **启动组装**：`_start` 的 cfg 字典 → `EngineBridge._run_engine` 的 engine_cfg 映射
   （**v8 是必经之路**）→ `run_crawler.crawl()` 对应消费（GlobalConfig 键 / project.config 合并）

**设置页的两种保存模式**：

- **即时保存**（滑杆/下拉/开关）：`win._wp_update(key, value)` —— 写键、即存、防抖重渲染
- **按钮保存**（密钥类/LLM）：专用 `_save_*` 函数（如 `_save_llm_settings`），带提示反馈

---

## 四、壁纸 / 底图系统（视觉改动最容易出问题的地方）

- **链路**：`_wp_render`（主线程）→ `_wp_worker`（**worker 线程**跑 cv2）→
  `wp_ready` 信号回主线程（转 `QPixmap`）→ `paintEvent` 铺满绘制 + 交叉溶解
  （`QVariantAnimation` 450ms）+ 自动取色 `extract_accent`（另起线程 → 信号 → `setThemeColor`）
- **红线（已由测试锁死：`tests/test_v2161_dl.py::TestWallpaperOriginUntouched`）**：
  `wallpaper.process` / `extract_accent` **只读源图、只出内存衍生图，任何路径都不许写盘**；
  图片下载落盘一律写原始字节（断点续传只做 Range 搬运，零转码）
- **Qt 线程规则**：worker 线程算完必须经 Signal 回主线程（跨线程直接用 `QTimer`
  或动画对象不会触发）
- **配置键**：`wp_mode(off/single/folder)` / `wp_path` / `wp_folder` / `wp_focus` /
  `wp_blur` / `wp_dim` / `wp_auto_dim` / `wp_accent_lock` / `wp_random_start`；
  外观卡控件在 `SettingsPage.build` 尾部，注意布局末尾的 `addStretch(1)` 必须留着

---

## 五、测试与冒烟（改完怎么验证）

| 工具 | 用途 | 怎么跑 |
|---|---|---|
| `tools/v9_smoke.py` | **主冒烟**：移屏外显示 + 五页截图 + 6 项控件断言（含壁纸/取色异步轮询） | `python tools/v9_smoke.py` → 输出走屏外窗口截图 + PASS/FAIL |
| `tools/v9_engine_smoke.py` | 重冒烟：GUI 按钮驱动真实爬 1 页，断言日志回传/统计卡跳动/停止语义 | 需网络；输出 STATE/CHECKS/ENGINE_SMOKE |
| `tools/v9_mini_smoke.py` | 快速诊断（用 example.com 判定死因） | 诊断用 |
| `tools/gui_perf_probe.py` | 离屏性能探针（稳态事件循环停顿 <200ms 为过） | `python tools/gui_perf_probe.py` |
| `tests/test_phase5_gui.py` | 引擎侧单测（pause/resume、`on_engine` 回调链）—— 不实例化窗口 | `pytest tests/test_phase5_gui.py` |

**两个已知事项**：

1. 离屏（offscreen）截图下中文显示为方块 —— 那是**字体缺失**，不是界面缺陷；
   真实窗口渲染正常。做视觉验收请看布局，而不是看离屏截图里的文字
2. 冒烟断言只覆盖"控件存在性 + 关键行为"，**不校验像素** —— 改视觉后需要人工对比截图

---

## 六、已知陷阱（改动时注意）

1. **统计卡**：必须显式注册进 `_stat_labels`（见第二节）
2. **接线时机**：`connect` 的目标必须在窗口已建之后 —— `_wire` 在页面 build 之后调用
3. **线程回 UI**：worker 线程 → UI 必须经 Qt 信号；动画类对象跨线程不触发
4. **cookie 双入口**：首页与设置页有双向同步（`_sync_cookie_to_settings` /
   `_on_cookie_settings`），改动时两边都要顾
5. **底图重渲染防抖 250ms**：`resizeEvent` 会触发重渲染，防抖不能省
6. **遗留占位**：`_on_finished` 里的 `self.badge = None` 是历史残留（真正的 badge 在首页）—— 不要引用
7. **玻璃分级 QSS**：`_apply_qfluent_theme` 只给日志/数据/任务页的 `CardWidget`
   与首页 3 张统计卡设背景；设置页卡片走 Fluent 默认主题（新增卡片无需写 QSS）
8. **停止语义**：`EngineBridge.stop` 是软停止（`_should_stop`），暂停是"批间不取批"
9. **退出码**：0 完成 / 1 主动停止 / 2 引擎崩溃 / 3 引擎加载失败（界面提示文案与之对应）

---

## 七、环境与常驻文件

- **配置**：`%LOCALAPPDATA%\KianaVnextPlus\launcher_config.json`；
  便携模式（`KIANA_PORTABLE=1`）下在 exe 同级 `KianaData/`
- 除底图源文件与运行期统计外，GUI 不产生其他状态文件；日志在现场会话内，不写进配置
- **图标**：`assets/icon.ico`（只读打包，**禁止重编码**）
- **打包**：`KianaLauncher.spec` 入口是 `launcher_v9.py`；
  `collect_submodules('kiana_vnext_plus')` 会自动收集包内新模块 ——
  **GUI 新增模块无需改 spec**（规则层 / Chromium / ffmpeg 是例外，见 spec 内注释）

---

## 八、无界面卡的高级配置键

以下键没有 GUI 入口，写在配置文件里生效：

| 键 | 作用 |
|---|---|
| `cdp_attach` / `cdp_port` | 接管一个已在运行的浏览器（实验特性） |
| `fingerprint_update_enabled` | 在线指纹库更新 |
| `sitemap_discover` | sitemap 播种（自动发现站点地图并入队） |
| `robots_respect` | robots.txt 合规（首页有开关，也可直接写配置） |
| `signature_text` / `signature_enabled` | 右下角落款文本与开关（默认关闭、文本为空） |
