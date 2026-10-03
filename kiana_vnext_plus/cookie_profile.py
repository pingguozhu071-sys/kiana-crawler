# -*- coding: utf-8 -*-
"""凭**持久化浏览器配置档**取登录态 —— 替代"手动导出 cookies.txt"那条循环。

## 这条路要解决的是什么

原来拿登录态是 5 步手工循环：
    浏览器登录 → 装扩展导出 cookies.txt → 拖进 GUI 指定路径 → 过几小时/几天失效 → 回到第 1 步
本模块把它压成 2 步：`打开专用浏览器 → 登录一次 → cookies 自动就位`。

## 三条**来自调研源码**的硬事实（不是本模块发明的，改动前先读）

1. **`storage_state()` 里的 localStorage 必须一起存。**
   B站的续期凭据 `refresh_token` 藏在 localStorage 的 `ac_time_value` 键里，
   **手动导出的 cookies.txt 里根本没有它**。将来若要接 B站续期接口，唯一来源就是这个值。
   ⇒ 本模块的落盘产物是**两件**：`cookies.txt`（给 yt-dlp 用）+ `storage_state.json`（留续期余地）。

2. **Chrome 136 起，`--remote-debugging-*` 配"默认用户数据目录"会被忽略**（防信息窃取）。
   这正是"专用配置档"能开工的原因 —— 而这套机制**只对非默认目录生效**。
   ⇒ **绝不要去连用户日常浏览器的配置档**：既连不上，又是在动机主的私人数据。

3. **反检测场景不要自定义 UA / headers。** 自造的 UA 与真实 TLS 指纹对不上，反而更容易被识别。
   ⇒ `launch_persistent_context` 只传 `user_data_dir` / `channel` / `headless`，**一个指纹参数都不加**。

## 为什么每站一个独立配置档（不能共用）

配置档目录里装的是 cookies / localStorage / IndexedDB / Session Storage —— 这些
**全都是按 context 隔离的**，多个站点塞进同一个目录就是**会话互相串**；
而且要"同时开两个站"时，同一个 `user_data_dir` 的第二个 Chrome 进程会直接
撞上 singleton 锁起不来。更实际的一条：串档之后 `storage_state.json` 会把
A 站的凭据和 B 站的凭据混在一份文件里，**泄漏面从"一个站"变成"全部站"**。

## 与既有代码的边界（重要）

* 写出 cookies.txt **只走** `cookie_utils.write_netscape_cookies` —— 7 列规格、
  CRLF、`#HttpOnly_` 前缀都由它保证（B站的 `SESSDATA` 正是 HttpOnly，
  少那个前缀会**静默丢掉登录态**，工程为此吃过 `code=-101`）。
* **刻意不 import `source_level_stealth` 的引擎级启动参数**：那是给无头爬取链用的
  （55 维覆写、注入脚本）。人为登录时要用**真实的 Chrome 指纹**；二者混用既违反上面第 3 条，
  也会让"登录态"与"爬取态"的特征对不上。
* 引擎（`solver_engine`）**依旧不用持久化上下文**（`source_level_stealth.py:656` 记录了
  "持久化会让用户数据落盘，与隐私第一冲突"的决定）。本模块是**另一个用途**：
  用户本人**主动、有头**地登录一次。**这个例外只属于本模块，别扩散到爬取链。**
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ══════════════════════════════════════════════════════════════════════
# patchright driver 子进程的编码问题与 solver_engine 同源：Windows 下子进程输出用 GBK
# 解码会抛 UnicodeDecodeError（0xb5），表现为 Event loop closed / 静默失败。
# **必须在导入 patchright 之前**设好（故放在类型 import 之后、patchright import 之前）。
# ══════════════════════════════════════════════════════════════════════
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PATCHRIGHT_SILENCE_MAJOR_ERRORS", "1")

logger = logging.getLogger(__name__)

# ── 引擎选择：优先 patchright（与 solver_engine 同款回退链），导入期不报错 ──
# 为什么要 try：本模块的**纯逻辑**（目录映射、storage_state 读写、cookies 转换）
# 必须能在没装浏览器引擎的机器上被 import（CI 里就是不装）。**把它做成模块级硬依赖
# 会让测试必须装 patchright** —— 那是把"能不能跑测试"和"能不能开浏览器"绑死。
#
# 类型先声明成 `Optional`（而不是靠 `except` 分支里的 `None` 去推断）：
# 不声明的话 mypy 会把变量定型成 `Callable[...]`，随后赋 `None` 就报
# "expression has type None, variable has type Callable" —— 那会算进本工程的
# mypy 锁死值里。声明写在这里，比事后加 `type: ignore` 更诚实。
_pr_async: Optional[Callable[[], Any]]
_pw_async: Optional[Callable[[], Any]]
try:
    from patchright.async_api import async_playwright as _pr_async
    HAS_PATCHRIGHT = True
except ImportError:                       # pragma: no cover - 取决于环境
    _pr_async = None
    HAS_PATCHRIGHT = False

try:
    from playwright.async_api import async_playwright as _pw_async
    HAS_PLAYWRIGHT = True
except ImportError:                       # pragma: no cover - 取决于环境
    _pw_async = None
    HAS_PLAYWRIGHT = False


# ══════════════════════════════════════════════════════════════════════
# 站点登记表 —— 站点标识的**唯一来源**
#
# 键（site id）刻意取了**可注册域**（`bilibili.com` 而不是 `bilibili`）：它与
# `cookie_armory.normalize_site()` 的输出同族，将来"按站身份池"要按站查东西时，
# 两边不会各算一套键（`cookie_armory:130` 记录过这个坑：存的是 `bilibili.com`、
# 查的是 `www.bilibili.com` ⇒ 库里躺着身份却永远查不到）。
#
# 用户输入**不受此限制**：`--site bilibili` / `bilibili.com` / `www.bilibili.com`
# 都归到同一个键（见 `resolve_site()`），归一实现复用 `cookie_armory.normalize_site`。
#
# `login_cookies` 的语义要看清：**用来"提前判断登录成了没有"，不是导出闸门**。
# 导出永远以"用户把窗口关掉"为准 —— 所以某个站的 cookie 名没列准，
# 后果只是"不能提前收工"，**不会**把登录态判丢。列表为空 = 该站不做提前判断。
# 这正是不确定时的**保守选项**：不猜。
# ══════════════════════════════════════════════════════════════════════
SITE_PROFILES: Dict[str, Dict[str, Any]] = {
    "bilibili.com": {
        "site_key": "bilibili",
        "label": "B站",
        "domains": ("bilibili.com", "b23.tv"),
        "login_url": "https://passport.bilibili.com/login",
        # SESSDATA 是 HttpOnly，且**只有它能判登录**（DedeUserID/bili_jct 在未登录时也可能有）
        "login_cookies": ("SESSDATA",),
    },
    "douyin.com": {
        "site_key": "douyin",
        "label": "抖音",
        "domains": ("douyin.com", "iesdouyin.com"),
        "login_url": "https://www.douyin.com/",
        # [不确定] 抖音的登录 cookie 名会随灰度变化，列了三个候选；
        # 列不准的代价仅为"不能提前收工"，见上方语义说明。
        "login_cookies": ("sessionid", "sessionid_ss", "sid_guard"),
    },
    "tieba.baidu.com": {
        "site_key": "tieba",
        "label": "贴吧",
        "domains": ("tieba.baidu.com",),
        "login_url": "https://tieba.baidu.com/",
        # BDUSS 是贴吧/百度系的登录凭据（HttpOnly）。STOKEN 单列不保险。
        "login_cookies": ("BDUSS",),
    },
    "kuaishou.com": {
        "site_key": "kuaishou",
        "label": "快手",
        "domains": ("kuaishou.com", "gifshow.com"),
        "login_url": "https://www.kuaishou.com/",
        # [不确定] 快手登录 cookie 名未在源码/工程内求证到，留空 = 只靠"关窗口"判定。
        "login_cookies": (),
    },
    "xiaohongshu.com": {
        "site_key": "xiaohongshu",
        "label": "小红书",
        "domains": ("xiaohongshu.com", "xhslink.com"),
        "login_url": "https://www.xiaohongshu.com/",
        "login_cookies": ("web_session",),
    },
    "mp.weixin.qq.com": {
        "site_key": "wechat_mp",
        "label": "公众号（微信公众平台）",
        # 公众号正文页（mp.weixin.qq.com/s/...）**不需要登录态**；
        # 需要登录的是**公众平台后台**。两者同域，靠 login_url 区分用途。
        "domains": ("mp.weixin.qq.com",),
        "login_url": "https://mp.weixin.qq.com/",
        # [不确定] 后台登录 cookie 名未求证到 → 留空，只靠"关窗口"判定。
        "login_cookies": (),
    },
}


def profiles_root(root: Optional[os.PathLike] = None) -> Path:
    """全部站点配置档的父目录：`<运行期数据根>/profiles/`。

    为什么放**运行期数据根**（`config.data_root()`）而不放仓库里：
    配置档目录 = **一份完整可复用的登录凭据**（cookies + localStorage + IndexedDB，
    全在本机明文），它绝不能被误 commit、误打包、误进安装器。
    `data_root()` 已经是 `%LOCALAPPDATA%\\KianaVnextPlus`（便携模式则是 exe 同级
    `KianaData/`），**仓库外 + 随用户走**，正是凭据该待的地方。

    `root` 参数只为测试注入用（不传就取数据根）。
    """
    if root is not None:
        return Path(root)
    from .config import data_root
    return Path(data_root()) / "profiles"


def normalize_site(site: str) -> str:
    """站点标识归一 —— **唯一实现是 `cookie_armory.normalize_site`，本函数只转发。**

    [v6 修正·**门禁抓到的真问题**] 我第一版写成"薄包装 + 依赖缺失时回退"，
    回退分支里**复制了一遍归一逻辑**。门禁第 14 项（"同一能力多份实现"）当场报：
        `normalize_site: ['cookie_armory.py:130', 'cookie_profile.py:176']`
    **那正是本工程最稳定的缺陷模式** —— 同一个"站点键"概念两处各算一遍，
    结果就是"存进去查不到"（`cookie_armory` 那份 docstring 里记着这桩血案）。

    **并且我当时的理由（"不想加载 cookie_armory 的重依赖"）实测不成立**：
        `import cookie_armory` → **115 ms**，只带 `sqlite3`（**标准库**），
        `cryptography` 根本没被载入（它是函数内惰性导入的）。
    ⇒ 回退分支是无谓的复杂度，**且它的存在本身就是漂移的种子**：
      两份实现只要有一处改了（比如多剥一层子域前缀），就会**静默分叉**。
    ⇒ 直接转发。**导入失败就让错误冒出来** —— 那说明包本身坏了，
      不该用一个"略有不同的第二套归一"把它糊过去。

    形参名也与原件对齐（`site`），免得门禁再判"形参不一致"。
    """
    from .cookie_armory import normalize_site as _norm
    return _norm(site)


def resolve_site(raw: str) -> str:
    """用户给的任意站点写法 → 登记表里的**规范键**。

    匹配顺序**不能改**（先精确、后宽泛；同为宽泛时取**最长**命中）：
      1. 直接是登记表的键（`bilibili.com`）；
      2. 归一后等于某个键（`www.BILIBILI.com` → `bilibili.com`）；
      3. **短名 `site_key`**（`bilibili` → `bilibili.com`）—— 命令行主用法；
      4. 命中某个站的 `domains` 或 `site_key` 的**域后缀**，取最长命中
         （`v.douyin.com` → `douyin.com`）。

    ⚠️ 第 3 条**不可省**：短名是用户敲得最多的写法，只认域名的第一版实现里
    `--site bilibili` 直接报"未知站点"——而那正是本功能对外承诺的用法。
    第 4 条**必须比第 3 条宽**且**取最长**：`tieba.baidu.com` 也以 `baidu.com` 结尾，
    宽泛匹配排前面/取最短就会把它错认成别的站。

    都不中 → `ValueError`，并且**把可用站点列给用户**。
    宁可不做：拿一个拼错的站名去建目录、写 cookies，会产生"看着成功、其实错位"的凭据
    —— 比直接报错难查得多。
    """
    s = (raw or "").strip().lower()
    if not s:
        raise ValueError("站点标识为空")
    if s in SITE_PROFILES:
        return s

    # 先剥协议/路径，否则 `https://www.bilibili.com/x` 这种输入永远匹配不上
    n = normalize_site(s)
    if n in SITE_PROFILES:
        return n

    # 短名（site_key）精确匹配 —— `--site bilibili`
    for key, meta in SITE_PROFILES.items():
        if n == meta["site_key"]:
            return key

    # 域后缀匹配：`domains` 与 `site_key` 合起来当候选，取**最长**者
    best: Optional[str] = None
    best_len = -1
    for key, meta in SITE_PROFILES.items():
        for d in tuple(meta["domains"]) + (str(meta["site_key"]),):
            if n == d or n.endswith("." + d):
                if len(d) > best_len:
                    best, best_len = key, len(d)
    if best:
        return best

    raise ValueError(
        f"未知站点 {raw!r}。可用站点：{', '.join(sorted(m['site_key'] for m in SITE_PROFILES.values()))}"
        "（也可直接用域名，如 www.bilibili.com）")


def site_options() -> List[Dict[str, Any]]:
    """登记表 → 便于打印的列表（GUI/CLI 共用的只读视图）。"""
    return [dict(meta, key=k) for k, meta in sorted(SITE_PROFILES.items())]


def profile_dir(site: str, *, create: bool = False,
                root: Optional[os.PathLike] = None) -> Path:
    """站点 → 该站**专属**配置档目录 `<profiles>/<site_key>/`。

    目录名用 `site_key`（`bilibili`）而不是可注册域（`bilibili.com`）：
    它是**给人和命令行用的短名**（`--site bilibili`），也免掉目录名里带点
    在 Windows 上的各种歧义。规范化仍统一走 `resolve_site()`，不与键名脱钩。
    """
    key = resolve_site(site)
    d = profiles_root(root) / SITE_PROFILES[key]["site_key"]
    if create:
        # mode=0o700：配置档 = 登录凭据，别的本地账户不该读得到。
        # Windows 上 mode 基本被忽略（NTFS ACL 由用户目录继承），
        # 但显式写出来能让"这份数据是私密的"这个意图留在代码里。
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def storage_state_path(site: str, *, root: Optional[os.PathLike] = None) -> Path:
    """该站 `storage_state.json` 的路径（**不建目录**，只算路径）。

    为什么是 `storage_state.json` 而不是 `.json`：这文件是**本机密钥**
    （含 SESSDATA 与 localStorage 里的 `ac_time_value`），
    名字必须让任何一眼扫过去的人知道它是什么。
    `.gitignore` 由既有的 `*cookie*.txt` / `KianaData/` 覆盖——它无论如何都在仓库外。
    """
    return profile_dir(site, root=root) / "storage_state.json"


def write_json_private(path: Path, data: Any) -> None:
    """以"私有文件"语义写 JSON：先写临时文件再 `os.replace` 原子替换。

    为什么不用 `Path.write_text`：直接覆盖时，**写一半崩溃会留下半截 JSON**
    —— 下次读回来是坏的，而调用方只看到"没有登录态"，无从判断是没登录还是文件坏了。
    原子替换让"文件在"与"文件完整"成为同一件事。

    [Windows 实测注意] `os.replace` 是原子替换语义，不会出现 `os.rename` 那种
    "目标已存在就报错"。POSIX 上顺带把权限收到 `0o600`（只本人可读写）。
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:                        # pragma: no cover - 非 POSIX
        pass
    os.replace(tmp, path)


