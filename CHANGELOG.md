# 变更日志（CHANGELOG）

本工程采用「版本号四件套同步」策略：`VERSION.json` / `pyproject.toml` / `kiana_vnext_plus/__init__.py` / `kiana_setup.nsi` 四处版本号必须一致，由 `tools/release_check.py` 门禁强制校验。

格式：`## [版本] - 日期` —— 每条发版记录「主题 + 要点 + 对应用户可感知的变化」。
日期取对应 tag 的提交日期（发版日）。

---

## [2.19.8] - 2026-09-18

**主题：CLI/GUI 键表接线缺陷修复 + 门禁与工具链自纠 + 文档同步**

- **`--page-timeout 0` 关不掉看门狗（声明与行为不符）**：帮助文本承诺"0=关闭"，但 argparse 默认值也是 0（"未指定"与"显式 0"不可区分），且 `crawl()` 的引擎键表里根本没有这个键 → 实际恒用 `DEFAULT_GLOBAL` 的 300s。GUI 侧手写在 `launcher_config.json` 的同一键同样到不了引擎。现三处对齐：参数默认改 `None`、显式值（含 0）无条件透传、两条入口键表都接上；`DEFAULT_GLOBAL` 仍是唯一默认源。
- **门禁两个自纠**：① 静态检查项原只认 `Found N errors` 正则，**把静态错误清干净反而判 FAIL**（干净时 ruff 输出 `All checks passed!` 拿不到数字）——改以退出码为准；② `--skip-network` 此前是死参数（声称跳过 pip-audit 却照旧联网），现真正生效并如实标注 SKIP。
- **安装器工具链两处修复（实测事故驱动）**：① `tools/installer_matrix.py` 的 `--dry` **从未被判断**（参数是摆设，传了仍真装真卸）→ 现真正短路，只打印命令；② 卸载器"恢复原安装"分支重建快捷方式时，`CreateShortCut` 的起始位置取的是本副本的临时 `$OUTDIR`（NSIS 语义）→ 恢复出来的桌面/开始菜单快捷方式工作目录指向随后被删的临时目录；现显式 `SetOutPath` 到被恢复的安装目录。
- **工具链**：`installer_matrix.py` 默认安装包改为**自动探测**仓库根目录最新 `KianaVnextPlus-Setup-*.exe`（原硬编码 2.17.1 早已不存在）。
- **文档同步**：CHANGELOG 补齐 2.19.0–2.19.7 共 7 个缺失条目；README/ARCHITECTURE/BUILD 的版本号、PO Token 路径、卸载器描述、`一键构建.bat` 的 onefile/onedir 误标一并修正。

**补记 2026-09-27（未改版本号）**

- **媒体下载会话未带 TLS 指纹 → 吞吐掉到 1/2~1/4**：`MediaDownloader.init_session()` 与 `UniversalDownloader.init()` 建 curl_cffi 会话时只传 `timeout` / `headers`，**没传 `impersonate`** → 落在"不模拟指纹"的默认档。同一文件、同一代理、不落盘、交叉轮替取样实测：无指纹 **4.71–8.17 MB/s**，带指纹（chrome136）**12.09–25.26 MB/s**（中位约 2×，高峰 25）。
  这不只是性能问题：工程把 curl_cffi 定位为"TLS 指纹模拟主通道"（requirements 注释如此），协议通道 `protocol_engine` 确实传了 impersonate，**唯独下载路径漏了** —— 同一次任务里页面请求与媒体请求的指纹不一致，本身就是可检测信号。
  现两处补 `impersonate=TLS_IMPERSONATE_POOL[0]`（取自工程自己的指纹池，不硬编码，与协议通道同源）。
  回归测试：`tests/test_v2198_download_tls.py`（结构 AST 断言 + 运行时会话属性断言；已做反证——去掉修复两条断言均失败）。
  暴露场景：用本工程采集某音声站两个作品（5.26 GB / 55 文件）时全程仅 ≈7 MB/s，逐层定位后确认瓶颈在工程自身（与线路、代理、分块大小、是否落盘均无关）。

## [2.19.7] - 2026-09-18

**主题：claude-security 扫描收口（含两处"不盲从扫描建议"的刻意保留）**

