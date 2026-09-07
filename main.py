from __future__ import annotations

import concurrent.futures as cf
import multiprocessing
import os
import queue
import re
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from markitdown import MarkItDown

if sys.stdout is None or sys.stderr is None:
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")

_shared_converter: MarkItDown | None = None


def _get_shared_converter() -> MarkItDown:
    global _shared_converter
    if _shared_converter is None:
        _shared_converter = MarkItDown()
    return _shared_converter


def _convert_source_in_process(source: Path) -> tuple[Path, str]:
    converter = _get_shared_converter()
    result = converter.convert(source)
    markdown = result.markdown or result.text_content or ""
    return source, markdown


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value).strip().strip(".")
    return cleaned or f"markitdown_{timestamp()}"


DEFAULT_WORKERS = max(1, min(4, os.cpu_count() or 2))
WORKER_CHOICES = ("1", "2", "4", "6", "8")


def default_output_name(source: Path | None = None, *, no_timestamp: bool = False) -> str:
    if source is None:
        return f"markitdown_{timestamp()}.md"
    if no_timestamp:
        return f"{sanitize_filename(source.stem)}.md"
    return f"{sanitize_filename(source.stem)}_{timestamp()}.md"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    index = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def open_path(path: Path) -> None:
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except OSError:
        if path.is_dir():
            os.system(f'explorer "{path}"')
        else:
            os.system(f'explorer /select,"{path}"')


@dataclass(slots=True)
class ConversionItem:
    source: Path
    output: Path | None
    success: bool
    error: str | None = None


@dataclass(slots=True)
class JobOptions:
    ordered_sources: list[Path]
    merge: bool
    output_dir: str
    output_name: str
    no_timestamp: bool
    concurrency: int = DEFAULT_WORKERS


def build_output_dir(source: Path, merged: bool, configured: str) -> Path:
    if configured:
        return Path(configured).expanduser().resolve()
    return source.parent


def resolve_output_name(
    source: Path,
    merged: bool,
    configured: str,
    total: int,
    *,
    no_timestamp: bool = False,
) -> str:
    custom = configured.strip()
    if merged or total == 1:
        base = sanitize_filename(
            custom or default_output_name(source, no_timestamp=no_timestamp)
        )
    elif custom:
        base = f"{sanitize_filename(custom)}_{sanitize_filename(source.stem)}"
    else:
        base = default_output_name(source, no_timestamp=no_timestamp)
    if not base.lower().endswith(".md"):
        base = f"{base}.md"
    return base


@dataclass(slots=True)
class ThemePalette:
    window_bg: str
    surface_bg: str
    panel_bg: str
    text_fg: str
    muted_fg: str
    accent: str
    accent_fg: str
    border: str
    preview_bg: str
    preview_fg: str
    select_bg: str
    select_fg: str


LIGHT_THEME = ThemePalette(
    window_bg="#eef2f7",
    surface_bg="#ffffff",
    panel_bg="#f6f8fc",
    text_fg="#0f172a",
    muted_fg="#5b6472",
    accent="#2563eb",
    accent_fg="#ffffff",
    border="#d6dbe5",
    preview_bg="#fbfcfe",
    preview_fg="#0f172a",
    select_bg="#cfe0ff",
    select_fg="#0f172a",
)

DARK_THEME = ThemePalette(
    window_bg="#0f172a",
    surface_bg="#111827",
    panel_bg="#172033",
    text_fg="#e5e7eb",
    muted_fg="#94a3b8",
    accent="#38bdf8",
    accent_fg="#082f49",
    border="#334155",
    preview_bg="#0b1220",
    preview_fg="#e5e7eb",
    select_bg="#1d4ed8",
    select_fg="#f8fafc",
)

SORT_FIELD_LABELS = {
    "name": "文件名",
    "created": "创建日期",
    "modified": "修改日期",
    "type": "文件类型",
}

SORT_FIELD_LOOKUP = {label: key for key, label in SORT_FIELD_LABELS.items()}


class Toast(tk.Toplevel):
    def __init__(
        self, master: tk.Tk, palette: ThemePalette, title: str, message: str
    ) -> None:
        super().__init__(master)
        self._palette = palette
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.0)
        self.configure(bg=palette.panel_bg)

        frame = ttk.Frame(self, padding=(14, 12), style="Surface.TFrame")
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=title, style="ToastTitle.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text=message,
            style="ToastBody.TLabel",
            wraplength=340,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        self.update_idletasks()
        width = max(self.winfo_reqwidth(), 360)
        height = max(self.winfo_reqheight(), 110)
        x = self.winfo_screenwidth() - width - 24
        y = self.winfo_screenheight() - height - 64
        self.geometry(f"{width}x{height}+{max(x, 16)}+{max(y, 16)}")
        self.deiconify()
        self._fade_in()
        self.after(4200, self._fade_out)

    def _fade_in(self) -> None:
        alpha = float(self.attributes("-alpha"))
        alpha = min(alpha + 0.08, 1.0)
        self.attributes("-alpha", alpha)
        if alpha < 1.0:
            self.after(20, self._fade_in)

    def _fade_out(self) -> None:
        try:
            alpha = float(self.attributes("-alpha"))
        except tk.TclError:
            return
        alpha = max(alpha - 0.08, 0.0)
        if alpha <= 0.0:
            self.destroy()
            return
        self.attributes("-alpha", alpha)
        self.after(20, self._fade_out)


class MarkItDownApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MarkItDown Tk GUI")
        self.geometry("1080x780")
        self.minsize(960, 680)

        self.selected_files: list[Path] = []
        self.worker_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker_running = False
        self.theme_var = tk.StringVar(value="dark")
        self.merge_var = tk.BooleanVar(value=False)
        self.merge_order_var = tk.StringVar(value="builtin")
        self.merge_sort_field_var = tk.StringVar(value=SORT_FIELD_LABELS["name"])
        self.merge_sort_direction_var = tk.StringVar(value="正序")
        self.output_dir_var = tk.StringVar(value="")
        self.output_name_var = tk.StringVar(value="")
        self.no_timestamp_var = tk.BooleanVar(value=False)
        self.concurrency_var = tk.StringVar(value=str(DEFAULT_WORKERS))
        self.open_after_var = tk.StringVar(value="none")
        self.status_var = tk.StringVar(value="请选择文件并确认选项后开始转换。")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="0 / 0")

        self.dark_mode_var = tk.BooleanVar(value=True)
        self._palette = DARK_THEME
        self._build_style()
        self._build_ui()
        self._bind_live_updates()
        self._apply_theme()
        self.after(120, self._poll_queue)
        self._refresh_preview()

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("AppTitle.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("SectionTitle.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("ToastTitle.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("ToastBody.TLabel")
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build_ui(self) -> None:
        self.root_frame = ttk.Frame(self, padding=16, style="AppRoot.TFrame")
        self.root_frame.pack(fill="both", expand=True)
        self.root_frame.columnconfigure(0, weight=1)
        self.root_frame.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root_frame, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header, text="MarkItDown 文件转 Markdown", style="AppTitle.TLabel"
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="可一次选择多个任意类型文件，支持深色模式、并发转换和可调整的合并顺序。",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        theme_row = ttk.Frame(header, style="Surface.TFrame")
        theme_row.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Label(theme_row, text="主题", style="SectionTitle.TLabel").pack(
            side="left", padx=(0, 8)
        )
        for text, value in [("深色", "dark"), ("浅色", "light")]:
            ttk.Radiobutton(
                theme_row,
                text=text,
                value=value,
                variable=self.theme_var,
                command=self._on_theme_changed,
            ).pack(side="left", padx=(0, 8))

        body = ttk.Panedwindow(self.root_frame, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew", pady=(14, 0))

        left = ttk.Frame(body, padding=(0, 0, 12, 0), style="Surface.TFrame")
        right = ttk.Frame(body, style="Surface.TFrame")
        body.add(left, weight=3)
        body.add(right, weight=2)

        file_box = ttk.LabelFrame(
            left, text="待转换文件", padding=8, style="Card.TLabelframe"
        )
        file_box.pack(fill="both", expand=True)

        toolbar = ttk.Frame(file_box, style="Surface.TFrame")
        toolbar.pack(fill="x", padx=2, pady=(2, 10))
        ttk.Button(
            toolbar, text="添加文件", command=self.add_files, style="Accent.TButton"
        ).pack(side="left")
        ttk.Button(toolbar, text="移除选中", command=self.remove_selected_files).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(toolbar, text="上移", command=lambda: self.move_selected(-1)).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(toolbar, text="下移", command=lambda: self.move_selected(1)).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(toolbar, text="置顶", command=self.move_selected_to_top).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(toolbar, text="置底", command=self.move_selected_to_bottom).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(
            toolbar, text="按当前规则排序", command=self.sort_files_by_name
        ).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="清空", command=self.clear_files).pack(
            side="left", padx=(8, 0)
        )

        list_frame = ttk.Frame(file_box, style="Surface.TFrame")
        list_frame.pack(fill="both", expand=True, padx=2, pady=(0, 2))
        self.file_list = tk.Listbox(
            list_frame,
            selectmode="extended",
            activestyle="dotbox",
            exportselection=False,
            relief="flat",
            highlightthickness=1,
            borderwidth=0,
        )
        file_scrollbar = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.file_list.yview
        )
        self.file_list.configure(yscrollcommand=file_scrollbar.set)
        self.file_list.pack(side="left", fill="both", expand=True)
        file_scrollbar.pack(side="right", fill="y")

        options_box = ttk.LabelFrame(
            right, text="输出选项", padding=10, style="Card.TLabelframe"
        )
        options_box.pack(fill="x")

        self._add_option_row(options_box, 0, "合并 Markdown", self._build_merge_toggle)
        self._add_option_row(options_box, 1, "合并顺序", self._build_merge_order)
        self._add_option_row(options_box, 2, "目标目录", self._build_target_dir)
        self._add_option_row(options_box, 3, "输出文件名", self._build_output_name)
        self._add_option_row(options_box, 4, "转换后打开", self._build_open_after)
        self._add_option_row(options_box, 5, "并发进程数", self._build_workers)

        action_box = ttk.Frame(right, style="Surface.TFrame")
        action_box.pack(fill="x", pady=(12, 0))
        self.convert_button = ttk.Button(
            action_box,
            text="开始转换",
            command=self.start_conversion,
            style="Accent.TButton",
        )
        self.convert_button.pack(fill="x")

        progress_box = ttk.LabelFrame(
            right, text="进度与状态", padding=10, style="Card.TLabelframe"
        )
        progress_box.pack(fill="x", pady=(12, 0))
        self.progress = ttk.Progressbar(
            progress_box, variable=self.progress_var, maximum=100
        )
        self.progress.pack(fill="x")
        ttk.Label(
            progress_box, textvariable=self.progress_text_var, style="Muted.TLabel"
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(progress_box, textvariable=self.status_var, wraplength=330).pack(
            anchor="w", pady=(4, 0)
        )

        preview_box = ttk.LabelFrame(
            right, text="实时预览", padding=10, style="Card.TLabelframe"
        )
        preview_box.pack(fill="both", expand=True, pady=(12, 0))
        self.preview = tk.Text(
            preview_box,
            height=12,
            wrap="word",
            state="disabled",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
        )
        preview_scrollbar = ttk.Scrollbar(
            preview_box, orient="vertical", command=self.preview.yview
        )
        self.preview.configure(yscrollcommand=preview_scrollbar.set)
        self.preview.pack(side="left", fill="both", expand=True)
        preview_scrollbar.pack(side="right", fill="y")

        log_box = ttk.LabelFrame(
            self.root_frame, text="日志", padding=10, style="Card.TLabelframe"
        )
        log_box.grid(row=2, column=0, sticky="nsew", pady=(14, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log = tk.Text(
            log_box,
            height=8,
            wrap="word",
            state="disabled",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
        )
        log_scrollbar = ttk.Scrollbar(
            log_box, orient="vertical", command=self.log.yview
        )
        self.log.configure(yscrollcommand=log_scrollbar.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))

    def _add_option_row(
        self, parent: ttk.LabelFrame, row: int, label: str, builder
    ) -> None:
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=8
        )
        holder = ttk.Frame(parent, style="Surface.TFrame")
        holder.grid(row=row, column=1, sticky="ew", pady=8)
        builder(holder)

    def _build_merge_toggle(self, parent: ttk.Frame) -> None:
        ttk.Checkbutton(
            parent,
            text="将所有结果合并为一个 Markdown 文件",
            variable=self.merge_var,
            command=self._refresh_preview,
        ).pack(anchor="w")

    def _build_merge_order(self, parent: ttk.Frame) -> None:
        self.sort_mode_row = ttk.Frame(parent, style="Surface.TFrame")
        self.sort_mode_row.pack(fill="x")
        for text, value in [("内置规则", "builtin"), ("自定义顺序", "custom")]:
            ttk.Radiobutton(
                self.sort_mode_row,
                text=text,
                value=value,
                variable=self.merge_order_var,
                command=self._on_merge_order_changed,
            ).pack(side="left", padx=(0, 12))

        self.sort_rule_row = ttk.Frame(parent, style="Surface.TFrame")
        self.sort_rule_row.pack(fill="x", pady=(8, 0))
        ttk.Label(self.sort_rule_row, text="排序字段", style="Muted.TLabel").pack(
            side="left", padx=(0, 8)
        )
        self.merge_sort_field_combo = ttk.Combobox(
            self.sort_rule_row,
            textvariable=self.merge_sort_field_var,
            values=list(SORT_FIELD_LABELS.values()),
            state="readonly",
            width=10,
        )
        self.merge_sort_field_combo.pack(side="left")
        self.merge_sort_field_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._on_builtin_sort_rule_changed()
        )
        ttk.Label(self.sort_rule_row, text="方向", style="Muted.TLabel").pack(
            side="left", padx=(12, 8)
        )
        self.merge_sort_direction_combo = ttk.Combobox(
            self.sort_rule_row,
            textvariable=self.merge_sort_direction_var,
            values=["正序", "倒序"],
            state="readonly",
            width=8,
        )
        self.merge_sort_direction_combo.pack(side="left")
        self.merge_sort_direction_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._on_builtin_sort_rule_changed()
        )
        ttk.Label(
            parent,
            text="内置规则支持文件名/创建日期/修改日期/文件类型，并可切换正序或倒序；自定义顺序时可用上移/下移/置顶/置底微调。",
            style="Muted.TLabel",
            wraplength=320,
        ).pack(anchor="w", pady=(4, 0))

    def _on_merge_order_changed(self) -> None:
        if self.merge_order_var.get() == "builtin":
            self._apply_builtin_sort()
            self._append_log(f"合并顺序已切换为{self._order_description()}。")
        else:
            self._append_log("合并顺序已切换为自定义顺序。")
        self._refresh_preview()

    def _on_builtin_sort_rule_changed(self) -> None:
        if self.merge_order_var.get() == "builtin":
            self._apply_builtin_sort()
            self._append_log(f"已切换为{self._order_description()}。")
        self._refresh_preview()

    def _build_dark_mode_toggle(self, parent: ttk.Frame) -> None:
        ttk.Checkbutton(
            parent,
            text="启用深色模式",
            variable=self.dark_mode_var,
            command=self.toggle_theme,
        ).pack(anchor="w")

    def toggle_theme(self) -> None:
        self.theme_var.set("dark" if self.dark_mode_var.get() else "light")
        self._apply_theme()

    def _sync_theme_checkbox(self) -> None:
        self.dark_mode_var.set(self.theme_var.get() == "dark")

    def _build_target_dir(self, parent: ttk.Frame) -> None:
        row = ttk.Frame(parent, style="Surface.TFrame")
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.output_dir_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(row, text="浏览", command=self.choose_target_dir).pack(
            side="left", padx=(8, 0)
        )
        ttk.Label(parent, text="留空则使用源文件所在目录", style="Muted.TLabel").pack(
            anchor="w", pady=(4, 0)
        )

    def _build_output_name(self, parent: ttk.Frame) -> None:
        ttk.Entry(parent, textvariable=self.output_name_var).pack(fill="x")
        ttk.Checkbutton(
            parent,
            text="默认文件名不加时间戳（仅影响留空时的自动命名）",
            variable=self.no_timestamp_var,
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            parent,
            text="留空时会自动使用源文件名 + 时间戳的 .md 文件名。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 0))

    def _build_open_after(self, parent: ttk.Frame) -> None:
        choices = ttk.Frame(parent, style="Surface.TFrame")
        choices.pack(fill="x")
        for text, value in [
            ("不打开", "none"),
            ("打开文件", "file"),
            ("打开所在文件夹", "folder"),
        ]:
            ttk.Radiobutton(
                choices, text=text, value=value, variable=self.open_after_var
            ).pack(side="left", padx=(0, 12))

    def _build_workers(self, parent: ttk.Frame) -> None:
        ttk.Combobox(
            parent,
            textvariable=self.concurrency_var,
            values=WORKER_CHOICES,
            state="readonly",
            width=6,
        ).pack(anchor="w")
        ttk.Label(
            parent,
            text="每批同时转换的文件数。内存占用 ≈ 并发数 × 单文件峰值；文件很大或内存紧张时请调小。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 0))

    def _bind_live_updates(self) -> None:
        for variable in (
            self.merge_var,
            self.merge_order_var,
            self.merge_sort_field_var,
            self.merge_sort_direction_var,
            self.output_dir_var,
            self.output_name_var,
            self.no_timestamp_var,
            self.open_after_var,
        ):
            variable.trace_add("write", lambda *_: self._refresh_preview())

    def _palette_for_theme(self, theme_name: str) -> ThemePalette:
        return DARK_THEME if theme_name == "dark" else LIGHT_THEME

    def _apply_theme(self) -> None:
        self._palette = self._palette_for_theme(self.theme_var.get())
        palette = self._palette
        style = ttk.Style(self)

        self.configure(bg=palette.window_bg)
        self.root_frame.configure(style="AppRoot.TFrame")

        style.configure("AppRoot.TFrame", background=palette.window_bg)
        style.configure("Surface.TFrame", background=palette.window_bg)
        style.configure(
            "Card.TLabelframe",
            background=palette.surface_bg,
            foreground=palette.text_fg,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=palette.surface_bg,
            foreground=palette.text_fg,
        )
        style.configure("TFrame", background=palette.window_bg)
        style.configure(
            "TLabel", background=palette.window_bg, foreground=palette.text_fg
        )
        style.configure(
            "Muted.TLabel", background=palette.window_bg, foreground=palette.muted_fg
        )
        style.configure(
            "TLabelframe", background=palette.surface_bg, foreground=palette.text_fg
        )
        style.configure(
            "TLabelframe.Label",
            background=palette.surface_bg,
            foreground=palette.text_fg,
        )
        style.configure("TButton", padding=(10, 5))
        style.configure(
            "Accent.TButton",
            background=palette.accent,
            foreground=palette.accent_fg,
            padding=(12, 6),
        )
        style.map(
            "Accent.TButton",
            background=[("active", palette.accent)],
            foreground=[("active", palette.accent_fg)],
        )
        style.configure(
            "TCheckbutton", background=palette.surface_bg, foreground=palette.text_fg
        )
        style.configure(
            "TRadiobutton", background=palette.surface_bg, foreground=palette.text_fg
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor=palette.border,
            background=palette.accent,
        )
        style.configure(
            "ToastTitle.TLabel", background=palette.panel_bg, foreground=palette.text_fg
        )
        style.configure(
            "ToastBody.TLabel", background=palette.panel_bg, foreground=palette.text_fg
        )

        self.file_list.configure(
            bg=palette.surface_bg,
            fg=palette.preview_fg,
            selectbackground=palette.select_bg,
            selectforeground=palette.select_fg,
            highlightbackground=palette.border,
            highlightcolor=palette.accent,
            activestyle="dotbox",
        )
        for widget in (self.preview, self.log):
            widget.configure(
                bg=palette.preview_bg,
                fg=palette.preview_fg,
                insertbackground=palette.preview_fg,
                selectbackground=palette.select_bg,
                selectforeground=palette.select_fg,
                highlightbackground=palette.border,
                highlightcolor=palette.accent,
            )
        self._refresh_preview()

    def _on_theme_changed(self) -> None:
        self._sync_theme_checkbox()
        self._apply_theme()

    def _write_text(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{now}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh_file_list(self) -> None:
        self.file_list.delete(0, "end")
        for path in self.selected_files:
            self.file_list.insert("end", str(path))
        self._refresh_preview()

    def _sort_key_components(self, path: Path) -> tuple[object, ...]:
        stat = path.stat()
        field = self.merge_sort_field_var.get()
        if field == SORT_FIELD_LABELS["created"]:
            value: object = stat.st_ctime
        elif field == SORT_FIELD_LABELS["modified"]:
            value = stat.st_mtime
        elif field == SORT_FIELD_LABELS["type"]:
            value = path.suffix.lower()
        else:
            value = path.name.lower()
        return (value, path.name.lower(), str(path).lower())

    def _sorted_files(self) -> list[Path]:
        reverse = self.merge_sort_direction_var.get() == "倒序"
        return sorted(
            self.selected_files, key=self._sort_key_components, reverse=reverse
        )

    def _apply_builtin_sort(self) -> None:
        self.selected_files = self._sorted_files()
        self._refresh_file_list()

    def _order_description(self) -> str:
        if self.merge_order_var.get() != "builtin":
            return "自定义顺序"
        field = self.merge_sort_field_var.get()
        direction = self.merge_sort_direction_var.get()
        return f"{field}{direction}"

    def _selected_indices(self) -> list[int]:
        return [int(index) for index in self.file_list.curselection()]

    def sort_files_by_name(self) -> None:
        if not self.selected_files:
            return
        if self.merge_order_var.get() != "builtin":
            self.merge_order_var.set("builtin")
        selected_names = {
            self.selected_files[index] for index in self._selected_indices()
        }
        self._apply_builtin_sort()
        self._refresh_file_list()
        self._restore_selection(selected_names)
        self.status_var.set(f"已按{self._order_description()}排序。")
        self._append_log(f"已按{self._order_description()}排序。")

    def _restore_selection(self, selected_paths: set[Path]) -> None:
        self.file_list.selection_clear(0, "end")
        for index, path in enumerate(self.selected_files):
            if path in selected_paths:
                self.file_list.selection_set(index)

    def move_selected(self, delta: int) -> None:
        indices = self._selected_indices()
        if not indices:
            return

        if self.merge_order_var.get() != "custom":
            self.merge_order_var.set("custom")
            self._append_log("已切换为自定义顺序以进行手动调整。")

        if delta < 0:
            for index in indices:
                if index <= 0:
                    continue
                self.selected_files[index - 1], self.selected_files[index] = (
                    self.selected_files[index],
                    self.selected_files[index - 1],
                )
        else:
            for index in reversed(indices):
                if index >= len(self.selected_files) - 1:
                    continue
                self.selected_files[index + 1], self.selected_files[index] = (
                    self.selected_files[index],
                    self.selected_files[index + 1],
                )

        selected_paths = {
            self.selected_files[index]
            for index in indices
            if 0 <= index < len(self.selected_files)
        }
        self._refresh_file_list()
        self._restore_selection(selected_paths)
        self.status_var.set("已调整合并顺序。")

    def move_selected_to_top(self) -> None:
        indices = self._selected_indices()
        if not indices:
            return
        if self.merge_order_var.get() != "custom":
            self.merge_order_var.set("custom")
            self._append_log("已切换为自定义顺序以进行手动调整。")
        selected_paths = [self.selected_files[index] for index in indices]
        remaining_paths = [
            path
            for index, path in enumerate(self.selected_files)
            if index not in set(indices)
        ]
        self.selected_files = selected_paths + remaining_paths
        self._refresh_file_list()
        self._restore_selection(set(selected_paths))
        self._refresh_preview()
        self._append_log("已将所选文件置顶。")

    def move_selected_to_bottom(self) -> None:
        indices = self._selected_indices()
        if not indices:
            return
        if self.merge_order_var.get() != "custom":
            self.merge_order_var.set("custom")
            self._append_log("已切换为自定义顺序以进行手动调整。")
        selected_paths = [self.selected_files[index] for index in indices]
        remaining_paths = [
            path
            for index, path in enumerate(self.selected_files)
            if index not in set(indices)
        ]
        self.selected_files = remaining_paths + selected_paths
        start_index = len(remaining_paths)
        self._refresh_file_list()
        self._reselect_range(start_index, len(selected_paths))
        self._refresh_preview()
        self._append_log("已将所选文件置底。")

    def _reselect_range(self, start_index: int, length: int) -> None:
        self.file_list.selection_clear(0, "end")
        for index in range(start_index, start_index + length):
            self.file_list.selection_set(index)
        self._append_log("已调整合并顺序。")

    def _merge_ordered_files(self) -> list[Path]:
        if self.merge_order_var.get() == "name":
            return self._sorted_files()
        return list(self.selected_files)

    def _refresh_preview(self) -> None:
        files = self.selected_files
        target_dir = self.output_dir_var.get().strip() or "源文件所在目录"
        preview_source = self._merge_ordered_files()[0] if files else None
        output_name = self.output_name_var.get().strip() or (
            default_output_name(preview_source, no_timestamp=self.no_timestamp_var.get())
            if preview_source
            else ("源文件名.md" if self.no_timestamp_var.get() else "源文件名_时间戳.md")
        )
        mode_text = (
            "合并成单个 Markdown"
            if self.merge_var.get()
            else "为每个文件分别生成 Markdown"
        )
        open_text = {"none": "不打开", "file": "打开文件", "folder": "打开文件夹"}.get(
            self.open_after_var.get(), "不打开"
        )
        order_text = (
            self._order_description()
            if self.merge_order_var.get() == "builtin"
            else "自定义顺序"
        )

        lines = [
            f"文件数量: {len(files)}",
            f"转换模式: {mode_text}",
            f"合并顺序: {order_text}",
            f"目标目录: {target_dir}",
            f"输出文件名: {output_name}",
            f"转换后打开: {open_text}",
            "",
            "状态: 已等待确认，调整好选项后点击开始转换。",
        ]
        if self.merge_var.get() and files:
            lines.extend(["", "合并预览顺序:"])
            for path in self._merge_ordered_files()[:8]:
                lines.append(f"- {path.name}")
            if len(files) > 8:
                lines.append(f"- ... 还有 {len(files) - 8} 个文件")
        elif files:
            lines.extend(["", "文件列表:"])
            for path in files[:8]:
                lines.append(f"- {path}")
            if len(files) > 8:
                lines.append(f"- ... 还有 {len(files) - 8} 个文件")

        self._write_text(self.preview, "\n".join(lines))
        self.progress_text_var.set(f"{self._completed_count()} / {max(len(files), 0)}")

    def _completed_count(self) -> int:
        return len([p for p in self.selected_files if p.exists()])

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择文件", filetypes=[("所有文件", "*.*")], parent=self
        )
        if not paths:
            return

        existing = {path.resolve() for path in self.selected_files}
        added = 0
        for raw in paths:
            path = Path(raw).resolve()
            if path in existing:
                continue
            self.selected_files.append(path)
            existing.add(path)
            added += 1

        if self.merge_order_var.get() == "builtin":
            self._apply_builtin_sort()

        self._refresh_file_list()
        self.status_var.set(f"已添加 {added} 个文件，等待开始转换。")
        self._append_log(f"已选择 {added} 个文件。")

    def remove_selected_files(self) -> None:
        selected = list(self.file_list.curselection())
        if not selected:
            return

        for index in reversed(selected):
            del self.selected_files[index]

        self._refresh_file_list()
        self.status_var.set(f"已移除所选文件，剩余 {len(self.selected_files)} 个。")
        self._append_log("已移除所选文件。")

    def clear_files(self) -> None:
        if not self.selected_files:
            return
        self.selected_files.clear()
        self._refresh_file_list()
        self.status_var.set("文件列表已清空。")
        self._append_log("文件列表已清空。")

    def choose_target_dir(self) -> None:
        directory = filedialog.askdirectory(title="选择目标目录", parent=self)
        if directory:
            self.output_dir_var.set(directory)

    def _build_confirmation_message(self) -> str:
        mode = (
            "合并为一个 Markdown 文件"
            if self.merge_var.get()
            else "分别生成多个 Markdown 文件"
        )
        target_dir = self.output_dir_var.get().strip() or "源文件所在目录"
        preview_source = self._merge_ordered_files()[0] if self.selected_files else None
        output_name = self.output_name_var.get().strip() or (
            default_output_name(preview_source, no_timestamp=self.no_timestamp_var.get())
            if preview_source is not None
            else ("源文件名.md" if self.no_timestamp_var.get() else "源文件名_时间戳.md")
        )
        order_text = (
            self._order_description()
            if self.merge_order_var.get() == "builtin"
            else "自定义顺序"
        )
        return (
            f"即将转换 {len(self.selected_files)} 个文件。\n\n"
            f"模式: {mode}\n"
            f"合并顺序: {order_text if self.merge_var.get() else 'none'}\n"
            f"目标目录: {target_dir}\n"
            f"输出文件名: {output_name}\n"
            f"转换后打开: {self.open_after_var.get()}\n\n"
            "是否继续？"
        )

    def start_conversion(self) -> None:
        if self.worker_running:
            return
        if not self.selected_files:
            messagebox.showwarning("提示", "请先添加至少一个文件。", parent=self)
            return
        if not messagebox.askyesno(
            "确认转换", self._build_confirmation_message(), parent=self
        ):
            self.status_var.set("用户取消了转换。")
            self._append_log("用户取消了转换。")
            return

        self.worker_running = True
        self.convert_button.configure(state="disabled")
        self._set_input_state("disabled")
        self.progress_var.set(0.0)
        self.progress_text_var.set(f"0 / {len(self.selected_files)}")
        self.status_var.set("正在准备转换任务...")
        self._append_log("开始转换。")

        options = JobOptions(
            ordered_sources=(
                self._merge_ordered_files()
                if self.merge_var.get()
                else list(self.selected_files)
            ),
            merge=self.merge_var.get(),
            output_dir=self.output_dir_var.get().strip(),
            output_name=self.output_name_var.get().strip(),
            no_timestamp=self.no_timestamp_var.get(),
            concurrency=self._selected_workers(),
        )

        thread = threading.Thread(target=self._worker, args=(options,), daemon=True)
        thread.start()

    def _set_input_state(self, state: str) -> None:
        self._toggle_widget_state(self.file_list, state)
        for child in self.winfo_children():
            self._toggle_widget_state(
                child, state, exclude={self.convert_button, self.file_list}
            )

    def _toggle_widget_state(
        self, widget: tk.Widget, state: str, exclude: set[tk.Widget] | None = None
    ) -> None:
        if exclude and widget in exclude:
            return
        try:
            if isinstance(
                widget,
                (ttk.Button, ttk.Checkbutton, ttk.Radiobutton, ttk.Entry, tk.Listbox),
            ):
                widget.configure(state=state)
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._toggle_widget_state(child, state, exclude)

    def _selected_workers(self) -> int:
        try:
            return max(1, int(self.concurrency_var.get()))
        except (ValueError, tk.TclError):
            return DEFAULT_WORKERS

    def _worker(self, options: JobOptions) -> None:
        try:
            if options.merge:
                items = self._convert_merged(options)
            else:
                items = self._convert_separate(options)
            self.worker_queue.put(("done", items))
        except Exception as exc:  # noqa: BLE001
            self.worker_queue.put(("fatal", exc))

    def _convert_separate(self, options: JobOptions) -> list[ConversionItem]:
        ordered_sources = options.ordered_sources
        total = len(ordered_sources)
        items: list[ConversionItem] = []
        if total == 0:
            return items

        max_workers = max(1, min(options.concurrency, total))
        with cf.ProcessPoolExecutor(max_workers=max_workers) as executor:
            future_info: dict[cf.Future, tuple[Path, Path]] = {}
            for source in ordered_sources:
                output_dir = build_output_dir(source, False, options.output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)
                output_path = unique_path(
                    output_dir
                    / resolve_output_name(
                        source,
                        False,
                        options.output_name,
                        total,
                        no_timestamp=options.no_timestamp,
                    )
                )
                future = executor.submit(_convert_source_in_process, source)
                future_info[future] = (source, output_path)

            completed = 0
            for future in cf.as_completed(future_info):
                source, output_path = future_info[future]
                completed += 1
                self.worker_queue.put(("status", f"正在转换第 {completed}/{total} 个文件"))
                try:
                    _, markdown = future.result()
                    output_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
                    items.append(
                        ConversionItem(source=source, output=output_path, success=True)
                    )
                    self.worker_queue.put(("log", f"已生成: {output_path}"))
                except Exception as exc:  # noqa: BLE001
                    items.append(
                        ConversionItem(
                            source=source, output=None, success=False, error=str(exc)
                        )
                    )
                    self.worker_queue.put(("log", f"转换失败: {source} -> {exc}"))

                self.worker_queue.put(("progress", completed / total * 100))
                self.worker_queue.put(("progress_text", f"{completed} / {total}"))

        return items

    def _convert_merged(self, options: JobOptions) -> list[ConversionItem]:
        ordered_sources = options.ordered_sources
        total = len(ordered_sources)
        items: list[ConversionItem] = []
        if total == 0:
            return items

        output_dir = build_output_dir(ordered_sources[0], True, options.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = unique_path(
            output_dir
            / resolve_output_name(
                ordered_sources[0],
                True,
                options.output_name,
                total,
                no_timestamp=options.no_timestamp,
            )
        )
        results: dict[Path, tuple[bool, str | None]] = {}

        max_workers = max(1, min(options.concurrency, total))
        with cf.ProcessPoolExecutor(max_workers=max_workers) as executor:
            future_to_source = {
                executor.submit(_convert_source_in_process, source): source
                for source in ordered_sources
            }
            completed = 0
            for future in cf.as_completed(future_to_source):
                source = future_to_source[future]
                completed += 1  # ruff: ignore[enumerate-for-loop]
                self.worker_queue.put(("status", f"正在转换 {source.name}"))
                try:
                    _, markdown = future.result()
                    results[source] = (True, markdown)
                    items.append(
                        ConversionItem(source=source, output=output_path, success=True)
                    )
                    self.worker_queue.put(("log", f"已转换: {source}"))
                except Exception as exc:  # noqa: BLE001
                    results[source] = (False, str(exc))
                    items.append(
                        ConversionItem(
                            source=source,
                            output=output_path,
                            success=False,
                            error=str(exc),
                        )
                    )
                    self.worker_queue.put(("log", f"转换失败: {source} -> {exc}"))

                self.worker_queue.put(("progress", completed / total * 100))
                self.worker_queue.put(("progress_text", f"{completed} / {total}"))

        merged_parts: list[str] = []
        for source in ordered_sources:
            success, payload = results.get(source, (False, "未获取到转换结果"))
            if success:
                merged_parts.append(
                    f"# {source.name}\n\n{(payload or '').strip()}".rstrip()
                )
            else:
                merged_parts.append(f"# {source.name}\n\n> 转换失败: {payload}")

        merged_markdown = "\n\n---\n\n".join(merged_parts).rstrip() + "\n"
        output_path.write_text(merged_markdown, encoding="utf-8")
        self.worker_queue.put(("log", f"已生成合并文件: {output_path}"))
        return [
            ConversionItem(
                source=item.source,
                output=output_path,
                success=item.success,
                error=item.error,
            )
            for item in items
        ]

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    self.progress_var.set(float(payload))
                elif kind == "progress_text":
                    self.progress_text_var.set(str(payload))
                elif kind == "log":
                    self._append_log(str(payload))
                elif kind == "done":
                    self._handle_done(payload)
                elif kind == "fatal":
                    self._handle_fatal(payload)
        except queue.Empty:
            pass
        finally:
            self.after(120, self._poll_queue)

    def _handle_done(self, items: list[ConversionItem]) -> None:
        self.worker_running = False
        self.convert_button.configure(state="normal")
        self._set_input_state("normal")

        success_count = sum(1 for item in items if item.success)
        fail_count = len(items) - success_count
        outputs = [item.output for item in items if item.output is not None]

        if self.open_after_var.get() == "file" and outputs:
            open_path(outputs[0])
        elif self.open_after_var.get() == "folder" and outputs:
            open_path(outputs[0].parent)

        self.status_var.set(
            f"转换完成：成功 {success_count} 个，失败 {fail_count} 个。"
        )
        self.progress_var.set(100.0)
        self.progress_text_var.set(f"{len(items)} / {len(self.selected_files)}")
        self._append_log(f"任务完成。成功 {success_count} 个，失败 {fail_count} 个。")

        if fail_count == 0:
            self._show_toast(
                "转换完成", f"任务已完成，成功输出 {success_count} 个 Markdown 文件。"
            )
        else:
            self._show_toast(
                "转换结束",
                f"任务已完成，成功 {success_count} 个，失败 {fail_count} 个。",
            )

    def _handle_fatal(self, exc: Exception) -> None:
        self.worker_running = False
        self.convert_button.configure(state="normal")
        self._set_input_state("normal")
        self.status_var.set(f"转换失败：{exc}")
        self._append_log(f"严重错误: {exc}")
        messagebox.showerror("转换失败", str(exc), parent=self)
        self._show_toast("转换失败", str(exc))

    def _show_toast(self, title: str, message: str) -> None:
        try:
            Toast(self, self._palette, title, message)
        except tk.TclError:
            pass


def main() -> None:
    multiprocessing.freeze_support()
    app = MarkItDownApp()
    app.mainloop()


if __name__ == "__main__":
    main()
