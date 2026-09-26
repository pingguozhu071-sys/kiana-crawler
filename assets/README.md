# assets/

构建与分发用的静态资产。

| 条目 | 说明 |
|---|---|
| `icon.ico` | 程序图标 —— **只读打包，禁止裁剪 / 重画 / 重编码** |
| `cover.png` | 安装器与仓库封面用图 |
| `fonts/` | 随包字体及其许可正文（Roboto = Apache-2.0；Mochiy Pop One / Yusei Magic = SIL OFL 1.1） |
| `installer/` | 安装器美术（由 `tools/gen_installer_art.py` 生成，版本徽章读 `VERSION.json`） |

**素材来源**：

- 字体与图形资产的许可情况见 [`docs/来源与合规声明.md`](../docs/来源与合规声明.md)
- `cover.png` **不是本工程的原创作品**（作者个人收藏，仅作装饰），
  声明见 [`docs/ASSET-PROVENANCE.md`](../docs/ASSET-PROVENANCE.md)
