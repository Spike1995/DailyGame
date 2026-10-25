"""Tkinter 前端：三款游戏一键日常图形界面。

状态显示、按钮规则、事件归并与关闭决策都在 daily_games.ui_components，
本模块只负责 Tk 控件与主线程调度。不要在导入本模块时创建 Tk
根窗口；入口为 run_gui(config_path)。
"""
from __future__ import annotations

import json
import os
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox, ttk

from daily_games.ui_components import (
    CLOSE_CANCEL,
    CLOSE_KEEP,
    CLOSE_STOP_THEN_EXIT,
    GAME_NAMES,
    GAME_ORDER,
    ATTENTION_STATES,
    UiController,
    card_elapsed_text,
    control_states,
    format_backup,
    format_change,
    format_check_result,
    format_event,
    state_label,
)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "user.json"
ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "game_icons"
POLL_INTERVAL_MS = 100
STOP_EXIT_TIMEOUT_MS = 30_000
AUTO_EXIT_SUCCESS_DELAY_MS = 3_000


def resolve_log_directories(
    config_path: str | os.PathLike[str],
    *,
    program_data: str | os.PathLike[str] | None = None,
) -> tuple[Path, ...]:
    """返回一般运行日志与已安装的受保护启动器日志目录。"""
    config_file = Path(config_path).resolve()
    default_logs = config_file.parent.parent / "logs"
    primary = default_logs
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
        paths = config.get("paths", {}) if isinstance(config, dict) else {}
        configured = paths.get("logs_dir") if isinstance(paths, dict) else None
        if isinstance(configured, str) and configured.strip():
            primary = Path(configured)
            if not primary.is_absolute():
                primary = config_file.parent.parent / primary
            primary = primary.resolve()
    except (OSError, UnicodeError, ValueError, TypeError):
        primary = default_logs

    directories = [primary]
    common_data = program_data
    if common_data is None:
        common_data = os.environ.get("ProgramData")
    if common_data:
        protected_logs = Path(common_data) / "DailyGames" / "logs"
        if protected_logs.is_dir():
            directories.append(protected_logs)

    unique: list[Path] = []
    seen: set[str] = set()
    for directory in directories:
        key = os.path.normcase(os.path.abspath(directory))
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return tuple(unique)


# 视觉令牌集中定义，避免主界面和弹窗各自出现近似但不一致的颜色。
COLORS = {
    "background": "#0A0D13",
    "content_background": "#0F141F",
    "surface": "#161D2C",
    "surface_alt": "#1E2637",
    "surface_hover": "#28324A",
    "log_background": "#0B0F18",
    "border": "#2B3549",
    "border_soft": "#20293A",
    "text": "#F0F4FA",
    "text_muted": "#97A3B6",
    "text_subtle": "#626D81",
    "primary": "#5E9BFF",
    "primary_hover": "#7FB2FF",
    "primary_pressed": "#4A85E8",
    "on_primary": "#0A1424",
    "success": "#3ED3A0",
    "warning": "#F4C763",
    "danger": "#F27E7E",
    "disabled": "#606B80",
}

GAME_APPEARANCE = {
    "arknights": {
        "index": "01",
        "latin_name": "ARKNIGHTS",
        "short_name": "明日方舟",
        "helper": "MAA",
        "accent": "#58C7E8",
        "icon": "arknights.png",
        "initials": "AK",
    },
    "endfield": {
        "index": "02",
        "latin_name": "ENDFIELD",
        "short_name": "终末地",
        "helper": "MaaEnd",
        "accent": "#E5C35B",
        "icon": "endfield.png",
        "initials": "EF",
    },
    "limbus": {
        "index": "03",
        "latin_name": "LIMBUS",
        "short_name": "边狱巴士",
        "helper": "AALC",
        "accent": "#E78150",
        "icon": "limbus.png",
        "initials": "LC",
    },
}

STATE_COLORS = {
    "idle": COLORS["text_subtle"],
    "checking": COLORS["primary"],
    "starting_game": COLORS["primary"],
    "waiting_game": COLORS["warning"],
    "starting_helper": COLORS["primary"],
    "running": "#8FA9FF",
    "cleaning": "#B99AF7",
    "completed": COLORS["success"],
    "failed": COLORS["danger"],
    "paused": COLORS["warning"],
    "skipped": COLORS["text_muted"],
    "stopping": COLORS["warning"],
    "stopped": COLORS["text_muted"],
}


# ---------------------------------------------------------------------------
# 自定义控件：纯 Tk 实现，不引入第三方主题包
# ---------------------------------------------------------------------------

def _parent_background(widget):
    """读取父控件底色；ttk 控件或读取失败时回退到主背景色。"""
    try:
        value = widget.cget("background")
    except tk.TclError:
        value = ""
    return value or COLORS["background"]


def _hex_to_rgb(color):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def _hex_mix(color_a, color_b, ratio):
    """把 color_a 按 ratio 混向 color_b（0 = 纯 a，1 = 纯 b）。"""
    red_a, green_a, blue_a = _hex_to_rgb(color_a)
    red_b, green_b, blue_b = _hex_to_rgb(color_b)
    return "#{:02X}{:02X}{:02X}".format(
        round(red_a + (red_b - red_a) * ratio),
        round(green_a + (green_b - green_a) * ratio),
        round(blue_a + (blue_b - blue_a) * ratio),
    )


def _rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    """在 Canvas 上绘制圆角矩形并返回 item id。"""
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    if radius <= 0:
        return canvas.create_rectangle(x1, y1, x2, y2, **kwargs)
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class RoundedPanel(tk.Canvas):
    """圆角面板容器。

    Canvas 负责绘制圆角底板与描边，实际内容放入 ``content`` 帧；
    面板尺寸随内容自适应，被 grid / pack 拉伸时底板自动重绘。
    """

    def __init__(self, parent, *, radius=14, background, border=None,
                 padding=0, parent_background=None, decorate=None):
        super().__init__(
            parent, highlightthickness=0, bd=0,
            background=parent_background or _parent_background(parent),
        )
        self._radius = radius
        self._fill = background
        self._border = border
        self._padding = padding
        self._fitting = False
        self._decorate = decorate
        self.content = tk.Frame(self, background=background)
        self._content_window = self.create_window(
            padding, padding, anchor="nw", window=self.content,
        )
        self.content.bind("<Configure>", self._fit_content)
        self.bind("<Configure>", self._redraw)

    def set_border(self, color):
        """切换描边颜色（例如按卡片状态描边）；无变化时不重绘。"""
        if color != self._border:
            self._border = color
            self._redraw()

    def fit(self):
        """按内容需求钉住请求尺寸。

        Canvas 未显式设置宽高时请求尺寸会退化为内容项的包围盒，
        进而把拉伸后的实际尺寸当成新的请求尺寸不断放大；内容填充完
        成后必须调用本方法钉住请求尺寸。"""
        self._fit_content()

    def _fit_content(self, _event=None):
        # 请求尺寸始终跟随内容需求（可增可减）；pack/grid 拉伸只影响
        # 实际分配尺寸，不反过来抬高请求尺寸，避免与布局管理器互相拉扯。
        if self._fitting:
            return
        width = self.content.winfo_reqwidth() + 2 * self._padding
        height = self.content.winfo_reqheight() + 2 * self._padding
        if (width, height) != (self.winfo_reqwidth(), self.winfo_reqheight()):
            self._fitting = True
            try:
                self.configure(width=width, height=height)
            finally:
                self._fitting = False

    def _redraw(self, _event=None):
        width = max(self.winfo_width(), 1)
        height = max(self.winfo_height(), 1)
        self.coords(self._content_window, self._padding, self._padding)
        self.itemconfigure(
            self._content_window,
            width=max(width - 2 * self._padding, 1),
            height=max(height - 2 * self._padding, 1),
        )
        self.delete("panel")
        if self._border:
            _rounded_rect(
                self, 1, 1, width - 2, height - 2, self._radius,
                fill=self._fill, outline=self._border, width=1,
                tags="panel",
            )
        else:
            _rounded_rect(
                self, 0, 0, width - 1, height - 1, self._radius,
                fill=self._fill, outline="", tags="panel",
            )
        if self._decorate is not None:
            self._decorate(self, width, height)
        self.tag_lower("panel")


