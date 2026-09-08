# markitdown-tk-gui

一个基于 Tkinter 的批量「文件转 Markdown」图形界面工具，底层使用微软 [MarkItDown](https://github.com/microsoft/markitdown) 库。

支持一次选择几十个任意类型文件（PDF、DOCX、XLSX、PPTX、图片、网页等）批量转换为 Markdown；提供合并输出、自定义排序、深色模式、实时预览与日志面板。

> **本项目最特别的地方**：它是为「一次性批量转换几十个超大 PDF（单文件可达 800MB 以上）且 GUI 不卡、内存不爆」而设计的。为此做了两层针对性改造——**PDF 走 C 原生 PyMuPDF 引擎**（省去 pdfminer 的逐字节纯 Python 解析，见下文基准），以及**常驻受管进程池 + 运行时内存自监控**（并发转换、超限自动降并发并回收进程）。

## 功能特性

- **批量转换**：一次导入多个文件，逐个生成对应的 `.md` 文件。
- **PDF 原生引擎**：PDF 不再走 pdfminer，而是用 C 原生 PyMuPDF 直读路径；110MB 文件约 5 秒完成、峰值内存仅 146MB，比旧路径快约 57 倍、内存低约 32 倍（含无边框表格识别逻辑移植）。
- **模型零拷贝（PDF 批次）**：纯 PDF 批次整个进程池不加载 Magika（ONNX）模型；markitdown 只在第一个非 PDF 文件到来时才懒加载，不浪费内存。
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
2. 在右侧设置输出选项：是否合并、排序规则、目标目录、输出文件名、转换后是否打开、并发进程数、内存上限。
3. 点击「开始转换」，确认弹窗后即可。进度与日志实时显示，转换期间界面不会卡死。

## 架构说明

本项目针对「几十个 PDF 批量转换时 GUI 卡死 + 内存爆炸」做过针对性重构，关键设计如下：

### 为什么线程不行：GIL

CPython 中**同一进程内的所有线程共享一把 GIL**。CPU 密集的解析任务会互相抢锁，连 Tk 主线程的定时器回调都抢不到执行权——结果就是界面冻结。因此转换工作必须放在不同进程。

### PDF 原生引擎 + 按文件类型分流

**PDF 直接由 C 原生库解析**，不经过 MarkItDown 的 pdfminer 旧路径：

- `_pool_child` 按扩展名分流：`.pdf` → 调用 `pdf_engine.convert_pdf_native`（PyMuPDF C 原生直读文件，路径直读不把整文件拷贝到内存）；其余类型 → 调用 MarkItDown。
- `convert_pdf_native` 移植了 MarkItDown 内建 PDF 转换器的无边框表格/表单几何启发式，保留了表格识别质量，同时用 PyMuPDF 的 C 接口替换 pdfminer+pdfplumber 纯 Python 路径。**失败回退**：若原生引擎抛异常或返回空白，自动回退到 MarkItDown 内置引擎重试一次（仅首次触发构造，不额外常驻模型）。
- `markitdown` 在模块顶部**懒导入**（`from markitdown import MarkItDown` 移至函数体内）：纯 PDF 批次整个进程池不加载 MarkItDown、不加载 Magika/ONNX Runtime，模型内存为零；只有在第一个非 PDF 文件到来时才构造一次、全程复用。
- 派发前的内存预估也按类型区分：PDF 原生路径峰值约等于文件大小，预估为 `max(96, 文件MB×1.5 + 64)`，不再乘 5，避免高估而限制并发。

### 进程隔离 + 常驻受管进程池

转换核心在 GUI 进程的工作线程里维护一个**常驻受管进程池**（见 `_convert_pooled`）：

- **多核并行**：每个子进程一把独立 GIL，与 GUI 进程互不争抢，界面恒定流畅。
- **派发前预估内存、复用优先**：派发前按路径区分的内存预估，优先把任务派给空闲且剩余容量足够的子进程；只有当池未满且不拥塞时才新建进程。新建只在池预热（数量=并发上限）和定点回收后发生。
- **自监控内存 + 在线调并发**：GUI 进程内 `_MemoryGovernor` 线程每 0.5s 统计「本进程 + 所有子进程」的 RSS，超过阈值（默认物理内存 85%，可在「输出选项 → 内存上限」调整）即标记拥塞：停止新建进程并压到只有 1 个在飞以等待高峰落地；回落后再逐步恢复到目标并发。
- **定点回收涨大的子进程**：每个子进程完成一个任务后上报自身 RSS，超过「每进程应得份额」就终止它并换一个新进程——被撑起来的堆内存随进程退出真正归还系统。
- **避免跨线程触碰 Tcl 解释器**：Tkinter 的变量 `.get()`/`.set()` 不是线程安全的。转换前置项在 GUI 主线程快照成 `JobOptions` 后传入工作线程与子进程，工作侧全程不再访问任何 Tk 变量。
- **`pythonw` 兼容**：Windows 下子进程通过 spawn 重新导入 `main.py`，靠顶层的 `if __name__ == "__main__"` 守卫避免递归创建 GUI；同时在模块顶部为无控制台的 `pythonw.exe` 补上 `stdout/stderr` 空设备兜底，避免子进程打印警告时崩溃。
- **防卡死兜底**：占用超过 `TASK_TIMEOUT`（600s）的任务视为子进程卡死，自动换新进程并重派（最多重试 2 次）；状态栏实时显示「内存 x MB / 上限 y MB」。

### 并发资源权衡

并发进程数（默认 `4`）决定「最多几个子进程同时运行」，真正兜住内存的是上面的自监控。PDF 批次每个子进程几乎不带模型内存，只占各自文件大小的解析开销；非 PDF 文件才加载 markitdown 模型基准（约 100–300MB/进程）。两个旋钮（并发数 / 内存上限）配合使用：内存紧就调小并发，或让程序自动降并发并回收。

## 性能基准

用同一份 110MB 的真实 PDF 在单核下对比原生引擎与 markitdown 旧路径（pdfminer+pdfplumber）：

| 引擎 | 耗时 | 峰值内存 |
|------|------|----------|
| markitdown 0.1.5（pdfminer/pdfplumber） | 272 秒 | 4.7 GB |
| **本项目的 PyMuPDF 原生引擎** | **约 5 秒** | **146 MB** |

约 **57 倍更快、32 倍更省内存**。838MB 的 PDF 用原生引擎约 34 秒、峰值 ~680MB。配合下方「常驻受管进程池 + 内存自监控」，几十个超大 PDF 可安全并发批量转换。

## 打包发布（GitHub Actions）

仓库内置 `.github/workflows/release.yml`，用 PyInstaller 在三种系统各自产出**便携版可执行文件**并自动发布到 GitHub Releases：

| 平台 | 产物 |
|------|------|
| Windows x64 | `markitdown-tk-gui-windows-x86_64.exe`（单文件可执行） |
| Linux x64 | `markitdown-tk-gui-linux-x86_64`（单文件可执行） |
| macOS（Apple Silicon） | `markitdown-tk-gui-macos-arm64.app`（.app 目录包） |

### 触发方式

- **推标签自动发布**：`git tag v1.0.0 && git push origin v1.0.0` → 自动构建三个平台产物并以该标签发布 Release（含自动 changelog 与 SHA256 校验和）。
- **手动构建**：Actions 页面 → 「Build and Release」→ «Run workflow»，填 `release_tag` 即构建并发布；留空则只构建不发布。

### 打包要点

- PyInstaller 需打包四个额外的 C 扩展包：`--collect-all magika`（ONNX 模型）、`--collect-all markitdown`（解析器注册入口）、`--collect-all onnxruntime`（运行时）、`--collect-all pymupdf`（PDFium 核心动态库）。
- 打包后每个子进程会重新执行打包后的可执行文件，因此 `main.py` 里已调用 `multiprocessing.freeze_support()`，保证并发转换在冻结环境下照常工作。
- Linux runner 需先 `apt install python3-tk`（GitHub 提供的 Python 构建默认不含 tkinter），并让 uv 优先用系统 Python（`UV_PYTHON_PREFERENCE=system`）。
- macOS 只提供 Apple Silicon 产物：GitHub 已停用 Intel (macos-13) 免费 runner。Intel Mac 如需 exe 请在本地执行同样的 PyInstaller 命令自行构建。

## 项目结构

```
main.py                   GUI、选项界面、受管进程池、调度器
pdf_engine.py             PDF→Markdown 原生转换器（PyMuPDF C 引擎，移植无边框表格启发式）
start.cmd                 启动入口（cmd 会闪一次，之后由 pythonw 运行 GUI）
pyproject.toml            依赖声明（markitdown[all] + pymupdf + psutil）
.github/workflows/        PyInstaller 三平台打包 + 自动发布 Releases
```

## License

GPL-3.0 License (see [LICENSE](LICENSE) for details)。

本项目依赖 PyMuPDF（AGPL-3.0）；组合后整体分发需遵循 AGPL 义务（本仓库已公开，源码可获取，合规无额外操作）。

## FAQ（常见问题）

1. **为什么在 WSL2 的 Debian 上报错 `ImportError: libxcb.so.1: cannot open shared object file: No such file or directory`？**

    这是因为 Tkinter 依赖的 X11 库在 WSL2 中缺失。解决方法：

    ```bash
    sudo apt update && sudo apt install libxcb1
    ```

    不推荐的做法：

    ```bash
    sudo apt update
    sudo apt install libxcb-xinerama0 libx11-6 libxext6 libxrender1 libxrandr2
   ```

   安装后重启 WSL2，再运行 GUI 即可。
