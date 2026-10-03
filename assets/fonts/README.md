# 字体（v2.17.1 安装器/GUI 共用）

| 文件 | 来源 | 许可 |
|---|---|---|
| Roboto-Regular.ttf | googlefonts/Roboto 官方仓库 (src/hinted) | Apache-2.0（见 LICENSE-ROBOTO.txt） |
| （中文/日文） | 不捆绑——安装器与 GUI 回退系统字体：微软雅黑/思源黑体(中)、Yu Gothic/Meiryo(日) | 系统分发 |

使用：安装器 `.onInit` 将 Roboto 注册到系统（FontRegistrar，可选默认开）→ 安装器与
GUI 字体栈优先 "Roboto"。中文/日文文本由系统字体渲染，无 license 风险。