_BUTTON_VARIANTS = {
    "primary": {
        "background": COLORS["primary"],
        "foreground": COLORS["on_primary"],
        "hover_background": COLORS["primary_hover"],
        "pressed_background": COLORS["primary_pressed"],
        "disabled_background": COLORS["border_soft"],
        "disabled_foreground": COLORS["disabled"],
    },
    "secondary": {
        "background": COLORS["surface_alt"],
        "foreground": COLORS["text"],
        "hover_background": COLORS["surface_hover"],
        "pressed_background": COLORS["border"],
        "disabled_background": COLORS["border_soft"],
        "disabled_foreground": COLORS["disabled"],
    },
    "danger": {
        "background": "#402227",
        "foreground": "#FFA8A8",
        "hover_background": "#52292F",
        "pressed_background": "#331B20",
        "disabled_background": COLORS["border_soft"],
        "disabled_foreground": COLORS["disabled"],
    },
    "ghost": {
        "background": None,
        "foreground": COLORS["text_muted"],
        "hover_background": COLORS["surface_alt"],
        "hover_foreground": COLORS["text"],
        "pressed_background": COLORS["border_soft"],
        "disabled_background": None,
        "disabled_foreground": COLORS["disabled"],
    },
    "card": {
        "background": COLORS["surface_alt"],
        "foreground": COLORS["text_muted"],
        "hover_background": COLORS["surface_hover"],
        "hover_foreground": COLORS["text"],
        "pressed_background": COLORS["border"],
        "disabled_background": COLORS["border_soft"],
        "disabled_foreground": COLORS["disabled"],
    },
}


class FlatButton(tk.Canvas):
    """圆角扁平按钮，支持悬停 / 按下 / 禁用状态。

    提供与 ttk 兼容的 ``state(["disabled"] / ["!disabled"])`` 接口，
    以沿用既有的控件启用逻辑。``background=None`` 的变体底色跟随父级，
    呈现透明（幽灵）效果。
    """

    def __init__(self, parent, text, command=None, *, variant="secondary",
                 parent_background=None, font=("Microsoft YaHei UI", 10, "bold"),
                 padx=18, height=36, radius=10):
        self._variant = _BUTTON_VARIANTS[variant]
        self._command = command
        self._font = font
        self._text = text
        self._padx = padx
        self._radius = radius
        self._enabled = True
        self._hover = False
        self._pressed = False
        text_width = tkfont.Font(font=font).measure(text)
        super().__init__(
            parent, width=text_width + 2 * padx, height=height,
            highlightthickness=0, bd=0,
            background=parent_background or _parent_background(parent),
            cursor="hand2",
        )
        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def state(self, states):
        """ttk 兼容接口：仅识别 disabled / !disabled。"""
        if isinstance(states, str):
            states = (states,)
        for item in states:
            if item == "disabled":
                self._set_enabled(False)
            elif item == "!disabled":
                self._set_enabled(True)

    def invoke(self):
        if self._enabled and self._command is not None:
            return self._command()
        return None

    def _set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled != self._enabled:
            self._enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
            self._redraw()

    def _colors(self):
        if not self._enabled:
            fill = self._variant["disabled_background"]
            foreground = self._variant["disabled_foreground"]
        elif self._pressed:
            fill = self._variant["pressed_background"]
            foreground = self._variant["foreground"]
        elif self._hover:
            fill = self._variant["hover_background"]
            foreground = self._variant.get(
                "hover_foreground", self._variant["foreground"]
            )
        else:
            fill = self._variant["background"]
            foreground = self._variant["foreground"]
        if fill is None:
            fill = self.cget("background")
        return fill, foreground

    def _redraw(self, _event=None):
        # 字体回退可能让实际渲染宽于测量值；分配宽度不足时自动撑开，
        # 避免文本被画布边缘裁切。
        needed = tkfont.Font(font=self._font).measure(self._text) + 2 * self._padx
        if self.winfo_width() < needed:
            self.configure(width=needed)
            return
        width = max(self.winfo_width(), 1)
        height = max(self.winfo_height(), 1)
        fill, foreground = self._colors()
        self.delete("all")
        _rounded_rect(
            self, 0, 0, width - 1, height - 1, self._radius,
            fill=fill, outline="", tags="button",
        )
        self.create_text(
            width / 2, height / 2, text=self._text, fill=foreground,
            font=self._font, tags="button",
        )

    def _on_enter(self, _event):
        if self._enabled:
            self._hover = True
            self._redraw()

    def _on_leave(self, _event):
        self._hover = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _event):
        if self._enabled:
            self._pressed = True
            self._redraw()

    def _on_release(self, event):
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        inside = 0 <= event.x <= self.winfo_width() and (
            0 <= event.y <= self.winfo_height()
        )
        if self._enabled and was_pressed and inside:
            self.invoke()


class StatusPill(tk.Canvas):
    """胶囊状态徽标：圆点 + 彩色文本，底色为状态色的深色混合。"""

    def __init__(self, parent, *, tint_base, parent_background=None,
                 font=("Microsoft YaHei UI", 9, "bold"), height=26, padx=12):
        super().__init__(
            parent, height=height, width=padx * 2,
            highlightthickness=0, bd=0,
            background=parent_background or _parent_background(parent),
        )
        self._font = font
        self._padx = padx
        self._tint_base = tint_base
        self._text = ""
        self._color = COLORS["text_subtle"]
        self.bind("<Configure>", self._redraw)

    def set(self, text, color):
        """更新徽标文本与状态色；底色按状态色向面板底色混合。"""
        self._text = text
        self._color = color
        text_width = tkfont.Font(font=self._font).measure(text)
        self.configure(width=text_width + 2 * self._padx + 12)
        self._redraw()
        # 徽标变宽会抬高所在面板的请求尺寸，通知面板重新钉住。
        ancestor = self.master
        while ancestor is not None and not isinstance(ancestor, RoundedPanel):
            ancestor = getattr(ancestor, "master", None)
        if ancestor is not None:
            ancestor.fit()

    def _redraw(self, _event=None):
        width = max(self.winfo_width(), 1)
        height = max(self.winfo_height(), 1)
        tint = _hex_mix(self._color, self._tint_base, 0.86)
        self.delete("all")
        _rounded_rect(
            self, 0, 0, width - 1, height - 1, height / 2,
            fill=tint, outline="", tags="pill",
        )
        center_y = height / 2
        self.create_oval(
            self._padx - 3, center_y - 3, self._padx + 3, center_y + 3,
            fill=self._color, outline="", tags="pill",
        )
        self.create_text(
            self._padx + 12, center_y, text=self._text, anchor="w",
            fill=self._color, font=self._font, tags="pill",
        )


