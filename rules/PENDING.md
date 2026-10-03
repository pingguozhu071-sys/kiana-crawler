# 规则层/能力待取证清单（v2.17）

诚实登记：以下条目**尚未写规则/未接入**，需要真实页面样本才能落地（不瞎选选择器）。

| 项目 | 卡点 | 解锁方式 |
|---|---|---|
| smzdm 笔记页（post.smzdm.com/p/） | 首页只出众测申请链（test.smzdm.com），无文章直链 | 浏览器打开站点→社区频道→任意笔记页→存 DOM（IAB 一轮即可） |
| 微信文章页（mp.weixin.qq.com） | 公众号文章无公开搜索入口 | 用户任意分享链接一条 → 存 DOM |
| canonical 归一（重复 URL 合并） | 需要 frontier 别名/合并设计（去重键扩展）——成本高于短期收益 | 列入下一迭代：url_utils + frontier.alias 表 |
| 头条列表/搜索页反爬采样 | 仅详情页规则（toutiao-article）就绪 | 不计划——列表/搜索页风控重，成本不划算 |

## 已就绪位注意

- 规则层现在 12 条（example/example.com 模板 2 + netease/sina/sohu/ifeng + weibo/zhihu + douban/sspai/toutiao/36kr）；
- 新规则都带真实样本资产（tests/assets/*.html）与离线断言——改版漂移会先红；
- 国内直连白名单在 exit_manager._DOMESTIC_DOMAINS（ifeng/36kr/sspai/weixin 已补）。

## 已接线（**保留记录，以免照旧说法重复排查**）

| 项目 | 原卡点 | 现状 |
|---|---|---|
| CookieArmory 引擎主链路接线（多账号按站轮换） | "需要 resolver 级 cookie 注入设计（每站 acquire/report 回传）——列下一迭代" | **已接线**：`page_processor._fetch_with_identity` 每页 `acquire → 注入 → fetch → report`（finally 清租约）；`protocol_engine` 提供页面级租约注入点；`crawler._init_cookie_armory` 由 `cookie_armory_enabled` 开关控制（**默认关**）。身份失效走**诚实降级**（可读原因），不做静默回退。详见 DEVLOG「M1-c 完成」 |

