# Kiana Vnext Plus

Windows 桌面数据采集引擎：**单进程 asyncio 协程爬虫**（20-50 路并发）+ PySide6/qfluentwidgets GUI。
Python ≥3.11（本机 3.14）；依赖全 `==` 钉死（`kiana_vnext_plus/requirements.txt`）；构建/发版统一走 `一键构建.bat`。

**对外表述基线（不可美化）**：本工程自 **v2.10.3.0 基线接手**并大幅推进，不是"从零构建"；内部安全审计是自查性质，**不构成"通过安全认证"**（`docs/优化计划_v219.md` 第五节）。

> 深入内容在 `docs/`：`DEVLOG.md`（每次改动"为什么"）、`ARCHITECTURE.md`（四层结构）、`BUILD.md`（打包发版）、`GUI施工手册.md`（改 GUI 必读）。本文件只放"一眼要看到 + 违反即事故"的东西。
> 本机专有路径与环境事实在 `.claude.local.md`（不进 git）。

---

## 一、六条红线（违反即事故）

1. **cookies 与密钥绝不入包 / 入库 / 入日志**。`cookies.txt` 是本机密钥，只经环境变量 `KIANA_COOKIE_FILES` 交付；`.gitignore` 必须覆盖 `*cookies*.txt` / `master.key*` / `KianaData/`（门禁第 7 项会查）。
2. **`assets/icon.ico` 用用户原图**：只读打包，禁止裁剪 / 重画 / 重编码。
3. **绝不静默安装交付**：脚本支持 `/S` 只是 NSIS 标准能力，默认必须是完整向导；发版由用户决策后再发。**不要用正式安装的 `Uninstall.exe` 去卸测试副本**——曾因此删掉正式安装的快捷方式与注册表登记（`docs/BUILD.md` 第五节）。
4. **所有动态 URL 请求必须过闸**：走 `url_utils.safe_get` / `safe_urlopen`（协议 + 私网 + 白名单 + `allow_redirects=False` **手动逐跳**复检）。裸 `session.get` 会被重定向绕过——v2.19.7 已用本机双服务 PoC 复现"内网正文以 .jpg 落盘"。
5. **脱敏的边界要分清**：日志 Filter 必须挂 **handler 上**（挂 root logger 无效——Python 只调用祖先的 handler，不调用祖先 logger 的 filter）；新建 handler 会自动获得脱敏（`config.py` 包装了 `logging.Handler.__init__`）。落盘/导出的**副本**统一过 `sanitizer.sanitize_record`。
6. **第三方代码先查许可**：GPL/AGPL/LGPL 类一律不引入；借鉴必须在注释写明"参考 XX 的算法/模式，实现为本工程重写"（`docs/来源与合规声明.md`）。

## 一·前0、**改完就只跑"那一个"测试，别等全量**（用户 2026-10-02 点名）

用户原话（大意）：**"你光跑一次全量……你自己弄出来的错误你又不去查……
及时掐断来重跑，纯浪费时间。"**

**我犯的实例**：改 `tools/stealth_bench.py` 加 `--kernel` 时，
把 `run_bench(probe)` 的**调用契约**从 `probe(url)` 悄悄改成了 `probe(url, kernel)`
—— 既有的两个测试传进来的探针只接 `url`，于是 `TypeError` 被
`except: unknown` 吞掉，表现成"全部站点取不到判据"。
**这个错误本该在改完的下一秒就被发现**（跑 `pytest tests/test_m3_stealth_bench.py`
只要 **0.1 秒**），结果我拖到 3 分钟的全量里才撞见，白等一轮。

### 规矩

| 改了什么 | 立刻跑什么（都是秒级） |
|---|---|
| `tools/stealth_bench.py` | `pytest tests/test_m3_stealth_bench.py` |
| `tools/sync_doc_counts.py` / 文档数字 | `pytest tests/test_doc_claims.py` |
| `kiana_vnext_plus/url_utils.py` | `pytest tests -k shortlink` |
| `kiana_vnext_plus/cookie_armory.py` | `pytest tests -k identity_pool` |
| `kiana_vnext_plus/universal_downloader.py` | `pytest tests -k "cookie or fake_success"` |
| 改了**函数签名**（尤其参数个数） | **先搜它的所有调用方**，别假设"只有我这一处" |