class DailyGamesApp:
    """主窗口。orchestrator 以注入方式提供，测试可传入测试替身。"""

    def __init__(self, root, orchestrator, config_path=DEFAULT_CONFIG_PATH,
                 auto_setup=True, auto_start=False):
        self.root = root
        self.orchestrator = orchestrator
        self.config_path = config_path
        self.controller = UiController(orchestrator)
        self._auto_start_pending = bool(auto_start)
        self._auto_exit_after_success = False
        self._auto_exit_scheduled = False
        self._exit_when_stopped = False
        self._stop_exit_wait_generation = 0
        self._initial_setup_in_progress = False
        self._initial_setup_resolved = False
        self._initial_check_results = []

        root.title("一键日常")
        root.minsize(1160, 780)
        root.configure(background=COLORS["background"])

        self._card_msg_vars = {}
        self._card_err_vars = {}
        self._card_elapsed_vars = {}
        self._log_placeholder = False
        self._last_seen_running = False
        self._card_frames = {}
        self._card_state_pills = {}
        self._stepper_items = {}
        self._game_icons = {}
        self._app_icon = None
        self._build_widgets()
        self._place_initial_window()
        _enable_dark_title_bar(root)

        self._unsubscribe = orchestrator.add_listener(self.controller.listener)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._refresh_controls()
        self._poll()
        if auto_setup:
            # 首次启动：先检查地址与配置，再确认是否仍有待应用变更。
            self.controller.check_configuration(self._on_initial_check)

    # ------------------------------------------------------------------
    # 版面
    # ------------------------------------------------------------------

    def _configure_styles(self):
        """配置一套不依赖第三方主题的深色 ttk 样式（仅保留 ttk 残留控件）。"""
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        for font_name, size, weight in (
            ("TkDefaultFont", 10, "normal"),
            ("TkTextFont", 10, "normal"),
            ("TkHeadingFont", 10, "bold"),
            ("TkMenuFont", 10, "normal"),
        ):
            try:
                tkfont.nametofont(font_name).configure(
                    family="Microsoft YaHei UI", size=size, weight=weight
                )
            except tk.TclError:
                pass

        style.configure(
            ".",
            background=COLORS["background"],
            foreground=COLORS["text"],
            fieldbackground=COLORS["surface"],
            bordercolor=COLORS["border"],
            darkcolor=COLORS["border"],
            lightcolor=COLORS["border"],
            font=("Microsoft YaHei UI", 10),
        )
        style.configure("TFrame", background=COLORS["background"])
        style.configure("TLabel", background=COLORS["background"],
                        foreground=COLORS["text"])
        style.configure(
            "Panel.TLabel", background=COLORS["surface"],
            foreground=COLORS["text"],
        )
        style.configure(
            "DialogTitle.TLabel", background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        style.configure(
            "Danger.TLabel", background=COLORS["background"],
            foreground=COLORS["danger"],
        )
        style.configure(
            "TLabelframe",
            background=COLORS["surface"],
            bordercolor=COLORS["border"],
            relief=tk.FLAT,
        )
        style.configure(
            "TLabelframe.Label",
            background=COLORS["surface"],
            foreground=COLORS["text_muted"],
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "TCheckbutton",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            focuscolor=COLORS["primary"],
        )
        style.map(
            "TCheckbutton",
            background=[("active", COLORS["surface"])],
            foreground=[("disabled", COLORS["text_subtle"])],
        )
        style.layout(
            "Minimal.Vertical.TScrollbar",
            [
                (
                    "Vertical.Scrollbar.trough",
                    {
                        "sticky": "ns",
                        "children": [
                            (
                                "Vertical.Scrollbar.thumb",
                                {"expand": "1", "sticky": "nswe"},
                            )
                        ],
                    },
                )
            ],
        )
        style.configure(
            "Minimal.Vertical.TScrollbar",
            background=COLORS["surface_alt"],
            troughcolor=COLORS["log_background"],
            bordercolor=COLORS["log_background"],
            lightcolor=COLORS["surface_alt"],
            darkcolor=COLORS["surface_alt"],
            gripcount=0,
            width=8,
        )
        style.map(
            "Minimal.Vertical.TScrollbar",
            background=[("active", COLORS["surface_hover"])],
        )

    def _load_game_icons(self):
        try:
            self._app_icon = tk.PhotoImage(
                master=self.root, file=str(ASSET_DIR.parent / "app_icon.png")
            )
            self.root.iconphoto(True, self._app_icon)
        except (OSError, tk.TclError):
            self._app_icon = None

        for game_id, meta in GAME_APPEARANCE.items():
            path = ASSET_DIR / meta["icon"]
            try:
                image = tk.PhotoImage(master=self.root, file=str(path))
                if image.width() >= 80 and image.height() >= 80:
                    image = image.subsample(2, 2)
                self._game_icons[game_id] = image
            except (OSError, tk.TclError):
                # 资源缺失时仍保留有辨识度的字母标记，界面不可因此启动失败。
                self._game_icons[game_id] = None

    def _place_initial_window(self):
        width, height = 1240, 860
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    @staticmethod
    def _prepare_dialog(dialog):
        dialog.configure(background=COLORS["background"])
        try:
            dialog.after_idle(lambda: _enable_dark_title_bar(dialog))
        except tk.TclError:
            pass

    @staticmethod
    def _style_text_widget(widget, *, monospace=False):
        widget.configure(
            background=COLORS["log_background"],
            foreground=COLORS["text_muted"],
            insertbackground=COLORS["text"],
            selectbackground="#28466E",
            selectforeground=COLORS["text"],
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=COLORS["border_soft"],
            highlightcolor=COLORS["primary"],
            padx=10,
            pady=8,
            font=("Cascadia Mono" if monospace else "Microsoft YaHei UI", 9),
        )

    def _build_widgets(self):
        self._configure_styles()
        self._load_game_icons()

        shell = tk.Frame(self.root, background=COLORS["background"])
        shell.pack(fill=tk.BOTH, expand=True)

        self._build_sidebar(shell)

        content_bg = COLORS["content_background"]
        main = tk.Frame(shell, background=content_bg)
        main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        header = tk.Frame(main, background=content_bg)
        header.pack(fill=tk.X, padx=28, pady=(24, 18))

        title_block = tk.Frame(header, background=content_bg)
        title_block.pack(side=tk.LEFT)
        tk.Label(
            title_block, text="今日队列", background=content_bg,
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 18, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            title_block, text="按顺序执行 · 异常时暂停等待处理",
            background=content_bg, foreground=COLORS["text_subtle"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor=tk.W, pady=(4, 0))

        self.overall_var = tk.StringVar(value="总状态：待命")
        self.current_var = tk.StringVar(value="当前游戏：—")
        status_panel = RoundedPanel(
            header, radius=12, background=COLORS["surface"],
            border=COLORS["border_soft"], padding=12,
            parent_background=content_bg,
        )
        status_panel.pack(side=tk.RIGHT)
        status_inner = status_panel.content
        caption_row = tk.Frame(status_inner, background=COLORS["surface"])
        caption_row.pack(fill=tk.X)
        tk.Label(
            caption_row, text="总状态", background=COLORS["surface"],
            foreground=COLORS["text_subtle"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 10))
        self._overall_pill = StatusPill(
            caption_row, tint_base=COLORS["surface"],
            parent_background=COLORS["surface"],
        )
        self._overall_pill.set(state_label("idle"), STATE_COLORS["idle"])
        self._overall_pill.pack(side=tk.LEFT)
        tk.Label(
            status_inner, textvariable=self.current_var,
            background=COLORS["surface"], foreground=COLORS["text_muted"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor=tk.W, pady=(8, 0))
        status_panel.fit()

        hero = RoundedPanel(
            main, radius=16, background=COLORS["surface"],
            border=COLORS["border_soft"], padding=18,
            parent_background=content_bg,
        )
        hero.pack(fill=tk.X, padx=28)
        hero_inner = hero.content
        hero_copy = tk.Frame(hero_inner, background=COLORS["surface"])
        hero_copy.pack(side=tk.LEFT, padx=(0, 20))
        tk.Label(
            hero_copy, text="完整队列", background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            hero_copy, text="依次完成三款游戏日常",
            background=COLORS["surface"], foreground=COLORS["text_subtle"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor=tk.W, pady=(4, 0))

        hero_controls = tk.Frame(hero_inner, background=COLORS["surface"])
        hero_controls.pack(side=tk.RIGHT)
        self.btn_retry = FlatButton(
            hero_controls, "重试当前", self.controller.retry_current,
            variant="secondary", padx=14,
        )
        self.btn_skip = FlatButton(
            hero_controls, "跳过当前", self.controller.skip_current,
            variant="secondary", padx=14,
        )
        self.btn_stop = FlatButton(
            hero_controls, "安全停止", self._on_stop, variant="danger",
            padx=14,
        )
        self.btn_start_all = FlatButton(
            hero_controls, "开始全部日常  ▶", self._on_start_all,
            variant="primary", height=46, padx=22,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        for btn in (self.btn_retry, self.btn_skip, self.btn_stop):
            btn.pack(side=tk.LEFT, padx=(0, 10))
        self.btn_start_all.pack(side=tk.LEFT)
        hero.fit()

        cards_frame = tk.Frame(main, background=content_bg)
        cards_frame.pack(fill=tk.X, padx=28, pady=(14, 0))
        self.btn_run_one = {}
        for col, game_id in enumerate(GAME_ORDER):
            cards_frame.columnconfigure(col, weight=1, uniform="game-cards")
            self._build_card(cards_frame, game_id, col)

        log_bg = COLORS["log_background"]
        log_panel = RoundedPanel(
            main, radius=16, background=log_bg,
            border=COLORS["border_soft"], padding=0,
            parent_background=content_bg,
        )
        log_panel.pack(fill=tk.BOTH, expand=True, padx=28, pady=(16, 24))
        log_inner = log_panel.content
        log_header = tk.Frame(log_inner, background=log_bg)
        log_header.pack(fill=tk.X, padx=16, pady=(12, 8))
        dots = tk.Canvas(
            log_header, width=42, height=12, background=log_bg,
            highlightthickness=0, bd=0,
        )
        dots.pack(side=tk.LEFT, padx=(0, 10))
        for index, tone in enumerate(
            (COLORS["danger"], COLORS["warning"], COLORS["success"])
        ):
            dots.create_oval(
                2 + index * 14, 3, 10 + index * 14, 11,
                fill=_hex_mix(tone, log_bg, 0.55), outline="",
            )
        tk.Label(
            log_header, text="事件记录", background=log_bg,
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side=tk.LEFT)
        live_pill = StatusPill(
            log_header, tint_base=log_bg,
            parent_background=log_bg,
            font=("Cascadia Mono", 8, "bold"), height=20, padx=10,
        )
        live_pill.set("LIVE", COLORS["success"])
        live_pill.pack(side=tk.LEFT, padx=(10, 0))
        self.btn_logs = FlatButton(
            log_header, "打开日志目录", self._on_open_logs,
            variant="ghost", parent_background=log_bg,
            height=30, padx=12, font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.btn_logs.pack(side=tk.RIGHT)

        log_body = tk.Frame(log_inner, background=log_bg)
        log_body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 10))
        self.log_text = tk.Text(
            log_body, height=8, wrap=tk.WORD, state=tk.DISABLED,
            background=log_bg, foreground=COLORS["text_muted"],
            insertbackground=COLORS["text"], selectbackground="#28466E",
            selectforeground=COLORS["text"], relief=tk.FLAT, borderwidth=0,
            highlightthickness=0, padx=12, pady=10,
            font=("Cascadia Mono", 9),
        )
        self.log_text.tag_configure("normal", foreground=COLORS["text_muted"])
        self.log_text.tag_configure("success", foreground=COLORS["success"])
        self.log_text.tag_configure("warning", foreground=COLORS["warning"])
        self.log_text.tag_configure("error", foreground=COLORS["danger"])
        self.log_text.tag_configure(
            "placeholder", foreground=COLORS["text_subtle"],
        )
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(
            tk.END, "暂无事件。开始执行后，各阶段进度会显示在这里。", "placeholder",
        )
        self.log_text.configure(state=tk.DISABLED)
        self._log_placeholder = True
        scrollbar = ttk.Scrollbar(
            log_body, command=self.log_text.yview,
            style="Minimal.Vertical.TScrollbar",
        )
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        log_panel.fit()

    def _build_sidebar(self, shell):
        sidebar = tk.Frame(shell, background=COLORS["background"], width=252)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, background=COLORS["background"])
        brand.pack(fill=tk.X, padx=22, pady=(26, 0))
        if self._app_icon is not None:
            self._brand_icon = self._app_icon.subsample(2, 2)
            tk.Label(
                brand, image=self._brand_icon,
                background=COLORS["background"], borderwidth=0,
            ).pack(side=tk.LEFT, padx=(0, 12))
        else:
            fallback = tk.Canvas(
                brand, width=32, height=32, background=COLORS["background"],
                highlightthickness=0,
            )
            fallback.pack(side=tk.LEFT, padx=(0, 12))
            _rounded_rect(
                fallback, 1, 1, 30, 30, 8, fill=COLORS["surface_alt"],
                outline=COLORS["border"], width=1,
            )
            fallback.create_text(
                16, 16, text="DG", fill=COLORS["primary"],
                font=("Segoe UI", 10, "bold"),
            )
        brand_text = tk.Frame(brand, background=COLORS["background"])
        brand_text.pack(side=tk.LEFT)
        tk.Label(
            brand_text, text="一键日常", background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            brand_text, text="DAILY OPS", background=COLORS["background"],
            foreground=COLORS["text_subtle"], font=("Segoe UI", 8),
        ).pack(anchor=tk.W, pady=(3, 0))

        tk.Frame(sidebar, height=1, background=COLORS["border_soft"]).pack(
            fill=tk.X, padx=22, pady=(20, 14)
        )

        self._build_stepper(sidebar)

        tk.Frame(sidebar, background=COLORS["background"]).pack(
            fill=tk.BOTH, expand=True
        )

        tk.Frame(sidebar, height=1, background=COLORS["border_soft"]).pack(
            fill=tk.X, padx=22, pady=(0, 12)
        )
        tools = tk.Frame(sidebar, background=COLORS["background"])
        tools.pack(fill=tk.X, padx=22, pady=(0, 22))
        tk.Label(
            tools, text="维护工具", background=COLORS["background"],
            foreground=COLORS["text_subtle"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(anchor=tk.W)
        self.btn_check = FlatButton(
            tools, "检查软件地址与配置", self._on_check_config,
            variant="ghost", height=34,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.btn_restore = FlatButton(
            tools, "恢复配置备份", self._on_restore_backup,
            variant="ghost", height=34,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.btn_check.pack(fill=tk.X, pady=(10, 0))
        self.btn_restore.pack(fill=tk.X, pady=(6, 0))
        tk.Label(
            tools, text="执行期间配置工具自动锁定",
            background=COLORS["background"], foreground=COLORS["text_subtle"],
            font=("Microsoft YaHei UI", 8),
        ).pack(anchor=tk.W, pady=(10, 0))

    def _build_stepper(self, sidebar):
        caption_row = tk.Frame(sidebar, background=COLORS["background"])
        caption_row.pack(fill=tk.X, padx=22)
        tk.Label(
            caption_row, text="执行队列", background=COLORS["background"],
            foreground=COLORS["text_subtle"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            caption_row, text="QUEUE", background=COLORS["background"],
            foreground=COLORS["text_subtle"],
            font=("Cascadia Mono", 7, "bold"),
        ).pack(side=tk.RIGHT, pady=(2, 0))

        self._stepper_icons = {}
        self._stepper_items = {}
        row_height = 68
        canvas = tk.Canvas(
            sidebar, width=208, height=row_height * len(GAME_ORDER) + 4,
            background=COLORS["background"], highlightthickness=0, bd=0,
        )
        canvas.pack(padx=22, pady=(8, 0), anchor=tk.W)
        centers = [
            row_height * index + row_height // 2
            for index in range(len(GAME_ORDER))
        ]
        canvas.create_line(
            16, centers[0], 16, centers[-1], fill=COLORS["border"], width=2,
        )
        for index, game_id in enumerate(GAME_ORDER):
            y = centers[index]
            dot = canvas.create_oval(
                9, y - 7, 23, y + 7,
                fill=STATE_COLORS["idle"],
                outline=COLORS["background"], width=3,
            )
            text_x = 40
            source_icon = self._game_icons.get(game_id)
            if source_icon is not None:
                icon = source_icon.subsample(2, 2)
                self._stepper_icons[game_id] = icon
                canvas.create_image(52, y, image=icon)
                text_x = 74
            canvas.create_text(
                text_x, y - 12, anchor="w",
                text=GAME_APPEARANCE[game_id]["short_name"],
                fill=COLORS["text"],
                font=("Microsoft YaHei UI", 10, "bold"),
            )
            state_item = canvas.create_text(
                text_x, y + 13, anchor="w", text=state_label("idle"),
                fill=STATE_COLORS["idle"], font=("Microsoft YaHei UI", 9),
            )
            self._stepper_items[game_id] = (dot, state_item)
        self._stepper_canvas = canvas


    def _build_card(self, parent, game_id, column):
        meta = GAME_APPEARANCE[game_id]
        left_pad = 0 if column == 0 else 7
        right_pad = 0 if column == len(GAME_ORDER) - 1 else 7
        panel = RoundedPanel(
            parent, radius=14, background=COLORS["surface"],
            border=COLORS["border"], padding=16,
            parent_background=COLORS["content_background"],
        )
        panel.grid(
            row=0, column=column, sticky="nsew", padx=(left_pad, right_pad)
        )
        body = panel.content

        top = tk.Frame(body, background=COLORS["surface"])
        top.pack(fill=tk.X)
        icon = self._game_icons.get(game_id)
        if icon is not None:
            tk.Label(
                top, image=icon, background=COLORS["surface"], borderwidth=0,
            ).pack(side=tk.LEFT, padx=(0, 12))
        else:
            fallback = tk.Canvas(
                top, width=48, height=48, background=COLORS["surface"],
                highlightthickness=0,
            )
            fallback.pack(side=tk.LEFT, padx=(0, 12))
            _rounded_rect(
                fallback, 1, 1, 46, 46, 10, fill=COLORS["surface_alt"],
                outline=meta["accent"], width=1,
            )
            fallback.create_text(
                24, 24, text=meta["initials"], fill=meta["accent"],
                font=("Segoe UI", 11, "bold"),
            )

        # 序号先占右侧，标题再分剩余宽度，长标题不会把序号挤出卡片。
        tk.Label(
            top, text=meta["index"], background=COLORS["surface"],
            foreground=meta["accent"], font=("Cascadia Mono", 10, "bold"),
        ).pack(side=tk.RIGHT, anchor=tk.N, padx=(8, 0))
        name_block = tk.Frame(top, background=COLORS["surface"])
        name_block.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            name_block, text=meta["short_name"], background=COLORS["surface"],
            foreground=COLORS["text"], anchor=tk.W,
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(fill=tk.X)
        # 单行副标题；7 号字保证最小窗口 + 150% 缩放下不被裁切也不撑高卡片。
        tk.Label(
            name_block,
            text=f"{meta['latin_name']} · {meta['helper']}",
            background=COLORS["surface"], foreground=COLORS["text_subtle"],
            anchor=tk.W, font=("Segoe UI", 7, "bold"),
        ).pack(fill=tk.X, pady=(4, 0))

        tk.Frame(body, height=1, background=COLORS["border_soft"]).pack(
            fill=tk.X, pady=(14, 12)
        )
        msg_var = tk.StringVar(value="最近消息：—")
        err_var = tk.StringVar(value="")
        elapsed_var = tk.StringVar(value="")

        pill = StatusPill(
            body, tint_base=COLORS["surface"],
            parent_background=COLORS["surface"],
        )
        pill.set(state_label("idle"), STATE_COLORS["idle"])
        pill.pack(anchor=tk.W)
        # 计时单独一行：长状态名在最小窗口与 150% 缩放下也不会挤掉计时。
        tk.Label(
            body, textvariable=elapsed_var, anchor=tk.W,
            background=COLORS["surface"], foreground=COLORS["text_muted"],
            font=("Cascadia Mono", 9),
        ).pack(fill=tk.X, pady=(6, 0))

        msg_label = tk.Label(
            body, textvariable=msg_var, wraplength=290, justify=tk.LEFT,
            anchor=tk.NW, background=COLORS["surface"],
            foreground=COLORS["text_muted"], height=3,
            font=("Microsoft YaHei UI", 9),
        )
        msg_label.pack(fill=tk.X, pady=(11, 0))
        err_label = tk.Label(
            body, textvariable=err_var, wraplength=290, justify=tk.LEFT,
            anchor=tk.W, background=COLORS["surface"],
            foreground=COLORS["danger"], height=2,
            font=("Cascadia Mono", 8, "bold"),
        )
        err_label.pack(fill=tk.X, pady=(3, 8))

        def _rewrap(event, labels=(msg_label, err_label)):
            # 换行宽度跟随卡片实际宽度，避免高 DPI 下文字被裁切。
            for label in labels:
                label.configure(wraplength=max(120, event.width - 4))

        body.bind("<Configure>", _rewrap, add="+")
        btn = FlatButton(
            body, "仅运行此游戏",
            lambda gid=game_id: self._on_run_one(gid),
            variant="card", parent_background=COLORS["surface"],
            height=36, font=("Microsoft YaHei UI", 9, "bold"),
        )
        btn.pack(fill=tk.X)

        self._card_frames[game_id] = panel
        self._card_state_pills[game_id] = pill
        self._card_msg_vars[game_id] = msg_var
        self._card_err_vars[game_id] = err_var
        self._card_elapsed_vars[game_id] = elapsed_var
        self.btn_run_one[game_id] = btn
        panel.fit()

    def _set_card_state_visual(self, game_id, state):
        color = STATE_COLORS.get(state, COLORS["text_muted"])
        pill = self._card_state_pills.get(game_id)
        if pill is not None:
            pill.set(state_label(state), color)
        panel = self._card_frames.get(game_id)
        if panel is not None:
            panel.set_border(color if state != "idle" else COLORS["border"])
        stepper_item = self._stepper_items.get(game_id)
        if stepper_item is not None:
            dot_item, state_item = stepper_item
            self._stepper_canvas.itemconfigure(dot_item, fill=color)
            self._stepper_canvas.itemconfigure(
                state_item, text=state_label(state), fill=color
            )

    # ------------------------------------------------------------------
    # 事件回圈（主线程）
    # ------------------------------------------------------------------

    def _poll(self):
        for item in self.controller.drain():
            kind = item[0]
            if kind == "event":
                self._on_event(item[1])
            elif kind == "callback":
                item[1](item[2])
        self._refresh_status()
        self._refresh_elapsed()
        self._refresh_controls_if_run_flag_changed()

        if self._exit_when_stopped and self._stop_finished():
            self._cancel_stop_exit_wait()
            self.root.destroy()
            return
        self._schedule_auto_exit_if_needed()
        self.root.after(POLL_INTERVAL_MS, self._poll)

    def _on_event(self, event):
        self.controller.apply_event(event)
        card = self.controller.cards.get(event.game_id)
        if card is not None:
            self._card_msg_vars[event.game_id].set(
                f"最近消息：{card.message or '—'}"
            )
            if card.state in ATTENTION_STATES and card.error_code:
                self._card_err_vars[event.game_id].set(
                    f"错误码 {card.error_code}"
                )
            else:
                self._card_err_vars[event.game_id].set("")
            self._set_card_state_visual(event.game_id, card.state)
        self._append_log(format_event(event))
        self._refresh_controls()

    def _append_log(self, line):
        if any(marker in line for marker in ("失败", "错误", "异常")):
            tag = "error"
        elif any(marker in line for marker in ("已暂停", "停止", "跳过")):
            tag = "warning"
        elif any(marker in line for marker in ("已完成", "通过")):
            tag = "success"
        else:
            tag = "normal"
        self.log_text.configure(state=tk.NORMAL)
        if self._log_placeholder:
            self.log_text.delete("1.0", tk.END)
            self._log_placeholder = False
        self.log_text.insert(tk.END, line + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _refresh_status(self):
        try:
            current_state = self.orchestrator.current_state
            current_game = self.orchestrator.current_game
        except Exception as exc:  # noqa: BLE001 - 状态列不能让回调静默失败
            self._append_log(f"读取编排器状态失败：{exc!r}")
            return
        state_text = state_label(current_state)
        self.overall_var.set(f"总状态：{state_text}")
        name = GAME_NAMES.get(current_game, current_game) if current_game else "—"
        self.current_var.set(f"当前游戏：{name}")
        color = STATE_COLORS.get(current_state, COLORS["text_muted"])
        self._overall_pill.set(state_text, color)

    def _refresh_elapsed(self):
        now = time.monotonic()
        for game_id, var in self._card_elapsed_vars.items():
            card = self.controller.cards.get(game_id)
            text = card_elapsed_text(card, now) if card is not None else ""
            if var.get() != text:
                var.set(text)

    def _refresh_controls_if_run_flag_changed(self):
        """队列结束时编排器只清除 is_running、不再发事件，需由轮询补刷按钮。"""
        try:
            running = bool(self.orchestrator.is_running)
        except Exception:  # noqa: BLE001 - _refresh_controls 会记录同类错误
            return
        if running != self._last_seen_running:
            self._refresh_controls()

    def _refresh_controls(self):
        try:
            running = bool(self.orchestrator.is_running)
            states = control_states(running, self.orchestrator.current_state)
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"更新控件状态失败：{exc!r}")
            return
        self._last_seen_running = running

        def set_state(widget, enabled):
            widget.state(["!disabled"] if enabled else ["disabled"])

        set_state(self.btn_start_all, states["start_all"])
        for btn in self.btn_run_one.values():
            set_state(btn, states["run_one"])
        set_state(self.btn_retry, states["retry"])
        set_state(self.btn_skip, states["skip"])
        set_state(self.btn_stop, states["stop"])
        set_state(self.btn_check, states["check_config"])
        set_state(self.btn_restore, states["restore_backup"])

    # ------------------------------------------------------------------
    # 按钮行为
    # ------------------------------------------------------------------

    def _on_start_all(self, *, auto_exit=False):
        started = self.controller.request_start_all()
        if not started:
            self._append_log("队列已在执行中，已忽略重复启动。")
        else:
            self._auto_exit_after_success = bool(auto_exit)
            self._auto_exit_scheduled = False
        self._refresh_controls()
        return started

    def _on_run_one(self, game_id):
        self._auto_exit_after_success = False
        self._auto_exit_scheduled = False
        if not self.controller.request_run_one(game_id):
            self._append_log("队列已在执行中，已忽略重复启动。")
        self._refresh_controls()

    def _on_stop(self):
        self.controller.request_stop()
        self._append_log("已送出安全停止请求，等待当前阶段收尾…")
        self._refresh_controls()

    # ------------------------------------------------------------------
    # 关闭窗口
    # ------------------------------------------------------------------

    def _stop_finished(self):
        try:
            return (
                not self.orchestrator.is_running
                or self.orchestrator.current_state == "stopped"
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _widget_is_alive(widget):
        """返回 Tk 控件是否仍存在，且不在回调中抛出异常。"""
        try:
            return bool(widget.winfo_exists())
        except Exception:  # noqa: BLE001 - 关闭窗口可能使控件失效。
            return False

    @staticmethod
    def _set_close_buttons_enabled(buttons, enabled):
        """尽量更新关闭对话框中三个按钮的状态。"""
        try:
            children = buttons.winfo_children()
        except Exception:  # noqa: BLE001
            return
        for child in children:
            try:
                child.state(["!disabled"] if enabled else ["disabled"])
            except Exception:  # noqa: BLE001 - 正在关闭的对话框可能销毁子控件。
                pass

    def _set_stop_exit_hint(self, hint, text):
        if not self._widget_is_alive(hint):
            return
        try:
            hint.config(text=text)
        except Exception:  # noqa: BLE001 - 关闭对话框可能正在退出。
            pass

    def _cancel_stop_exit_wait(self):
        """取消 UI 自动退出，但不撤销协作式停止请求。"""
        self._exit_when_stopped = False
        self._stop_exit_wait_generation += 1

    def _auto_run_finished_successfully(self):
        """自动一键队列仅在完整成功且工作线程退出后才可关闭界面。"""
        try:
            return (
                not self.orchestrator.is_running
                and self.orchestrator.current_state == "completed"
            )
        except Exception:  # noqa: BLE001 - 自动退出失败时应保留界面。
            return False

    def _schedule_auto_exit_if_needed(self):
        """成功完成的一键队列延迟关闭，释放下一次启动所需的单实例锁。"""
        if (
            not self._auto_exit_after_success
            or self._auto_exit_scheduled
            or not self._auto_run_finished_successfully()
        ):
            return False
        self._auto_exit_scheduled = True
        self._append_log("全部任务已完成，界面将在 3 秒后自动关闭。")
        try:
            self.root.after(
                AUTO_EXIT_SUCCESS_DELAY_MS,
                self._finish_auto_exit,
            )
        except Exception:  # noqa: BLE001 - 计时器异常时保留窗口供用户处理。
            self._auto_exit_scheduled = False
            return False
        return True

    def _finish_auto_exit(self):
        """计时结束时重新核对状态，避免误关用户刚启动的新任务。"""
        if not self._auto_exit_scheduled:
            return
        self._auto_exit_scheduled = False
        if self._auto_exit_after_success and self._auto_run_finished_successfully():
            self._auto_exit_after_success = False
            self.root.destroy()
            return
        self._auto_exit_after_success = False

    def _start_stop_exit_wait(self, dialog, hint, buttons):
        """请求协作式停止，并开始有界的自动退出等待。"""
        self.controller.handle_close_choice(CLOSE_STOP_THEN_EXIT)
        self._exit_when_stopped = True
        self._stop_exit_wait_generation += 1
        generation = self._stop_exit_wait_generation
        self._set_stop_exit_hint(
            hint,
            "正在安全停止队列，完成后会自动关闭，请稍候…",
        )
        self._set_close_buttons_enabled(buttons, False)
        try:
            self.root.after(
                STOP_EXIT_TIMEOUT_MS,
                lambda: self._on_stop_exit_timeout(
                    generation, dialog, hint, buttons
                ),
            )
        except Exception:  # noqa: BLE001 - 不得让关闭中的 UI 卡在自动退出模式。
            self._cancel_stop_exit_wait()
            self._set_stop_exit_hint(
                hint,
                "无法启动停止等待计时；停止请求仍有效，可重新选择。",
            )
            self._set_close_buttons_enabled(buttons, True)
        return generation

    def _on_stop_exit_timeout(self, generation, dialog, hint, buttons):
        """协作式停止超过 UI 等待时间时，恢复关闭选项。"""
        if (
            not self._exit_when_stopped
            or generation != self._stop_exit_wait_generation
            or self._stop_finished()
            or not self._widget_is_alive(dialog)
        ):
            return
        self._cancel_stop_exit_wait()
        self._set_stop_exit_hint(
            hint,
            "停止请求仍有效，但尚未完成。可继续等待、保留进程退出，或返回主窗口。",
        )
        self._set_close_buttons_enabled(buttons, True)

    def _on_close(self):
        if not self.controller.close_choices():
            self.root.destroy()
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("关闭确认")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        self._prepare_dialog(dialog)

        ttk.Label(
            dialog,
            text="任务仍在执行中。请选择如何关闭：",
            padding=12,
            style="DialogTitle.TLabel",
        ).pack(anchor=tk.W)
        hint = ttk.Label(dialog, text="", padding=12, style="Danger.TLabel")
        hint.pack(anchor=tk.W)

        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill=tk.X)

        def choose_keep():
            self._cancel_stop_exit_wait()
            self.controller.handle_close_choice(CLOSE_KEEP)
            dialog.destroy()
            self.root.destroy()

        def choose_stop():
            self._start_stop_exit_wait(dialog, hint, buttons)

        def choose_cancel():
            self._cancel_stop_exit_wait()
            self.controller.handle_close_choice(CLOSE_CANCEL)
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", choose_cancel)

        FlatButton(
            buttons, "保留游戏与挂机程序，仅退出界面", choose_keep,
            variant="secondary",
        ).pack(fill=tk.X, pady=3)
        FlatButton(
            buttons, "安全停止队列后退出", choose_stop,
            variant="danger",
        ).pack(fill=tk.X, pady=3)
        FlatButton(
            buttons, "取消，返回主窗口", choose_cancel,
            variant="primary",
        ).pack(fill=tk.X, pady=3)

        dialog.grab_set()

    # ------------------------------------------------------------------
    # 配置向导 / 检查 / 恢复
    # ------------------------------------------------------------------

    def _on_initial_check(self, results):
        if getattr(self, "_initial_setup_resolved", False) or getattr(
            self, "_initial_setup_in_progress", False,
        ):
            return
        self._initial_check_results = list(results)
        if any(not r.ok for r in results):
            self._initial_setup_resolved = True
            self._auto_start_pending = False
            self._show_setup_wizard(results)
            return
        self._initial_setup_in_progress = True
        self.controller.plan_configuration_changes(self._on_initial_plan)

    def _on_initial_plan(self, changes):
        if getattr(self, "_initial_setup_resolved", False):
            return
        self._initial_setup_in_progress = False
        self._initial_setup_resolved = True
        if changes:
            self._auto_start_pending = False
            self._show_setup_wizard(self._initial_check_results)
            return
        if self._auto_start_pending:
            # 一键入口只在首次检查全部通过后自动启动一次；先清除标记，
            # 防止重复回调或并发刷新造成第二次启动。
            self._auto_start_pending = False
            self._append_log("配置检查通过，正在自动开始全部任务。")
            self._on_start_all(auto_exit=True)

    def _show_setup_wizard(self, results):
        wizard = tk.Toplevel(self.root)
        wizard.title("首次配置向导")
        wizard.transient(self.root)
        wizard.minsize(640, 460)
        self._prepare_dialog(wizard)

        failed_results = [result for result in results if not result.ok]
        intro = (
            "以下软件地址或配置检查未通过，可加载建议变更后勾选应用："
            if failed_results
            else "检测到一键运行所需的待确认配置变更："
        )
        ttk.Label(
            wizard,
            text=intro,
            padding=(12, 8),
            style="DialogTitle.TLabel",
        ).pack(anchor=tk.W)

        failed_text = tk.Text(wizard, height=6, wrap=tk.WORD,
                              state=tk.DISABLED)
        failed_text.pack(fill=tk.X, padx=12)
        self._style_text_widget(failed_text)
        self._set_text(failed_text, "\n".join(
            format_check_result(r) for r in failed_results
        ) or "（地址检查已通过，仍有待确认的配置变更）")

        changes_frame = ttk.LabelFrame(wizard, text="建议变更", padding=8)
        changes_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        holder = {"changes": [], "vars": {}}

        def populate(changes):
            holder["changes"] = list(changes)
            for child in changes_frame.winfo_children():
                child.destroy()
            holder["vars"].clear()
            if not holder["changes"]:
                ttk.Label(
                    changes_frame, text="没有可用的建议变更。",
                    style="Panel.TLabel",
                ).pack(
                    anchor=tk.W
                )
                return
            for change in holder["changes"]:
                var = tk.BooleanVar(value=False)
                holder["vars"][change.change_id] = var
                cb = ttk.Checkbutton(
                    changes_frame, text=format_change(change), variable=var
                )
                cb.pack(anchor=tk.W)
                if not change.verified:
                    cb.state(["disabled"])

        def load_changes():
            self.controller.plan_configuration_changes(populate)

        def apply():
            selected = [
                cid for cid, var in holder["vars"].items() if var.get()
            ]
            if not selected:
                messagebox.showinfo("配置向导", "尚未勾选任何变更。", parent=wizard)
                return
            if not messagebox.askyesno(
                "配置向导",
                f"即将应用 {len(selected)} 项已验证的配置变更，确定吗？",
                parent=wizard,
            ):
                return
            try:
                self.controller.apply_selected_changes(
                    holder["changes"], selected, self._show_check_results
                )
            except ValueError as exc:
                messagebox.showerror("配置向导", str(exc), parent=wizard)

        actions = ttk.Frame(wizard, padding=12)
        actions.pack(fill=tk.X)
        FlatButton(
            actions, "加载建议变更", load_changes, variant="secondary",
        ).pack(side=tk.LEFT)
        FlatButton(
            actions, "应用勾选的变更", apply, variant="primary",
        ).pack(side=tk.LEFT, padx=8)
        FlatButton(
            actions, "关闭", wizard.destroy, variant="secondary",
        ).pack(side=tk.RIGHT)

        load_changes()

    def _on_check_config(self):
        self.controller.check_configuration(self._show_check_results)

    def _show_check_results(self, results):
        self._show_text_dialog(
            "软件地址与配置检查结果",
            "\n".join(format_check_result(r) for r in results)
            or "（无检查结果）",
        )

    def _on_restore_backup(self):
        self.controller.list_backups(self._show_backup_dialog)

    def _show_backup_dialog(self, backups):
        dialog = tk.Toplevel(self.root)
        dialog.title("恢复备份")
        dialog.transient(self.root)
        dialog.minsize(620, 360)
        self._prepare_dialog(dialog)

        ttk.Label(
            dialog, text="选择要恢复的备份（恢复前会再次确认）：",
            padding=(12, 8), style="DialogTitle.TLabel",
        ).pack(anchor=tk.W)

        listbox = tk.Listbox(
            dialog, height=8, background=COLORS["log_background"],
            foreground=COLORS["text_muted"], selectbackground="#28466E",
            selectforeground=COLORS["text"], activestyle="none",
            relief=tk.FLAT, borderwidth=0, highlightthickness=1,
            highlightbackground=COLORS["border_soft"],
            highlightcolor=COLORS["primary"],
            font=("Microsoft YaHei UI", 9),
        )
        listbox.pack(fill=tk.BOTH, expand=True, padx=12)
        for backup in backups:
            listbox.insert(tk.END, format_backup(backup))
        if not backups:
            listbox.insert(tk.END, "（当前没有任何备份）")
            listbox.configure(state=tk.DISABLED)

        actions = ttk.Frame(dialog, padding=12)
        actions.pack(fill=tk.X)

        def restore():
            selection = listbox.curselection()
            if not selection:
                messagebox.showinfo("恢复备份", "请先选择一个备份。", parent=dialog)
                return
            backup = backups[selection[0]]
            if not messagebox.askyesno(
                "恢复备份",
                f"确定要恢复备份 {backup.backup_id} 吗？\n"
                "对应的第三方配置将被覆盖。",
                parent=dialog,
            ):
                return
            dialog.destroy()
            self.controller.restore_backup(
                backup.backup_id, confirmed=True,
                on_done=self._show_check_results,
            )

        FlatButton(
            actions, "恢复所选备份", restore, variant="danger",
        ).pack(side=tk.LEFT)
        FlatButton(
            actions, "取消", dialog.destroy, variant="secondary",
        ).pack(side=tk.RIGHT)

    def _show_text_dialog(self, title, content):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.minsize(620, 360)
        self._prepare_dialog(dialog)
        text = tk.Text(dialog, wrap=tk.WORD, state=tk.DISABLED)
        self._style_text_widget(text, monospace=True)
        scrollbar = ttk.Scrollbar(
            dialog, command=text.yview, style="Minimal.Vertical.TScrollbar"
        )
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0),
                  pady=12)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 12), pady=12)
        self._set_text(text, content)
        FlatButton(
            dialog, "关闭", dialog.destroy, variant="primary",
        ).pack(pady=(0, 12))

    @staticmethod
    def _set_text(widget, content):
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, content)
        widget.configure(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # 日志目录
    # ------------------------------------------------------------------

    def _on_open_logs(self):
        try:
            directories = resolve_log_directories(self.config_path)
            directories[0].mkdir(parents=True, exist_ok=True)
            for directory in directories:
                os.startfile(directory)  # type: ignore[attr-defined]  # Windows
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"打开日志目录失败：{exc!r}")


def _enable_dpi_awareness():
    try:
        import ctypes

        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        set_process_dpi_awareness = shcore.SetProcessDpiAwareness
        set_process_dpi_awareness.argtypes = [ctypes.c_int]
        set_process_dpi_awareness.restype = ctypes.c_long
        set_process_dpi_awareness(2)
    except Exception:  # noqa: BLE001 - DPI 配置失败不影响功能
        pass


def _enable_dark_title_bar(window):
    """在支持的 Windows 版本上让原生标题栏与深色界面保持一致。"""
    try:
        import ctypes

        window.update_idletasks()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        dwmapi.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        window_handle = int(window.winfo_id())
        hwnd = user32.GetParent(ctypes.c_void_p(window_handle)) or window_handle
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):
            result = dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                attribute,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result == 0:
                break
    except Exception:  # noqa: BLE001 - 标题栏装饰失败不影响应用功能
        pass


def run_gui(config_path=DEFAULT_CONFIG_PATH, *, auto_start=False) -> None:
    """前端入口：创建编排器与主窗口并进入 mainloop。"""
    from daily_games.orchestrator import DailyOrchestrator

    _enable_dpi_awareness()
    orchestrator = DailyOrchestrator(config_path)
    root = tk.Tk()
    DailyGamesApp(root, orchestrator, config_path, auto_start=auto_start)
    root.mainloop()
