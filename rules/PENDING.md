# 规则层 / 能力待取证清单（v2.19.9）

登记以下条目**尚未写规则 / 未接入**，需要真实页面样本才能落地（工程纪律：不凭猜测选选择器）。

| 项目 | 卡点 | 解锁方式 |
|---|---|---|
| canonical 归一（重复 URL 合并） | 需要 frontier 别名/合并设计（去重键扩展）——成本高于短期收益 | 列入下一迭代：url_utils + frontier.alias 表 |
| 头条列表/搜索页反爬采样 | 仅详情页规则（toutiao-article）就绪 | 不计划——列表/搜索页风控重，成本不划算 |

> 曾经列在本表的 smzdm 社区文章页与微信公众号文章页**已落地为规则**（v2.19.8 新增，
> 见下节）。保留此记录，以免后续按旧说法重复排查。

## 当前规则层状态

- 规则层现有 **14 条**（`rules/sites/*.yaml` 实数）：
  模板 2 条（`example.yaml`、`example.com.yaml`）+
  真实站点 12 条（netease / sina / sohu / ifeng / weibo / zhihu / douban / sspai /
  toutiao / 36kr / **smzdm-post** / **wechat-article**）；
- 12 条真实站点规则均带真实样本资产（`tests/assets/*.html`）与离线断言——改版漂移会先红；
- 国内直连白名单在 `exit_manager._DOMESTIC_DOMAINS`（ifeng / 36kr / sspai /
  weixin.qq.com / mp.weixin.qq.com / smzdm.com 均已补）。

## 已接线（**保留记录，以免照旧说法重复排查**）

| 项目 | 原卡点 | 现状 |
|---|---|---|
| CookieArmory 引擎主链路接线（多账号按站轮换） | "需要 resolver 级 cookie 注入设计（每站 acquire/report 回传）——列下一迭代" | **已接线**：`page_processor._fetch_with_identity` 每页 `acquire → 注入 → fetch → report`（finally 清租约）；`protocol_engine` 提供页面级租约注入点；`crawler._init_cookie_armory` 由 `cookie_armory_enabled` 开关控制（**默认关**）。身份失效走**诚实降级**（可读原因），不做静默回退 |

> 上表首行的「下一迭代」在 v2.19.8–v2.19.9 窗口内闭环（原施工记录见 `CHANGELOG.md`
> 的 `[2.19.8]` 补记「身份层接通」一节；对应内部工单编号 `M1-c`）。