**全量测试只在"一轮工作收尾、准备提交"时跑一次**，不是每改一个文件就跑。

### 更根本的一条

**改签名 = 改契约。** 改之前先 `grep` 所有调用点；
能被"几秒的定向测试"抓住的错，**绝不留到全量**。

## 一·前、改完引擎代码的**固定动作**（省一次全量测试）

`tests/test_doc_claims.py` 强制文档里的「引擎 XX 行」**精确等于实测**。
而**每改一次引擎代码这个数就变** → 那条测试必红。

**原来的浪费**（用户点名过）：
```
改代码 → 跑全量（3 分钟）→ 文档守卫红 → 手改数字 → 再跑一遍全量（3 分钟）→ 提交
```
每轮白烧一次全量。

**现在固定成**：
```powershell
python tools/sync_doc_counts.py      # 改完引擎代码、跑测试**之前**先同步
python -m pytest tests -q            # 然后全量只需跑一次
```
`--check` 只检查不同步（不一致退出码 1）。
它**只改「引擎 XX 行」**，绝不碰启动器的 `1,717` / `1,348`。

## 一·补、工作方式（用户 2026-10-01 明确指示）

**能自己跑的就自己跑，不要找用户要日志。** 原话：

> 「你要说什么日志啥的你跑测试你直接自己去看日志不就行吗，你直接全自动托管不就行吗」

| 事情 | 谁做 |
|---|---|
| 无界面（headless）爬取、命令行跑引擎 | **Agent 自己跑**，日志自己读 |
| 门禁 / 全量测试 / 各类自检工具 | **Agent 自己跑** |
| 起浏览器但不弹窗的（如靶场探针） | **Agent 自己跑**（避开用户打游戏时段） |
| **会弹 GUI 窗口的**（`tools/v9_smoke.py`、`tools/gui_perf_probe.py`） | **需要用户**（避开打游戏时段）——用户明确要求"不要在前台冒弹窗" |

**凡"我能自己执行的"，一律不列进"需要用户"清单。** 只列真的做不到的：
需要用户本人的账号、需要用户决定、或会弹窗打扰用户的。

> **为什么写进红线区**：此前多轮把"跑一次真爬取并读日志"当成需要用户的任务，
> 实际完全可以自理。这种**过度索求**会让用户以为工程卡住了，是真实的沟通成本。

---

## 二、Commands

```bash
# 环境
pip install -r kiana_vnext_plus/requirements.txt    # 运行时（25 条全 == 钉死）
pip install -r requirements-dev.txt                 # 开发/测试（pytest/ruff/mypy/pyinstaller/pip-audit）

# 跑起来
python launcher_v9.py                               # GUI 真入口（launcher_v8.py 不是入口，见下）
python run_crawler.py "https://example.com" -d 0 -m 1 -o ./out    # CLI 单页（-d 0=只爬种子）
python run_crawler.py -f urls.txt -d 3 -m 500 -o ./out            # 清单批量
python -m kiana_vnext_plus.cli status --project <pid>             # 任务管理子命令（status/export/errors/rule-*）

# 测试与门禁（全部离线，约 2 分钟）
python -m pytest tests -q                           # 全量（门禁要求 ≥400 passed 且 0 failed）
python -m pytest tests -q -p no:warnings            # CI 同款
python tools/release_check.py                       # 14 项发版门禁，任一项 FAIL → exit 1
python tools/verify_all.py [--skip-*]               # 多线回归：1 条离线规则线 + 7 条在线线（需公网与 cookies）

# GUI 验证（不弹窗 / 不遮挡桌面）
python tools/gui_perf_probe.py                      # 离屏性能探针（稳态停顿 <200ms；exit 0=PASS）
python tools/v9_smoke.py                            # 移屏外 GUI 冒烟（五页截图 + 6 项控件断言）

# 静态检查（锁死上限，只降不升）
python -m ruff check kiana_vnext_plus
python -m mypy kiana_vnext_plus --ignore-missing-imports

# 打包（见第七节；PO Token 缺件会硬中止构建）
set "POT_SERVER_DIR=%LOCALAPPDATA%\KianaVnextPlus\vendor\bgutil-pot\server"
set "DENO_EXE=%LOCALAPPDATA%\KianaVnextPlus\vendor\deno.exe"
python -m PyInstaller --noconfirm --clean KianaLauncher.spec   # → dist/KianaLauncher/（onedir）
python -m PyInstaller --noconfirm --clean KianaCrawler.spec    # → dist/KianaCrawler/（onedir）
一键构建.bat                                        # 或走这条：7 步全流程（约 20-40 分钟）
```

