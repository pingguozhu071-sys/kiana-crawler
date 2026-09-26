# 字体（安装器 / GUI 共用）

| 文件 | 来源 | 许可 |
|---|---|---|
| Roboto-Regular.ttf | googlefonts/Roboto 官方仓库 (src/hinted) | Apache-2.0（见 `LICENSE-ROBOTO.txt`） |
| MochiyPopOne-Regular.ttf | MochiyPop 项目（github.com/fontdasu/Mochiypop） | **SIL OFL 1.1**（见 `LICENSE-OFL-MochiyPopOne.txt`） |
| YuseiMagic-Regular.ttf | Yusei Magic 项目（github.com/tanukifont/YuseiMagic） | **SIL OFL 1.1**（见 `LICENSE-OFL-YuseiMagic.txt`） |

**使用**：安装器 `.onInit` 把 Roboto 注册到系统（可选，默认开）→ 安装器与 GUI 字体栈优先 "Roboto"。
界面正文的中文/日文由**系统字体**渲染（微软雅黑 / 思源黑体、Yu Gothic / Meiryo）。
上面两款艺术字体仅在"电子签名"水印处按需加载（`QFontDatabase.addApplicationFont`）。

**许可注意**：两款艺术字体为 SIL OFL 1.1，随仓库与打包产物一并分发，许可正文已就近放置
（OFL 要求随字体分发许可文本）。它们**不是**系统字体回退项，属于随包资源。
