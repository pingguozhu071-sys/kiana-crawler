# 构建与打包文档（BUILD）

> 适用版本：v2.19.9（`VERSION.json` 为权威源）｜ 描述从源码到安装包的完整流程、命令与坑。
> **注意**：本文档曾长期滞后于代码（版本号、PO Token 路径、卸载器行为），v2.19.8 已逐处修正；
> 引用具体行为前请以代码为准：`kiana_setup.nsi` / `KianaLauncher.spec` / `一键构建.bat`。

---

## 一、构建总览

```
源码
 ├─[1] PyInstaller → dist/KianaLauncher/   (onedir，GUI，consoless)
 ├─[1] PyInstaller → dist/KianaCrawler/    (onedir，CLI，console)
 │        ↑ 两者都需环境变量注入 PO Token 组件（否则 YouTube 下载失效）
 └─[2] NSIS → KianaVnextPlus-Setup-X.Y.Z.exe
          · File /r 合并两个 onedir（共享 $INSTDIR/_internal）
          · 八语种安装/卸载向导
          · 装后 ~1.8 GB，压缩后 ~540 MB
```

**关键设计**：GUI 与 CLI 各自独立 onedir，安装时**合并进同一 `$INSTDIR`**（共享 `_internal`），桌面/开始菜单指向 `KianaLauncher.exe`。

---

## 二、前置条件

| 项目 | 要求 |
|---|---|
| Python | **3.11+**（三处口径统一说明见下） |
| Python 依赖 | `pip install -r kiana_vnext_plus/requirements.txt`（运行时，**25** 个包全部 `==` 钉死） |
| 开发/测试依赖 | `pip install -r requirements-dev.txt`（**6** 个包：pytest / pytest-cov / ruff / mypy / pyinstaller / pip-audit，v2.19.3 新增） |
| PyInstaller | 随 `requirements-dev.txt` 安装（`==6.21.0` 钉死，勿随意升级） |
| NSIS | 3.x（`C:\Program Files (x86)\NSIS\makensis.exe`） |
| Chromium | ms-playwright 缓存（`python -m patchright install chromium`）—— 随包分发 |
| PO Token 组件 | bgutil server（含 `build/main.js`）+ `deno.exe` —— 随包分发，**需环境变量指定路径** |

**Python 版本口径（三处含义不同，不矛盾）**：

| 位置 | 值 | 含义 |
|---|---|---|
| `pyproject.toml` `requires-python` | `>=3.11` | **兼容下限**：声明可运行的最低版本 |
| `pyproject.toml` mypy `python_version` | `3.12` | **类型检查目标**：类型推断按该版本的语义做 |
| 本机实测 / CI | 3.14 / 3.12 | 开发机跑 3.14、CI runner 跑 3.12，**均通过全部测试** |

> 注：`pytest` / `ruff` / `mypy` / `pyinstaller` 此前不在任何清单里（换机无法复现测试线），
> v2.19.3 已补入 `requirements-dev.txt`。

### 2.1 PO Token 三件套准备（YouTube 下载必需）

```bash
# bgutil server（Node 实现）
#   git clone https://github.com/Brainicism/bgutil-ytdlp-pot-provider
#   cd server && npm install && npx tsc      → 产出 server/build/main.js
# deno.exe（JS 运行时，server 依赖）
#   winget install DenoLand.Deno  或  从 https://deno.land 下载
```

**组件定位（v2.19 起自动，无需手动指定路径）**：

```
优先级 1：环境变量 POT_SERVER_DIR / DENO_EXE（CI 或自定义位置时用）
优先级 2：%LOCALAPPDATA%\KianaVnextPlus\vendor\bgutil-pot\server 与 vendor\deno.exe
          ← 本机标准位置；「一键构建.bat」会自动设置这两个环境变量指向此处
```

> v2.19 已把组件从 `%TEMP%\bgutil-pot` 迁至上述 vendor 目录（TEMP 会被系统清理，
> 曾导致打包静默丢组件），并让 spec 自动定位 + 缺件时**直接报错中止**。
> 自用测试构建若确要跳过：设 `KIANA_SKIP_POT=1`。

---

## 三、打包步骤

### 3.1 第一步：PyInstaller 双包

**必须带环境变量**（否则 PO Token 组件静默不入包 → 出包后 YouTube 下载失效）：