---

## 三、Architecture

```
launcher_v9.py        GUI 真入口（FluentWindow 五页）          ← KianaLauncher.spec 指向它
  └ launcher_v8.py    只有 6 个符号是活的：v9 直接 import 的 CONFIG_FILE / EngineBridge /
                      load_captcha_keys / save_captcha_keys，加 EngineBridge 内部用的
                      _LineStream / _QtLogHandler（其余 ~60KB 是 legacy 死壳，但**不许删**
                      ——v9 硬依赖，删了 GUI 直接 ImportError）
run_crawler.py        CLI 真入口 + GUI 的进程内引擎（crawl()，也是"UI cfg → 引擎 cfg"最后一站）
  └ kiana_vnext_plus/crawler.py    引擎总装：7 个 _init_* 是接线总表，run() 是主循环
      ├ engine_router.py    5 级回退：protocol → stealth → tls_switch → solver(浏览器) → direct
      ├ page_processor.py   单页流水线：质量闸 → CAS → 持久化/导出 → 媒体入队 → 出链
      ├ frontier.py         SQLite 前沿队列：租约 + CAS + 8 张表 + PRAGMA user_version v5 迁移
      └ url_utils.py        安全原语（safe_get / safe_urlopen / is_private_url），全仓最高扇入
```

- **引擎不是子进程**：`EngineBridge` 在 **daemon 线程 + 独立 asyncio 事件循环**里进程内调 `crawl()`；print/logging 双路桥接为 Qt 信号。GUI **禁止直接触碰引擎对象**（只能经 `EngineBridge` 的 stop/pause/resume 与 `_engine_ref` 只读）。
- **退出码语义固定**：0 完成 / 1 主动停止 / 2 引擎崩溃 / 3 引擎加载失败。
- **运行期数据根** `config.data_root()`：默认 `%LOCALAPPDATA%\KianaVnextPlus`；`KIANA_PORTABLE=1` 时 = **exe 同级** `KianaData/`（**不得改用 `sys._MEIPASS`**——onedir 下指向 `_internal`，升级时会被 `RMDir /r` 连带删除）。
- 任务目录 `<outdir>/cli_<sha8>/`：`frontier.db` / `export/data/<domain>/*.jsonl|csv` / `export/videos|images|audio/` / `stats.jsonl`（GUI 统计卡消费契约）/ `checkpoint.json` / `logs/`。

---

## 四、不碰清单（改前先读这条为什么）

