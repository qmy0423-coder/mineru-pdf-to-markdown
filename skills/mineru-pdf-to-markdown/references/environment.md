# 环境安装、GPU 选择与版本兼容

本 Skill 开发验证版本为 MinerU **3.4.5**（2026-09-08）。后续运行查询实时版本。

## 先检查

在当前项目、用户指定工具目录、已有 venv/conda 和 PATH 中定位环境。不要扫描整个磁盘或读取无关配置。传给探测脚本的 `--python` 应是解释器路径，不是虚拟环境目录。

```text
python <Skill目录>/scripts/inspect_environment.py --python <目标Python> --disk-path <安装磁盘上的现有目录> --output <工作目录>/environment.json
```

它实际执行 CUDA/MPS 小张量运算，并记录显存、RAM、磁盘空间、已装依赖和 PyPI 版本。`unknown` 表示联网检查失败，`offline` 表示明确跳过查询，均不代表最新版。`different_version_check_compatibility` 需要比较版本和兼容性，不能直接判成旧版本。

当前官方要求 Python 3.10–3.13；Windows 的通用安装路径优先选 3.10–3.12。官方资源建议包含 16 GB RAM、20 GB 磁盘空间，pipeline 约 4 GB 显存、VLM/hybrid 本地引擎约 8 GB 显存。实际还受文档、并发和空闲显存影响。探测脚本的后端推荐只是小样起点。

## 安装与更新

没有合适环境时，在用户指定位置或工作目录下建立独立环境。下面用 `<Python>` 表示新环境的解释器；Windows 通常是 `.venv/Scripts/python.exe`，Linux/macOS 是 `.venv/bin/python`。

```text
python -m venv <环境目录>
<Python> -m pip install --upgrade pip
<Python> -m pip install --upgrade "mineru[all]"
```

`mineru[all]` 是当前官方跨平台通用安装方式。只有纯 CPU、磁盘/依赖受限且不需要 VLM 时，可选官方 `mineru[pipeline]` 扩展。不要把 Linux 专用 vLLM 安装方式套用到原生 Windows。

已有环境先查版本和 `pip check`，核对变化涉及的 Python、Torch、模型及私有格式化接口。本 Skill 的校对代码调用 MinerU 内部格式化函数，所以**新解析环境和校对环境必须配套**。优先在新 venv 验证更新，保留可工作的旧环境；沿用用户缓存和模型位置，不无故重下模型。不要为了“最新”破坏用户固定版本的可工作环境。

更新完成后再次运行探测脚本、`mineru --help` 和小样解析/校对构建，确认后再使用新版。若回退旧版本，解释兼容原因，不称旧版为最新版。没有网络时如实记录版本检查失败；已有完整本地模型可继续解析。

## 设备选择

| 条件 | 处理方式 |
| --- | --- |
| CUDA 运算通过、约 8 GB 或更多显存 | 从 `hybrid-engine --effort high` 或 `vlm-engine` 小样开始；关注空闲显存 |
| CUDA 运算通过但显存较少 | `pipeline`，通过 `MINERU_DEVICE_MODE=cuda` 或实际设备索引使用 GPU |
| NVIDIA 硬件存在但 Torch CUDA 不可用 | 先按 PyTorch 官方安装选择器修复目标环境中的 CUDA Torch/torchvision，再复测 |
| Apple Silicon / MPS 通过 | 优先验证 `pipeline` + `MINERU_DEVICE_MODE=mps`；VLM 使用当前官方 macOS 指导 |
| 没有可用 GPU | `pipeline` + `MINERU_DEVICE_MODE=cpu`；说明速度变化 |

给子进程设置环境变量，避免修改系统全局环境。多卡机器记录实际 GPU；如果用 `CUDA_VISIBLE_DEVICES`，注意 Torch 可见索引会重新编号。显存不足时降低当前官方支持的批量设置或改用 GPU pipeline；仍失败才回退 CPU 并说明原因。只对明确故障做有针对性的重试，保留日志。

Windows 常见情况是安装了 CPU 版 Torch。依据显卡架构、驱动和 Python，在 [PyTorch 官方安装选择器](https://pytorch.org/get-started/locally/) 取得当前匹配命令。不能只根据 `nvidia-smi` 显示的 CUDA 上限硬编码某个 wheel。特殊架构要求见 [MinerU Windows FAQ](https://opendatalab.github.io/MinerU/faq/)。

## 模型与命令

- 模型可能在首次解析时下载，下载未完成不算解析完成。
- `MINERU_MODEL_SOURCE` 支持当前官方定义的模型源；保留用户现有设置。`local` 模式必须有有效的模型配置。
- `local` 模式下还要确认**所选后端**的模型齐备；只有 VLM 模型不代表 pipeline/hybrid 也能运行。缺少模型时补齐对应模型或使用满足需求的已有后端，不能把模型缺失误判为硬件故障。
- `MINERU_TOOLS_CONFIG_JSON` 可指向现有配置。不要将本机配置或绝对路径写进 Skill。
- 配置下载位置时使用用户选定的缓存/模型目录。下载模型与上传用户 PDF 是不同操作；本 Skill 默认本地解析。
- 不带 `--api-url` 的 MinerU 3.x CLI 会启动本地临时服务。使用官方 CLI 正常退出和清理流程，不能杀死其他任务的 MinerU 进程。
- 如果本地 `/health` 直连正常，但 CLI 长时间等不到服务就绪且没有任务提交，检查系统代理是否代理了回环地址。可为本次子进程把 `127.0.0.1,localhost,::1` 加入 `NO_PROXY`，保留原有条目；不要全局关闭用户代理。
- 小样可用 `-s 0 -e 1`（0 起始、包含结束页），或单独准备短 PDF；全文解析移除这些参数。依本次版本的 `--help` 再确认参数。
- 命令使用参数数组或正确的 shell 引号，支持路径空格和中文。

## 官方参考

- [MinerU 安装与硬件要求](https://github.com/opendatalab/MinerU#local-deployment)
- [扩展模块](https://opendatalab.github.io/MinerU/quick_start/extension_modules/)
- [CLI 和环境变量](https://opendatalab.github.io/MinerU/usage/cli_tools/)
- [模型源](https://opendatalab.github.io/MinerU/usage/model_source/)
- [高级参数](https://opendatalab.github.io/MinerU/usage/advanced_cli_parameters/)
- [PyPI 版本](https://pypi.org/project/mineru/)

若当前环境提供 Context7 MCP，先 resolve MinerU，再分别查询安装/设备和命令用法；不可用时使用以上官方资料及目标环境 CLI。不要声称执行过未提供的 MCP 工具。
