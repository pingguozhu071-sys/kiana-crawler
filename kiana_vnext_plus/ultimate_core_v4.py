"""Kiana 池化/抽取件（自研实现；V1→V4 收敛，本版仅保留在用的三类）

- AutoscaledPool：自缩放批处理池（按负载调并发——业界常见模式的自研实现，
  min/max 与回收语义见类内注释）
- FullSourceExtractor：全源链接抽取（a/img/iframe/注释等多源启发式，与
  link_scoring 过滤链配合）

[v2.19 清理] 原 `DomainManager` 类已删除：其 `self._states:dict[str,DomainState]`
引用了**全仓未定义**的 `DomainState`（ruff F821）——一旦被调用即 NameError；
且该类外部零引用（域状态管理职责实际由 frontier/defense_protocols 承担）。

外部同功能框架仅作"业界模式"对照；文件内实现与接口均为 Kiana 定义。
来源与合规声明见 docs/来源与合规声明.md。"""
import asyncio
import time
import logging
import re
from urllib.parse import urlparse
logger=logging.getLogger(__name__)

class AutoscaledPool:
    """Multi-factor adaptive: CPU + Memory + EventLoop latency + Error rate"""
    def __init__(self, min_con: int = 5, max_con: int = 300):
        self.concurrency=min_con;self.min=min_con;self.max=max_con
        self._stats={'ok':0,'err':0,'latency':[],'cpu':[],'mem':[]}
        self._running=False
        self._apply=None  # [FIXED & MODIFIED] v2.10.5 P1-7 写回全局信号量的回调

    def set_apply(self, fn):
        """[FIXED & MODIFIED] v2.10.5 P1-7 注册调优结果写回回调（外部提供 async adjust_global 包装）"""
        self._apply = fn

    async def run(self, interval: float = 2.0):
        self._running=True
        while self._running:
            await asyncio.sleep(interval)
            self._tune()

    def _tune(self, apply_con: callable = None):
        try:
            import psutil
            # [FIXED & MODIFIED] v2.14 非阻塞采样（原 interval=0.1 同步睡 100ms 冻结 loop）
            cpu=psutil.cpu_percent(interval=None)
            mem=psutil.virtual_memory().percent
            self._stats['cpu'].append(cpu)
            self._stats['mem'].append(mem)
        except Exception:pass

        total=self._stats['ok']+self._stats['err']
        err_rate=self._stats['err']/total if total>0 else 0
        latencies=self._stats['latency'][-50:]
        avg_lat=sum(latencies)/len(latencies) if latencies else 0
        cpu_avg=sum(self._stats['cpu'][-10:])/max(1,len(self._stats['cpu'][-10:]))
        mem_avg=sum(self._stats['mem'][-10:])/max(1,len(self._stats['mem'][-10:]))

        if mem_avg>97 or err_rate>0.5:
            self.concurrency=max(self.min,int(self.concurrency*0.5))
        elif mem_avg>92 or err_rate>0.2 or avg_lat>5:
            self.concurrency=max(self.min,int(self.concurrency*0.8))
        elif mem_avg<85 and err_rate<0.05 and avg_lat<2:
            self.concurrency=min(self.max,int(self.concurrency*1.1))

        self._stats['ok']=max(0,self._stats['ok']-20)
        self._stats['err']=max(0,self._stats['err']-10)
        self._stats['latency']=self._stats['latency'][-100:]
        self._stats['cpu']=self._stats['cpu'][-30:]
        self._stats['mem']=self._stats['mem'][-30:]
        # [FIXED & MODIFIED] v2.10.5 P1-7 接线：调优结果写回全局信号量（此前 self.concurrency
        # 只改自身 → 幻影池，调优从未生效）
        _apply = apply_con or getattr(self, '_apply', None)
        if _apply:
            try:
                _apply(self.concurrency)
            except Exception:
                pass

    def record(self, ok: bool, latency: float = 0):
        if ok:self._stats['ok']+=1
        else:self._stats['err']+=1
        if latency>0:self._stats['latency'].append(latency)

    def stop(self):self._running=False

# ═══ 全源链接抽取器（Kiana 自研多源启发式）═══
class FullSourceExtractor:
    """Extract links from ALL sources: a/img/script/iframe/form/meta/comments/js events"""

    @staticmethod
    def extract(html: str, base_url: str = "") -> list[str]:
        links=set()
        # a[href]
        links.update(re.findall(r'<a[^>]+href=["\']([^"\']+)["\']',html,re.IGNORECASE))
        # img[src]
        links.update(re.findall(r'<img[^>]+src=["\']([^"\']+)["\']',html,re.IGNORECASE))
        # script[src]
        links.update(re.findall(r'<script[^>]+src=["\']([^"\']+)["\']',html,re.IGNORECASE))
        # link[href] (CSS)
        links.update(re.findall(r'<link[^>]+href=["\']([^"\']+)["\']',html,re.IGNORECASE))
        # iframe[src]
        links.update(re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']',html,re.IGNORECASE))
        # form[action]
        links.update(re.findall(r'<form[^>]+action=["\']([^"\']+)["\']',html,re.IGNORECASE))
        # meta refresh
        links.update(re.findall(r'<meta[^>]+content=["\']\d+;\s*url=([^"\']+)["\']',html,re.IGNORECASE))
        # video/audio/source
        links.update(re.findall(r'<(?:video|audio|source)[^>]+src=["\']([^"\']+)["\']',html,re.IGNORECASE))
        # object[data] + embed[src]
        links.update(re.findall(r'<(?:object|embed)[^>]+(?:data|src)=["\']([^"\']+)["\']',html,re.IGNORECASE))
        # HTML comments
        links.update(re.findall(r'<!--(.*?)-->',html,re.DOTALL|re.IGNORECASE))
        # JS event handlers: onclick/onmouseover window.open / location.href
        js_urls=re.findall(r'(?:window\.location|location\.href|window\.open)\s*[=(]\s*["\']([^"\']+)["\']',html,re.IGNORECASE)
        links.update(js_urls)
        # JS strings: absolute URL patterns
        # [FIXED & MODIFIED] v2.10.4 字符白名单收紧：原 [^"']+ 贪婪吞掉 URL 后相邻的 HTML
        # 尾巴（如 "https://www.ruanyifeng.com/li class=\"module-list-item\">Email..."）→
        # 垃圾 URL 入队重复重试烧时间。只允许 URL 合法字符（不含空白/引号/尖括号）。
        js_str=re.findall(r'"((?:https?:)?//[A-Za-z0-9\-._~:/?#\[\]@!$\'&()*+,;=%]+)"',html)
        links.update([u for u in js_str if u.startswith('http')])

        # Normalize: absolute URLs
        # [FIXED & MODIFIED] v2.10.5c 统一兜底：标签属性正则([^"']+)仍可能吞 HTML 尾巴
        # （<a href="...class='module-list-item'>）→ 归一化时过滤含空白/尖括号/引号/汉字
        # 的伪 URL（HTML 尾巴必含这些字符之一；真实 URL 不含）。
        result=[]
        for u in links:
            u=u.strip();u=u.split('#')[0]
            # 伪 URL 过滤：标签属性吞 HTML 尾巴的兜底（与 _is_junk_link 形态拦截一致）
            if re.search(r'[\s<>"\'`\u4e00-\u9fff]', u):
                continue
            if u.startswith('//'):u='https:'+u
            if not u.startswith('http'):
                if base_url:
                    from urllib.parse import urljoin
                    u=urljoin(base_url,u)
                else:continue
            if u.startswith('http'):result.append(u)
        return list(set(result))