1. `launcher_v8.py` **不许删**（见上）；`installer/lang_strings.nsi` 与 `installer/langs/*` 是**生成物**（改文案要跑 `tools/gen_nsis_langs.py`）。
2. **不改并发模型**：asyncio 协程选型；不引多进程 / Celery / Scrapy（`multiprocessing` 还被两个 spec 排除）。
3. **不引 aiohttp**：本机 aiohttp 外网全超时，全仓已换 `curl_cffi`（`README.md`）。
4. **不给运行态 URL 脱敏**：`frontier.normalized_url`、`video_downloads.video_url` 是"稍后还要再请求一次"的钥匙，抹掉 `?token=`/签名参数会让续爬/续下 403、状态更新匹配不到行。敏感 URL 的收口在**导出侧**（`sanitize_record`），这两张表也不参与任何导出。**别"顺手补闸"。**
5. **不给 `llm_client` 加 SSRF 闸**：那里是用户自填端点，套闸会把 `http://localhost:11434`（Ollama）/ LM Studio 这类本地推理服务拦死——是明确支持的用法。
6. 不装系统级工具（node/deno/ffmpeg 只认随包或 PATH 已有）；新增运行期依赖先审批。
7. 不改 GUI 视觉/交互除非任务明确点名；纯 GUI 任务里 `kiana_vnext_plus/` 一行都不许动（唯一例外是 `__init__.py` 版本号）。
8. 不用 `git filter-repo`/BFG 重写历史；验证产物（`.tmp_*`）不入库。
9. 不删 `kiana_setup.nsi` 里的：卸载段 `un.` 前缀、卸载器 cmd 自清理段；不删 `KianaLauncher.spec` 的 `collect_data_files('qfluentwidgets')`（spec:42，漏了打包后白屏）；`kiana_setup.nsi` 必须保持 **UTF-8 BOM**。
10. 不为"消除零调用/提升指标"去强改核心调用链。

---

## 五、陷阱表（现象 → 正确做法）

| 现象 | 正确做法 |
|---|---|
| 用 `resp.url` 报 AttributeError 被外层 except 吞掉 | **curl_cffi 的 Response 没有 `.url` 属性**；要复检落点只能"请求前校验 + 手动逐跳"（`url_utils.safe_get`） |
| 内网内容被当正常响应保存 | `allow_redirects=False` + 逐跳复检（curl_cffi 默认**跟随 30 跳**） |
| 被拦的 URL 跑进了浏览器层 | 拦截返回 **400**，不能返回 **403**——403 是本工程"升级到浏览器"的信号，浏览器层 `page.goto()` 原生跟随重定向 |
| 子 logger 里的 token 明文进了日志 | Filter 挂 handler（不是 root logger）；非 logging 出口（print 桥 / Qt handler 的 traceback / 引擎异常 emit）也要脱敏 |
| worker 线程 emit 信号偶发崩 | 跨线程回主线程用 `QApplication.postEvent`，**不是 Signal** |
| 中文路径读图静默返回 None | 用 `np.fromfile` + `cv2.imdecode`，不用 `cv2.imread` |
| 视频下载后目录空 / 文件名乱码 | Windows 子进程 GBK 解码坑（yt-dlp 调 ffmpeg）：文件名改 hash + `locale` 设 C.UTF-8 |
| 一次网络抖动被当成"永久失败" | 传输异常必须**抛出**（`None` 只表示"被闸拦下/协议非法"）；调用方靠 `except` 重试（safe_get 的语义边界） |
| `database is locked` 被当成"租约易主" | 每条连接设 `busy_timeout`；CAS 是**三态** `True/False/None`，`None` 必须抛出重试 |
| CSV 第二批起数据被 BOM 污染 | BOM 只在**新文件首写**一次（追加模式不能用 `utf-8-sig`） |
| 单页挂起冻住整批 | 阻塞调用一律 `asyncio.to_thread`（simhash / 全文脱敏 / 文件 IO / 同步 resolver 全在内）；单页看门狗 `page_timeout` 默认 **300s**（`config.DEFAULT_GLOBAL`）。**[v2.19.8 已修]** CLI 的 `--page-timeout 0` 曾传不进引擎（argparse 默认 0 使"未指定"与"显式 0"不可区分，且 `crawl()` 的 gcfg 键表缺该键）→ 现默认 `None` + `page_timeout_override()`，显式值（含 `0`）无条件透传，`0` 真能关掉看门狗 |
| 统计卡/日志"接好了但不更新" | 统计卡必须显式注册进 `_stat_labels`（`findChildren` 扫描已废弃）；GUI 桥接与页面构建**只在 `__init__` 执行一次**（曾因缩进事故每次 Show/Resize 重建 1910 个控件，卡顿 7.6 秒） |
| 改 `.bat` 后 cmd 报"xxx 不是内部或外部命令" | **`.bat` 必须 CRLF 换行 + 纯 ASCII 注释**：Write 工具默认 LF 会让 `REM` 行被拆开执行（还会生成垃圾文件）；中文注释用 OEM 码页解释 UTF-8 字节，问题更重（`一键构建.bat` 是 CRLF+GBK 才没事） |
| 跑 `tools/installer_matrix.py` 想先看会做什么 | 加 `--dry`——但**只对 v2.19.8+ 有效**：旧版的 `--dry` 是摆设、传了仍真装真卸（实测把正式安装的快捷方式起始位置改成了临时目录） |
| 装/卸测试后快捷方式"起始位置"失效 | 卸载器恢复分支曾把 `CreateShortCut` 的起始位置写成自己的临时 `$OUTDIR`（NSIS 语义，v2.19.8 已修）；用旧安装包测试仍会留下这个痕迹，测完核对桌面与开始菜单快捷方式 |

