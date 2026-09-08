# 离线校对页与兼容性

代码已随 Skill 放入 `scripts/review/`，运行时不依赖原电脑的目录。

## 校对页功能

- 双击打开 `校对.html` 即可使用，原文页图、提取图片、字体、Markdown 渲染器和公式渲染器都已内嵌。
- 左侧连续显示 PDF 全页；点识别框定位右侧 Markdown，右侧点选联动原文。可缩放、翻页、拖动分栏。
- 双击正文编辑，`Ctrl+Enter` 应用，`Esc` 取消。页眉页脚和页码是只读参考，不写入正文导出。
- “保存 Markdown”下载包含 `.md` 和 `images/` 的 ZIP；保留解压后的相对路径。浏览器下载不覆盖 MinerU 原始文件。
- 浏览器本地草稿只作便利保存，持久保存使用 ZIP 导出。
- 续接线仅标注程序能够验证的 MinerU 关系；尚未确认的语义衔接留给 Codex 第三步报告。

## 输入与输出约束

`--input` 是一个文档的实际解析子目录，含唯一的 `*_middle.json`、同名 `.md`、`*_origin.pdf` 和引用到的图片。完整文档和小样必须用不同目录。支持 MinerU 3.4.5 的 VLM、hybrid 和 pipeline 格式；pipeline 使用独立官方格式化器，保留其原始段落粒度。

`--output` 必须在原始解析目录外。默认文件名是输入目录上一级的 `校对.html`。默认页图缓存位于系统临时目录，构建后清理；`--cache-dir` 可指定工作目录中的缓存。

`--profile-dir` 只用于显式应用已核对的外部段落清单。默认不加载，Skill 不携带旧文档清单。清单按 Markdown SHA-256 命名，且校验原块、断点字串和跳过的图表。用户仅要求修复建议时不使用这个参数。

## 验证与失败处理

构建器检查输入目录前后哈希一致、原始 Markdown 的精确重组、PDF 页数/页面尺寸、图片引用和 vendored 文件哈希。初始导出的 Markdown 应与原始文件一致（用户编辑或显式应用修复以后除外）。

打开校对页检查首尾页及包含图片、表格、公式的代表页面。至少验证一次：点选定位、编辑应用/取消、ZIP 可解压且 Markdown 图片引用存在。有浏览器自动化时，可用 `window.mineruViewer.getMarkdown()` 比较初始文本，并读取 `window.mineruViewer.data` 验证页数。

若报 `Extracted Markdown differs`、`Pipeline formatter differs` 或内部函数导入失败，检查中间 JSON 的 `_version_name` 是否与解析环境一致，以及配置中的公式分隔符等设置。不要通过删除精确比较来绕过错误。可使用相应环境重建；新版格式变化时需要针对实际输出更新适配器并重新验证。

## 代码来源与浏览器依赖

`build_review.py`、`extract_blocks.py`、`reviewed_continuations.py`、页面模板/CSS/JS 来自用户提供的本地校对工具，已移除固定文档路径和自动加载历史修复记录，并把页图缓存迁出 Skill。

新增加了 pipeline 适配、环境探测和复核分块脚本。没有打包原始 PDF、解析 Markdown/JSON、历史 profile、虚拟环境、模型、缓存或凭据。

保留的浏览器资源来自官方 npm 发布包，构建时逐文件校验 SHA-256：

| 资源 | 版本 | 许可与来源 |
| --- | --- | --- |
| markdown-it | 14.1.0 | MIT；`scripts/review/vendor/markdown-it/LICENSE`；[npm](https://www.npmjs.com/package/markdown-it/v/14.1.0) |
| KaTeX 和字体 | 0.16.22 | MIT；`scripts/review/vendor/katex/LICENSE`；[npm](https://www.npmjs.com/package/katex/v/0.16.22) |

`vendor-manifest.json` 记录包源、npm integrity 和逐文件摘要。正常生成不需要联网；显式 `--download-vendor` 只供维护者恢复缺失的固定版本资源。MinerU/Torch 由运行环境单独安装，未包含在此仓库中，其许可与模型条款以各官方项目为准。