```bash
cd <工程根目录>

# [v2.19.8 文档修正] 旧版此处指向 %TEMP%\bgutil-pot 与 WinGet 的 deno 路径——那两处
# 早已停用（TEMP 会被系统清理，曾导致打包静默丢组件）。v2.19 起组件统一在 vendor 目录，
# 且 spec 会自动定位；不设这两个变量通常也能过。要显式指定就写：
export POT_SERVER_DIR="$LOCALAPPDATA/KianaVnextPlus/vendor/bgutil-pot/server"
export DENO_EXE="$LOCALAPPDATA/KianaVnextPlus/vendor/deno.exe"

python -m PyInstaller --noconfirm --clean KianaLauncher.spec   # GUI
python -m PyInstaller --noconfirm --clean KianaCrawler.spec    # CLI
```

> 界面版打包约 5-10 分钟/个（Chromium 体积大）。可用「一键构建.bat」替代（内含依赖检查+语法检查+pytest+双包+NSIS）。

### 3.2 第二步：验证产物（**不可跳过**）

```bash
# PO Token 三件套必须在包内
find dist/KianaLauncher/_internal/pot_server -name "deno.exe"
find dist/KianaLauncher/_internal/pot_server -name "main.js" -path "*build*"
# Chromium 必须在包内
find dist/KianaLauncher/_internal/browsers -name chrome.exe
# 双 exe 存在
ls dist/KianaLauncher/KianaLauncher.exe dist/KianaCrawler/KianaCrawler.exe
```

**任一为空 = 构建失败，必须排查后重打**（最常见原因：漏设 `POT_SERVER_DIR` / `DENO_EXE`）。

### 3.3 第三步：NSIS 汇编

```bash
# 注意：必须用 python subprocess 调用（Git Bash 会把 /S 等参数改写成路径）
python -c "import subprocess; subprocess.run([r'C:\Program Files (x86)\NSIS\makensis.exe','kiana_setup.nsi'], cwd=r'<工程根目录>')"
```

产出：`KianaVnextPlus-Setup-<VERSION>.exe`（约 540 MB）

---

## 四、NSIS 脚本要点（kiana_setup.nsi）

| 要点 | 说明 |
|---|---|
| `File /r` 双 onedir | `File /r "${DIST_DIR}\KianaLauncher\*.*"` + `File /r "${DIST_DIR}\KianaCrawler\*.*"`——**必须递归整目录**，因为这是 onedir 构建（不是单 exe） |
| **升级前清 `_internal`** | 安装 Section 开头 `RMDir /r "$INSTDIR\_internal"`——两个 onedir 合并共享 `_internal`，不清理会导致新旧 DLL 混跑 |
| 卸载 Section 必须带 `un.` | `Section "un.KianaVnextPlus"`——**漏了 `un.` 前缀会变成普通 Section 并在安装时执行**（曾导致装完即被 `RMDir /r` 全删，v2.17.2 修复） |
| 向导页 | MUI2：欢迎→目录→安装→完成；卸载：确认→卸载 |
| 字体清晰化 | `ManifestDPIAware true` + 按语言 `SetFont`（中文 SimSun 10pt / 日文 MS PGothic / 韩文 Gulim / 西文 Segoe UI） |
| 安装器美术 | `tools/gen_installer_art.py` 生成 `assets/installer/{header,welcome,unwelcome}.bmp`（版本徽章自动读 VERSION.json） |
| 卸载自清理 | 卸载 Section 末尾 `Exec cmd /C ping 127.0.0.1 -n 3 & rmdir /S /Q "$INSTDIR"`——卸载器运行中删不掉自身，靠 cmd 脱手扫尾（实测零残留） |

---

## 五、发版流程（完整清单）