---

## 六、并发与一致性（改 frontier / page_processor 前必读）

- 跨 `await` 的**读-改-写就是真竞态**；首选写法是"**同步块内先占位再 await**"（robots 门闸、渲染预算都按此实现）。
- 写结果路径全部经 **CAS**（传 `leased_at`）；**CAS 打卡必须在持久化之前**；CAS 失守则**一行都不写**。
- `mark_failed` 的 UPDATE 必须带 `AND status != 'done'` 守卫（看门狗曾把已赢 CAS 的页打回重试 → 整页重爬 + 重复导出）。
- **限流 ≠ 重试**：throttled 路径刻意**不**递增 `retry_count`，兜底用独立 `throttle_count`（连续 10 次转 dead）。否则任务永驻 retry，主循环退出条件永不成立。
- 租约心跳续的是 `lease_expires`，**不是** `leased_at`（改后者会破坏 CAS 闭环）。
- 三级并发仲裁取 **min**；控制器停止/恢复必须**归还诉求并重算**，否则残留低值会永久钳死并发。
- DB 迁移：`PRAGMA user_version` 幂等 ALTER + 存量回填；**`CREATE TABLE IF NOT EXISTS` 补不了新列**。

---

## 七、扩展点速查

- **加站点规则（零代码，首选）**：`rules/sites/<domain>.yaml` → `cli rule-new` 生成骨架 → `rule-validate` / `rule-test` 离线验证；**必须配真实样本资产**（`rules/PENDING.md` 约定）；注意 `exclude` 键，避免与视频/音乐通道抢 URL。
- **加平台解析器**：新建 `<site>_resolver.py`（返回同构 `{ok, ...}` dict，并产出经 `media_schema` 收口的 `media`）→ 在 `UniversalDownloader._PLATFORM_RESOLVERS` **加一行**（域后缀 → 模块）。直链截取走统一 schema（`media.streams[0].url`），失败自动退回原 URL、绝不抛异常。**两个硬约束**：resolver 是纯同步实现 → 必须经 `resolve_direct_url` 的 `asyncio.to_thread`（否则冻结引擎）；解析出的直链来自**远端页面状态** → 替换后必须复检 `is_private_url`（旧版抖音块漏了这一步）。需国内直连则加进 `exit_manager` 的 `_DOMESTIC_DOMAINS`。
- **加 GUI 设置项**（**缺任何一处 = "能填不生效"**，历史事故固定点）：① 控件 create → ② `_wire` 接线 → ③ `_start()` 的 cfg → ④ `launcher_v8.EngineBridge` 的 engine_cfg 翻译表 → ⑤ `run_crawler.crawl()` 的 gcfg 键表 → ⑥ `config.DEFAULT_GLOBAL` 默认值。

---

## 八、发版

