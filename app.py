"""
每日投资日报 — 图形界面（优化版）
"""

import customtkinter as ctk
import threading
import subprocess
import importlib
import tempfile
import webbrowser
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv, set_key

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

BASE_DIR  = Path(__file__).parent
ENV_FILE  = BASE_DIR / ".env"
SCRIPT    = BASE_DIR / "daily_investment_report.py"
PREF_FILE = BASE_DIR / ".prefs.json"
LOG_FILE  = BASE_DIR / ".run_log.json"
MAX_LOG   = 50


def _write_log(record: dict) -> None:
    try:
        logs = json.loads(LOG_FILE.read_text(encoding="utf-8")) if LOG_FILE.exists() else []
    except Exception:
        logs = []
    logs.insert(0, record)
    LOG_FILE.write_text(json.dumps(logs[:MAX_LOG], ensure_ascii=False, indent=2), encoding="utf-8")


def _read_logs() -> list[dict]:
    try:
        return json.loads(LOG_FILE.read_text(encoding="utf-8")) if LOG_FILE.exists() else []
    except Exception:
        return []

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


# ── 偏好存取 ──────────────────────────────────────────
def _load_pref(key: str, default):
    try:
        return json.loads(PREF_FILE.read_text(encoding="utf-8")).get(key, default)
    except Exception:
        return default


def _save_pref(key: str, value):
    try:
        data = json.loads(PREF_FILE.read_text(encoding="utf-8")) if PREF_FILE.exists() else {}
    except Exception:
        data = {}
    data[key] = value
    PREF_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── 托盘图标 ──────────────────────────────────────────
def _make_icon_image(size: int = 64) -> "Image.Image":
    img = Image.new("RGB", (size, size), (21, 101, 192))
    d = ImageDraw.Draw(img)
    s = size / 64
    pts = [
        (int(8 * s), int(50 * s)), (int(22 * s), int(33 * s)),
        (int(34 * s), int(39 * s)), (int(46 * s), int(19 * s)),
        (int(57 * s), int(7 * s)),
    ]
    d.line(pts, fill="white", width=max(3, int(4 * s)))
    d.polygon(
        [(int(52 * s), int(3 * s)), (int(62 * s), int(3 * s)), (int(62 * s), int(14 * s))],
        fill="white",
    )
    return img


# ── 脚本执行 & 日志 ───────────────────────────────────
def _run_script(trigger: str, on_line=None, on_done=None) -> None:
    start = datetime.now()
    lines: list[str] = []
    returncode = -1
    try:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(SCRIPT)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(BASE_DIR), env=env,
        )
        for raw in proc.stdout:
            line = raw.rstrip()
            lines.append(line)
            if on_line:
                on_line(line)
        proc.wait()
        returncode = proc.returncode
    except Exception as e:
        err = f"启动失败: {e}"
        lines.append(err)
        if on_line:
            on_line(err)

    duration = int((datetime.now() - start).total_seconds())
    joined = "\n".join(lines)
    if returncode == 0 and "成功发送" in joined:
        status = "成功"
    elif returncode == 0 and "没有找到文章" in joined:
        status = "无新文章"
    elif returncode == 0:
        status = "完成"
    else:
        status = "失败"

    kept = [l for l in lines if not l.startswith("[PROGRESS]")]
    _write_log({
        "time":       start.strftime("%Y-%m-%d %H:%M:%S"),
        "trigger":    trigger,
        "status":     status,
        "returncode": returncode,
        "duration_s": duration,
        "lines":      kept[-30:],
    })
    if on_done:
        on_done(returncode == 0, returncode, duration)