- **下载通道可被重定向绕过 SSRF 闸（PoC 复现）**：先在本机起两个服务复现"外部 URL 302 → 169.254.169.254、内网正文以 .jpg 落盘"。新增共享原语 `url_utils.safe_get`（入口校验 + `allow_redirects=False` 手动逐跳复检 + 中间跳响应显式关闭），接入通用下载 / m3u8 分片 / 订阅源 / sitemap 与 robots / 代理源 / 媒体 worker。
- **CLI 链路脱敏结构性缺口**：命令行链路从不加载配置模块 → 日志脱敏 Filter 从未装配（含 token 的 URL 明文进终端）；主密码还有公开字面量兜底 `kiana-fallback`（弹药库加密形同虚设）。已补装配并删除弱兜底。
- **GUI 打码密钥：谎报加密 + 从不传导到引擎**：原明文写 `launcher_config.json` 却提示"DPAPI 加密"，且三个密钥框的值到不了引擎（能填能存不生效）→ 改真 DPAPI 密文（老明文启动时自动迁移并抹除）+ 打通传导链。
- **导出副本脱敏不完整**：原只脱敏 `text`，`url` / `images[].src`（签名 CDN）/ `author` / `entities` 原样落盘 → 新增 `sanitize_record`。**刻意不脱敏**运行态 `frontier.normalized_url` 与 `video_downloads.video_url`（是"稍后还要再请求一次"的钥匙，抹掉签名会让续爬/续下 403）；`llm_client` 也刻意不加 SSRF 闸（作者自填端点，含 localhost 的 Ollama/LM Studio）。

## [2.19.6] - 2026-09-18

**主题：pr-review-toolkit 专项审查——我的安全修复本身有根本缺陷**

- SSRF 闸曾用 403 作拦截态，而 403 恰是本工程"升级到浏览器"的信号 → 被拦 URL 交给无闸的 Playwright，浏览器自行跟随重定向进内网（闸完全失效）。改 400 + 浏览器层单点闸。
- 重定向上限 5 且超限静默返回 3xx → 不可重试 → 永久 dead 且零错误记录；而 curl_cffi 默认跟随 30 跳。改上限 10 + 超限 508 / 缺 Location 502。
- 另修：下载闸下沉共享原语、m3u8 闸改 DNS 校验、`mark_failed` 加"不覆盖 done"守卫、`mark_done_checked` 改三态、redis 后端签名对齐、lastResort 脱敏。

## [2.19.5] - 2026-09-18

**主题：根治"装副本会破坏原安装"（质检实测发现）**

- 安装器此前**无条件覆盖** `InstallDir` → 卸载副本时连带删掉正式安装的快捷方式与注册表登记。现记录 `PrevInstallDir`/`PrevDisplayVersion`，卸载器三分支：非登记安装→什么都不动；登记安装且有可恢复原安装→**恢复**（快捷方式+注册表+版本号）；否则彻底清理。②③ 用 `Goto` 明确互斥（早期版本"恢复后又走到删除"）。

## [2.19.4] - 2026-09-13

**主题：对抗性验证发现的两处补强**

- SSRF 闸补 CGNAT 漏点（`100.64.0.0/10` 不在 Python `ip.is_private` 内，此前可直接穿过）。
- 日志脱敏改为**自动挂载**（包装 `logging.Handler.__init__`）——"忘记挂 Filter"这一失效模式被结构性消除，不再依赖调用方记性。

## [2.19.3] - 2026-09-13

**主题：治理收尾（CI + 开发依赖 + 抽象层标注）**

- 新增 `.github/workflows/ci.yml`（版本四件套一致性 + 静态检查 + 全量测试 + 覆盖率，全离线）与 `requirements-dev.txt`（开发依赖 `==` 钉死）。
- 计划 4.5/5.5 的抽象层零调用项按"接线而非删除"标注保留。

## [2.19.2] - 2026-09-13

**主题：反检测/m3u8 回归补测 + 覆盖率基线**

- 新增 35 例反检测与 m3u8 回归；补覆盖率基线（行级 52%，只报告不设阈值）；清理重复实现。

## [2.19.1] - 2026-09-13

**主题：租约/CAS 收口（外部评估报告的高优先级项全清）**

- 所有写结果路径收口到 CAS；**CAS 打卡前移到持久化之前**（原序使"CAS 失守时产物已落盘"）；限流转 dead 兜底（`throttle_count`）；单页看门狗默认由 0 改为 300s（原默认等于"一页挂起冻住整批"）。