1. **版本四件套同步**：`VERSION.json` / `pyproject.toml` / `kiana_vnext_plus/__init__.py`（三段式 `X.Y.Z`）+ `kiana_setup.nsi` 的 `!define APP_VERSION`（三段）**与** `!define VERSION`（**四段 `X.Y.Z.0`**）。两个 nsi 字段都要改，漏一个 CI 就红。**版本权威源只有 VERSION.json**（README/ARCHITECTURE/CHANGELOG 头部已滞后，别照抄）。
2. `python tools/release_check.py` → 必须 10/10（含"git 工作区必须干净"——**未跟踪文件也算脏**）。
3. 打包（第二节命令）→ **产物完整性检查**：`dist/KianaLauncher/_internal/` 下必须有 `pot_server/**/deno.exe`、`pot_server/**/build/main.js`、`browsers/**/chrome.exe`。任一为空 = 构建失败必须重打。
4. 装/卸实测：**别用正式 `Uninstall.exe` 卸测试副本**；静默探针捕获窗口后必须 `TerminateProcess`（发 `WM_CLOSE` 会触发连环弹窗）。
5. CHANGELOG 顶部插条目 → commit → `git tag vX.Y.Z-final`。

**打包侧硬约束**：PO Token 缺件时 spec 会 `raise SystemExit` 中止（逃生舱 `KIANA_SKIP_POT=1` 仅限自用测试构建）；Chromium 版本号必须通配不硬编码；在 **Git Bash 里调 makensis 必须用 python subprocess**（MSYS 会把 `/S` 改写成 `S:/`）。

---

## 九、工作纪律（用户明确要求，长期有效）

- 汇报用 **commit / 里程碑**，**不用"X 天"计工作量**；中文、先给结论再给细节。
- 改源码用 Edit/Write 或 python 脚本；**不要用 git bash 的 `sed`/`echo` 直接改源码**。
- **不盲从任务文档或审计报告**：先回源码求证再动手。本工程历史文档存在版本滞后（见下），报告里的"应该没问题"不算证据。
- 先写测试、后改实现；改安全机制后要做**对抗性验证**（不是重跑测试）。
- 测试桩必须与真实接口签名一致——窄签名桩会让被测代码走异常分支，制造**假绿**。
- 一个修复 = 一个 commit = 一组回归测试；大改前打锚点 tag，以便 `git reset --hard` 回退。
- 报告要**如实**：失败就说失败并给输出；跳过就说跳过。别把"看起来有防护"当成"实际有防护"。

---

## 十、已知文档滞后（引用前先核对）

- **`docs/残余风险登记表.md` 是残余风险的唯一入口**：所有"已评估、决定不改"与"需用户在场才能推进"的项都在那里，逐条带理由。**新增残余风险先登记进那张表，再考虑加守卫**；本表不接受空白格（没结论就写"未评估"）。
- `docs/ARCHITECTURE.md` 头部版本号**仍停在 v2.18.2**（`docs/BUILD.md` 与 `CHANGELOG.md` 已同步到 v2.19.8）→ 版本只信 `VERSION.json`。
- CI 步骤名写"基线锁死 ruff≤84 / mypy≤104"，**实际语义是"有任意告警即红"**（比本地门禁严格）。本地门禁的锁死值已按实测收紧到 **80/103**。
- `tools/gui_smoke_shot.py` 与根目录 `_syntax_check.py` / `全方位检查.py` / `启动爬虫.bat` 是**遗留脚本**（分别指向 v8 旧壳、被内联取代、无调用点、硬编码本机 Python 路径）——别以它们为准。

**[v2.19.8 已修，保留记录以免照旧说法重复排查]**

- `docs/BUILD.md` 的 PO Token 示例路径已改为 `…\KianaVnextPlus\vendor\`；同文件对卸载器快捷方式的描述也已按 v2.19.5 起的三分支条件逻辑更正。
- 门禁静态检查不再用 `Found N errors` 正则（那样清零反而判 FAIL）→ 改为 `_static_count()` + 退出码判定，真清零能正常 PASS。
- `tools/installer_matrix.py` 的默认 `--setup` 不再是失效的 2.17.1 路径，改为自动探测仓库根目录最新安装包。
- `tools/installer_matrix.py --dry` 现为真短路（旧版是摆设，传了仍真装真卸）。
