# 网站维护 {#website-maintenance}

公开 GitHub Pages 源码位于 `site/`。网站为静态双语页面：`index.html` 为英文，`zh.html` 为中文。保持内容与锚点对应，共用 `styles.css` 和 `app.js`。

## 设计与产品预览 {#design-and-product-previews}

沿用控制台中性底色、白色内容面、细边框、8px 卡片圆角和蓝色主操作。首页以明确标记的交互示意展示应用、路由、指标、交付和可观测性。`example.com` 域名和资源值是示例数据，不是真实集群或截图。路由关系说明应与连通性及健康检查区分。

示意使用原生 HTML/SVG，无需远程图片。标签页支持方向键、Home、End。JavaScript 还提供移动菜单和命令复制；核心内容与文档在无 JavaScript 时仍可阅读。

Geist 字体与许可证自托管于 `site/assets/fonts/`，中文使用平台 CJK 字体。既有品牌资产仍保留，不加载旧装饰性首屏图片。

## 构建与预览 {#build-and-preview}

使用与 CI 一致的 Python 3.12：

```bash
python -m venv /tmp/luma-site-venv
/tmp/luma-site-venv/bin/python -m pip install Markdown==3.8.2
/tmp/luma-site-venv/bin/python scripts/build-site.py
python scripts/check-site.py
python -m http.server 5182 --directory _site
```

打开 `http://localhost:5182/` 或 `http://localhost:5182/zh.html`。模拟项目 Pages 前缀时，把产物复制到 `luma/` 目录并托管父目录；所有本地 URL 都必须在此前缀下工作。

构建复制静态资源与原始文档，再将 `docs/**/*.md`（排除内部 `docs/research/`）渲染为同目录 HTML。支持 Markdown 表格、围栏代码和页内目录。文档相对链接改写为 HTML，仓库代码和目录链接指向 GitHub。Mermaid 保留为代码示例，不执行远程渲染器。

编辑 Markdown 源码，不编辑生成 HTML。重要指南加入 `scripts/build-site.py` 的 `GUIDES`，按需同步两种首页入口。不要提交 `_site/`。输出含 `.luma-site-output` 标记；后续构建只替换带标记目录，拒绝覆盖其他非空目录。

## 发布 {#publishing}

相关变更进入 `main` 或手动触发时，`.github/workflows/pages.yml` 构建并部署网站。它安装固定版本 Markdown 渲染器，通过 GitHub Pages 发布 `_site` 产物。

本地构建不等于公开发布。合并发布前检查中英文、窄屏导航、预览标签、命令复制与文档链接。
## 中文文档 {#chinese-documentation}

每篇公开英文指南都有对应的 `*.zh-CN.md` 中文源文件。原本就是中文的文档保留原路径。更新正文时同步维护译文；命令、配置键和围栏代码示例须保持一致。CLI 帮助在两个语言版本中均保留终端实际输出。先运行 CLI 参考生成器，再将更新后的帮助代码块同步到中文参考。

中文页面使用中文导航，并将文内文档链接指向中文版本。显式标题 ID（`{#original-heading-id}`）保留已有英文章节链接及语言切换位置。翻译新增章节时，请添加对应 ID。中英文首页应链接到各自语言的文档。发布前，`scripts/check-site.py` 检查中文覆盖、译文代码块、章节 ID 和本地链接。

控制台预览的 25 个左侧菜单和子菜单均可切换对应示例页面，面包屑与选中状态同步更新。顶部五个快捷标签共用同一导航状态，并支持方向键、Home 和 End。维护时须同步中英文菜单目标及示例页面。