```bash
# ① 版本四件套同步（必须一致，门禁强制校验）
#    kiana_setup.nsi      : !define APP_VERSION "X.Y.Z" + !define VERSION "X.Y.Z.0"
#    pyproject.toml       : version = "X.Y.Z"
#    kiana_vnext_plus/__init__.py : __version__ = "X.Y.Z"
#    VERSION.json         : latest / released / notes
python tools/gen_installer_art.py             # ② 重生成安装器美术（读 VERSION.json）

# ③ 门禁（14 项：版本一致性/Git卫生/全量测试/静态检查/依赖审计/密钥扫描/凭据卫生/规则资产/verify_all/构建面/结构指纹/静默失败扫描/脱敏链路/同一能力多份实现）
python tools/release_check.py                 # 必须 14/14 PASS

# ④ 打包（见第三节：双包 + 产物验证 + NSIS）

# ⑤ 装/卸实测（/S 仅内部验证手段，交付产品是完整向导）
#    [v2.19.8 文档修正] 卸载器的行为在 v2.19.5 已改为**三分支条件逻辑**，不再是"无条件删除"：
#      ① 本安装不是登记安装（$INSTDIR != InstallDir）→ 什么都不动；
#      ② 是登记安装且有可恢复的 PrevInstallDir → 恢复原安装（快捷方式+注册表+版本号）；
#      ③ 是登记安装且无可恢复 → 彻底清理（快捷方式 + 注册表全删）。
#    但"别用正式安装的 Uninstall.exe 去卸测试副本"这条**依然成立**（它本身就是登记安装，
#    会走分支③，把正式安装的共享资源删掉）。
#    **注意**：另一半同样要小心（2026-09-18 实测事故）：**测试副本自己的 Uninstall.exe 也会改写
#       正式安装的快捷方式**——它在分支②恢复时，CreateShortCut 的"起始位置"取的是本副本的
#       临时 $OUTDIR，于是恢复出来的桌面/开始菜单快捷方式 WorkDir 指向随后被删的临时目录。
#       该缺陷已在 kiana_setup.nsi 修复（恢复分支显式 SetOutPath "$1"）；但用**旧版安装包**
#       做这类测试仍会留下这个痕迹，测完请核对两个快捷方式的"起始位置"。
#    安全做法：
#         · 备份 %USERPROFILE%\Desktop\Kiana.lnk 与开始菜单目录 → 验证 → 还原；
#         · 或整个装卸验证放虚拟机/测试账户里做；
#         · 只想看会执行什么命令：`python tools/installer_matrix.py --dry`
#           （[v2.19.8] 该开关此前是摆设、传了仍真装真卸，现已真正短路）。
#    背景（自包含记录，原详述文档不在公开快照内）：2026-09-06 的「测试副本卸载器破坏正式安装」
#    与 2026-09-18 的「测试副本恢复快捷方式时工作目录指向临时目录」两次实测事故，
#    修复均已落在 kiana_setup.nsi（三分支卸载逻辑 + 恢复分支显式 SetOutPath）。
#    装 → 验证条目数/PO Token/Chromium → CLI 冒烟 → 卸 → 确认零残留 → 还原快捷方式

# ⑥ 提交与打标
git add -A && git commit -m "..."
git tag vX.Y.Z-final
```

---

## 六、常见坑与对策

| # | 坑 | 对策 |
|---|---|---|
| 1 | **漏设 `POT_SERVER_DIR`/`DENO_EXE`** → PO Token 静默不入包（无任何报错） | 打包后**强制** `find .../pot_server -name deno.exe` 验证 |
| 2 | Git Bash 调 `makensis` → `/S` 参数被 MSYS2 改写成 `S:\` 路径 | 一律用 python subprocess 调用 |
| 3 | **卸载 Section 漏 `un.` 前缀** → 安装时执行卸载逻辑，装完即被删光 | 检查 `Section "un.XXX"` 前缀；用静默装到 TEMP 验证目录非空 |
| 4 | 双 onedir 合并 `_internal` 时同名 DLL 冲突 | 安装 Section 先 `RMDir /r "$INSTDIR\_internal"`；当前两个包同名文件逐字节一致（实测 0 冲突） |
| 5 | PyInstaller 升级后依赖版本变动 → 旧 `_internal` 残留混跑 | 同上，升级前清理 |
| 6 | NSIS 内存映射失败（单个 onefile > ~600MB） | 本项目用 **onedir** 而非 onefile，规避此限制 |
| 7 | 打包漏 `qfluentwidgets` 资源 → GUI 白屏 | spec 已含 `collect_data_files('qfluentwidgets')`（`KianaLauncher.spec`，勿删） |
| 8 | NSIS 脚本必须 UTF-8 BOM | 编辑时保持 BOM，否则中文文案乱码 |
| 9 | 卸载后残留 `Uninstall.exe` | 已有 cmd 自清理收尾（勿删该段） |

---

## 七、构建清理

打包中间产物可随时删除（已 gitignore）：

```bash
rm -rf build/ dist/            # PyInstaller 产物（可重建）
rm -rf .mypy_cache/ .ruff_cache/ .pytest_cache/ **/__pycache__/
```

单个 `dist/` 约 3.5 GB（含 Chromium 与 PO Token 组件），`build/` 约 170 MB。不发布新版本时无需保留。
