# 字体（安装器 / GUI 共用）

| 文件 | 来源 | 许可 |
|---|---|---|
| Roboto-Regular.ttf | googlefonts/Roboto 官方仓库 (src/hinted) | Apache-2.0（见 LICENSE-ROBOTO.txt） |
| MochiyPopOne-Regular.ttf | Google Fonts · The MochiyPop Project Authors（日文圆体，覆盖假名与常用汉字） | **SIL OFL 1.1**（见 LICENSE-OFL-MochiyPopOne.txt） |
| YuseiMagic-Regular.ttf | Google Fonts · The Yusei Magic Project Authors（日文手写体，覆盖假名与常用汉字） | **SIL OFL 1.1**（见 LICENSE-OFL-YuseiMagic.txt） |

**用途与加载路径**：

- `Roboto-Regular.ttf` —— 安装器与 GUI 的正文字体首选；安装器 `.onInit` 经 FontRegistrar 注册到系统
  （可选，默认开），安装时另行释放到 `$INSTDIR\fonts`。
- `MochiyPopOne-Regular.ttf` / `YuseiMagic-Regular.ttf` —— **不作为正文字体注册**，仅供 GUI 签名标签
  按运行时顺序尝试加载（`QFontDatabase.addApplicationFont`，先 Mochiy Pop One 后 Yusei Magic，
  两者都不存在时回退系统字体）。
- 三者随构建产物一同分发（`KianaLauncher.spec` 将整个 `assets/` 目录收进包内），
  **不额外依赖用户系统已装同名字体**。

**系统字体回退**：正文的中文/日文文本仍由系统字体渲染 —— 中文优先微软雅黑 / 思源黑体，
日文优先 Yu Gothic / Meiryo；上述 SIL OFL 字体只覆盖签名标签，不改变正文的字体栈。
