"""Tkinter 前端：三款游戏一键日常图形界面。

状态显示、按钮规则、事件归并与关闭决策都在 daily_games.ui_components，
本模块只负责 Tk 控件与主线程调度。不要在导入本模块时创建 Tk
根窗口；入口为 run_gui(config_path)。
"""
from __future__ import annotations

import os
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


# 视觉令牌集中定义，避免主界面和弹窗各自出现近似但不一致的颜色。
COLORS = {
    "background": "#090C12",
    "surface": "#111722",
    "surface_alt": "#171E2B",
    "surface_hover": "#202A3A",
    "log_background": "#0C111A",
    "border": "#263143",
    "border_soft": "#1D2634",
    "text": "#F3F6FB",
    "text_muted": "#98A4B7",
    "text_subtle": "#667287",
    "primary": "#6AA6FF",
    "primary_hover": "#89B9FF",
    "primary_pressed": "#4C88E5",
    "success": "#46D39A",
    "warning": "#F4C463",
    "danger": "#F07676",
    "disabled": "#354052",
}

GAME_APPEARANCE = {
    "arknights": {
        "index": "01",
        "latin_name": "ARKNIGHTS",
        "helper": "MAA",
        "accent": "#58C7E8",
        "icon": "arknights.png",
        "initials": "AK",
    },
    "endfield": {
        "index": "02",
        "latin_name": "ENDFIELD",
        "helper": "MaaEnd",
        "accent": "#E5C35B",
        "icon": "endfield.png",
        "initials": "EF",
    },
    "limbus": {
        "index": "03",
        "latin_name": "LIMBUS COMPANY",
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
        root.minsize(920, 680)
        root.configure(background=COLORS["background"])

        self._card_state_vars = {}
        self._card_msg_vars = {}
        self._card_err_vars = {}
        self._card_frames = {}
        self._card_state_labels = {}
        self._card_state_dots = {}
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
        """配置一套不依赖第三方主题的深色 ttk 样式。"""
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

        self._configure_button_style(
            style,
            "Primary.TButton",
            COLORS["primary"],
            "#08111F",
            COLORS["primary_hover"],
            COLORS["primary_pressed"],
            padding=(20, 13),
        )
        self._configure_button_style(
            style,
            "Secondary.TButton",
            COLORS["surface_alt"],
            COLORS["text"],
            COLORS["surface_hover"],
            COLORS["border"],
            padding=(14, 10),
        )
        self._configure_button_style(
            style,
            "Danger.TButton",
            "#3A2028",
            "#FFB6B6",
            "#502A34",
            "#281820",
            padding=(14, 10),
        )
        self._configure_button_style(
            style,
            "CardAction.TButton",
            COLORS["surface_alt"],
            COLORS["text_muted"],
            COLORS["surface_hover"],
            COLORS["border"],
            padding=(12, 9),
        )
        self._configure_button_style(
            style,
            "Tool.TButton",
            COLORS["background"],
            COLORS["text_muted"],
            COLORS["surface_alt"],
            COLORS["border_soft"],
            padding=(12, 8),
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

    @staticmethod
    def _configure_button_style(style, name, background, foreground,
                                active, pressed, *, padding):
        style.configure(
            name,
            background=background,
            foreground=foreground,
            borderwidth=0,
            relief=tk.FLAT,
            padding=padding,
            font=("Microsoft YaHei UI", 10, "bold"),
            focuscolor=COLORS["primary"],
        )
        style.map(
            name,
            background=[
                ("disabled", COLORS["border_soft"]),
                ("pressed", pressed),
                ("active", active),
            ],
            foreground=[("disabled", COLORS["disabled"])],
            relief=[("pressed", tk.FLAT)],
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
        width, height = 1120, 780
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

        header = tk.Frame(shell, background=COLORS["background"])
        header.pack(fill=tk.X, padx=26, pady=(20, 14))

        brand = tk.Canvas(
            header, width=48, height=48, background=COLORS["background"],
            highlightthickness=0,
        )
        brand.pack(side=tk.LEFT, padx=(0, 13))
        brand.create_rectangle(2, 2, 46, 46, fill=COLORS["surface_alt"],
                               outline=COLORS["border"], width=1)
        brand.create_rectangle(2, 2, 7, 46, fill=COLORS["primary"],
                               outline="")
        brand.create_text(
            27, 24, text="DG", fill=COLORS["text"],
            font=("Segoe UI", 13, "bold"),
        )

        title_block = tk.Frame(header, background=COLORS["background"])
        title_block.pack(side=tk.LEFT)
        tk.Label(
            title_block, text="一键日常", background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 21, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            title_block, text="DAILY OPS  ·  三款游戏顺序执行与自动收尾",
            background=COLORS["background"], foreground=COLORS["text_subtle"],
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, pady=(2, 0))

        self.overall_var = tk.StringVar(value="总状态：待命")
        self.current_var = tk.StringVar(value="当前游戏：—")
        status_panel = tk.Frame(
            header, background=COLORS["surface"], highlightthickness=1,
            highlightbackground=COLORS["border_soft"], padx=14, pady=9,
        )
        status_panel.pack(side=tk.RIGHT)
        dot = tk.Canvas(
            status_panel, width=12, height=12, background=COLORS["surface"],
            highlightthickness=0,
        )
        dot.pack(side=tk.LEFT, padx=(0, 9))
        self._overall_dot_item = dot.create_oval(
            2, 2, 10, 10, fill=STATE_COLORS["idle"], outline=""
        )
        self._overall_dot_canvas = dot
        status_text = tk.Frame(status_panel, background=COLORS["surface"])
        status_text.pack(side=tk.LEFT)
        self._overall_value_label = tk.Label(
            status_text, textvariable=self.overall_var,
            background=COLORS["surface"], foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self._overall_value_label.pack(anchor=tk.W)
        tk.Label(
            status_text, textvariable=self.current_var,
            background=COLORS["surface"], foreground=COLORS["text_muted"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor=tk.W, pady=(2, 0))

        content = tk.Frame(shell, background=COLORS["background"])
        content.pack(fill=tk.BOTH, expand=True, padx=26, pady=(0, 22))

        queue_header = tk.Frame(content, background=COLORS["background"])
        queue_header.pack(fill=tk.X, pady=(0, 9))
        tk.Label(
            queue_header, text="今日队列", background=COLORS["background"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            queue_header, text="  按顺序执行 · 异常时暂停等待处理",
            background=COLORS["background"], foreground=COLORS["text_subtle"],
            font=("Microsoft YaHei UI", 9),
        ).pack(side=tk.LEFT, pady=(3, 0))
        tk.Label(
            queue_header, text="01  →  02  →  03",
            background=COLORS["background"], foreground=COLORS["text_subtle"],
            font=("Cascadia Mono", 9, "bold"),
        ).pack(side=tk.RIGHT, pady=(3, 0))

        cards_frame = tk.Frame(content, background=COLORS["background"])
        cards_frame.pack(fill=tk.X)
        self.btn_run_one = {}
        for col, game_id in enumerate(GAME_ORDER):
            cards_frame.columnconfigure(col, weight=1, uniform="game-cards")
            self._build_card(cards_frame, game_id, col)

        action_panel = tk.Frame(
            content, background=COLORS["surface"], highlightthickness=1,
            highlightbackground=COLORS["border_soft"], padx=18, pady=14,
        )
        action_panel.pack(fill=tk.X, pady=(14, 10))
        tk.Frame(action_panel, background=COLORS["primary"], width=3).pack(
            side=tk.LEFT, fill=tk.Y, padx=(0, 15)
        )
        action_copy = tk.Frame(action_panel, background=COLORS["surface"])
        action_copy.pack(side=tk.LEFT, padx=(0, 18))
        tk.Label(
            action_copy, text="完整队列", background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            action_copy, text="依次完成三款游戏日常",
            background=COLORS["surface"], foreground=COLORS["text_subtle"],
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor=tk.W, pady=(2, 0))
        self.btn_start_all = ttk.Button(
            action_panel, text="开始全部日常  ▶", style="Primary.TButton",
            command=self._on_start_all,
        )
        self.btn_start_all.pack(side=tk.LEFT)

        controls = tk.Frame(action_panel, background=COLORS["surface"])
        controls.pack(side=tk.RIGHT)
        self.btn_retry = ttk.Button(
            controls, text="重试当前", style="Secondary.TButton",
            command=self.controller.retry_current,
        )
        self.btn_skip = ttk.Button(
            controls, text="跳过当前", style="Secondary.TButton",
            command=self.controller.skip_current,
        )
        self.btn_stop = ttk.Button(
            controls, text="安全停止", style="Danger.TButton", command=self._on_stop
        )
        for btn in (self.btn_retry, self.btn_skip, self.btn_stop):
            btn.pack(side=tk.LEFT, padx=(8, 0))

        tool_frame = tk.Frame(content, background=COLORS["background"])
        tool_frame.pack(fill=tk.X, pady=(0, 10))
        tk.Label(
            tool_frame, text="维护工具", background=COLORS["background"],
            foreground=COLORS["text_subtle"],
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.btn_check = ttk.Button(
            tool_frame, text="检查软件地址与配置", style="Tool.TButton",
            command=self._on_check_config,
        )
        self.btn_restore = ttk.Button(
            tool_frame, text="恢复配置备份", style="Tool.TButton",
            command=self._on_restore_backup,
        )
        self.btn_check.pack(side=tk.LEFT)
        self.btn_restore.pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(
            tool_frame, text="执行期间配置工具会自动锁定",
            background=COLORS["background"], foreground=COLORS["text_subtle"],
            font=("Microsoft YaHei UI", 9),
        ).pack(side=tk.RIGHT)

        log_frame = tk.Frame(
            content, background=COLORS["surface"], highlightthickness=1,
            highlightbackground=COLORS["border_soft"],
        )
        log_frame.pack(fill=tk.BOTH, expand=True)
        log_header = tk.Frame(log_frame, background=COLORS["surface"])
        log_header.pack(fill=tk.X, padx=14, pady=(10, 8))
        tk.Label(
            log_header, text="事件记录", background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            log_header, text="  LIVE", background=COLORS["surface"],
            foreground=COLORS["success"], font=("Cascadia Mono", 8, "bold"),
        ).pack(side=tk.LEFT, pady=(3, 0))
        self.btn_logs = ttk.Button(
            log_header, text="打开日志目录", style="Tool.TButton",
            command=self._on_open_logs,
        )
        self.btn_logs.pack(side=tk.RIGHT)

        log_body = tk.Frame(log_frame, background=COLORS["log_background"])
        log_body.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        self.log_text = tk.Text(
            log_body, height=8, wrap=tk.WORD, state=tk.DISABLED,
            background=COLORS["log_background"], foreground=COLORS["text_muted"],
            insertbackground=COLORS["text"], selectbackground="#28466E",
            selectforeground=COLORS["text"], relief=tk.FLAT, borderwidth=0,
            highlightthickness=0, padx=12, pady=10,
            font=("Cascadia Mono", 9),
        )
        self.log_text.tag_configure("normal", foreground=COLORS["text_muted"])
        self.log_text.tag_configure("success", foreground=COLORS["success"])
        self.log_text.tag_configure("warning", foreground=COLORS["warning"])
        self.log_text.tag_configure("error", foreground=COLORS["danger"])
        scrollbar = ttk.Scrollbar(
            log_body, command=self.log_text.yview,
            style="Minimal.Vertical.TScrollbar",
        )
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_card(self, parent, game_id, column):
        meta = GAME_APPEARANCE[game_id]
        left_pad = 0 if column == 0 else 6
        right_pad = 0 if column == len(GAME_ORDER) - 1 else 6
        frame = tk.Frame(
            parent, background=COLORS["surface"], highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        frame.grid(
            row=0, column=column, sticky="nsew", padx=(left_pad, right_pad)
        )
        tk.Frame(frame, height=3, background=meta["accent"]).pack(fill=tk.X)
        body = tk.Frame(frame, background=COLORS["surface"], padx=15, pady=13)
        body.pack(fill=tk.BOTH, expand=True)

        top = tk.Frame(body, background=COLORS["surface"])
        top.pack(fill=tk.X)
        icon = self._game_icons.get(game_id)
        if icon is not None:
            tk.Label(
                top, image=icon, background=COLORS["surface"], borderwidth=0,
            ).pack(side=tk.LEFT, padx=(0, 11))
        else:
            fallback = tk.Canvas(
                top, width=48, height=48, background=COLORS["surface"],
                highlightthickness=0,
            )
            fallback.pack(side=tk.LEFT, padx=(0, 11))
            fallback.create_rectangle(
                1, 1, 47, 47, fill=COLORS["surface_alt"],
                outline=meta["accent"], width=1,
            )
            fallback.create_text(
                24, 24, text=meta["initials"], fill=meta["accent"],
                font=("Segoe UI", 11, "bold"),
            )

        name_block = tk.Frame(top, background=COLORS["surface"])
        name_block.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            name_block, text=GAME_NAMES[game_id], background=COLORS["surface"],
            foreground=COLORS["text"], anchor=tk.W,
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(fill=tk.X)
        tk.Label(
            name_block,
            text=f"{meta['latin_name']}  ·  {meta['helper']}",
            background=COLORS["surface"], foreground=COLORS["text_subtle"],
            anchor=tk.W, font=("Segoe UI", 8, "bold"),
        ).pack(fill=tk.X, pady=(3, 0))
        tk.Label(
            top, text=meta["index"], background=COLORS["surface"],
            foreground=meta["accent"], font=("Cascadia Mono", 10, "bold"),
        ).pack(side=tk.RIGHT, anchor=tk.N)

        tk.Frame(body, height=1, background=COLORS["border_soft"]).pack(
            fill=tk.X, pady=(12, 10)
        )
        state_var = tk.StringVar(value=f"阶段：{state_label('idle')}")
        msg_var = tk.StringVar(value="最近讯息：—")
        err_var = tk.StringVar(value="")

        state_row = tk.Frame(body, background=COLORS["surface"])
        state_row.pack(fill=tk.X)
        state_dot = tk.Canvas(
            state_row, width=12, height=12, background=COLORS["surface"],
            highlightthickness=0,
        )
        state_dot.pack(side=tk.LEFT, padx=(0, 7))
        dot_item = state_dot.create_oval(
            2, 2, 10, 10, fill=STATE_COLORS["idle"], outline=""
        )
        state_label_widget = tk.Label(
            state_row, textvariable=state_var, background=COLORS["surface"],
            foreground=STATE_COLORS["idle"], anchor=tk.W,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        state_label_widget.pack(side=tk.LEFT)
        tk.Label(
            body, textvariable=msg_var, wraplength=280, justify=tk.LEFT,
            anchor=tk.W, background=COLORS["surface"],
            foreground=COLORS["text_muted"], height=2,
            font=("Microsoft YaHei UI", 9),
        ).pack(fill=tk.X, pady=(7, 0))
        err_label = tk.Label(
            body, textvariable=err_var, wraplength=280, justify=tk.LEFT,
            anchor=tk.W, background=COLORS["surface"],
            foreground=COLORS["danger"], height=1,
            font=("Cascadia Mono", 8, "bold"),
        )
        err_label.pack(fill=tk.X, pady=(2, 7))
        btn = ttk.Button(
            body, text="仅运行此游戏", style="CardAction.TButton",
            command=lambda gid=game_id: self._on_run_one(gid),
        )
        btn.pack(fill=tk.X)

        self._card_frames[game_id] = frame
        self._card_state_labels[game_id] = state_label_widget
        self._card_state_dots[game_id] = (state_dot, dot_item)
        self._card_state_vars[game_id] = state_var
        self._card_msg_vars[game_id] = msg_var
        self._card_err_vars[game_id] = err_var
        self.btn_run_one[game_id] = btn

    def _set_card_state_visual(self, game_id, state):
        color = STATE_COLORS.get(state, COLORS["text_muted"])
        label = self._card_state_labels.get(game_id)
        if label is not None:
            label.configure(foreground=color)
        dot = self._card_state_dots.get(game_id)
        if dot is not None:
            canvas, item = dot
            canvas.itemconfigure(item, fill=color)
        frame = self._card_frames.get(game_id)
        if frame is not None:
            border = color if state != "idle" else COLORS["border"]
            frame.configure(highlightbackground=border)

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
            self._card_state_vars[event.game_id].set(
                f"阶段：{state_label(card.state)}"
            )
            self._card_msg_vars[event.game_id].set(
                f"最近讯息：{card.message or '—'}"
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
        self.overall_var.set(f"总状态：{state_label(current_state)}")
        name = GAME_NAMES.get(current_game, current_game) if current_game else "—"
        self.current_var.set(f"当前游戏：{name}")
        color = STATE_COLORS.get(current_state, COLORS["text_muted"])
        self._overall_dot_canvas.itemconfigure(self._overall_dot_item, fill=color)
        self._overall_value_label.configure(foreground=color)

    def _refresh_controls(self):
        try:
            states = control_states(
                self.orchestrator.is_running, self.orchestrator.current_state
            )
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"更新控件状态失败：{exc!r}")
            return

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

        ttk.Button(
            buttons, text="保留游戏与挂机程序，仅退出界面",
            style="Secondary.TButton", command=choose_keep,
        ).pack(fill=tk.X, pady=2)
        ttk.Button(
            buttons, text="安全停止队列后退出", style="Danger.TButton",
            command=choose_stop,
        ).pack(fill=tk.X, pady=2)
        ttk.Button(
            buttons, text="取消，返回主窗口", style="Primary.TButton",
            command=choose_cancel,
        ).pack(fill=tk.X, pady=2)

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
        ttk.Button(
            actions, text="加载建议变更", style="Secondary.TButton",
            command=load_changes,
        ).pack(
            side=tk.LEFT
        )
        ttk.Button(
            actions, text="应用勾选的变更", style="Primary.TButton",
            command=apply,
        ).pack(
            side=tk.LEFT, padx=8
        )
        ttk.Button(
            actions, text="关闭", style="Secondary.TButton",
            command=wizard.destroy,
        ).pack(
            side=tk.RIGHT
        )

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

        ttk.Button(
            actions, text="恢复所选备份", style="Danger.TButton",
            command=restore,
        ).pack(
            side=tk.LEFT
        )
        ttk.Button(
            actions, text="取消", style="Secondary.TButton",
            command=dialog.destroy,
        ).pack(
            side=tk.RIGHT
        )

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
        ttk.Button(
            dialog, text="关闭", style="Primary.TButton", command=dialog.destroy,
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
            logs_dir = Path(self.config_path).resolve().parent.parent / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(logs_dir)  # type: ignore[attr-defined]  # Windows
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"打开日志目录失败：{exc!r}")


def _enable_dpi_awareness():
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - DPI 配置失败不影响功能
        pass


def _enable_dark_title_bar(window):
    """在支持的 Windows 版本上让原生标题栏与深色界面保持一致。"""
    try:
        import ctypes

        window.update_idletasks()
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        user32.GetParent.argtypes = (ctypes.c_void_p,)
        user32.GetParent.restype = ctypes.c_void_p
        hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):
            result = dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(attribute),
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
