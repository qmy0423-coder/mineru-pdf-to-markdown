# MinerU PDF 转 Markdown Skill

供 Codex 使用的三阶段工作流：

1. 检测电脑配置，安装适合硬件的 MinerU；已有环境检查版本更新，优先使用 GPU。
2. 解析 PDF，调用内置校对工具生成离线、可编辑的 `校对.html`。
3. 由 Codex 阅读完整 Markdown、核对原文中的段落断点，输出 `修复建议.md`。

## 在 Codex 中安装

把下面这句话发送到 Codex 对话框。`$skill-installer` 用于调用安装 Skill；整行不在终端执行。

```text
$skill-installer Install https://github.com/qmy0423-coder/mineru-pdf-to-markdown/tree/main/skills/mineru-pdf-to-markdown
```

也可调用已安装的 Skill Installer 脚本（脚本目录随 Codex 安装位置变化）：

```text
python <skill-installer-dir>/scripts/install-skill-from-github.py --repo qmy0423-coder/mineru-pdf-to-markdown --path skills/mineru-pdf-to-markdown
```

安装后可在下一轮任务调用。若名称未出现，重新加载/重启 Codex。安装 Skill 仅下载工作流和脚本，首次实际解析时才检查并准备 MinerU 环境。

## 使用

```text
$mineru-pdf-to-markdown 将这个 PDF 转成 Markdown，优先使用 GPU，并生成校对.html 和修复建议.md。输入：<PDF路径>，输出：<结果目录>。
```

已有解析结果也可以直接生成校对页并复核。校对页包含原始 PDF 页图、识别框联动、Markdown 编辑、公式与表格显示，以及带图片的 Markdown ZIP 导出。修复建议由 Codex 在任务中完成，不需要单独的 LLM API key；默认保留原文，仅提出建议。

已同步新版通用段落续接检测：默认在校对页数据和复核材料中记录候选，包含断点原文、行号、页码及浮动图表位置。需要规则自动合并时，可给 `build_review.py` 加 `--auto-continuations`；显式 `--profile-dir` 中匹配源哈希的人工复核清单优先。合并保留原始片段和映射，原始解析文件不变。规则候选仍需 Codex 核对，详细模式见 [校对页说明](skills/mineru-pdf-to-markdown/references/review-page.md)。

每份 PDF 的结果包括：原始 Markdown、`images/`、中间 JSON/原文副本、`校对.html`、`修复建议.md`。

## 兼容性

开发验证基线：MinerU 3.4.5、Python 3.12。工作流会实时查更新；校对代码依赖 MinerU 的内部格式化接口，升级后需用小样验证。支持 VLM/hybrid 与 pipeline 输出；实际设备和质量以每次解析日志及复核为准。

详细说明见 [SKILL.md](skills/mineru-pdf-to-markdown/SKILL.md)。安装行为参考 [OpenAI 官方 Skill 文档](https://learn.chatgpt.com/docs/build-skills)。

仓库仅包含可复用代码、说明和离线浏览器依赖，不包含业务 PDF、历史解析结果、文档专用修复清单、模型或本机配置。markdown-it、KaTeX 的许可证与完整性清单随资源保留；MinerU 单独安装。

## 已执行的验证

- Windows / Python 3.12 / MinerU 3.4.5：RTX 4070 Laptop 的 CUDA 运算与 VLM 解析通过，2/2 页自建样本完成。
- 同一样本的 hybrid 高强度模式（本次小样关闭公式模型）和 CPU pipeline 基础模式（本次小样关闭公式及表格识别模型）均完成 2/2 页；三种输出都通过校对页构建与 Markdown 精确重组。
- 额外用一份 41 页既有 VLM 输出回归校验：632 个编辑块、18 张图片，原文和图片保留。
- Edge 离线验证：页面打开、首尾页导航、编辑应用/取消、Markdown ZIP 下载通过；导出图片字节一致，未产生外部请求。
- Codex 完整检查自建样本，定位 1 处跨页断句并生成修复建议，原始 Markdown 未修改。
- 2026-09-11 更新验证：11 项续接测试通过；复用 VLM、hybrid、pipeline 小样及 41 页既有解析结果验证默认候选、可选自动合并和原文逆向还原。浏览器编辑、ZIP 内容与图片、旧草稿合并恢复及不同段落策略的草稿备份通过；本次没有重新运行 OCR。

可用 `tests/validate_run.py --input <解析子目录> --html <校对.html> --packets <复核材料目录>` 复查无损重组、素材引用、分块覆盖和源文件保护。该验证器不替代 Codex 的语义复核。

续接规则与模式测试可运行 `python -m unittest discover -s tests -p test_continuations.py`，无需安装 MinerU 或准备业务文档。