# ── 内置调度器 ────────────────────────────────────────
class Scheduler:
    def __init__(self, run_fn):
        self._run = run_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_date: str | None = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def restart(self):
        self.stop()
        if self._thread:
            self._thread.join(timeout=3)
        self.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def next_run_str(self) -> str:
        load_dotenv(ENV_FILE, override=True)
        if os.getenv("SCHEDULE_ENABLED", "true").lower() != "true":
            return "已禁用"
        t = os.getenv("SCHEDULE_TIME", "09:00")
        try:
            h, m = map(int, t.split(":"))
        except Exception:
            return "时间格式有误"
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        sched_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now >= sched_dt and self._last_date != today_str:
            return f"即将补跑（下次轮询触发，{t}）"
        nxt = sched_dt if sched_dt > now else sched_dt + timedelta(days=1)
        total_min = int((nxt - now).total_seconds() // 60)
        if total_min < 60:
            return f"{total_min} 分钟后 ({t})"
        return f"{total_min // 60} 小时 {total_min % 60} 分钟后 ({t})"

    def _loop(self):
        while not self._stop.wait(30):
            try:
                load_dotenv(ENV_FILE, override=True)
                if os.getenv("SCHEDULE_ENABLED", "true").lower() != "true":
                    continue
                t = os.getenv("SCHEDULE_TIME", "09:00")
                try:
                    h, m = map(int, t.split(":"))
                except Exception:
                    continue
                now = datetime.now()
                today_str = now.strftime("%Y-%m-%d")
                sched_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if now >= sched_dt and self._last_date != today_str:
                    self._last_date = today_str
                    threading.Thread(target=self._run, daemon=True).start()
            except Exception:
                pass


# ── 主窗口 ────────────────────────────────────────────
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("每日投资日报")
        self.geometry("900x600")
        self.minsize(720, 480)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._scheduler = Scheduler(self._do_scheduled_run)
        self._scheduler.start()
        self._tray = self._setup_tray() if HAS_TRAY else None
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 侧边栏
        sidebar = ctk.CTkFrame(self, width=165, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(6, weight=1)

        ctk.CTkLabel(sidebar, text="📈 投资日报",
                     font=ctk.CTkFont(size=15, weight="bold")
                     ).grid(row=0, column=0, padx=16, pady=(22, 28))

        self._nav: dict[str, ctk.CTkButton] = {}
        for idx, (key, lbl) in enumerate([
            ("run",      "▶  立即运行"),
            ("settings", "⚙  配置设置"),
            ("schedule", "🕘  定时任务"),
            ("log",      "📋  运行日志"),
            ("preview",  "👁  预览日报"),
        ], start=1):
            btn = ctk.CTkButton(
                sidebar, text=lbl, width=140, anchor="w",
                fg_color="transparent", text_color=("gray10", "gray90"),
                hover_color=("gray75", "gray30"),
                command=lambda k=key: self._show(k),
            )
            btn.grid(row=idx, column=0, padx=12, pady=3)
            self._nav[key] = btn

        ctk.CTkOptionMenu(
            sidebar, values=["System", "Light", "Dark"],
            command=ctk.set_appearance_mode, width=140,
        ).grid(row=6, column=0, padx=12, pady=16, sticky="s")

        # 面板
        self._panels: dict[str, ctk.CTkFrame] = {
            "run":      RunPanel(self),
            "settings": SettingsPanel(self),
            "schedule": SchedulePanel(self, self._scheduler),
            "log":      LogPanel(self),
            "preview":  PreviewPanel(self),
        }
        self._show("run")

    def _show(self, key: str):
        for p in self._panels.values():
            p.grid_forget()
        self._panels[key].grid(row=0, column=1, sticky="nsew", padx=24, pady=24)
        for k, b in self._nav.items():
            b.configure(fg_color=("gray75", "gray28") if k == key else "transparent")
        if key == "schedule":
            self._panels["schedule"].refresh()

    def _setup_tray(self):
        img = _make_icon_image()
        menu = pystray.Menu(
            pystray.MenuItem("打开界面", lambda icon, item: self._restore(), default=True),
            pystray.MenuItem("立即运行", lambda icon, item: threading.Thread(
                target=self._do_run, args=("手动(托盘)",), daemon=True).start()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda icon, item: self.after(0, self._quit)),
        )
        icon = pystray.Icon("投资日报", img, "每日投资日报", menu)
        threading.Thread(target=icon.run, daemon=True).start()
        return icon

    def _restore(self):
        self.after(0, lambda: (self.deiconify(), self.lift(), self.focus_force()))

    def _on_close(self):
        behavior = _load_pref("close_behavior", "ask")
        if behavior == "minimize":
            self._hide()
        elif behavior == "quit":
            self._quit()
        else:
            CloseDialog(self, self._close_choice)

    def _close_choice(self, choice: str, remember: bool):
        if remember:
            _save_pref("close_behavior", choice)
        if choice == "minimize":
            self._hide()
        else:
            self._quit()

    def _hide(self):
        if HAS_TRAY:
            self.withdraw()
        else:
            self.iconify()

    def _quit(self):
        self._scheduler.stop()
        if self._tray:
            self._tray.stop()
        self.destroy()

    def _do_run(self, trigger="定时"):
        _run_script(trigger)

    def _do_scheduled_run(self):
        """定时调度触发：把输出同步写到 RunPanel 日志框，方便用户事后查看。"""
        run_panel: RunPanel = self._panels["run"]
        self.after(0, run_panel._append, f"[{datetime.now().strftime('%H:%M:%S')}] 🕘 定时任务触发")
        _run_script(
            "定时",
            on_line=lambda line: self.after(0, run_panel._append, line),
            on_done=lambda ok, code, dur: self.after(0, run_panel._append,
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"{'✅ 定时任务完成' if ok else f'❌ 定时任务失败（退出码 {code}）'}"
                f"，耗时 {dur} 秒"),
        )


# ── 关闭对话框 ────────────────────────────────────────
class CloseDialog(ctk.CTkToplevel):
    def __init__(self, master, callback):
        super().__init__(master)
        self.title("关闭选项")
        self.geometry("340x180")
        self.resizable(False, False)
        self.grab_set()
        self.after(50, self.lift)
        self._cb = callback
        self._remember = ctk.BooleanVar(value=False)

        ctk.CTkLabel(self, text="要关闭程序还是最小化到后台？",
                     font=ctk.CTkFont(size=13)
                     ).pack(pady=(24, 14))

        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack()
        ctk.CTkButton(bf, text="最小化到托盘", width=135,
                      command=lambda: self._pick("minimize")
                      ).pack(side="left", padx=6)
        ctk.CTkButton(bf, text="退出程序", width=110,
                      fg_color="gray40", hover_color="gray30",
                      command=lambda: self._pick("quit")
                      ).pack(side="left", padx=6)

        ctk.CTkCheckBox(self, text="记住我的选择（可在设置中重置）",
                         variable=self._remember
                         ).pack(pady=(16, 0))

    def _pick(self, choice: str):
        self._cb(choice, self._remember.get())
        self.destroy()


# ── 立即运行 ──────────────────────────────────────────
class RunPanel(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(self, text="立即运行",
                     font=ctk.CTkFont(size=19, weight="bold")
                     ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        self._log = ctk.CTkTextbox(self, state="disabled",
                                   font=ctk.CTkFont(family="Consolas", size=12))
        self._log.grid(row=1, column=0, sticky="nsew")

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        bar.grid_columnconfigure(1, weight=1)

        self._run_btn = ctk.CTkButton(bar, text="▶  生成并发送日报",
                                       width=190, height=36, command=self._run)
        self._run_btn.grid(row=0, column=0)

        self._status = ctk.CTkLabel(bar, text="")
        self._status.grid(row=0, column=1, padx=14, sticky="w")

        ctk.CTkButton(bar, text="清空", width=70, height=36,
                      fg_color="gray40", hover_color="gray30",
                      command=self._clear
                      ).grid(row=0, column=2)

        self._last_was_progress = False

    _PROGRESS_PREFIX = "[PROGRESS]"

    def _append(self, line: str):
        is_progress = line.startswith(self._PROGRESS_PREFIX)
        if is_progress:
            line = line[len(self._PROGRESS_PREFIX):]

        self._log.configure(state="normal")
        if is_progress and self._last_was_progress:
            # 替换最后一行
            self._log.delete("end-1c linestart", "end-1c")
            self._log.insert("end-1c", line)
        else:
            self._log.insert("end", line + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")
        self._last_was_progress = is_progress

    def _clear(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")
        self._last_was_progress = False

    def _run(self):
        self._run_btn.configure(state="disabled", text="运行中…")
        self._status.configure(text="", text_color="gray")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            _run_script(
                "手动",
                on_line=lambda line: self.after(0, self._append, line),
                on_done=lambda ok, code, _dur: self.after(0, lambda: self._status.configure(
                    text="✅ 完成" if ok else f"❌ 退出码 {code}",
                    text_color="green" if ok else "red",
                )),
            )
        except Exception as e:
            self.after(0, self._append, f"错误: {e}")
        finally:
            self.after(0, lambda: self._run_btn.configure(
                state="normal", text="▶  生成并发送日报"))


# ── 配置设置 ──────────────────────────────────────────
_FIELDS = [
    ("api_key",          "DeepSeek API Key",  True),
    ("model",            "模型名称",           False),
    ("EMAIL_FROM",       "发件邮箱",           False),
    ("EMAIL_TO",         "收件邮箱",           False),
    ("EMAIL_PASSWORD",   "邮箱授权码",         True),
    ("GITHUB_TOKEN",     "GitHub Token",       True),
    ("GITHUB_REPO",      "GitHub 仓库",        False),
    ("GITHUB_PAGES_URL", "Pages 自定义域名",   False),
]


class RSSSourceList(ctk.CTkFrame):
    """RSS 来源列表编辑器，支持增删，格式：名称|URL|条数"""

    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)

        # Header row
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(hdr, text="来源名称", width=110, anchor="w",
                     font=ctk.CTkFont(size=11), text_color="gray"
                     ).grid(row=0, column=0, padx=(0, 6))
        ctk.CTkLabel(hdr, text="RSS URL", anchor="w",
                     font=ctk.CTkFont(size=11), text_color="gray"
                     ).grid(row=0, column=1, padx=(0, 6), sticky="w")
        ctk.CTkLabel(hdr, text="条数", width=55, anchor="w",
                     font=ctk.CTkFont(size=11), text_color="gray"
                     ).grid(row=0, column=2, padx=(0, 34))

        self._scroll = ctk.CTkScrollableFrame(self, height=130)
        self._scroll.grid(row=1, column=0, sticky="ew")
        self._scroll.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(self, text="＋ 添加来源", height=28, width=110,
                      fg_color=("gray80", "gray30"), hover_color=("gray70", "gray40"),
                      text_color=("gray10", "gray90"),
                      command=lambda: self._add_row()
                      ).grid(row=2, column=0, sticky="w", pady=(6, 0))

        self._rows: list[tuple[ctk.CTkEntry, ctk.CTkEntry, ctk.CTkEntry, ctk.CTkCheckBox]] = []
        self._load()

    def _load(self):
        load_dotenv(ENV_FILE, override=True)
        raw = os.getenv("RSS_URLS", "")
        for item in raw.replace("；", ";").split(";"):
            item = item.strip()
            if not item:
                continue
            parts = item.split("|")
            if len(parts) >= 3:
                name, url, count = parts[0].strip(), parts[1].strip(), parts[2].strip()
            elif len(parts) == 2:
                name, url, count = parts[0].strip(), parts[1].strip(), "10"
            else:
                from urllib.parse import urlparse
                url = item
                name = urlparse(url).netloc or ""
                count = "10"
            is_deep = len(parts) >= 4 and parts[3].strip().lower() == "deep"
            self._add_row(name, url, count, is_deep)

    def _add_row(self, name: str = "", url: str = "", count: str = "", is_deep: bool = False):
        idx = len(self._rows)
        name_e = ctk.CTkEntry(self._scroll, placeholder_text="来源名称", width=110)
        name_e.grid(row=idx, column=0, padx=(0, 6), pady=3)
        if name:
            name_e.insert(0, name)

        url_e = ctk.CTkEntry(self._scroll, placeholder_text="RSS URL")
        url_e.grid(row=idx, column=1, padx=(0, 6), pady=3, sticky="ew")
        if url:
            url_e.insert(0, url)

        count_e = ctk.CTkEntry(self._scroll, placeholder_text="10", width=55)
        count_e.grid(row=idx, column=2, padx=(0, 6), pady=3)
        if count:
            count_e.insert(0, count)

        deep_cb = ctk.CTkCheckBox(self._scroll, text="深度", width=62)
        deep_cb.grid(row=idx, column=3, padx=(0, 6), pady=3)
        if is_deep:
            deep_cb.select()

        ctk.CTkButton(self._scroll, text="✕", width=28, height=28,
                      fg_color="gray40", hover_color="gray30",
                      command=lambda i=idx: self._del_row(i)
                      ).grid(row=idx, column=4, pady=3)

        self._rows.append((name_e, url_e, count_e, deep_cb))

    def _del_row(self, idx: int):
        values = [(n.get(), u.get(), c.get(), bool(d.get())) for n, u, c, d in self._rows]
        values.pop(idx)
        for w in self._scroll.winfo_children():
            w.destroy()
        self._rows.clear()
        for n, u, c, d in values:
            self._add_row(n, u, c, d)

    def get_value(self) -> str:
        from urllib.parse import urlparse
        parts = []
        for name_e, url_e, count_e, deep_cb in self._rows:
            name  = name_e.get().strip()
            url   = url_e.get().strip()
            count = count_e.get().strip() or "10"
            is_deep = bool(deep_cb.get())
            if url:
                # 始终输出 name|url|count 三段格式，避免与 _parse_rss_sources 解析不一致
                if not name:
                    name = urlparse(url).netloc or url
                base = f"{name}|{url}|{count}"
                parts.append(f"{base}|deep" if is_deep else base)
        return ";".join(parts)


class SettingsPanel(ctk.CTkScrollableFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self, text="配置设置",
                     font=ctk.CTkFont(size=19, weight="bold")
                     ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        self._entries: dict[str, ctk.CTkEntry] = {}
        for i, (key, label, secret) in enumerate(_FIELDS, start=1):
            ctk.CTkLabel(self, text=label, anchor="e", width=120
                         ).grid(row=i, column=0, padx=(0, 12), pady=4, sticky="e")
            e = ctk.CTkEntry(self, show="●" if secret else "", placeholder_text=key)
            e.grid(row=i, column=1, sticky="ew", pady=4)
            self._entries[key] = e

        # 发布方式
        pub_row = len(_FIELDS) + 1
        ctk.CTkLabel(self, text="发布方式", anchor="e", width=120
                     ).grid(row=pub_row, column=0, padx=(0, 12), pady=(12, 4), sticky="e")
        pub_frame = ctk.CTkFrame(self, fg_color="transparent")
        pub_frame.grid(row=pub_row, column=1, sticky="w", pady=(12, 4))
        self._pub_email_var  = ctk.BooleanVar(value=True)
        self._pub_github_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(pub_frame, text="📧 邮箱发送",
                         variable=self._pub_email_var
                         ).grid(row=0, column=0, padx=(0, 24))
        ctk.CTkCheckBox(pub_frame, text="🌐 GitHub Pages",
                         variable=self._pub_github_var
                         ).grid(row=0, column=1)

        # RSS 来源列表
        rss_row = len(_FIELDS) + 2
        ctk.CTkLabel(self, text="RSS 来源", anchor="e", width=120
                     ).grid(row=rss_row, column=0, padx=(0, 12), pady=(8, 0), sticky="ne")
        self._rss_list = RSSSourceList(self)
        self._rss_list.grid(row=rss_row, column=1, sticky="ew", pady=(8, 0))

        btn_row = rss_row + 1
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=btn_row, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        btn_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(btn_frame, text="重置关闭偏好", width=130, height=30,
                      fg_color="gray40", hover_color="gray30",
                      command=self._reset_close
                      ).grid(row=0, column=0, sticky="w")

        self._msg = ctk.CTkLabel(btn_frame, text="")
        self._msg.grid(row=0, column=1, padx=12)

        ctk.CTkButton(btn_frame, text="保存配置", width=120, height=34,
                      command=self._save
                      ).grid(row=0, column=2, sticky="e")

        self._load()

    def _load(self):
        load_dotenv(ENV_FILE, override=True)
        for key, entry in self._entries.items():
            entry.delete(0, "end")
            entry.insert(0, os.getenv(key, ""))
        self._pub_email_var.set( os.getenv("PUBLISH_EMAIL",  "true").lower()  == "true")
        self._pub_github_var.set(os.getenv("PUBLISH_GITHUB", "false").lower() == "true")

    def _save(self):
        for key, entry in self._entries.items():
            set_key(str(ENV_FILE), key, entry.get())
        set_key(str(ENV_FILE), "RSS_URLS",       self._rss_list.get_value())
        set_key(str(ENV_FILE), "PUBLISH_EMAIL",  "true" if self._pub_email_var.get()  else "false")
        set_key(str(ENV_FILE), "PUBLISH_GITHUB", "true" if self._pub_github_var.get() else "false")
        self._msg.configure(text="✅ 已保存", text_color="green")
        self.after(2500, lambda: self._msg.configure(text=""))

    def _reset_close(self):
        _save_pref("close_behavior", "ask")
        self._msg.configure(text="✅ 关闭偏好已重置", text_color="green")
        self.after(2500, lambda: self._msg.configure(text=""))


# ── 定时任务 ──────────────────────────────────────────
class SchedulePanel(ctk.CTkFrame):
    def __init__(self, master, scheduler: Scheduler):
        super().__init__(master, fg_color="transparent")
        self._scheduler = scheduler
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text="定时任务",
                     font=ctk.CTkFont(size=19, weight="bold")
                     ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        ctk.CTkLabel(self,
                     text="程序内置调度，无需 Windows 任务计划。\n关闭窗口时请选「最小化到托盘」以保持后台运行。",
                     text_color="gray", justify="left"
                     ).grid(row=1, column=0, sticky="w", pady=(0, 20))

        cfg = ctk.CTkFrame(self)
        cfg.grid(row=2, column=0, sticky="ew")
        cfg.grid_columnconfigure(1, weight=1)

        # 启用开关
        ctk.CTkLabel(cfg, text="启用定时任务：", anchor="e", width=120
                     ).grid(row=0, column=0, padx=(14, 10), pady=14, sticky="e")
        self._enabled_var = ctk.StringVar()
        self._switch = ctk.CTkSwitch(cfg, text="", variable=self._enabled_var,
                                      onvalue="true", offvalue="false")
        self._switch.grid(row=0, column=1, pady=14, sticky="w")

        # 时间选择
        ctk.CTkLabel(cfg, text="执行时间：", anchor="e", width=120
                     ).grid(row=1, column=0, padx=(14, 10), pady=14, sticky="e")
        tf = ctk.CTkFrame(cfg, fg_color="transparent")
        tf.grid(row=1, column=1, pady=14, sticky="w")

        self._hour = ctk.CTkOptionMenu(tf, values=[f"{h:02d}" for h in range(24)], width=72)
        self._hour.grid(row=0, column=0)
        ctk.CTkLabel(tf, text=" 时 ").grid(row=0, column=1)
        self._minute = ctk.CTkOptionMenu(tf, values=[f"{m:02d}" for m in range(0, 60, 5)], width=72)
        self._minute.grid(row=0, column=2)
        ctk.CTkLabel(tf, text=" 分").grid(row=0, column=3)

        ctk.CTkButton(self, text="保存并应用", width=120, height=34,
                      command=self._save
                      ).grid(row=3, column=0, sticky="e", pady=(16, 0))

        self._next_lbl = ctk.CTkLabel(self, text="", text_color="gray")
        self._next_lbl.grid(row=4, column=0, sticky="w", pady=(14, 0))

        self._load()

    def _load(self):
        load_dotenv(ENV_FILE, override=True)
        enabled = os.getenv("SCHEDULE_ENABLED", "true").lower() == "true"
        self._enabled_var.set("true" if enabled else "false")
        if enabled:
            self._switch.select()
        else:
            self._switch.deselect()
        t = os.getenv("SCHEDULE_TIME", "09:00")
        try:
            h, m = t.split(":")
            h_int = int(h)
            m_int = int(m)
            m_rounded = round(m_int / 5) * 5
            if m_rounded >= 60:
                m_rounded -= 60
                h_int += 1
            h_int = h_int % 24
            self._hour.set(f"{h_int:02d}")
            self._minute.set(f"{m_rounded:02d}")
        except Exception:
            self._hour.set("09")
            self._minute.set("00")

    def refresh(self):
        self._load()
        nxt = self._scheduler.next_run_str()
        state = "已启用" if self._enabled_var.get() == "true" else "已禁用"
        thread_ok = "线程运行中 ✓" if self._scheduler.is_alive() else "⚠️ 线程已停止"
        self._next_lbl.configure(text=f"状态：{state}  ·  {thread_ok}  ·  下次运行：{nxt}")

    def _save(self):
        t = f"{self._hour.get()}:{self._minute.get()}"
        set_key(str(ENV_FILE), "SCHEDULE_TIME", t)
        set_key(str(ENV_FILE), "SCHEDULE_ENABLED", self._enabled_var.get())
        self._scheduler.restart()
        self.refresh()


# ── 运行日志 ──────────────────────────────────────────
class LogPanel(ctk.CTkFrame):
    _STATUS_COLOR = {"成功": "#2e7d32", "无新文章": "#e65100", "完成": "#1565c0", "失败": "#c62828"}
    _STATUS_ICON  = {"成功": "✅", "无新文章": "⚠️", "完成": "✅", "失败": "❌"}

    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text="运行日志",
                     font=ctk.CTkFont(size=19, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(hdr, text="刷新", width=70, height=30,
                      command=self.refresh).grid(row=0, column=1, padx=(8, 0))
        ctk.CTkButton(hdr, text="清空", width=70, height=30,
                      fg_color="gray40", hover_color="gray30",
                      command=self._clear).grid(row=0, column=2, padx=(8, 0))

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=1, column=0, sticky="nsew")
        self._scroll.grid_columnconfigure(0, weight=1)
        self.refresh()

    def refresh(self):
        for w in self._scroll.winfo_children():
            w.destroy()
        logs = _read_logs()
        if not logs:
            ctk.CTkLabel(self._scroll, text="暂无运行记录", text_color="gray"
                         ).grid(row=0, column=0, pady=40)
            return
        for i, rec in enumerate(logs):
            self._make_card(i, rec)

    def _make_card(self, row: int, rec: dict):
        status = rec.get("status", "未知")
        color  = self._STATUS_COLOR.get(status, "gray")
        icon   = self._STATUS_ICON.get(status, "❓")

        card = ctk.CTkFrame(self._scroll, border_width=1,
                            border_color=("gray80", "gray30"), corner_radius=6)
        card.grid(row=row, column=0, sticky="ew", pady=3)
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkFrame(card, width=4, fg_color=color, corner_radius=0
                     ).grid(row=0, column=0, sticky="ns", rowspan=2)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=1, sticky="ew", padx=12, pady=(8, 2))
        top.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(top, text=f"{icon} {status}", text_color=color,
                     font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(top, text=rec.get("time", ""),
                     text_color="gray", font=ctk.CTkFont(size=11)
                     ).grid(row=0, column=1, padx=12, sticky="w")
        ctk.CTkLabel(top,
                     text=f"触发：{rec.get('trigger', '')}  ·  耗时：{rec.get('duration_s', 0)} 秒",
                     text_color="gray", font=ctk.CTkFont(size=11)
                     ).grid(row=0, column=2, sticky="e")

        lines = [l for l in rec.get("lines", []) if l]
        preview = lines[-1] if lines else ""
        ctk.CTkLabel(card, text=preview, anchor="w",
                     text_color=("gray40", "gray60"),
                     font=ctk.CTkFont(family="Consolas", size=11)
                     ).grid(row=1, column=1, sticky="ew", padx=12, pady=(0, 8))

    def _clear(self):
        try:
            LOG_FILE.write_text("[]", encoding="utf-8")
        except Exception:
            pass
        self.refresh()


# ── 预览辅助：同时返回历史文章 ───────────────────────────
def _fetch_all_with_history(dsr) -> tuple[list, list, list, list]:
    """返回 (deep_today, regular_today, deep_history, regular_history)"""
    import concurrent.futures

    deep_today, regular_today = [], []
    deep_hist,  regular_hist  = [], []

    def fetch_one(source):
        name, url, count = source[0], source[1], source[2]
        is_deep = len(source) > 3 and bool(source[3])
        try:
            t, h = dsr.fetch_articles(url, count, source_name=name, keep_html=is_deep)
            return is_deep, t, h
        except Exception as e:
            print(f"[警告] 抓取失败 {name}: {e}", flush=True)
            return is_deep, [], []

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(dsr.RSS_SOURCES) or 4, 8)) as ex:
        for is_deep, t, h in ex.map(fetch_one, dsr.RSS_SOURCES):
            if is_deep:
                deep_today.extend(t);   deep_hist.extend(h)
            else:
                regular_today.extend(t); regular_hist.extend(h)

    return deep_today, regular_today, deep_hist, regular_hist


# ── 预览日报 ──────────────────────────────────────────
class PreviewPanel(ctk.CTkFrame):
    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._html_path: str | None = None

        ctk.CTkLabel(self, text="预览日报",
                     font=ctk.CTkFont(size=19, weight="bold")
                     ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        desc_row = ctk.CTkFrame(self, fg_color="transparent")
        desc_row.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ctk.CTkLabel(desc_row, text="生成预览不会发送邮件，仅在浏览器中打开 HTML 日报。",
                     text_color="gray"
                     ).grid(row=0, column=0, sticky="w")
        self._all_articles_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(desc_row, text="包含历史文章（测试用）",
                         variable=self._all_articles_var,
                         text_color="gray"
                         ).grid(row=0, column=1, padx=(20, 0), sticky="w")

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_columnconfigure(4, weight=1)

        self._prev_btn = ctk.CTkButton(bar, text="🔍  生成预览",
                                        width=160, height=36, command=self._generate)
        self._prev_btn.grid(row=0, column=0)

        self._open_btn = ctk.CTkButton(bar, text="在浏览器打开", width=130, height=36,
                                        fg_color="gray40", hover_color="gray30",
                                        state="disabled", command=self._open)
        self._open_btn.grid(row=0, column=1, padx=(12, 0))

        self._pub_btn = ctk.CTkButton(bar, text="🌐 发布到 Pages", width=140, height=36,
                                       fg_color="gray40", hover_color="gray30",
                                       state="disabled", command=self._publish)
        self._pub_btn.grid(row=0, column=2, padx=(12, 0))

        self._status = ctk.CTkLabel(bar, text="", anchor="w")
        self._status.grid(row=0, column=4, padx=14, sticky="w")

        # URL 行（发布成功后显示）
        self._url_bar = ctk.CTkFrame(bar, fg_color="transparent")
        self._url_bar.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(6, 0))
        self._url_lbl = ctk.CTkLabel(self._url_bar, text="", text_color=("gray40", "gray60"),
                                      anchor="w", font=ctk.CTkFont(size=11))
        self._url_lbl.grid(row=0, column=0, sticky="w")
        self._copy_btn = ctk.CTkButton(self._url_bar, text="📋 复制链接", width=100, height=26,
                                        fg_color="gray40", hover_color="gray30",
                                        command=self._copy_url)
        self._copy_btn.grid(row=0, column=1, padx=(10, 0))
        self._url_bar.grid_remove()  # 初始隐藏

        self._web_html:  str | None = None   # build_html_web 结果缓存
        self._date_file: str | None = None   # 发布用文件名日期
        self._pages_url: str | None = None   # 发布后的 URL

        self._log = ctk.CTkTextbox(self, state="disabled",
                                   font=ctk.CTkFont(family="Consolas", size=11))
        self._log.grid(row=3, column=0, sticky="nsew", pady=(14, 0))

    def _append(self, line: str):
        self._log.configure(state="normal")
        self._log.insert("end", line + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _generate(self):
        self._prev_btn.configure(state="disabled", text="生成中…")
        self._open_btn.configure(state="disabled")
        self._status.configure(text="")
        self._html_path = None
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            if str(BASE_DIR) not in sys.path:
                sys.path.insert(0, str(BASE_DIR))
            load_dotenv(ENV_FILE, override=True)

            import daily_investment_report as dsr
            importlib.reload(dsr)

            use_all = self._all_articles_var.get()
            self.after(0, self._append, f"正在抓取 RSS（并发模式{'，含历史文章' if use_all else ''}）…")
            deep_today, regular_today, deep_hist, regular_hist = _fetch_all_with_history(dsr)

            if use_all:
                deep_articles    = deep_today    + deep_hist
                regular_articles = regular_today + regular_hist
            else:
                deep_articles    = deep_today
                regular_articles = regular_today

            today = deep_articles + regular_articles
            self.after(0, self._append,
                       f"抓取完成：{len(today)} 条"
                       f"（深度 {len(deep_articles)}，扩展 {len(regular_articles)}"
                       + ("，含历史" if use_all else "，仅今日") + "）")

            if not today:
                self.after(0, self._append, "⚠️ 暂无文章，请检查 RSS 源或勾选「包含历史文章」。")
                self.after(0, lambda: self._status.configure(text="无文章", text_color="orange"))
            else:
                deep_today    = deep_articles
                regular_today = regular_articles
                deep_proc, regular_proc = [], []

                if deep_today:
                    self.after(0, self._append, f"深度分析 {len(deep_today)} 篇公众号文章（正在进行并发提取）…")
                    deep_proc = dsr.translate_and_summarize_deep(deep_today)

                if regular_today:
                    self.after(0, self._append, "调用 DeepSeek API 分析扩展资讯（并发模式）…")
                    regular_proc = dsr.translate_and_summarize(regular_today)
                    url_to_source = {a["url"]: a["source"] for a in regular_today}
                    for p in regular_proc:
                        p["source"] = url_to_source.get(p.get("url", ""), "")

                date_str = datetime.now().strftime("%Y年%m月%d日")
                html     = dsr.build_html(deep_proc, regular_proc, date_str)

                tmp = tempfile.NamedTemporaryFile(
                    delete=False, suffix=".html", mode="w", encoding="utf-8"
                )
                tmp.write(html)
                tmp.close()
                self._html_path = tmp.name

                # 同时生成 Web 暗色版（供发布到 GitHub Pages）
                self._web_html  = dsr.build_html_web(deep_proc, regular_proc, date_str)
                self._date_file = datetime.now().strftime("%Y-%m-%d")
                self._pages_url = None

                self.after(0, self._append, "✅ 预览生成完成，正在打开浏览器…")
                self.after(0, lambda: self._status.configure(text="✅ 预览就绪", text_color="green"))
                self.after(0, lambda: self._open_btn.configure(state="normal"))
                # 若已配置 GitHub，启用发布按钮
                load_dotenv(ENV_FILE, override=True)
                if os.getenv("GITHUB_TOKEN", "").strip() and os.getenv("GITHUB_REPO", "").strip():
                    self.after(0, lambda: self._pub_btn.configure(state="normal"))
                self.after(200, self._open)

        except Exception as e:
            self.after(0, self._append, f"❌ 错误: {e}")
            self.after(0, lambda: self._status.configure(text="生成失败", text_color="red"))
        finally:
            self.after(0, lambda: self._prev_btn.configure(
                state="normal", text="🔍  生成预览"))

    def _open(self):
        if self._html_path:
            webbrowser.open(f"file:///{self._html_path}")

    def _publish(self):
        if not self._web_html:
            return
        self._pub_btn.configure(state="disabled", text="发布中…")
        self._status.configure(text="", text_color="gray")
        threading.Thread(target=self._publish_worker, daemon=True).start()

    def _publish_worker(self):
        try:
            if str(BASE_DIR) not in sys.path:
                sys.path.insert(0, str(BASE_DIR))
            load_dotenv(ENV_FILE, override=True)
            import daily_investment_report as dsr
            importlib.reload(dsr)

            url = dsr.publish_to_github(self._web_html, self._date_file)
            self._pages_url = url

            def _done():
                self._status.configure(text="🌐 已发布！", text_color="green")
                self._pub_btn.configure(state="normal", text="🌐 发布到 Pages")
                self._url_lbl.configure(text=url)
                self._url_bar.grid()   # 显示 URL 行

            self.after(0, _done)
            self.after(0, self._append, f"🌐 已发布到: {url}")

        except Exception as e:
            self.after(0, self._append, f"❌ 发布失败: {e}")
            self.after(0, lambda: self._status.configure(text="发布失败", text_color="red"))
            self.after(0, lambda: self._pub_btn.configure(state="normal", text="🌐 发布到 Pages"))

    def _copy_url(self):
        if self._pages_url:
            self.winfo_toplevel().clipboard_clear()
            self.winfo_toplevel().clipboard_append(self._pages_url)
            self._copy_btn.configure(text="✅ 已复制")
            self.after(2000, lambda: self._copy_btn.configure(text="📋 复制链接"))


if __name__ == "__main__":
    app = App()
    app.mainloop()