## [2.19.0] - 2026-09-13

**主题：外部评估问题全量修复（6 批次，21 项论断逐条源码求证）**

- **安全 P0**：主抓取通道补 SSRF 闸（请求前校验 + 手动逐跳）、下载通道补闸、m3u8 四层闸、入队闸；**日志脱敏根治**（Filter 必须挂 handler——挂 root logger 对子 logger 完全无效）；HTTP 缓存改存脱敏后内容。
- **并发**：robots 门闸改预约式占位（Crawl-delay 不再被同域并发击穿）、渲染预算同步占位+失败回滚、评论任务持强引用。
- **质量/治理**：静态检查清零并锁死基线、36 例安全/并发回归、便携数据根改 exe 同级（原 `sys._MEIPASS` 会在升级时被删）、构建门禁、卸载器跨实例防护、5 处文档漂移修正。

## [2.18.2] - 2026-09-06

**主题：GUI 布局与视觉重设计（体验版）**

- **设置页「挤在一起」修复**：`_Page` 基类改为 ScrollArea 包裹 + 内容层透明装底。根因是页面无滚动容器，设置页 8 张卡片自然高度约 1550px、超过 1080p 屏可用视口约 950px，被布局压扁导致控件堆叠重叠。现全页面可滚动，卡片保持自然高度。
- **中央黑灰块改渐变玻璃**：日志/历史任务/数据/任务页原为 92% 不透明纯色块（`rgba(22,27,34,235)`），改为渐变玻璃（透明度梯度 + 1px 半透明描边 + 12px 圆角），壁纸透出、文字仍清晰。
- **玻璃透明度可调（作者新要求）**：设置页新增「面板玻璃」滑杆（40-95%）+ 四档预设（通透 40 / 标准 65 / 柔和 78 / 实心 95），即调即生效、即存盘。
- 窗口启动尺寸兜底（1360×860，最小 980×620），消除内容区 sizeHint 撑高窗口的风险。

## [2.18.1] - 2026-09-06

**主题：GUI 卡顿总根因修复 + 安装向导重设计（体验版）**

- **卡顿总根因**：`__init__` 引导段（EngineBridge + 五个页面 + addSubInterface + 全应用主题，约 1900 个控件 setStyleSheet）曾被缩进事故吞进 `_sig_place`，导致窗口每次 Show/Resize/激活都完整重建界面（单次约 1.2 秒）。已复位回 `__init__`，**探针实测事件循环最大停顿 7602ms → 0ms**。
- 壁纸系统加固：跨线程投递改 `QApplication.postEvent`（原 Signal 在部分环境偶发 arity 异常）；自动取色加同色守卫（避免 `setThemeColor` 触发全应用样式重算风暴）；`paintEvent` 改 cover-blit 快路径（不再逐帧 CPU 拉伸大图）；交叉溶解限帧 30fps；整窗声明 `WA_OpaquePaintEvent`。
- **安装/卸载向导全新设计**（深空玻璃）：渐变光晕 + 点阵纹理 + Logo 芯片 + 特性列表 + 版本徽章（自动读 VERSION.json）；**卸载向导首次有专属侧图**（"程序移除、数据保留"语义）；卸载器加自清理收尾，实测**零残留**。

## [2.18.0] - 2026-09-06

**主题：稳定性大版本 —— 44 项深查修复**

- **P0**：风控一次误判后全局并发永久钳死（defense 恢复 GREEN 时未归还 min-wins 诉求，`forget_source` 全仓零调用）。现并发仲裁表分域化 + 恢复时全量重算。
- **P1（11 项）**：LLM 链接打分串行阻塞 35 分钟启动（改 8 路并发 + 总预算超时）；页面租约 300s 短于单页最坏处理时长导致同 URL 双协程并发（加租约心跳续期，DB 迁移 v4）；`pop_batch` 返回旧 `leased_at` 使 CAS 闭环对首次租约失效（真 bug）；`stats.jsonl` 单页任务 0 字节**真根因**（纯直链媒体任务零页面批，收尾补 final 快照）；CSV 追加模式 BOM 中部污染；质量拒收时序倒置（低分页先落盘才拒收）；304 分支 report_result 双写；代理 inflight 泄漏；裸 `<link rel="canonical">` 打穿整页解析；m3u8 中文路径 GBK 写崩；邮箱正则 O(n²) 卡死 worker。
- **P2（13 项）**：限流令牌"归还后不重扣"击穿全局限流；cookie 多文件解析接入全部消费点；FTS5 换 trigram 分词（中文可搜）；代理校验三处宽松口收紧；site_rules 逐段容错；`pop_batch` 锁重试；NSIS 升级前清 `_internal`（防新旧 DLL 混跑）；B站 `__INITIAL_STATE__` 改括号平衡扫描；fire-and-forget 任务持强引用；urllib 回退解码语义统一；配置 int 守卫；弹药库解密告警。
- **P3（15 项 + 5 项复核豁免）**：详见 `docs/DEVLOG.md` 对应条目。
- 新增回归测试 11 例（`tests/test_v218_p0_defense.py`、`tests/test_v218_p1_fixes.py`）。

