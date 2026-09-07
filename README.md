# markitdown-tk-gui

一个基于 Tkinter 的批量「文件转 Markdown」图形界面工具，底层使用微软 [MarkItDown](https://github.com/microsoft/markitdown) 库。

支持一次选择几十个任意类型文件（PDF、DOCX、XLSX、PPTX、图片、网页等）批量转换为 Markdown；提供合并输出、自定义排序、深色模式、实时预览与日志面板。

## 功能特性

- **批量转换**：一次导入多个文件，逐个生成对应的 `.md` 文件。
- **合并模式**：将所有结果按指定顺序合并为单个 Markdown 文件。
- **顺序控制**：内置按文件名 / 创建日期 / 修改日期 / 文件类型排序（可正序/倒序），或手动上移/下移/置顶/置底。
- **输出控制**：自定义目标目录与输出文件名（留空自动使用「源文件名 _ 时间戳」；可勾选「默认文件名不加时间戳」）。
- **转换后打开**：可自动打开输出文件或所在文件夹。
- **界面体验**：深色/浅色主题、进度条、实时预览、日志面板、完成通知（Toast）。
- **并发与不卡界面**：多文件转换使用多进程并行，GUI 全程流畅（见下方「架构说明」）。

## 快速开始

依赖 Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)（项目使用 `uv.lock` 锁定依赖）。

```bash
# 1. 首次使用：安装依赖（仅需要 Python 3.12+ 与 uv，会自动创建 .venv）
uv sync

# 2. 启动 GUI
start.cmd
```

> 本项目**不需要任何额外运行时依赖**。仓库自带 `.venv` 时可直接双击 `start.cmd` 或运行 `uv run python main.py`，无需再装任何东西。
>
> `start.cmd` 用 `pythonw.exe` 启动 GUI 本身无控制台，但 cmd 窗口会短暂闪现一次；如在意可改用 `uv run python main.py`（保持终端在前台，也便于查看报错日志）。

## 使用说明

1. 点击「添加文件」选择要转换的文件（支持多选）。
2. 在右侧设置输出选项：是否合并、排序规则、目标目录、输出文件名、转换后是否打开。
3. 点击「开始转换」，确认弹窗后即可。进度与日志实时显示，转换期间界面不会卡死。

## 架构说明

本项目针对「几十个 PDF 批量转换时 GUI 卡死」做过一轮针对性重构，关键设计如下：

### 为什么线程不行：GIL

CPython 中**同一进程内的所有线程共享一把 GIL**。PDF 解析（pdfminer）是纯 Python 的 CPU 密集任务，线程越多越会互相抢锁，连 Tk 主线程的定时器回调都抢不到执行权——结果就是界面冻结。`ThreadPoolExecutor` 对这类 CPU 密集场景无能为力。

### 进程隔离：每进程一把独立的 GIL

真正的解法是把「抢 GIL 的重活」和「GUI 主循环」放进**不同的进程**：

- **多进程并行转换**：`main.py` 用 `ProcessPoolExecutor` 把每个文件的转换提交给子进程。子进程各有自己的 GIL，与 GUI 进程完全互不争抢，所以界面恒定流畅，同时获得真正的多核并行。
- **每个子进程只加载一次模型**：`MarkItDown()` 会在内部加载 Magika（一个 ONNX 神经网络文件类型分类器）与各解析依赖，非常耗时。模块级懒加载 `_get_shared_converter()` 保证每个子进程只构造一次、全程复用。
- **避免跨线程触碰 Tcl 解释器**：Tkinter 的变量 `.get()`/`.set()` 不是线程安全的。转换前置项（合并开关、目标目录、输出名等）在 GUI 主线程快照成 `JobOptions` 后传入工作线程与子进程，工作侧全程不再访问任何 Tk 变量。
- **`pythonw` 兼容**：Windows 下子进程通过 spawn 重新导入 `main.py`，靠顶层的 `if __name__ == "__main__"` 守卫避免递归创建 GUI；同时在模块顶部为无控制台的 `pythonw.exe` 补上 `stdout/stderr` 空设备兜底，避免子进程打印警告时崩溃。
- **完成顺序汇报进度**：无论是否合并，结果都用 `as_completed` 按完成顺序回报，单个慢文件不再拖累其余文件的进度显示。

### 并发资源权衡

进程池数量取 `min(8, CPU 核数, 文件数)`。子进程会各自加载一份模型与解析库，内存占用随并发数线性增长——文件很少或机型内存紧张时可下调该上限。

## 打包发布（GitHub Actions）

仓库内置 `.github/workflows/release.yml`，用 PyInstaller 在三种系统各自产出**便携版可执行文件**并自动发布到 GitHub Releases：

| 平台 | 产物 |
|------|------|
| Windows x64 | `markitdown-tk-gui-windows-x86_64.zip`（内含单个 `.exe`） |
| Linux x64 | `markitdown-tk-gui-linux-x86_64.tar.gz` |
| macOS Intel | `markitdown-tk-gui-macos-x86_64.tar.gz`（内含 `.app`） |
| macOS Apple Silicon | `markitdown-tk-gui-macos-arm64.tar.gz`（内含 `.app`） |

### 触发方式

- **推标签自动发布**：`git tag v1.0.0 && git push origin v1.0.0` → 自动构建四个平台产物并以该标签发布 Release（含自动 changelog 与 SHA256 校验和）。
- **手动构建**：Actions 页面 → 「Build and Release」→ «Run workflow»，填 `release_tag` 即构建并发布；留空则只构建不发布。

### 打包要点

- 每个 runner 用 `uv sync` 安装 `markitdown[all]` 后，以 `--collect-all magika/onnxruntime` 收集 ONNX 模型等数据文件，产出单文件可执行程序。
- 打包后 `ProcessPoolExecutor` 的子进程会重新执行打包后的可执行文件，因此 `main()` 里已调用 `multiprocessing.freeze_support()`，保证并发转换在冻结环境下照常工作（含各子进程内模型独立加载）。
- macOS 构建分别用 `macos-13`/`macos-14` 覆盖 Intel 与 Apple Silicon；若 GitHub 淘汰对应镜像，把 `release.yml` 里的 `runs-on` 换成仍可用的 macos 标签即可。

## 项目结构

```
main.py                             全部代码（GUI + 转换逻辑，单文件）
start.cmd                           启动入口（cmd 会闪一次，之后由 pythonw 运行 GUI）
pyproject.toml                      依赖声明（markitdown[all] >= 0.1.5）
.github/workflows/release.yml       PyInstaller 三平台打包 + 自动发布 Releases
```

## License

GPL-3.0 License (see [LICENSE](LICENSE) for details)