# ══════════════════════════════════════════════════════════════════════
# cookies 形态转换：patchright `ctx.cookies()` → `write_netscape_cookies()` 认的形状
#
# **唯一的字段改名是 `httpOnly` → `http_only`，而它恰恰是最要命的一处**：
# 改名漏了，B站的 `SESSDATA`（HttpOnly）就会写成不带 `#HttpOnly_` 前缀的普通行，
# 严格解析器按注释丢掉 → 文件"看着是好的"、服务端说未登录（`code=-101`）。
# 工程在 `cookie_utils.py:22-40` 记录过这个坑，别在这里再犯一次。
# ══════════════════════════════════════════════════════════════════════

def cookies_for_netscape(cookies: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """patchright 的 cookie 列表 → `write_netscape_cookies` 认的字典列表。

    三处**必须**做的处理：

    · `httpOnly` → `http_only`（见上，漏了会静默丢登录态）。
    · **跳过带 `partitionKey` 的 cookie**：那是 CHIPS（分区）cookie，语义是
      "只在某个顶层站点的分区里生效"。Netscape 7 列格式**没有地方表达分区**
      ⇒ 写出去会把它降级成普通 cookie，等于**把分区 cookie 泄漏给所有顶层请求**。
      丢掉它是保守且正确的：yt-dlp 的请求本来也不在那些分区里。
    · `expires` **在本层原样透传**（patchright 用 `-1` 表示会话 cookie）。
      本层不改它 —— 因为"会话期"这个语义在这里是真的，改了会丢信息。

      ⚠️ **但写盘那层会把它归一成 `0`，这是必须的**（`cookie_utils` 的
      `write_netscape_cookies`，v6 实测修复）。原因：
      yt-dlp 对 expires 的校验是 `[0-9]+` 加可选小数（正则里的点要转义），**`-1` 不合法**，
      它会
      ```
      WARNING: skipping cookie file entry due to invalid expires at -1
      ```
      **把整行跳过**。实测复现过。
      ⇒ **两层各司其职**：本层保真（`-1`），写盘层归一（`0`）。
        两处都写 `0` 是会话 cookie 的标准写法，语义不变、且不会被跳过。
      **别看到本层保留 `-1` 就把写盘层的归一也"顺手去掉"。**
    """
    out: List[Dict[str, Any]] = []
    partitioned = 0
    for c in (cookies or []):
        if c.get("partitionKey"):
            partitioned += 1
            continue
        out.append({
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure")),
            "http_only": bool(c.get("httpOnly")),
            "expires": c.get("expires"),
        })
    if partitioned:
        # 一句话说清"丢了几个、为什么丢"—— 静默丢弃正是本工程反复吃亏的模式
        logger.info("跳过 %d 个分区(partitioned) cookie：Netscape 7 列格式无法表达分区语义",
                    partitioned)
    return out


def state_cookies(state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`storage_state()` 的内容 → cookie 字典列表。

    `storage_state()` 返回的 cookies 结构与 `ctx.cookies()` **同形**
    （同样是 `httpOnly` 驼峰键），故共用同一份转换，不写第二遍。
    """
    if not state:
        return []
    return cookies_for_netscape(list(state.get("cookies") or []))


def save_storage_state(state: Dict[str, Any], site: str, *,
                       root: Optional[os.PathLike] = None) -> Optional[Path]:
    """落盘 `storage_state.json`（cookies + localStorage），返回路径。

    空 state（既没 cookie 也没 origin）**返回 `None` 且不写文件** ——
    写一个空壳文件会让 `--status` 把"从没登录过"报成"有配置档"，是假信号。

    **localStorage 为什么必须存**（本模块存在的理由之一）：
    B站的 `refresh_token` 在 localStorage 的 `ac_time_value` 键里，
    手动导出的 cookies.txt **拿不到它**。现在不接续期接口，但这个值必须先留住，
    否则将来接的时候得让用户重新登录一遍。
    """
    if not state or not (state.get("cookies") or state.get("origins")):
        logger.info("storage_state 为空（用户没有产生任何 cookie/本地存储）——不落盘")
        return None
    p = storage_state_path(site, root=root)
    write_json_private(p, state)
    logger.info("已保存 storage_state（cookies=%d, origins=%d）→ %s",
                len(state.get("cookies") or []), len(state.get("origins") or []), p)
    return p


def load_storage_state(site: str, *,
                       root: Optional[os.PathLike] = None) -> Optional[Dict[str, Any]]:
    """读取 `storage_state.json`。

    **坏文件返回 `None` 并告警，不抛异常**：文件损坏与"从没登录过"对调用方是
    同样可处理的局面（都得重新登录一次），但日志里必须说清是哪一种，
    否则用户会以为配置档还在。与 `cookie_armory.ReportResult` 的三态精神一致：
    "不知道"不要伪装成"没有"。
    """
    p = storage_state_path(site, root=root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("storage_state 读取失败（文件损坏？）%s: %s —— 视为无登录态", p, e)
        return None
    if not isinstance(data, dict):
        logger.warning("storage_state 结构异常（顶层不是对象）%s —— 视为无登录态", p)
        return None
    return data


def local_storage_of(state: Optional[Dict[str, Any]], origin: str) -> Dict[str, str]:
    """从 storage_state 里取某个 origin 的 localStorage（展平成 `{k: v}`）。

    给"B站 `ac_time_value` 在哪"这类问题一个**可测的**取用点：
    调用方不必自己遍历 `origins[].localStorage[]`。
    """
    out: Dict[str, str] = {}
    for o in (state or {}).get("origins") or []:
        if str(o.get("origin") or "").rstrip("/") != str(origin or "").rstrip("/"):
            continue
        for kv in o.get("localStorage") or []:
            name = kv.get("name")
            if name is not None:
                out[str(name)] = str(kv.get("value") or "")
    return out


def export_cookies_from_state(state: Optional[Dict[str, Any]], site: str,
                              out: Optional[os.PathLike] = None) -> Optional[Path]:
    """从 `storage_state` 导出 Netscape cookies.txt（给 yt-dlp / 引擎链用）。

    写出**只走** `cookie_utils.write_netscape_cookies`（7 列 / CRLF / `#HttpOnly_`
    前缀都由它保证），本模块不自己拼一行格式。

    没有 cookie → 返回 `None` 且**不写空文件**：一个只有 3 行注释头的 cookies.txt
    会被 `_ensure_cookie_file()` 当成"配好了但啥都没有"，比"文件不存在"更难查
    （`universal_downloader.py:167-179` 记过同类误导）。
    """
    rows = state_cookies(state)
    if not rows:
        return None
    kw: Dict[str, Any] = {}
    if out is not None:
        kw["path"] = str(out)
    from .cookie_utils import write_netscape_cookies
    # 不接返回值：**要文本的人传 `out=None`**（那时本函数返回 None，文本由调用方
    # 自己走 `write_netscape_cookies` 拿）。在这里接一个永不返回的 text 只会误导。
    write_netscape_cookies(rows, **kw)
    if out is not None:
        logger.info("已导出 cookies.txt（%d 条）→ %s", len(rows), out)
    else:
        logger.info("已生成 cookies.txt 文本（%d 条，未落盘）", len(rows))
    return Path(out) if out is not None else None


def export_profile_cookies(site: str, *, out: Optional[os.PathLike] = None,
                           root: Optional[os.PathLike] = None) -> Optional[Path]:
    """已存配置档 → cookies.txt（**不开浏览器**）。

    与 `export_cookies_from_state` 的分工：这个是"从磁盘上的配置档导出"的入口，
    供 `--export-only` 这类事后重导用（比如用户手动动过 cookies.txt，想恢复一份）。
    """
    state = load_storage_state(site, root=root)
    if state is None:
        return None
    if out is None:
        out = profile_dir(site, root=root) / "cookies.txt"
    return export_cookies_from_state(state, site, out=out)


def merge_cookie_files(paths: Sequence[os.PathLike],
                       out: Optional[os.PathLike] = None) -> Tuple[str, int]:
    """把多份 cookies.txt 合成**一份**（返回 `(文本, 条数)`；给了 `out` 就落盘）。

    为什么需要它：yt-dlp / `_ensure_cookie_file()` 最终只要**一个**文件，
    而"每站一个配置档"必然产出多份。合成规则与
    `universal_downloader._ensure_cookie_file` 的既有约定一致：
    **按 `(domain, name)` 去重，后面的文件覆盖前面的**。

    **刻意 `return text` 而不是只认落盘**：合并逻辑本身必须能离线测
    （不需要真 cookie 文件，也不需要浏览器）。
    """
    from .cookie_utils import parse_netscape_cookies, write_netscape_cookies
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for p in (paths or []):
        try:
            text = Path(p).read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            logger.warning("合并时跳过读不到的文件：%s", p)
            continue
        for c in parse_netscape_cookies(text):
            # 解析端给的键是 httponly（全小写），写出端要 http_only —— 显式对齐，
            # 免得 HttpOnly 的 SESSDATA 在"合并"这一步被摘掉前缀。
            merged[(str(c["domain"]), str(c["name"]))] = {
                "name": c["name"], "value": c["value"], "domain": c["domain"],
                "path": c["path"], "secure": str(c["secure"]).upper() == "TRUE",
                "http_only": bool(c.get("httponly")), "expires": c["expires"],
            }
    rows = list(merged.values())
    kw: Dict[str, Any] = {}
    if out is not None:
        kw["path"] = str(out)
    text = write_netscape_cookies(rows, **kw)
    return text, len(rows)


# ══════════════════════════════════════════════════════════════════════
# 有头登录（本模块唯一会开浏览器的部分）
# ══════════════════════════════════════════════════════════════════════

def _browser_factory():
    """(async_playwright 工厂, 名字)；都不可用 → `None`。

    与 `solver_engine._launch_browser` 同款回退顺序（patchright 优先）。
    **这里不做 patchright 注入通道自证**：那条自证是为"注入隐身脚本"服务的，
    而本模块**刻意不注入任何脚本**（见文件头第 3 条）—— 自证一份用不到的能力
    只会多一次浏览器启动、多一堆日志。
    """
    if HAS_PATCHRIGHT:
        return _pr_async, "patchright"
    if HAS_PLAYWRIGHT:
        return _pw_async, "playwright"
    return None


def _detect_login_cookie(cookies: Sequence[Dict[str, Any]], site: str) -> bool:
    """cookie 列表里是否已出现"该站的登录凭据"。

    **只用于"提前收工"，绝不用于判定导出成败**（导出永远以用户关窗口为准）——
    所以某个站 cookie 名没列准的后果只是"不能提前收工"，不会误判登录态。
    """
    meta = SITE_PROFILES.get(resolve_site(site)) or {}
    names = {str(n) for n in meta.get("login_cookies") or ()}
    if not names:
        return False
    domains = tuple(str(d) for d in meta.get("domains") or ())
    for c in (cookies or []):
        if str(c.get("name") or "") not in names:
            continue
        dom = str(c.get("domain") or "").lstrip(".").lower()
        # 域名必须沾边：同名 cookie 出现在无关域上（`name` 撞名）不算登录成功
        if any(dom == d or dom.endswith("." + d) for d in domains):
            return True
    return False


def _context_alive(ctx: Any) -> bool:
    """浏览器窗口是否还开着（用户关掉 → False）。"""
    try:
        return bool(ctx.pages)
    except Exception:
        # 上下文已销毁时访问 `pages` 会抛 —— 那就是"关了"
        return False


async def open_profile_for_login(site: str, *,
                                 url: Optional[str] = None,
                                 timeout_s: Optional[float] = None,
                                 wait_for_login: bool = False,
                                 out: Optional[os.PathLike] = None,
                                 root: Optional[os.PathLike] = None) -> Dict[str, Any]:
    """打开**该站专属**配置档的浏览器窗口，等用户登录，然后导出 cookies。

    参数：
        url            覆盖默认登录页（默认取登记表的 `login_url`）
        timeout_s      最长等多久（`None` = 一直等，直到用户关窗口）
        wait_for_login 登录凭据一出现就**自动收工**并关窗口；默认 `False`，
                       即"以用户关窗口为准"。默认值这么定是因为
                       "已经登录过、只是想再导一次"的人不该被关掉浏览器，
                       而"登录成了"这个判断依赖各站 cookie 名（未必列得准）。
        out            cookies.txt 的落盘路径（默认 `<profile>/cookies.txt`）
        root           配置档根目录（测试注入用）

    返回 dict（**永不抛业务异常**，失败以 `status` 表达，与工程既有风格一致）：
        ok / status / site / key / profile_dir / cookies_path / state_path
        / cookie_count / origins / login_detected / note
      `status` 取值：`ok`（拿到 cookie）/ `no_login`（跑完了但一条 cookie 都没有）
        / `no_browser`（没装 patchright/playwright）
        / `bad_url`（登录页不是 https，**拒绝导航** —— 与 `no_browser` 分开，
          免得用户去装一个根本不需要装的东西）

    **有头是硬编码的**：`launch_persistent_context(headless=False)` 写死，
    连参数都不给 —— 扫码登录必须有窗口，"无头登录"这件事不存在。
    """
    key = resolve_site(site)
    meta = SITE_PROFILES[key]
    pdir = profile_dir(key, create=True, root=root)
    state_path = pdir / "storage_state.json"
    cookies_path = Path(out) if out is not None else pdir / "cookies.txt"
    target = url or str(meta["login_url"])
    # URL 闸：本模块会把它交给浏览器导航，**只认 https**（本机回环除外）。
    # 这类"用户可传任意 URL 给浏览器"的入口不给闸，等于开了一个无日志的
    # SSRF/钓鱼跳板；白名单写法与 url_utils 同精神，但不复用它的私网判定
    # （那条是针对爬取目标设计的，这里只需要挡住明文与非 http(s) 协议）。
    if not (target.startswith("https://")
            or target.startswith("http://127.0.0.1")
            or target.startswith("http://localhost")):
        return {
            # 独立的 status 而不是复用 no_browser：**"拒绝导航"与"没装引擎"
            # 是两回事**，混成一个值会让用户去装一个根本不需要装的东西。
            "ok": False, "status": "bad_url",
            "site": key, "key": meta["site_key"], "profile_dir": str(pdir),
            "cookies_path": None, "state_path": str(state_path),
            "cookie_count": 0, "origins": 0, "login_detected": False,
            "note": f"拒绝导航到非 https 地址：{target!r}",
        }

    result: Dict[str, Any] = {
        "ok": False, "status": "no_browser", "site": key, "key": meta["site_key"],
        "profile_dir": str(pdir), "cookies_path": None, "state_path": str(state_path),
        "cookie_count": 0, "origins": 0, "login_detected": False, "note": "",
    }

    factory = _browser_factory()
    if factory is None:
        result["note"] = ("没装浏览器引擎：patchright / playwright 都 import 不到。"
                          "装一个再试（本工程栈里应已有 patchright）。")
        return result
    pw_factory, engine = factory

    logger.info("[%s] 打开配置档：%s（引擎=%s，有头）", meta["label"], pdir, engine)

    async with pw_factory() as p:
        # ── 只传真需要的三个参数 ────────────────────────────────────────
        # user_data_dir：专用配置档（**不是** <user_data_dir> 以外的任何既有 Chrome 目录）
        # channel="chrome"：用系统 Chrome 而非随包 chromium —— 反检测场景下
        #   真 Chrome 的指纹比"随包 chromium"更常见、更不显眼。
        # headless=False：登录必须有头（写死，不给参数）。
        # **一个 UA / header / viewport 都不加**：自定义 UA 与 TLS 指纹会对不上，
        # 反而把自己标注出来（文件头第 3 条）。
        ctx = await p.chromium.launch_persistent_context(
            str(pdir),
            channel="chrome",
            headless=False,
        )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(target, wait_until="domcontentloaded", timeout=60_000)

            deadline = (time.monotonic() + float(timeout_s)) if timeout_s else None
            poll = 0.5
            # ══════════════════════════════════════════════════════════════
            # [v6 修复·**真机踩到的致命 bug**] **必须边轮询边存快照。**
            #
            # 原来的写法是：循环里只判"活着没有"，**等跳出循环之后**才读一次
            #     cookies = await ctx.cookies()
            # 而循环**唯一的跳出条件就是"用户关掉了窗口"**（那是设计里说的权威信号）
            # ⇒ 那一刻 context **已经死了**，`ctx.cookies()` 抛异常 →
            #   被下面的 `except` 吞成 `cookies = []` ⇒ 文件不写 ⇒ 界面报"没获取到"。
            #
            # **真机实测**（机主 2026-10-03）：扫码登录成功、主页也进去了、
            # 按提示关了窗口 → 界面说"没有正常获取到"，
            # 而配置档目录里 **`cookies.txt` 与 `storage_state.json` 都不存在** ⇒
            # **不是他没登录，是我们在关窗之后才去读，读了个空。**
            #
            # 修法：**每一轮都刷一份快照**，关窗后用最后那份。
            # 这样"用户关窗口"这个信号依然权威，但**不再要求 context 还活着**。
            # ══════════════════════════════════════════════════════════════
            last_cookies: list = []
            last_state: Dict[str, Any] = {}
            while True:
                # ① 先刷快照（**在判活之前**：这一轮 context 还活着就读得到）
                if _context_alive(ctx):
                    try:
                        last_cookies = list(await ctx.cookies())
                        last_state = dict(await ctx.storage_state())
                    except Exception as e:
                        logger.debug("配置档快照读取失败（继续轮询）: %s", e)
                else:
                    break                       # 用户关掉了窗口 —— 唯一的权威信号
                if wait_for_login:
                    try:
                        if _detect_login_cookie(last_cookies, key):
                            result["login_detected"] = True
                            break
                    except Exception:
                        break                   # 浏览器中途死了 → 别再 poll 下去
                if deadline is not None and time.monotonic() >= deadline:
                    result["note"] = f"等待超时（{timeout_s}s）——按当前已拿到的 cookie 导出"
                    break
                await asyncio.sleep(poll)

            # ── 导出：用**轮询期间攒下的最后一份快照**（见上面的说明）──
            cookies, state = last_cookies, last_state
            if not cookies or not state:
                # 快照一直没取到（比如窗口开没几秒就被关）→ 最后再试一次活的 context，
                # 拿得到就拿，拿不到就如实报 no_login（**不编**）。
                try:
                    cookies = list(await ctx.cookies())
                except Exception:
                    cookies = cookies or []
                try:
                    state = dict(await ctx.storage_state())
                except Exception:
                    state = state or {}
        finally:
            # 无论上面怎么退出，都必须关 —— 挂着的 Chrome 进程会锁住
            # user_data_dir，下次启动直接 singleton 报错。
            try:
                await ctx.close()
            except Exception:
                pass

    result["cookie_count"] = len(cookies)
    result["origins"] = len(state.get("origins") or [])
    saved = save_storage_state(state, key, root=root)
    if saved is not None:
        result["state_path"] = str(saved)
    exported = export_cookies_from_state(state, key, out=cookies_path) if cookies else None
    if exported is not None:
        result["cookies_path"] = str(exported)
        result["ok"] = True
        result["status"] = "ok"
    elif not result["note"]:
        result["status"] = "no_login"
        result["note"] = "没有任何 cookie —— 9 成是没登录成功（或登录后没等页面跳转就关了窗口）"
    else:
        result["status"] = "no_login"
    return result