> 完整清单与逐项修复记录见 `docs/DEVLOG.md`。

## [2.17.2] - 2026-09-05

**主题：安装器致命缺陷修复 + 字体清晰化**

- **P0 双雷**（自 v2.10.3 起潜伏）：
  1. 卸载逻辑误写成**无 `un.` 前缀的普通 Section** → 静默安装后 `RMDir /r "$INSTDIR"` 把刚装的 520MB 全删光；改为 `Section "un.KianaVnextPlus"` 后卸载器才有实体。
  2. 卸载器 0 Section 空壳（点了不删任何东西）→ 补齐，实测删净 1766MB。
- **字体清晰化**：删 `MUI_FONT_1/2` Roboto 死定义，加 `ManifestDPIAware true`（高分屏不再被 DWM 位图拉伸）+ 按语言 `SetFont`（中文 SimSun 10pt / 日文 MS PGothic / 韩文 Gulim / 西文 Segoe UI）。
- PO Token 三件套随包（双 onedir 全验证）。

## [2.17.1] - 2026-09-05

**主题：多语言安装器 + 工程卫生**

- 安装/卸载向导 8 语种（en-GB/en-US/zh-CN/zh-TW/zh-HK/ja-JP/ko-KR/fr-FR），自建 en-GB(2057) 与 zh-HK(3076) 语言段。
- GUI 电子签名（右下角，颜色随强调色联动）。
- `cookie_utils.parse_cookie_file_list`：cookies 多文件解析（分号/换行/注释/去重）。
- 安装器美术生成器（PIL，GUI 令牌对齐）+ 字体策略定稿。
- 凭据卫生门禁第 10 项（公开分发零内置凭据）+ `.gitignore` 凭据规则。

## [2.17.0] - 2026-08-30

**主题：六边形战士**

- 质量收口 11 项（网易云 form / 白名单重试 / 断点续传统一）。
- 平台解析器扩充：网易云 weapi、小红书/快手状态通道、B站二级评论、弹幕 ASS。
- 统一媒体 schema；镜像回退。
- 协议合规：robots 合规（GUI 开关）、sitemap 播种、限流≠重试、爬行策略 BFS-DFS-BFF。
- 指纹保鲜（chrome136 段 + 在线更新）；屏幕一致性资产；身份捆绑 Session；快手页内签名（run_in_page/CDP 实验）。
- LLM 默认关接线；`verify_all` 七线门禁。

## [2.16.0] - 2026-08-29

**主题：满分计划（可移植性 / 可观测性 / 兼容性）**

- **可移植性**：Chromium 随包、PO Token 自包含、便携版（`KIANA_PORTABLE=1` → 数据落 exe 同级 `KianaData/`）。
- **性能数据化**：`stats.jsonl` 结构化进度信号 + `tools/dashboard.py` 数据看板 + 基准落盘（`stress_bench.py`）。
- **LLM 兼容层**：四格式（openai/anthropic/gemini/ollama）自由组合。
- 打码 GUI 入口、YT 一键脚本、代理池卡、隐私扫描器、血缘审计（`parent_hash`）、`rule-new`、质量全链、304 TTL。
- 电源三档、版本检查器。

---

## 维护约定

新增版本时：
1. 同步四处版本号（`VERSION.json` / `pyproject.toml` / `kiana_vnext_plus/__init__.py` / `kiana_setup.nsi`）
2. 在本文件顶部插入新条目（对应日期与主题）
3. `python tools/release_check.py` 门禁全绿后再打 tag：`git tag vX.Y.Z-final`
