#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Server Control Panel (over SSH)
===============================
A desktop control panel for a Linux server (built with FASTPANEL-style servers in mind).
It talks to the server only through SSH/SFTP - nothing is installed on the server.

Pages
  Dashboard      CPU / memory / disk / load, service status
  Sites & Files  pick any site, browse files, edit code (syntax colors, line numbers)
  Users          list / create / delete users, passwords, lock, shell, sudo
  Services       start / stop / restart / enable / disable, view service logs
  Databases      MySQL/MariaDB databases + users, create / drop / grant / backup
  Cron Jobs      view and edit any user's crontab
  Firewall       UFW rules + Fail2ban banned IPs
  SSL            certificate expiry check for all sites
  Logs           tail and filter log files (live follow)
  Terminal       run commands

Install (once):   pip install paramiko
Run:              python server_panel.py

NOTE: everything here changes the operating system directly. A control panel such as
FASTPANEL keeps its own database, so it will not know about changes made here.
Use this for emergencies / direct administration.
"""

import bisect
import datetime as dt
import json
import os
import queue
import posixpath
import random
import re
import shlex
import stat as st
import string
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import paramiko

try:  # sharper text on Windows
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

DONATE_URL = "https://paypal.me/MahmoudOsama657"
PROFILE_PATH = os.path.join(os.path.expanduser("~"), ".server_panel.json")
MAX_OPEN_BYTES = 3 * 1024 * 1024
MAX_HIGHLIGHT_CHARS = 400_000
DUMMY = "//__dummy__"

THEMES = {
    "dark": dict(
        bg="#1a1d23", panel="#21252d", panel2="#2b303a", border="#363c48",
        fg="#e6e9ef", muted="#8f98a8", accent="#4c9aff", accent_hover="#6aaeff", accent_fg="#ffffff",
        sel="#2d5a9e", ed_bg="#1d2027", ed_fg="#dde1e8", ln_bg="#181b21", ln_fg="#586070", cur="#262b35",
        found_bg="#ffd54f", found_fg="#000000", term_bg="#14171c", term_fg="#cfd5df",
        ok="#5fd38d", err="#ff7b7b", warn="#ffc861", dir_fg="#8cc2ff",
        syn_comment="#6f7a8c", syn_string="#a6d98f", syn_keyword="#c79bff",
        syn_number="#ffa070", syn_tag="#7fb2ff", syn_var="#ffd479"),
    "light": dict(
        bg="#f3f4f7", panel="#ffffff", panel2="#e6e9ef", border="#cfd4de",
        fg="#1e2330", muted="#667085", accent="#2563eb", accent_hover="#1d4fc4", accent_fg="#ffffff",
        sel="#bcd4fb", ed_bg="#fbfbfd", ed_fg="#1e2330", ln_bg="#eceff4", ln_fg="#98a1b3", cur="#eaf1fd",
        found_bg="#ffe066", found_fg="#000000", term_bg="#f7f8fa", term_fg="#1e2330",
        ok="#16833f", err="#c62828", warn="#a85f00", dir_fg="#1d4fc4",
        syn_comment="#8a94a6", syn_string="#2f7d32", syn_keyword="#7b3fb8",
        syn_number="#c4511b", syn_tag="#1d4ed8", syn_var="#a85f00"),
}

# ------------------------------------------------------------- syntax colors
KEYWORDS = ("function|return|if|else|elseif|foreach|for|while|switch|case|break|continue|class|new|public|"
            "private|protected|static|namespace|use|echo|require|require_once|include|include_once|try|catch|"
            "finally|throw|const|let|var|import|from|export|default|async|await|def|self|None|True|False|null|"
            "true|false|and|or|not|in|is|as|extends|implements|interface|trait|fn|match|isset|empty|array|print")
SYNTAX_EXT = {"php", "html", "htm", "js", "jsx", "ts", "tsx", "vue", "css", "scss", "less", "json", "xml",
              "py", "sh", "conf", "ini", "env", "yml", "yaml", "htaccess", "twig", "blade"}
HASH_EXT = {"py", "sh", "conf", "ini", "env", "yml", "yaml", "htaccess"}
SYN_KINDS = ("comment", "string", "tag", "var", "number", "keyword")

USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
DBNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
MYHOST_RE = re.compile(r"^[A-Za-z0-9_.%:-]{1,60}$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
SYSTEM_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}
WEB_STACK = ("nginx", "apache", "httpd", "mysql", "maria", "php", "fastpanel", "fail2ban", "ssh", "ufw",
             "cron", "redis", "postfix", "dovecot", "bind", "named", "proftpd", "vsftpd", "pure-ftpd",
             "memcached", "supervisor", "docker", "exim", "opendkim", "spamassassin", "clamav")

DASH_CMD = r'''
echo "HOST:$(hostname)"
echo "OS:$(. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME")"
echo "UP:$(uptime -p 2>/dev/null)"
echo "LOAD:$(cut -d' ' -f1-3 /proc/loadavg)"
echo "CORES:$(nproc)"
echo "MEM:$(free -m | awk '/^Mem:/{print $2, $7}')"
echo "DISK:$(df -P / | awk 'NR==2{print $2, $3, $5}')"
echo "IP:$(hostname -I 2>/dev/null | awk '{print $1}')"
head -n1 /proc/stat; sleep 1; head -n1 /proc/stat
echo SVC
for s in nginx apache2 mysql mariadb fastpanel2 fail2ban ssh sshd ufw cron redis-server $(systemctl list-unit-files --no-legend 'php*fpm*.service' 2>/dev/null | awk '{print $1}' | sed 's/\.service$//'); do
  if systemctl cat "$s" >/dev/null 2>&1; then echo "$s $(systemctl is-active "$s")"; fi
done
'''


# ------------------------------------------------------------------ helpers
def build_syntax(ext):
    hash_c = r"|#[^\n]*" if ext in HASH_EXT else ""
    pattern = "|".join([
        r"(?P<comment>/\*.*?\*/|<!--.*?-->|//[^\n]*" + hash_c + ")",
        r"(?P<string>\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*')",
        r"(?P<tag></?[A-Za-z][\w:.-]*|\?>|/?>)",
        r"(?P<var>\$\w+)",
        r"(?P<number>\b\d+(?:\.\d+)?\b)",
        r"(?P<keyword>\b(?:" + KEYWORDS + r")\b)",
    ])
    return re.compile(pattern, re.S)


def offset_to_index(starts, off):
    i = bisect.bisect_right(starts, off) - 1
    return f"{i + 1}.{off - starts[i]}"


def site_short_name(path):
    if "/data/www/" in path:
        return path.split("/data/www/", 1)[1].split("/", 1)[0]
    return ""


def sql_str(s):
    """Quote a Python string as a MySQL string literal."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def human_gb(kb):
    return f"{kb / 1024 / 1024:.1f} GB"


class Remote:
    """Runs commands on the server through an existing paramiko connection."""

    def __init__(self, ssh):
        self.ssh = ssh

    def run(self, cmd, stdin=None, timeout=90):
        i, o, e = self.ssh.exec_command(cmd, timeout=timeout)
        if stdin is not None:
            i.write(stdin)
            i.flush()
            i.channel.shutdown_write()
        out = o.read().decode("utf-8", "replace")
        err = e.read().decode("utf-8", "replace")
        rc = o.channel.recv_exit_status()
        return out, err, rc


# =================================================================== the app
class App(tk.Tk):
    NAV = [("dashboard", "📊", "Dashboard"), ("files", "📁", "Sites & Files"), ("users", "👤", "Users"),
           ("services", "⚙", "Services"), ("databases", "🗄", "Databases"), ("cron", "⏰", "Cron Jobs"),
           ("firewall", "🛡", "Firewall"), ("ssl", "🔒", "SSL Certificates"), ("logs", "📜", "Logs"),
           ("terminal", "💻", "Terminal")]

    def __init__(self):
        super().__init__()
        self.title("Server Control Panel")
        self.geometry("1320x820")
        self.minsize(1000, 620)

        self.ssh = self.sftp = self.R = None
        self.elevated = False
        self.has_sudo = False
        self.is_root = True
        self.login_user = ""
        self.profile = self.load_profile()
        self.theme = self.profile.get("theme", "dark")
        if self.theme not in THEMES:
            self.theme = "dark"
        self.T = THEMES[self.theme]
        self.fsize = int(self.profile.get("fsize", 12))

        fams = set(tkfont.families())
        mono = next((f for f in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New") if f in fams),
                    "TkFixedFont")
        ui = next((f for f in ("Segoe UI", "Noto Sans", "DejaVu Sans") if f in fams), "TkDefaultFont")
        self.ui_font = (ui, 10)
        self.ui_bold = (ui, 10, "bold")
        self.h1_font = (ui, 18, "bold")
        self.big_font = (ui, 22, "bold")
        self.efont = tkfont.Font(family=mono, size=self.fsize)
        self.tfont = tkfont.Font(family=mono, size=max(8, self.fsize - 2))

        # state
        self.sites = {}
        self.site_root = None
        self.types = {}
        self.tabs = {}
        self.pages = {}
        self.nav_btns = {}
        self.loaded = set()
        self.current_page = None
        self.my_cnf = None
        self.sudo_group = "sudo"
        self.users_cache = []
        self.term_cwd = None
        self.term_hist = []
        self.term_hidx = 0
        self.logs_busy = False

        self.ui_q = queue.Queue()
        self.after(40, self.drain_ui)

        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.apply_theme()
        self.build_login()

    # worker threads never touch Tk directly: they post callables here
    def post(self, fn):
        self.ui_q.put(fn)

    def drain_ui(self):
        try:
            while True:
                self.ui_q.get_nowait()()
        except queue.Empty:
            pass
        except Exception as e:  # never let one bad callback kill the loop
            print("UI callback error:", e)
        self.after(40, self.drain_ui)

    # ------------------------------------------------------------- profile
    def load_profile(self):
        try:
            with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_profile(self, data=None):
        if data:
            self.profile.update(data)
        try:
            with open(PROFILE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.profile, f)
        except Exception:
            pass

    # --------------------------------------------------------------- theme
    def apply_theme(self):
        T = self.T = THEMES[self.theme]
        s = self.style
        s.configure(".", background=T["bg"], foreground=T["fg"], fieldbackground=T["panel"],
                    bordercolor=T["border"], lightcolor=T["bg"], darkcolor=T["bg"],
                    troughcolor=T["bg"], font=self.ui_font, focuscolor=T["bg"])
        s.configure("TFrame", background=T["bg"])
        s.configure("Panel.TFrame", background=T["panel"])
        s.configure("Card.TFrame", background=T["panel"], bordercolor=T["border"], relief="solid", borderwidth=1)
        s.configure("TLabel", background=T["bg"], foreground=T["fg"])
        s.configure("Panel.TLabel", background=T["panel"], foreground=T["fg"])
        s.configure("Card.TLabel", background=T["panel"], foreground=T["fg"])
        s.configure("CardMuted.TLabel", background=T["panel"], foreground=T["muted"])
        s.configure("Big.TLabel", background=T["panel"], foreground=T["fg"], font=self.big_font)
        s.configure("Muted.TLabel", background=T["bg"], foreground=T["muted"])
        s.configure("H1.TLabel", background=T["bg"], foreground=T["fg"], font=self.h1_font)
        s.configure("Title.TLabel", background=T["panel"], foreground=T["fg"], font=self.h1_font)
        s.configure("Head.TLabel", background=T["bg"], foreground=T["muted"], font=self.ui_bold)
        s.configure("Status.TLabel", background=T["panel"], foreground=T["muted"], padding=(10, 5))
        s.configure("Path.TLabel", background=T["bg"], foreground=T["muted"], padding=(8, 0))
        s.configure("Warn.TLabel", background=T["bg"], foreground=T["warn"])

        s.configure("TButton", background=T["panel2"], foreground=T["fg"], padding=(12, 7),
                    borderwidth=0, focusthickness=0, relief="flat")
        s.map("TButton", background=[("pressed", T["border"]), ("active", T["border"])],
              foreground=[("disabled", T["muted"])])
        s.configure("Accent.TButton", background=T["accent"], foreground=T["accent_fg"], padding=(14, 7),
                    font=self.ui_bold)
        s.map("Accent.TButton", background=[("pressed", T["accent_hover"]), ("active", T["accent_hover"]),
                                            ("disabled", T["border"])])
        s.configure("Danger.TButton", background="#c0392b", foreground="#ffffff", padding=(12, 7))
        s.map("Danger.TButton", background=[("pressed", "#e04a3a"), ("active", "#e04a3a")])
        s.configure("Small.TButton", padding=(8, 4))
        s.configure("Tool.TButton", padding=(10, 6))
        s.configure("Donate.TButton", background="#e5527a", foreground="#ffffff", padding=(10, 4),
                    font=self.ui_bold)
        s.map("Donate.TButton", background=[("pressed", "#f0678b"), ("active", "#f0678b")])
        s.configure("Nav.TButton", background=T["panel"], foreground=T["muted"], anchor="w", padding=(18, 11),
                    font=self.ui_font)
        s.map("Nav.TButton", background=[("active", T["panel2"])], foreground=[("active", T["fg"])])
        s.configure("NavOn.TButton", background=T["panel2"], foreground=T["accent"], anchor="w",
                    padding=(18, 11), font=self.ui_bold)
        s.map("NavOn.TButton", background=[("active", T["panel2"])])

        s.configure("TEntry", fieldbackground=T["panel"], foreground=T["fg"], insertcolor=T["fg"],
                    padding=7, bordercolor=T["border"])
        s.map("TEntry", bordercolor=[("focus", T["accent"])], fieldbackground=[("focus", T["panel"])])
        s.configure("TCombobox", fieldbackground=T["panel"], background=T["panel2"], foreground=T["fg"],
                    arrowcolor=T["fg"], bordercolor=T["border"], padding=6)
        s.map("TCombobox", fieldbackground=[("readonly", T["panel"])], foreground=[("readonly", T["fg"])],
              selectbackground=[("readonly", T["panel"])], selectforeground=[("readonly", T["fg"])])
        self.option_add("*TCombobox*Listbox.background", T["panel"])
        self.option_add("*TCombobox*Listbox.foreground", T["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", T["sel"])
        self.option_add("*TCombobox*Listbox.selectForeground", T["fg"])
        s.configure("TCheckbutton", background=T["bg"], foreground=T["fg"])
        s.map("TCheckbutton", background=[("active", T["bg"])],
              indicatorcolor=[("selected", T["accent"]), ("!selected", T["panel"])])

        s.configure("Treeview", background=T["panel"], fieldbackground=T["panel"], foreground=T["fg"],
                    rowheight=28, borderwidth=0, font=self.ui_font)
        s.map("Treeview", background=[("selected", T["sel"])], foreground=[("selected", T["fg"])])
        s.configure("Treeview.Heading", background=T["panel2"], foreground=T["muted"], relief="flat",
                    padding=(8, 6), font=self.ui_bold)
        s.map("Treeview.Heading", background=[("active", T["border"])])

        s.configure("TNotebook", background=T["bg"], borderwidth=0, tabmargins=(4, 6, 4, 0))
        s.configure("TNotebook.Tab", background=T["panel2"], foreground=T["muted"], padding=(14, 8),
                    borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", T["ed_bg"])], foreground=[("selected", T["fg"])])
        s.configure("TScrollbar", background=T["panel2"], troughcolor=T["bg"], bordercolor=T["bg"],
                    arrowcolor=T["muted"], relief="flat", gripcount=0)
        s.map("TScrollbar", background=[("active", T["border"])])
        s.configure("TPanedwindow", background=T["border"])
        s.configure("Sash", sashthickness=5)
        for name, color in (("", T["accent"]), ("Ok.", T["ok"]), ("Warn.", T["warn"]), ("Bad.", T["err"])):
            s.configure(name + "Horizontal.TProgressbar", troughcolor=T["panel2"], background=color,
                        bordercolor=T["panel2"], lightcolor=color, darkcolor=color, thickness=10)

        self.configure(bg=T["bg"])
        self.restyle_widgets()

    def restyle_widgets(self):
        T = self.T
        for attr in ("site_list",):
            w = getattr(self, attr, None)
            if w:
                w.config(bg=T["panel"], fg=T["fg"], selectbackground=T["sel"], selectforeground=T["fg"],
                         highlightbackground=T["border"], highlightcolor=T["accent"])
        for attr in ("tree",):
            w = getattr(self, attr, None)
            if w:
                w.tag_configure("dir", foreground=T["dir_fg"])
                w.tag_configure("file", foreground=T["fg"])
        for tv in getattr(self, "all_tables", []):
            for tag, key in (("ok", "ok"), ("bad", "err"), ("warn", "warn")):
                tv.tag_configure(tag, foreground=T[key])
        for attr in ("term_out", "logs_text"):
            w = getattr(self, attr, None)
            if w:
                w.config(bg=T["term_bg"], fg=T["term_fg"], insertbackground=T["term_fg"],
                         selectbackground=T["sel"])
                w.tag_config("cmd", foreground=T["accent"])
                w.tag_config("err", foreground=T["err"])
                w.tag_config("warn", foreground=T["warn"])
        if getattr(self, "cron_text", None):
            self.cron_text.config(bg=T["ed_bg"], fg=T["ed_fg"], insertbackground=T["ed_fg"],
                                  selectbackground=T["sel"])
        if getattr(self, "menu", None):
            self.menu.config(bg=T["panel"], fg=T["fg"], activebackground=T["accent"],
                             activeforeground=T["accent_fg"], bd=0)
        if getattr(self, "welcome", None):
            self.welcome.config(bg=T["ed_bg"], fg=T["muted"])
        for info in self.tabs.values():
            self.style_editor(info)

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.save_profile({"theme": self.theme})
        self.apply_theme()
        if "dashboard" in self.loaded:
            self.refresh_dashboard()

    def change_font(self, delta):
        self.fsize = max(8, min(30, self.fsize + delta))
        self.efont.configure(size=self.fsize)
        self.tfont.configure(size=max(8, self.fsize - 2))
        self.save_profile({"fsize": self.fsize})

    # ------------------------------------------------------------- dialogs
    def form(self, title, fields, ok_text="OK", note=None, danger=False):
        """fields: (key, label, default, kind[, options]) with kind text|password|check|choice|combo."""
        d = tk.Toplevel(self)
        d.title(title)
        d.configure(bg=self.T["bg"])
        d.transient(self)
        d.resizable(False, False)
        body = ttk.Frame(d, padding=(22, 18, 22, 6))
        body.pack(fill="both", expand=True)
        r = 0
        if note:
            ttk.Label(body, text=note, wraplength=470, justify="left").grid(
                row=r, column=0, columnspan=2, sticky="w", pady=(0, 12))
            r += 1
        vars_, first = {}, None
        for f in fields:
            key, label, default, kind = f[:4]
            opts = f[4] if len(f) > 4 else []
            if kind == "check":
                v = tk.BooleanVar(value=bool(default))
                ttk.Checkbutton(body, text=label, variable=v).grid(row=r, column=0, columnspan=2, sticky="w", pady=5)
            else:
                v = tk.StringVar(value=default)
                ttk.Label(body, text=label).grid(row=r, column=0, sticky="e", padx=(0, 12), pady=5)
                if kind in ("choice", "combo"):
                    w = ttk.Combobox(body, textvariable=v, values=opts, width=34,
                                     state="readonly" if kind == "choice" else "normal")
                else:
                    w = ttk.Entry(body, textvariable=v, width=36, show="•" if kind == "password" else "")
                w.grid(row=r, column=1, pady=5, sticky="w")
                first = first or w
            vars_[key] = v
            r += 1
        res = {"v": None}

        def ok(_e=None):
            res["v"] = {k: v.get() for k, v in vars_.items()}
            d.destroy()

        row = ttk.Frame(d, padding=(22, 10, 22, 18))
        row.pack(fill="x")
        ttk.Button(row, text=ok_text, style="Danger.TButton" if danger else "Accent.TButton",
                   command=ok).pack(side="right")
        ttk.Button(row, text="Cancel", command=d.destroy).pack(side="right", padx=8)
        d.bind("<Return>", ok)
        d.bind("<Escape>", lambda _e: d.destroy())
        d.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - d.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - d.winfo_height()) // 3
        d.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        if first:
            first.focus_set()
        d.grab_set()
        self.wait_window(d)
        return res["v"]

    def ask(self, title, prompt, initial=""):
        r = self.form(title, [("v", prompt, initial, "text")])
        return r["v"] if r else None

    def confirm_typed(self, title, message, expected):
        r = self.form(title, [("v", f'Type "{expected}" to confirm', "", "text")], ok_text="Confirm",
                      note=message, danger=True)
        return bool(r) and r["v"].strip() == expected

    # --------------------------------------------------------------- login
    def build_login(self):
        self.login = ttk.Frame(self)
        self.login.pack(fill="both", expand=True)
        card = ttk.Frame(self.login, style="Card.TFrame", padding=(40, 32))
        card.place(relx=0.5, rely=0.5, anchor="center")
        ttk.Label(card, text="🖥  Server Control Panel", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, pady=(0, 4))
        ttk.Label(card, text="Manage your Linux server over SSH", style="CardMuted.TLabel").grid(
            row=1, column=0, columnspan=3, pady=(0, 22))

        self.v_host = tk.StringVar(value=self.profile.get("host", ""))
        self.v_port = tk.StringVar(value=str(self.profile.get("port", 22)))
        self.v_user = tk.StringVar(value=self.profile.get("user", "root"))
        self.v_pass = tk.StringVar()
        self.v_key = tk.StringVar(value=self.profile.get("key", ""))
        rows = [("Server IP / host", self.v_host, False), ("SSH port", self.v_port, False),
                ("Username", self.v_user, False), ("Password", self.v_pass, True),
                ("SSH key file (optional)", self.v_key, False)]
        for i, (label, var, secret) in enumerate(rows, start=2):
            ttk.Label(card, text=label, style="Card.TLabel").grid(row=i, column=0, sticky="e", padx=(0, 12), pady=6)
            ttk.Entry(card, textvariable=var, width=34, show="•" if secret else "").grid(row=i, column=1, pady=6)
        ttk.Button(card, text="Browse...", command=self.pick_key).grid(row=6, column=2, padx=(8, 0))
        self.btn_connect = ttk.Button(card, text="Connect", style="Accent.TButton", command=self.connect)
        self.btn_connect.grid(row=7, column=0, columnspan=3, pady=(22, 8), sticky="ew")
        self.lbl_login = ttk.Label(card, text="Your password is never saved on this computer.",
                                   style="CardMuted.TLabel")
        self.lbl_login.grid(row=8, column=0, columnspan=3)
        ttk.Button(card, text="❤  Support this tool - Donate via PayPal", style="Donate.TButton",
                   command=self.open_donate).grid(row=9, column=0, columnspan=3, pady=(16, 0), sticky="ew")
        self.bind("<Return>", lambda e: self.connect() if self.ssh is None else None)

    def open_donate(self):
        webbrowser.open(DONATE_URL)

    def pick_key(self):
        p = filedialog.askopenfilename(title="Select your private key file")
        if p:
            self.v_key.set(p)

    def login_msg(self, text, kind="muted"):
        color = {"err": self.T["err"], "ok": self.T["ok"]}.get(kind, self.T["muted"])
        self.lbl_login.config(text=text, foreground=color)

    def connect(self):
        host = self.v_host.get().strip()
        if not host:
            self.login_msg("Enter the server address.", "err")
            return
        try:
            port = int(self.v_port.get() or 22)
        except ValueError:
            self.login_msg("Port must be a number.", "err")
            return
        user, pwd = self.v_user.get().strip(), self.v_pass.get()
        key = self.v_key.get().strip() or None
        self.btn_connect.config(state="disabled")
        self.login_msg("Connecting...")

        def work():
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(host, port=port, username=user, password=pwd or None, key_filename=key,
                            timeout=15, banner_timeout=20, allow_agent=False, look_for_keys=not pwd)
                ssh.get_transport().set_keepalive(30)
                sftp, has_sudo = None, False
                if user != "root":
                    try:
                        _i, o, _e = ssh.exec_command("sudo -n true")
                        has_sudo = o.channel.recv_exit_status() == 0
                        if has_sudo:
                            sftp = self.open_elevated_sftp(ssh)
                    except Exception:
                        sftp = None
                elevated = sftp is not None
                if sftp is None:
                    sftp = ssh.open_sftp()
                self.post(lambda: self.on_connected(ssh, sftp, host, port, user, key, elevated, has_sudo))
            except Exception as e:
                msg = f"Connection failed: {e}"
                self.post(lambda: (self.btn_connect.config(state="normal"), self.login_msg(msg, "err")))

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def open_elevated_sftp(ssh):
        """SFTP server running through sudo, so every site's files are readable/writable.
        Needs passwordless sudo for this login user. Returns None when not possible."""
        _i, o, _e = ssh.exec_command("sudo -n true && echo SUDO_OK")
        if "SUDO_OK" not in o.read().decode("utf-8", "replace"):
            return None
        _i, o, _e = ssh.exec_command(
            "grep -hiE '^[[:space:]]*Subsystem[[:space:]]+sftp' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf "
            "2>/dev/null | awk '{print $3}' | head -1; "
            "ls /usr/lib/openssh/sftp-server /usr/libexec/openssh/sftp-server /usr/lib/ssh/sftp-server 2>/dev/null")
        paths = [l.strip() for l in o.read().decode("utf-8", "replace").splitlines() if l.strip().startswith("/")]
        for path in dict.fromkeys(paths):
            chan = ssh.get_transport().open_session()
            try:
                chan.exec_command("sudo -n " + shlex.quote(path))
                sftp = paramiko.SFTPClient(chan)
                sftp.listdir("/")
                return sftp
            except Exception:
                try:
                    chan.close()
                except Exception:
                    pass
        return None

    def on_connected(self, ssh, sftp, host, port, user, key, elevated=False, has_sudo=False):
        self.elevated = elevated or user == "root"
        self.has_sudo = has_sudo
        self.ssh, self.sftp, self.R = ssh, sftp, Remote(ssh)
        self.login_user, self.is_root = user, (user == "root")
        self.conn_label = f"{user}@{host}"
        self.save_profile({"host": host, "port": port, "user": user, "key": key or ""})
        self.login.destroy()
        self.unbind("<Return>")
        self.title(f"Server Control Panel - {self.conn_label}")
        self.build_main()
        self.show_page("dashboard")
        if not self.elevated:
            self.set_status(f"Logged in as '{user}' without passwordless sudo: files readable by this user work "
                            f"normally; some admin pages (firewall, services, users...) may be limited.", "warn")

    # ------------------------------------------------------------ plumbing
    def priv(self, cmd):
        if self.is_root or not self.has_sudo:
            return cmd
        return "sudo -n sh -c " + shlex.quote(cmd)

    def run_priv(self, cmd, stdin=None, timeout=90):
        return self.R.run(self.priv(cmd), stdin=stdin, timeout=timeout)

    def bg(self, fn, done=None, status=None):
        if status:
            self.set_status(status)

        def work():
            try:
                res, err = fn(), None
            except Exception as e:
                res, err = None, e
            if done:
                self.post(lambda: done(res, err))
            elif err:
                self.post(lambda: self.set_status(f"✖ {err}", "err"))

        threading.Thread(target=work, daemon=True).start()

    def do_cmd(self, cmd, ok_msg, then=None, stdin=None, timeout=90, busy=None):
        """Run a privileged command in the background, report the result, optionally refresh."""
        def fn():
            out, err, rc = self.run_priv(cmd, stdin, timeout)
            if rc != 0:
                raise RuntimeError((err or out).strip() or f"exit code {rc}")
            return out

        def done(res, err):
            if err:
                self.set_status(f"✖ {err}", "err")
                messagebox.showerror("Command failed", str(err))
            else:
                self.set_status(f"✓ {ok_msg}", "ok")
            if then:
                then()

        self.bg(fn, done, busy or "Working...")

    def set_status(self, msg, kind="muted"):
        color = {"ok": self.T["ok"], "err": self.T["err"], "warn": self.T["warn"]}.get(kind, self.T["muted"])
        if hasattr(self, "status"):
            self.status.config(text=msg, foreground=color)

    # -------------------------------------------------------- layout helpers
    def header(self, parent, title, subtitle=None):
        f = ttk.Frame(parent)
        f.pack(fill="x", padx=24, pady=(20, 6))
        left = ttk.Frame(f)
        left.pack(side="left")
        ttk.Label(left, text=title, style="H1.TLabel").pack(anchor="w")
        if subtitle:
            ttk.Label(left, text=subtitle, style="Muted.TLabel").pack(anchor="w")
        return f

    def toolbar(self, parent, items, pady=(6, 8)):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=24, pady=pady)
        for it in items:
            if it == "|":
                ttk.Frame(bar, width=14).pack(side="left")
                continue
            text, cmd = it[0], it[1]
            style = it[2] if len(it) > 2 else "Tool.TButton"
            ttk.Button(bar, text=text, command=cmd, style=style).pack(side="left", padx=(0, 6))
        return bar

    def table(self, parent, columns, padx=24, pady=(0, 14), expand=True, height=None):
        """columns: (id, heading, width, anchor). Returns the Treeview."""
        frame = ttk.Frame(parent)
        frame.pack(fill="both" if expand else "x", expand=expand, padx=padx, pady=pady)
        tv = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", selectmode="browse",
                          **({"height": height} if height else {}))
        for cid, head, w, anc in columns:
            tv.heading(cid, text=head)
            tv.column(cid, width=w, anchor=anc, stretch=True)
        sb = ttk.Scrollbar(frame, command=tv.yview)
        tv.config(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, key in (("ok", "ok"), ("bad", "err"), ("warn", "warn")):
            tv.tag_configure(tag, foreground=self.T[key])
        self.all_tables = getattr(self, "all_tables", []) + [tv]
        return tv

    @staticmethod
    def fill(tv, rows, tags=None):
        tv.delete(*tv.get_children())
        for i, row in enumerate(rows):
            tv.insert("", "end", iid=f"r{i}", values=row, tags=(tags[i],) if tags and tags[i] else ())

    @staticmethod
    def selected_values(tv):
        sel = tv.selection()
        return tv.item(sel[0], "values") if sel else None

    # ---------------------------------------------------------------- main UI
    def build_main(self):
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        topbar = ttk.Frame(self, style="Panel.TFrame", padding=(14, 8))
        topbar.pack(fill="x")
        ttk.Label(topbar, text="🖥  Server Control Panel", style="Panel.TLabel", font=self.ui_bold).pack(side="left")
        ttk.Label(topbar, text=f"   ●  connected to {self.conn_label}", style="Panel.TLabel",
                  foreground=self.T["ok"]).pack(side="left")
        mode = "full access" if self.login_user == "root" else ("sudo file access" if self.elevated else "no sudo")
        ttk.Label(topbar, text=f"   ({mode})", style="Panel.TLabel",
                  foreground=self.T["ok"] if self.elevated else self.T["warn"]).pack(side="left")
        ttk.Button(topbar, text="❤  Donate", style="Donate.TButton", command=self.open_donate).pack(side="right",
                                                                                                  padx=(8, 0))
        ttk.Button(topbar, text="🌗  Theme", style="Small.TButton", command=self.toggle_theme).pack(side="right")
        ttk.Button(topbar, text="A+", style="Small.TButton", width=3, command=lambda: self.change_font(1)).pack(
            side="right", padx=4)
        ttk.Button(topbar, text="A−", style="Small.TButton", width=3, command=lambda: self.change_font(-1)).pack(
            side="right")
        ttk.Label(topbar, text="Editor font:", style="Panel.TLabel").pack(side="right", padx=(0, 6))

        self.status = ttk.Label(self, text="Ready", anchor="w", style="Status.TLabel")
        self.status.pack(fill="x", side="bottom")

        side = ttk.Frame(self, style="Panel.TFrame", width=220)
        side.pack(fill="y", side="left")
        side.pack_propagate(False)
        ttk.Label(side, text="MENU", style="Panel.TLabel", foreground=self.T["muted"],
                  font=self.ui_bold).pack(anchor="w", padx=20, pady=(18, 8))
        for key, icon, label in self.NAV:
            b = ttk.Button(side, text=f"{icon}   {label}", style="Nav.TButton", command=lambda k=key: self.show_page(k))
            b.pack(fill="x")
            self.nav_btns[key] = b

        self.content = ttk.Frame(self)
        self.content.pack(fill="both", expand=True, side="left")
        builders = {"dashboard": self.build_dashboard, "files": self.build_files, "users": self.build_users,
                    "services": self.build_services, "databases": self.build_databases,
                    "cron": self.build_cron, "firewall": self.build_firewall, "ssl": self.build_ssl,
                    "logs": self.build_logs, "terminal": self.build_terminal}
        for key, fn in builders.items():
            page = ttk.Frame(self.content)
            self.pages[key] = page
            fn(page)

        self.bind_all("<Control-s>", lambda e: self.save_current() if self.current_page == "files" else None)
        self.bind_all("<Control-f>", lambda e: self.find_text() if self.current_page == "files" else None)
        self.bind_all("<Control-w>", lambda e: self.close_current() if self.current_page == "files" else None)
        self.bind_all("<Control-equal>", lambda e: self.change_font(1))
        self.bind_all("<Control-minus>", lambda e: self.change_font(-1))
        self.restyle_widgets()

    def show_page(self, key):
        if self.current_page:
            self.pages[self.current_page].pack_forget()
        self.current_page = key
        self.pages[key].pack(fill="both", expand=True)
        for k, b in self.nav_btns.items():
            b.config(style="NavOn.TButton" if k == key else "Nav.TButton")
        loaders = {"dashboard": self.refresh_dashboard, "files": self.load_sites, "users": self.load_users,
                   "services": self.load_services, "databases": self.refresh_db, "cron": self.cron_init,
                   "firewall": self.refresh_firewall, "ssl": self.ssl_init, "logs": self.logs_init,
                   "terminal": self.term_focus}
        if key == "dashboard" or key not in self.loaded:
            self.loaded.add(key)
            loaders[key]()
        if key == "dashboard":
            self.schedule("dash_job", 15000, self.dash_tick)
        if key == "logs":
            self.schedule("logs_job", 3000, self.logs_tick)

    # =============================================================== DASHBOARD
    def build_dashboard(self, parent):
        head = self.header(parent, "Dashboard", "Live overview of your server")
        ttk.Button(head, text="↻  Refresh", command=self.refresh_dashboard).pack(side="right")
        self.dash_sys = ttk.Label(parent, text="", style="Muted.TLabel")
        self.dash_sys.pack(anchor="w", padx=26, pady=(0, 4))

        grid = ttk.Frame(parent)
        grid.pack(fill="x", padx=18, pady=10)
        self.cards = {}
        for i, (key, title, bar) in enumerate((("cpu", "CPU usage", True), ("mem", "Memory", True),
                                               ("disk", "Disk (/)", True), ("load", "Load average", False))):
            grid.columnconfigure(i, weight=1, uniform="c")
            card = ttk.Frame(grid, style="Card.TFrame", padding=18)
            card.grid(row=0, column=i, sticky="nsew", padx=6)
            ttk.Label(card, text=title, style="CardMuted.TLabel").pack(anchor="w")
            val = ttk.Label(card, text="…", style="Big.TLabel")
            val.pack(anchor="w", pady=(6, 8))
            pb = None
            if bar:
                pb = ttk.Progressbar(card, maximum=100, style="Ok.Horizontal.TProgressbar")
                pb.pack(fill="x")
            sub = ttk.Label(card, text="", style="CardMuted.TLabel")
            sub.pack(anchor="w", pady=(8, 0))
            self.cards[key] = (val, pb, sub)

        ttk.Label(parent, text="Services", style="Head.TLabel").pack(anchor="w", padx=26, pady=(18, 6))
        self.svc_box = ttk.Frame(parent)
        self.svc_box.pack(fill="x", padx=18)

    def refresh_dashboard(self):
        def fn():
            out, err, rc = self.R.run(DASH_CMD, timeout=40)
            return out

        self.bg(fn, self.on_dashboard, "Refreshing dashboard...")

    def schedule(self, attr, ms, fn):
        old = getattr(self, attr, None)
        if old:
            try:
                self.after_cancel(old)
            except Exception:
                pass
        setattr(self, attr, self.after(ms, fn))

    def dash_tick(self):
        if self.current_page == "dashboard" and self.ssh:
            self.refresh_dashboard()
            self.schedule("dash_job", 15000, self.dash_tick)

    def set_bar(self, pb, pct):
        pb.config(value=pct, style=("Bad." if pct >= 90 else "Warn." if pct >= 75 else "Ok.") + "Horizontal.TProgressbar")

    def on_dashboard(self, out, err):
        if err or not out:
            self.set_status(f"✖ Could not read server stats: {err}", "err")
            return
        data, cpu_lines, svcs, mode = {}, [], [], None
        for line in out.splitlines():
            if line.strip() == "SVC":
                mode = "svc"
                continue
            if mode == "svc":
                p = line.split()
                if len(p) == 2:
                    svcs.append(p)
            elif line.startswith("cpu "):
                cpu_lines.append([int(x) for x in line.split()[1:]])
            elif ":" in line and line.split(":", 1)[0] in ("HOST", "OS", "UP", "LOAD", "CORES", "MEM", "DISK", "IP"):
                k, v = line.split(":", 1)
                data[k] = v.strip()
        self.dash_sys.config(text=f"{data.get('HOST', '?')}   •   {data.get('OS', '')}   •   {data.get('IP', '')}"
                                  f"   •   {data.get('UP', '')}")
        # CPU
        val, pb, sub = self.cards["cpu"]
        if len(cpu_lines) == 2:
            a, b = cpu_lines
            idle = (b[3] + (b[4] if len(b) > 4 else 0)) - (a[3] + (a[4] if len(a) > 4 else 0))
            total = sum(b) - sum(a)
            pct = max(0.0, min(100.0, 100.0 * (1 - idle / total))) if total else 0.0
            val.config(text=f"{pct:.0f}%")
            self.set_bar(pb, pct)
            sub.config(text=f"{data.get('CORES', '?')} cores")
        # memory
        try:
            total_mb, avail_mb = [int(x) for x in data["MEM"].split()]
            used = total_mb - avail_mb
            pct = 100.0 * used / total_mb
            val, pb, sub = self.cards["mem"]
            val.config(text=f"{pct:.0f}%")
            self.set_bar(pb, pct)
            sub.config(text=f"{used / 1024:.1f} GB of {total_mb / 1024:.1f} GB used")
        except Exception:
            pass
        # disk
        try:
            tot, used, pctstr = data["DISK"].split()
            pct = float(pctstr.strip("%"))
            val, pb, sub = self.cards["disk"]
            val.config(text=f"{pct:.0f}%")
            self.set_bar(pb, pct)
            sub.config(text=f"{human_gb(int(used))} of {human_gb(int(tot))} used")
        except Exception:
            pass
        # load
        val, _pb, sub = self.cards["load"]
        la = data.get("LOAD", "").split()
        if la:
            val.config(text=la[0])
            sub.config(text=f"1 / 5 / 15 min: {' / '.join(la)}")
        # services
        for w in self.svc_box.winfo_children():
            w.destroy()
        for i, (name, state) in enumerate(svcs):
            ok = state == "active"
            chip = ttk.Frame(self.svc_box, style="Card.TFrame", padding=(14, 8))
            chip.grid(row=i // 5, column=i % 5, padx=6, pady=5, sticky="w")
            ttk.Label(chip, text="●", style="Card.TLabel",
                      foreground=self.T["ok"] if ok else self.T["err"]).pack(side="left")
            ttk.Label(chip, text=f"  {name}", style="Card.TLabel").pack(side="left")
            ttk.Label(chip, text=f"   {state}", style="CardMuted.TLabel").pack(side="left")
        self.set_status(f"Dashboard updated {time.strftime('%H:%M:%S')}", "ok")

    # ================================================================ FILES
    def build_files(self, parent):
        main = ttk.Frame(parent)
        main.pack(fill="both", expand=True)

        left = ttk.Frame(main, width=350)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        head = ttk.Frame(left)
        head.pack(fill="x", padx=10, pady=(16, 6))
        ttk.Label(head, text="① Choose a site", style="Head.TLabel").pack(side="left")
        ttk.Button(head, text="Custom path", style="Small.TButton", command=self.add_manual_site).pack(side="right")
        ttk.Button(head, text="↻", style="Small.TButton", width=3, command=self.load_sites).pack(side="right", padx=4)
        self.v_filter = tk.StringVar()
        self.v_filter.trace_add("write", lambda *a: self.fill_site_list())
        ttk.Entry(left, textvariable=self.v_filter).pack(fill="x", padx=10)
        self.lbl_count = ttk.Label(left, text="", style="Muted.TLabel")
        self.lbl_count.pack(anchor="w", padx=12, pady=(4, 2))
        lf = ttk.Frame(left)
        lf.pack(fill="x", padx=10, pady=(0, 6))
        self.site_list = tk.Listbox(lf, height=9, exportselection=False, relief="flat", bd=0, highlightthickness=1,
                                    activestyle="none", font=self.ui_font)
        sb = ttk.Scrollbar(lf, command=self.site_list.yview)
        self.site_list.config(yscrollcommand=sb.set)
        self.site_list.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        self.site_list.bind("<<ListboxSelect>>", self.on_site_select)

        ttk.Label(left, text="② Open a file (double-click)", style="Head.TLabel").pack(anchor="w", padx=10, pady=(8, 4))
        tf = ttk.Frame(left)
        tf.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.tree = ttk.Treeview(tf, show="tree", selectmode="browse")
        tsb = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.config(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewOpen>>", lambda e: self.expand_pending())
        self.tree.bind("<Double-1>", self.on_tree_double)
        self.tree.bind("<Button-3>", self.on_tree_menu)

        self.menu = tk.Menu(self, tearoff=0, font=self.ui_font)
        for label, cmd in (("📄  New file", self.new_file), ("📁  New folder", self.new_folder),
                           ("⬆  Upload files here", self.upload_here), (None, None),
                           ("✏  Rename", self.rename_item), ("⬇  Download", self.download_item),
                           ("🗑  Delete", self.delete_item), (None, None), ("↻  Refresh", self.refresh_selected)):
            if label:
                self.menu.add_command(label=label, command=cmd)
            else:
                self.menu.add_separator()

        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True)
        bar = ttk.Frame(right)
        bar.pack(fill="x", padx=6, pady=8)
        ttk.Button(bar, text="💾  Save   Ctrl+S", style="Accent.TButton", command=self.save_current).pack(side="left")
        ttk.Button(bar, text="🔍  Find   Ctrl+F", command=self.find_text).pack(side="left", padx=6)
        ttk.Button(bar, text="✖  Close tab", command=self.close_current).pack(side="left")
        self.lbl_pos = ttk.Label(bar, text="", style="Path.TLabel")
        self.lbl_pos.pack(side="left", padx=10)
        holder = tk.Frame(right, bg=self.T["ed_bg"])
        holder.pack(fill="both", expand=True)
        self.nb = ttk.Notebook(holder)
        self.nb.pack(fill="both", expand=True)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.update_pos())
        self.welcome = tk.Label(holder, bg=self.T["ed_bg"], fg=self.T["muted"], font=(self.ui_font[0], 13),
                                justify="center", text="👈  Choose a site on the left,\nthen double-click a file to edit it.")
        self.update_welcome()

    def update_welcome(self):
        if self.tabs:
            self.welcome.place_forget()
        else:
            self.welcome.place(relx=0.5, rely=0.45, anchor="center")
            self.welcome.lift()

    def load_sites(self, then=None):
        self.set_status("Looking for sites...")
        self.update_idletasks()

        def fn():
            sites = {}
            out, _e, _r = self.R.run("ls -d /var/www/*/data/www/*/ 2>/dev/null")  # FASTPANEL layout
            if not out.strip() and self.has_sudo:
                out, _e, _r = self.run_priv("ls -d /var/www/*/data/www/*/ 2>/dev/null")
            for line in out.splitlines():
                p = line.strip().rstrip("/")
                if p:
                    parts = p.split("/")
                    sites[f"{parts[-1]}    ·  {parts[3] if len(parts) > 3 else ''}"] = p
            if not sites:
                out, _e, _r = self.R.run("ls -d /var/www/*/ /home/*/public_html /home/*/domains/*/public_html "
                                         "2>/dev/null")
                for line in out.splitlines():
                    p = line.strip().rstrip("/")
                    if p:
                        sites[p] = p
            return sites

        def done(res, err):
            if err:
                self.set_status(f"✖ {err}", "err")
                return
            self.sites = res
            self.fill_site_list()
            self.set_status(f"Found {len(res)} sites", "ok" if res else "muted")
            if then:
                then()

        self.bg(fn, done)

    def fill_site_list(self):
        q = self.v_filter.get().strip().lower()
        self.site_list.delete(0, "end")
        shown = 0
        for name in sorted(self.sites):
            if q in name.lower():
                self.site_list.insert("end", name)
                shown += 1
        self.lbl_count.config(text=f"{shown} of {len(self.sites)} sites")

    def add_manual_site(self):
        p = self.ask("Custom path", "Folder path on the server:", "/var/www/")
        if p and p.strip():
            p = p.strip().rstrip("/") or "/"
            self.sites[p] = p
            self.fill_site_list()
            self.open_site(p)

    def on_site_select(self, _e=None):
        sel = self.site_list.curselection()
        if sel:
            self.open_site(self.sites[self.site_list.get(sel[0])])

    def open_site(self, path):
        self.site_root = path
        self.tree.delete(*self.tree.get_children())
        self.types = {path: "d"}
        self.tree.insert("", "end", iid=path, text="📂 " + (site_short_name(path) or path), tags=("dir",))
        self.tree.insert(path, "end", iid=path + DUMMY, text="...")
        self.tree.item(path, open=True)
        self.load_children(path)
        self.set_status(f"Current site: {path}")

    def load_children(self, parent):
        for c in self.tree.get_children(parent):
            self.tree.delete(c)
        try:
            items = self.sftp.listdir_attr(parent)
        except Exception as e:
            hint = ("" if self.elevated else "\n\nYou are not root and have no passwordless sudo, so this "
                    "folder is closed to you. Log in as root (or enable NOPASSWD sudo).")
            messagebox.showerror("Error", f"Cannot read {parent}\n{e}{hint}")
            return
        items.sort(key=lambda a: (not st.S_ISDIR(a.st_mode), a.filename.lower()))
        for a in items:
            p = posixpath.join(parent, a.filename)
            if st.S_ISDIR(a.st_mode):
                self.types[p] = "d"
                self.tree.insert(parent, "end", iid=p, text="📁 " + a.filename, tags=("dir",))
                self.tree.insert(p, "end", iid=p + DUMMY, text="...")
            else:
                self.types[p] = "f"
                self.tree.insert(parent, "end", iid=p, text="📄 " + a.filename, tags=("file",))

    def expand_pending(self):
        iid = self.tree.focus()
        if iid:
            kids = self.tree.get_children(iid)
            if len(kids) == 1 and kids[0].endswith(DUMMY):
                self.load_children(iid)

    def on_tree_double(self, _e=None):
        iid = self.tree.focus()
        if iid and self.types.get(iid) == "f":
            self.open_file(iid)

    def on_tree_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        self.menu.tk_popup(event.x_root, event.y_root)

    def selected(self):
        return self.tree.focus() or self.site_root

    def selected_dir(self):
        iid = self.selected()
        if not iid:
            return None
        return iid if self.types.get(iid) == "d" else posixpath.dirname(iid)

    def refresh_dir(self, d):
        if d in self.types and self.tree.exists(d):
            self.load_children(d)
            self.tree.item(d, open=True)

    def refresh_selected(self):
        d = self.selected_dir()
        if d:
            self.refresh_dir(d)

    def chown_like(self, path, ref):
        try:
            s = self.sftp.stat(ref)
            self.sftp.chown(path, s.st_uid, s.st_gid)
        except Exception:
            pass

    def new_file(self):
        d = self.selected_dir()
        if not d:
            return
        name = self.ask("New file", f"File name (inside {d}):")
        if not name or not name.strip():
            return
        p = posixpath.join(d, name.strip())
        try:
            self.sftp.open(p, "wb").close()
            self.chown_like(p, d)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        self.refresh_dir(d)
        self.open_file(p)

    def new_folder(self):
        d = self.selected_dir()
        if not d:
            return
        name = self.ask("New folder", f"Folder name (inside {d}):")
        if not name or not name.strip():
            return
        p = posixpath.join(d, name.strip())
        try:
            self.sftp.mkdir(p)
            self.chown_like(p, d)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        self.refresh_dir(d)

    def upload_here(self):
        d = self.selected_dir()
        if not d:
            return
        files = filedialog.askopenfilenames(title="Choose files to upload")
        for f in files:
            try:
                dest = posixpath.join(d, os.path.basename(f))
                self.sftp.put(f, dest)
                self.chown_like(dest, d)
            except Exception as e:
                messagebox.showerror("Upload error", f"{f}\n{e}")
        if files:
            self.refresh_dir(d)
            self.set_status(f"Uploaded {len(files)} file(s) ✓", "ok")

    def rename_item(self):
        iid = self.tree.focus()
        if not iid or iid == self.site_root:
            return
        new = self.ask("Rename", "New name:", posixpath.basename(iid))
        if not new or not new.strip() or new.strip() == posixpath.basename(iid):
            return
        try:
            self.sftp.rename(iid, posixpath.join(posixpath.dirname(iid), new.strip()))
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        self.refresh_dir(posixpath.dirname(iid))

    def download_item(self):
        iid = self.tree.focus()
        if not iid or self.types.get(iid) != "f":
            messagebox.showinfo("Download", "Select a file (not a folder).")
            return
        dest = filedialog.asksaveasfilename(initialfile=posixpath.basename(iid))
        if dest:
            try:
                self.sftp.get(iid, dest)
                self.set_status(f"Downloaded to {dest} ✓", "ok")
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def delete_item(self):
        iid = self.tree.focus()
        if not iid:
            return
        if iid == self.site_root or iid.count("/") < 4:
            messagebox.showwarning("Delete", "Deleting the site root or a folder close to / is not allowed here.")
            return
        if not messagebox.askyesno("Confirm delete", f"Permanently delete?\n\n{iid}", icon="warning"):
            return
        try:
            if self.types.get(iid) == "d":
                _o, err, rc = self.run_priv("rm -rf -- " + shlex.quote(iid))
                if rc != 0:
                    raise RuntimeError(err)
            else:
                self.sftp.remove(iid)
            for p in [p for p in self.tabs if p == iid or p.startswith(iid + "/")]:
                self.force_close(p)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        self.refresh_dir(posixpath.dirname(iid))

    # ---------------------------------------------------------------- editor
    def tab_title(self, path, dirty=False):
        site = site_short_name(path)
        name = posixpath.basename(path)
        return ("● " if dirty else "") + (f"{name}  ·  {site}" if site else name)

    def style_editor(self, info):
        T = self.T
        t, ln = info["text"], info["ln"]
        info["frame"].config(bg=T["ed_bg"])
        t.config(bg=T["ed_bg"], fg=T["ed_fg"], insertbackground=T["ed_fg"], selectbackground=T["sel"],
                 selectforeground=T["ed_fg"], inactiveselectbackground=T["sel"])
        ln.config(bg=T["ln_bg"], fg=T["ln_fg"])
        t.tag_config("curline", background=T["cur"])
        t.tag_config("found", background=T["found_bg"], foreground=T["found_fg"])
        for k in SYN_KINDS:
            t.tag_config("syn_" + k, foreground=T["syn_" + k])
        t.tag_lower("curline")
        t.tag_raise("found")
        t.tag_raise("sel")

    def open_file(self, path):
        if path in self.tabs:
            self.nb.select(self.tabs[path]["frame"])
            return
        try:
            size = self.sftp.stat(path).st_size
            if size > MAX_OPEN_BYTES and not messagebox.askyesno("Large file", f"This file is {size // 1024} KB. Open anyway?"):
                return
            with self.sftp.open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            messagebox.showerror("Error", f"Cannot open the file\n{e}")
            return
        if b"\x00" in data[:8192]:
            messagebox.showwarning("Binary file", "This looks like a binary file (image/archive...). Use Download instead.")
            return
        try:
            txt, enc = data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            txt, enc = data.decode("latin-1"), "latin-1"
        crlf = "\r\n" in txt
        txt = txt.replace("\r\n", "\n")

        frame = tk.Frame(self.nb, bg=self.T["ed_bg"])
        ln = tk.Text(frame, width=4, padx=8, pady=4, takefocus=0, bd=0, highlightthickness=0, state="disabled",
                     cursor="arrow", font=self.efont, wrap="none")
        ln.tag_config("r", justify="right")
        text = tk.Text(frame, undo=True, wrap="none", font=self.efont, bd=0, highlightthickness=0, padx=10, pady=4,
                       tabs=("1.2c",))
        ys = ttk.Scrollbar(frame, orient="vertical")
        xs = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)

        def on_yscroll(first, last):
            ys.set(first, last)
            ln.yview_moveto(first)

        def yview(*a):
            text.yview(*a)
            ln.yview(*a)

        ys.config(command=yview)
        text.config(yscrollcommand=on_yscroll, xscrollcommand=xs.set)
        ln.pack(side="left", fill="y")
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        text.pack(side="left", fill="both", expand=True)

        fname = posixpath.basename(path).lower()
        ext = fname.rsplit(".", 1)[-1] if "." in fname else fname
        info = {"frame": frame, "text": text, "ln": ln, "crlf": crlf, "enc": enc, "dirty": False,
                "backed_up": False, "regex": build_syntax(ext) if ext in SYNTAX_EXT else None,
                "nlines": 0, "hl_job": None}
        self.tabs[path] = info
        self.style_editor(info)
        text.insert("1.0", txt)
        text.edit_reset()
        text.edit_modified(False)
        self.update_lines(info)
        self.highlight(path)

        text.bind("<<Modified>>", lambda e, p=path: self.on_modified(p))
        for ev in ("<KeyRelease>", "<ButtonRelease-1>"):
            text.bind(ev, lambda e, p=path: self.update_cursor(p))
        text.bind("<Control-MouseWheel>", lambda e: (self.change_font(1 if e.delta > 0 else -1), "break")[1])
        ln.bind("<MouseWheel>", lambda e: text.yview_scroll(int(-e.delta / 120), "units") or "break")

        self.nb.add(frame, text=self.tab_title(path))
        self.nb.select(frame)
        self.update_welcome()
        self.update_cursor(path)
        text.focus_set()
        self.set_status(f"Opened {path}")

    def update_lines(self, info):
        t, ln = info["text"], info["ln"]
        n = int(t.index("end-1c").split(".")[0])
        if n == info["nlines"]:
            return
        info["nlines"] = n
        ln.config(state="normal", width=max(3, len(str(n))) + 1)
        ln.delete("1.0", "end")
        ln.insert("1.0", "\n".join(str(i) for i in range(1, n + 1)))
        ln.tag_add("r", "1.0", "end")
        ln.config(state="disabled")
        ln.yview_moveto(t.yview()[0])

    def highlight(self, path):
        info = self.tabs.get(path)
        if not info or not info["regex"]:
            return
        info["hl_job"] = None
        t = info["text"]
        content = t.get("1.0", "end-1c")
        for k in SYN_KINDS:
            t.tag_remove("syn_" + k, "1.0", "end")
        if len(content) > MAX_HIGHLIGHT_CHARS:
            return
        starts = [0] + [m.end() for m in re.finditer("\n", content)]
        for m in info["regex"].finditer(content):
            t.tag_add("syn_" + m.lastgroup, offset_to_index(starts, m.start()), offset_to_index(starts, m.end()))

    def on_modified(self, path):
        info = self.tabs.get(path)
        if not info:
            return
        t = info["text"]
        if not t.edit_modified():
            return
        t.edit_modified(False)
        if not info["dirty"]:
            info["dirty"] = True
            self.nb.tab(info["frame"], text=self.tab_title(path, True))
        self.update_lines(info)
        if info["hl_job"]:
            self.after_cancel(info["hl_job"])
        info["hl_job"] = self.after(350, lambda: self.highlight(path))

    def update_cursor(self, path):
        info = self.tabs.get(path)
        if not info:
            return
        t = info["text"]
        t.tag_remove("curline", "1.0", "end")
        t.tag_add("curline", "insert linestart", "insert lineend+1c")
        self.update_pos()

    def path_of_current(self):
        cur = self.nb.select()
        for p, info in self.tabs.items():
            if str(info["frame"]) == str(cur):
                return p
        return None

    def update_pos(self):
        p = self.path_of_current()
        if p:
            line, col = self.tabs[p]["text"].index("insert").split(".")
            self.lbl_pos.config(text=f"{p}    •    Ln {line}, Col {int(col) + 1}")
        else:
            self.lbl_pos.config(text="")

    def backup_remote(self, path, info):
        """Backup outside the web root, so it can never be downloaded publicly."""
        if info["backed_up"]:
            return
        try:
            home = self.sftp.normalize(".")
            bdir = posixpath.join(home, "panel_backups")
            try:
                self.sftp.mkdir(bdir)
            except IOError:
                pass
            name = time.strftime("%Y%m%d_%H%M%S_") + path.strip("/").replace("/", "__")
            with self.sftp.open(path, "rb") as src, self.sftp.open(posixpath.join(bdir, name), "wb") as dst:
                dst.write(src.read())
            info["backed_up"] = True
        except Exception:
            pass

    def save_current(self):
        p = self.path_of_current()
        if not p:
            return
        info = self.tabs[p]
        text = info["text"].get("1.0", "end-1c")
        if info["crlf"]:
            text = text.replace("\n", "\r\n")
        try:
            self.backup_remote(p, info)
            with self.sftp.open(p, "wb") as f:
                f.write(text.encode(info["enc"], "replace"))
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            self.set_status(f"Save failed: {e}", "err")
            return
        info["dirty"] = False
        self.nb.tab(info["frame"], text=self.tab_title(p))
        self.set_status(f"✓ Saved {p}   ({time.strftime('%H:%M:%S')})", "ok")

    def force_close(self, path):
        info = self.tabs.pop(path, None)
        if info:
            if info["hl_job"]:
                self.after_cancel(info["hl_job"])
            self.nb.forget(info["frame"])
            info["frame"].destroy()
        self.update_welcome()

    def close_current(self):
        p = self.path_of_current()
        if not p:
            return
        if self.tabs[p]["dirty"]:
            ans = messagebox.askyesnocancel("Unsaved changes", "Save before closing?")
            if ans is None:
                return
            if ans:
                self.save_current()
        self.force_close(p)
        self.update_pos()

    def find_text(self):
        p = self.path_of_current()
        if not p:
            return
        t = self.tabs[p]["text"]
        q = self.ask("Find", "Search for:", getattr(self, "last_q", ""))
        if not q:
            return
        self.last_q = q
        t.tag_remove("found", "1.0", "end")
        pos = (t.search(q, t.index("insert +1c"), stopindex="end", nocase=True)
               or t.search(q, "1.0", stopindex="end", nocase=True))
        if not pos:
            self.set_status("No matches", "err")
            return
        t.tag_add("found", pos, f"{pos}+{len(q)}c")
        t.mark_set("insert", pos)
        t.see(pos)
        t.focus_set()
        self.update_cursor(p)

    # ================================================================= USERS
    def build_users(self, parent):
        head = self.header(parent, "Users", "System (Linux) users on the server")
        ttk.Button(head, text="↻  Refresh", command=self.load_users).pack(side="right")
        warn = ttk.Label(parent, text="⚠  Changes are applied directly to the operating system. A control panel "
                                      "like FASTPANEL keeps its own records and will not know about them.",
                         style="Warn.TLabel", wraplength=900, justify="left")
        warn.pack(anchor="w", padx=26, pady=(0, 6))
        parent.bind("<Configure>", lambda e: warn.config(wraplength=max(300, e.width - 60)))
        self.toolbar(parent, [("＋ New user", self.user_create, "Accent.TButton"), "|",
                              ("🔑 Password", self.user_password), ("🔒 Lock / Unlock", self.user_lock),
                              ("🐚 Shell", self.user_shell), ("⭐ Toggle sudo", self.user_sudo)], pady=(4, 2))
        self.toolbar(parent, [("📊 Disk usage", self.user_du), ("📁 Open home in Files", self.user_open_home), "|",
                              ("🗑 Delete user", self.user_delete, "Danger.TButton")], pady=(2, 8))
        self.users_tv = self.table(parent, [("user", "Username", 160, "w"), ("uid", "UID", 70, "center"),
                                            ("home", "Home directory", 300, "w"), ("shell", "Shell", 170, "w"),
                                            ("pw", "Password", 110, "center"), ("sudo", "Sudo", 70, "center")])

    def load_users(self):
        def fn():
            out, _e, _r = self.run_priv("getent passwd; echo '#---'; passwd -Sa 2>/dev/null; echo '#---'; "
                                        "getent group sudo; getent group wheel")
            return out

        def done(res, err):
            if err:
                self.set_status(f"✖ {err}", "err")
                return
            parts = res.split("#---\n")
            passwd = parts[0].splitlines()
            pwstat = {}
            if len(parts) > 1:
                for line in parts[1].splitlines():
                    f = line.split()
                    if len(f) >= 2:
                        pwstat[f[0]] = f[1]
            sudoers, present = set(), []
            if len(parts) > 2:
                for line in parts[2].splitlines():
                    f = line.split(":")
                    if len(f) >= 4:
                        present.append(f[0])
                        sudoers.update(x for x in f[3].split(",") if x)
            self.sudo_group = "sudo" if "sudo" in present else "wheel" if "wheel" in present else "sudo"
            rows, tags = [], []
            self.users_cache = []
            for line in passwd:
                f = line.split(":")
                if len(f) < 7:
                    continue
                name, uid = f[0], int(f[2]) if f[2].isdigit() else -1
                if not (uid == 0 or 1000 <= uid < 65534):
                    continue
                state = {"P": "Set", "L": "Locked", "NP": "None"}.get(pwstat.get(name, ""), "?")
                rows.append((name, uid, f[5], f[6], state, "Yes" if name in sudoers or uid == 0 else "No"))
                tags.append("warn" if state == "Locked" else None)
                self.users_cache.append(name)
            self.fill(self.users_tv, rows, tags)
            self.set_status(f"{len(rows)} users", "ok")

        self.bg(fn, done, "Loading users...")

    def sel_user(self):
        v = self.selected_values(self.users_tv)
        if not v:
            messagebox.showinfo("Users", "Select a user in the table first.")
            return None
        return v

    def user_create(self):
        r = self.form("New user", [
            ("name", "Username", "", "text"), ("pw", "Password", "", "password"),
            ("home", "Home directory (optional)", "", "text"),
            ("shell", "Shell", "/bin/bash", "combo", ["/bin/bash", "/bin/sh", "/usr/sbin/nologin", "/bin/false"]),
            ("sudo", "Give sudo (administrator) rights", False, "check")],
            ok_text="Create user", note="Creates a Linux user. Lowercase letters, digits, - and _ only.")
        if not r:
            return
        name, pw, home, shell = r["name"].strip(), r["pw"], r["home"].strip(), r["shell"].strip() or "/bin/bash"
        if not USER_RE.match(name):
            messagebox.showerror("Invalid", "Invalid username.")
            return
        if len(pw) < 8:
            messagebox.showerror("Invalid", "Use a password with at least 8 characters.")
            return
        if home and not PATH_RE.match(home):
            messagebox.showerror("Invalid", "Invalid home directory path.")
            return
        if not PATH_RE.match(shell):
            messagebox.showerror("Invalid", "Invalid shell path.")
            return
        cmd = (f"useradd -m -s {shlex.quote(shell)} " + (f"-d {shlex.quote(home)} " if home else "") +
               shlex.quote(name) + " && chpasswd" +
               (f" && usermod -aG {self.sudo_group} {shlex.quote(name)}" if r["sudo"] else ""))
        self.do_cmd(cmd, f"User {name} created", then=self.load_users, stdin=f"{name}:{pw}\n")

    def user_password(self):
        v = self.sel_user()
        if not v:
            return
        r = self.form(f"Password for {v[0]}", [("a", "New password", "", "password"),
                                               ("b", "Repeat password", "", "password")], ok_text="Change password")
        if not r:
            return
        if r["a"] != r["b"] or len(r["a"]) < 8:
            messagebox.showerror("Invalid", "Passwords must match and be at least 8 characters.")
            return
        self.do_cmd("chpasswd", f"Password changed for {v[0]}", stdin=f"{v[0]}:{r['a']}\n")

    def user_lock(self):
        v = self.sel_user()
        if not v:
            return
        name, locked = v[0], v[4] == "Locked"
        if name == self.login_user and not locked:
            messagebox.showwarning("Lock", "You are logged in as this user. Locking the password may lock you out.")
        flag, word = ("-U", "unlocked") if locked else ("-L", "locked")
        self.do_cmd(f"usermod {flag} {shlex.quote(name)}", f"{name} {word}", then=self.load_users)

    def user_shell(self):
        v = self.sel_user()
        if not v:
            return
        r = self.form(f"Shell for {v[0]}", [("sh", "Login shell", v[3], "combo",
                                             ["/bin/bash", "/bin/sh", "/usr/sbin/nologin", "/bin/false"])],
                      ok_text="Change shell")
        if not r:
            return
        sh = r["sh"].strip()
        if not PATH_RE.match(sh):
            messagebox.showerror("Invalid", "Invalid shell path.")
            return
        self.do_cmd(f"usermod -s {shlex.quote(sh)} {shlex.quote(v[0])}", f"Shell changed for {v[0]}",
                    then=self.load_users)

    def user_sudo(self):
        v = self.sel_user()
        if not v:
            return
        name, has = v[0], v[5] == "Yes"
        if name == "root":
            messagebox.showinfo("Sudo", "root already has full rights.")
            return
        if has and name == self.login_user:
            messagebox.showwarning("Sudo", "You can't remove sudo from the account you are logged in with.")
            return
        g = self.sudo_group
        cmd = f"gpasswd -d {shlex.quote(name)} {g}" if has else f"usermod -aG {g} {shlex.quote(name)}"
        self.do_cmd(cmd, f"sudo {'removed from' if has else 'granted to'} {name}", then=self.load_users)

    def user_du(self):
        v = self.sel_user()
        if not v:
            return

        def fn():
            out, _e, _r = self.run_priv(f"du -sh -- {shlex.quote(v[2])} 2>/dev/null | cut -f1", timeout=300)
            return out.strip()

        self.bg(fn, lambda res, err: self.set_status(
            f"✖ {err}" if err else f"{v[0]}: {res or '?'} used in {v[2]}", "err" if err else "ok"),
            f"Calculating size of {v[2]} ...")

    def user_open_home(self):
        v = self.sel_user()
        if not v:
            return
        self.show_page("files")
        self.open_site(v[2])

    def user_delete(self):
        v = self.sel_user()
        if not v:
            return
        name, uid = v[0], int(v[1])
        if name == "root" or uid < 1000:
            messagebox.showerror("Delete", "System accounts can't be deleted here.")
            return
        if name == self.login_user:
            messagebox.showerror("Delete", "You can't delete the account you are logged in with.")
            return
        r = self.form("Delete user", [("home", f"Also delete the home directory and ALL its files ({v[2]})", False, "check")],
                      ok_text="Continue", danger=True,
                      note=f"You are about to delete the user '{name}'. Running processes of this user will be killed.")
        if not r:
            return
        if not self.confirm_typed("Confirm deletion", f"This cannot be undone.", name):
            return
        flag = "-r " if r["home"] else ""
        self.do_cmd(f"pkill -KILL -u {shlex.quote(name)}; userdel {flag}{shlex.quote(name)}",
                    f"User {name} deleted", then=self.load_users)

    # =============================================================== SERVICES
    def build_services(self, parent):
        head = self.header(parent, "Services", "Start, stop and restart system services")
        ttk.Button(head, text="↻  Refresh", command=self.load_services).pack(side="right")
        self.toolbar(parent, [("▶ Start", lambda: self.svc_act("start")),
                              ("■ Stop", lambda: self.svc_act("stop")),
                              ("⟳ Restart", lambda: self.svc_act("restart"), "Accent.TButton"),
                              ("↺ Reload", lambda: self.svc_act("reload")), "|",
                              ("Enable at boot", lambda: self.svc_act("enable")),
                              ("Disable at boot", lambda: self.svc_act("disable")), "|",
                              ("📜 Logs", self.svc_logs)], pady=(6, 4))
        frow = ttk.Frame(parent)
        frow.pack(fill="x", padx=24, pady=(2, 8))
        ttk.Label(frow, text="Search:").pack(side="left", padx=(0, 8))
        self.v_svc_filter = tk.StringVar()
        self.v_svc_filter.trace_add("write", lambda *a: self.fill_services())
        ttk.Entry(frow, textvariable=self.v_svc_filter, width=30).pack(side="left")
        self.v_svc_web = tk.BooleanVar(value=True)
        ttk.Checkbutton(frow, text="Web stack only (uncheck to see every service)", variable=self.v_svc_web,
                        command=self.fill_services).pack(side="left", padx=16)
        self.svc_tv = self.table(parent, [("unit", "Service", 260, "w"), ("active", "Status", 90, "center"),
                                          ("sub", "State", 90, "center"), ("desc", "Description", 520, "w")])
        self.svc_all = []

    def load_services(self):
        def fn():
            out, _e, _r = self.R.run("systemctl list-units --type=service --all --no-legend --plain --no-pager",
                                     timeout=40)
            rows = []
            for line in out.splitlines():
                p = line.lstrip("● ").split(None, 4)
                if len(p) >= 4 and p[1] != "not-found":
                    rows.append((p[0], p[2], p[3], p[4] if len(p) > 4 else ""))
            return rows

        def done(res, err):
            if err:
                self.set_status(f"✖ {err}", "err")
                return
            self.svc_all = res
            self.fill_services()
            self.set_status(f"{len(res)} services", "ok")

        self.bg(fn, done, "Loading services...")

    def fill_services(self):
        q = self.v_svc_filter.get().strip().lower()
        web = self.v_svc_web.get()
        rows, tags = [], []
        for unit, active, sub, desc in sorted(self.svc_all):
            name = unit.replace(".service", "")
            if web and not any(k in name.lower() for k in WEB_STACK):
                continue
            if q and q not in name.lower() and q not in desc.lower():
                continue
            rows.append((name, active, sub, desc))
            tags.append("ok" if active == "active" else "bad" if active == "failed" else None)
        self.fill(self.svc_tv, rows, tags)

    def svc_act(self, action):
        v = self.selected_values(self.svc_tv)
        if not v:
            messagebox.showinfo("Services", "Select a service first.")
            return
        name = v[0]
        if action in ("stop", "disable", "restart") and name in ("ssh", "sshd", "ssh.socket"):
            if not messagebox.askyesno("Careful", f"'{name}' is the SSH service. Stopping it will disconnect "
                                                  f"this panel and may lock you out.\n\nContinue?", icon="warning"):
                return
        self.do_cmd(f"systemctl {action} {shlex.quote(name)}", f"{action} {name}: done",
                    then=self.load_services, busy=f"{action} {name}...")

    def svc_logs(self):
        v = self.selected_values(self.svc_tv)
        if not v:
            messagebox.showinfo("Services", "Select a service first.")
            return
        self.show_page("logs")
        self.logs_set_journal(v[0])

    # ============================================================== DATABASES
    def build_databases(self, parent):
        head = self.header(parent, "Databases", "MySQL / MariaDB databases and users")
        ttk.Button(head, text="↻  Refresh", command=self.refresh_db).pack(side="right")
        self.db_msg = ttk.Label(parent, text="", style="Warn.TLabel")
        self.db_msg.pack(anchor="w", padx=26)
        self.toolbar(parent, [("＋ New database", self.db_create, "Accent.TButton"),
                              ("💾 Backup (.sql)", self.db_backup), ("🗑 Drop database", self.db_drop, "Danger.TButton"),
                              "|", ("🔌 MySQL login...", self.db_login)])
        ttk.Label(parent, text="Databases", style="Head.TLabel").pack(anchor="w", padx=26, pady=(4, 4))
        self.db_tv = self.table(parent, [("name", "Database", 300, "w"), ("size", "Size (MB)", 110, "e"),
                                         ("tables", "Tables", 90, "e")], pady=(0, 8))
        self.toolbar(parent, [("＋ New user", self.dbu_create), ("🔑 Set password", self.dbu_password),
                              ("🛡 Grant access to a database", self.dbu_grant),
                              ("🗑 Drop user", self.dbu_drop, "Danger.TButton")], pady=(2, 6))
        ttk.Label(parent, text="MySQL users", style="Head.TLabel").pack(anchor="w", padx=26, pady=(0, 4))
        self.dbu_tv = self.table(parent, [("user", "User", 300, "w"), ("host", "Host", 200, "w")], pady=(0, 14))

    def my_base(self, prog="mysql"):
        return prog + (f" --defaults-extra-file={shlex.quote(self.my_cnf)}" if self.my_cnf else "")

    def mysql_run(self, sql, batch=True):
        cmd = self.my_base() + (" -N -B" if batch else "")
        out, err, rc = self.run_priv(cmd, stdin=sql, timeout=120)
        if rc != 0:
            raise RuntimeError(err.strip() or "mysql command failed")
        return out

    def refresh_db(self):
        def fn():
            dbs = self.mysql_run(
                "SELECT s.schema_name, IFNULL(ROUND(SUM(t.data_length+t.index_length)/1024/1024,2),0), "
                "COUNT(t.table_name) FROM information_schema.schemata s LEFT JOIN information_schema.tables t "
                "ON t.table_schema=s.schema_name GROUP BY s.schema_name ORDER BY 1;")
            users = self.mysql_run("SELECT user, host FROM mysql.user ORDER BY user, host;")
            return dbs, users

        def done(res, err):
            if err:
                self.db_msg.config(text=f"Cannot connect to MySQL: {err}   →   click “MySQL login...” to enter credentials.")
                self.set_status("✖ MySQL not reachable", "err")
                return
            self.db_msg.config(text="")
            dbs, users = res
            rows, tags = [], []
            for line in dbs.splitlines():
                f = line.split("\t")
                if len(f) == 3:
                    rows.append((f[0], f[1], f[2]))
                    tags.append("warn" if f[0] in SYSTEM_SCHEMAS else None)
            self.fill(self.db_tv, rows, tags)
            self.fill(self.dbu_tv, [tuple(l.split("\t")) for l in users.splitlines() if "\t" in l])
            self.set_status(f"{len(rows)} databases", "ok")

        self.bg(fn, done, "Loading databases...")

    def db_login(self):
        r = self.form("MySQL login", [("u", "MySQL user", "root", "text"), ("p", "MySQL password", "", "password")],
                      ok_text="Use these", note="Needed only if root can't log in over the socket. The password is "
                      "kept in a private (chmod 600) temp file on the server until you close this panel.")
        if not r:
            return
        esc = r["p"].replace("\\", "\\\\").replace('"', '\\"')
        content = f'[client]\nuser={r["u"]}\npassword="{esc}"\nhost=localhost\n'
        path = "/tmp/.panel_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=10)) + ".cnf"
        try:
            with self.sftp.open(path, "w") as f:
                f.write(content)
            self.sftp.chmod(path, 0o600)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        self.remove_my_cnf()
        self.my_cnf = path
        self.refresh_db()

    def remove_my_cnf(self):
        if self.my_cnf and self.sftp:
            try:
                self.sftp.remove(self.my_cnf)
            except Exception:
                pass
        self.my_cnf = None

    def run_sql(self, sql, ok_msg):
        def fn():
            return self.mysql_run(sql)

        def done(res, err):
            if err:
                self.set_status(f"✖ {err}", "err")
                messagebox.showerror("MySQL error", str(err))
            else:
                self.set_status(f"✓ {ok_msg}", "ok")
            self.refresh_db()

        self.bg(fn, done, "Running...")

    def db_create(self):
        r = self.form("New database", [
            ("name", "Database name", "", "text"),
            ("user", "Also create / use this MySQL user (optional)", "", "text"),
            ("pw", "Password for that user (if new)", "", "password")],
            ok_text="Create", note="Creates the database (utf8mb4). If you give a user, it gets full rights on it.")
        if not r:
            return
        name, user, pw = r["name"].strip(), r["user"].strip(), r["pw"]
        if not DBNAME_RE.match(name):
            messagebox.showerror("Invalid", "Database name: letters, digits and _ only (max 64).")
            return
        sql = f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
        if user:
            if not DBNAME_RE.match(user):
                messagebox.showerror("Invalid", "User name: letters, digits and _ only.")
                return
            if pw:
                sql += f"CREATE USER IF NOT EXISTS {sql_str(user)}@'localhost' IDENTIFIED BY {sql_str(pw)};\n"
            sql += f"GRANT ALL PRIVILEGES ON `{name}`.* TO {sql_str(user)}@'localhost';\nFLUSH PRIVILEGES;\n"
        self.run_sql(sql, f"Database {name} created")

    def sel_db(self):
        v = self.selected_values(self.db_tv)
        if not v:
            messagebox.showinfo("Databases", "Select a database in the table first.")
            return None
        return v[0]

    def db_drop(self):
        name = self.sel_db()
        if not name:
            return
        if name in SYSTEM_SCHEMAS or not DBNAME_RE.match(name):
            messagebox.showerror("Drop", "System databases can't be dropped from here.")
            return
        if not self.confirm_typed("Drop database", f"This permanently deletes the database '{name}' and all its data.", name):
            return
        self.run_sql(f"DROP DATABASE `{name}`;", f"Database {name} dropped")

    def db_backup(self):
        name = self.sel_db()
        if not name:
            return
        if not DBNAME_RE.match(name):
            messagebox.showerror("Backup", "Unsupported database name.")
            return
        dest = filedialog.asksaveasfilename(initialfile=f"{name}_{time.strftime('%Y%m%d_%H%M')}.sql",
                                            defaultextension=".sql")
        if not dest:
            return

        def fn():
            cmd = self.priv(self.my_base("mysqldump") + f" --single-transaction --routines --triggers {name}")
            i, o, e = self.ssh.exec_command(cmd, timeout=600)
            total = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = o.channel.recv(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
            rc = o.channel.recv_exit_status()
            if rc != 0:
                raise RuntimeError(e.read().decode("utf-8", "replace").strip() or "mysqldump failed")
            return total

        self.bg(fn, lambda res, err: self.set_status(
            f"✖ {err}" if err else f"✓ Backup saved: {dest} ({res / 1024 / 1024:.1f} MB)", "err" if err else "ok"),
            f"Backing up {name} ...")

    def dbu_create(self):
        r = self.form("New MySQL user", [("u", "Username", "", "text"), ("p", "Password", "", "password"),
                                         ("h", "Host", "localhost", "text")], ok_text="Create")
        if not r:
            return
        if not DBNAME_RE.match(r["u"].strip()) or not MYHOST_RE.match(r["h"].strip()) or len(r["p"]) < 8:
            messagebox.showerror("Invalid", "Check the values (password: at least 8 characters).")
            return
        self.run_sql(f"CREATE USER {sql_str(r['u'].strip())}@{sql_str(r['h'].strip())} IDENTIFIED BY {sql_str(r['p'])};",
                     f"User {r['u']} created")

    def sel_dbu(self):
        v = self.selected_values(self.dbu_tv)
        if not v:
            messagebox.showinfo("Databases", "Select a MySQL user first.")
            return None
        return v

    def dbu_password(self):
        v = self.sel_dbu()
        if not v:
            return
        r = self.form(f"Password for {v[0]}@{v[1]}", [("p", "New password", "", "password")], ok_text="Change")
        if not r or len(r["p"]) < 8:
            if r:
                messagebox.showerror("Invalid", "Use at least 8 characters.")
            return
        self.run_sql(f"ALTER USER {sql_str(v[0])}@{sql_str(v[1])} IDENTIFIED BY {sql_str(r['p'])};",
                     f"Password changed for {v[0]}")

    def dbu_grant(self):
        v = self.sel_dbu()
        if not v:
            return
        dbs = [self.db_tv.item(i, "values")[0] for i in self.db_tv.get_children()
               if self.db_tv.item(i, "values")[0] not in SYSTEM_SCHEMAS]
        if not dbs:
            messagebox.showinfo("Grant", "No user databases found.")
            return
        r = self.form("Grant access", [("d", "Database", dbs[0], "choice", dbs)], ok_text="Grant ALL privileges",
                      note=f"Give {v[0]}@{v[1]} full rights on one database.")
        if not r:
            return
        self.run_sql(f"GRANT ALL PRIVILEGES ON `{r['d']}`.* TO {sql_str(v[0])}@{sql_str(v[1])};\nFLUSH PRIVILEGES;",
                     f"Granted {v[0]} access to {r['d']}")

    def dbu_drop(self):
        v = self.sel_dbu()
        if not v:
            return
        if v[0] in ("root", "mysql.sys", "mysql.session", "mysql.infoschema", "debian-sys-maint"):
            messagebox.showerror("Drop user", "This is a system account and can't be dropped here.")
            return
        if not self.confirm_typed("Drop MySQL user", f"Delete the MySQL user {v[0]}@{v[1]}?", v[0]):
            return
        self.run_sql(f"DROP USER {sql_str(v[0])}@{sql_str(v[1])};", f"User {v[0]} dropped")

    # ================================================================== CRON
    def build_cron(self, parent):
        self.header(parent, "Cron Jobs", "View and edit the scheduled tasks of any user")
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=24, pady=(6, 8))
        ttk.Label(bar, text="User:").pack(side="left", padx=(0, 8))
        self.v_cron_user = tk.StringVar()
        self.cron_combo = ttk.Combobox(bar, textvariable=self.v_cron_user, state="readonly", width=24)
        self.cron_combo.pack(side="left")
        self.cron_combo.bind("<<ComboboxSelected>>", lambda e: self.cron_load())
        ttk.Button(bar, text="↻ Reload", command=self.cron_load).pack(side="left", padx=8)
        ttk.Button(bar, text="💾 Save crontab", style="Accent.TButton", command=self.cron_save).pack(side="left")
        ttk.Label(parent, text="Format:  minute  hour  day  month  weekday  command      e.g.  */5 * * * * "
                               "php /var/www/user/data/www/site.com/cron.php",
                  style="Muted.TLabel").pack(anchor="w", padx=26, pady=(0, 6))
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        self.cron_text = tk.Text(frame, font=self.efont, wrap="none", bd=0, highlightthickness=0, padx=12, pady=10,
                                 undo=True)
        sb = ttk.Scrollbar(frame, command=self.cron_text.yview)
        self.cron_text.config(yscrollcommand=sb.set)
        self.cron_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def cron_init(self):
        def fn():
            out, _e, _r = self.run_priv("getent passwd | awk -F: '$3==0 || ($3>=1000 && $3<65534) {print $1}'")
            return out.split()

        def done(res, err):
            if err:
                self.set_status(f"✖ {err}", "err")
                return
            self.cron_combo.config(values=res)
            if res:
                self.v_cron_user.set(self.login_user if self.login_user in res else res[0])
                self.cron_load()

        self.bg(fn, done, "Loading users...")

    def cron_load(self):
        user = self.v_cron_user.get()
        if not user or not USER_RE.match(user):
            return

        def fn():
            out, err, rc = self.run_priv(f"crontab -l -u {shlex.quote(user)}")
            return out if rc == 0 else ""

        def done(res, err):
            self.cron_text.delete("1.0", "end")
            self.cron_text.insert("1.0", res or "")
            self.set_status(f"Crontab of {user} loaded" if res else f"{user} has no crontab yet", "ok")

        self.bg(fn, done)

    def cron_save(self):
        user = self.v_cron_user.get()
        if not user or not USER_RE.match(user):
            return
        text = self.cron_text.get("1.0", "end-1c")
        if text.strip() and not text.endswith("\n"):
            text += "\n"
        if not text.strip():
            if not messagebox.askyesno("Empty crontab", f"Remove the crontab of {user} completely?"):
                return
            self.do_cmd(f"crontab -r -u {shlex.quote(user)}", f"Crontab of {user} removed")
            return
        self.do_cmd(f"crontab -u {shlex.quote(user)} -", f"Crontab of {user} saved", stdin=text)

    # =============================================================== FIREWALL
    def build_firewall(self, parent):
        head = self.header(parent, "Firewall", "UFW rules and Fail2ban")
        ttk.Button(head, text="↻  Refresh", command=self.refresh_firewall).pack(side="right")
        self.fw_state = ttk.Label(parent, text="", style="Muted.TLabel")
        self.fw_state.pack(anchor="w", padx=26)
        self.toolbar(parent, [("＋ Add rule", self.fw_add, "Accent.TButton"),
                              ("🗑 Delete rule", self.fw_delete, "Danger.TButton"), "|",
                              ("Enable firewall", self.fw_enable), ("Disable firewall", self.fw_disable)])
        self.fw_tv = self.table(parent, [("n", "#", 50, "center"), ("to", "To (port)", 260, "w"),
                                         ("action", "Action", 140, "w"), ("src", "From", 300, "w")], pady=(0, 6))
        ttk.Label(parent, text="Fail2ban - banned IP addresses", style="Head.TLabel").pack(anchor="w", padx=26, pady=(8, 4))
        self.toolbar(parent, [("Unban selected IP", self.f2b_unban)], pady=(0, 6))
        self.f2b_tv = self.table(parent, [("jail", "Jail", 200, "w"), ("ip", "Banned IP", 300, "w")], pady=(0, 14))

    def refresh_firewall(self):
        def fn():
            ufw, _e, rc = self.run_priv("command -v ufw >/dev/null && ufw status numbered || echo 'NOUFW'")
            f2b, _e, _r = self.run_priv(
                "command -v fail2ban-client >/dev/null || { echo NOF2B; exit 0; }; "
                "for j in $(fail2ban-client status | sed -n 's/.*Jail list:[[:space:]]*//p' | tr -d ','); do "
                "echo \"JAIL:$j\"; fail2ban-client status $j | sed -n 's/.*Banned IP list:[[:space:]]*//p'; done")
            return ufw, f2b

        def done(res, err):
            if err:
                self.set_status(f"✖ {err}", "err")
                return
            ufw, f2b = res
            rules, status = [], "unknown"
            if "NOUFW" in ufw:
                status = "UFW is not installed"
            else:
                for line in ufw.splitlines():
                    if line.startswith("Status:"):
                        status = line.split(":", 1)[1].strip()
                    m = re.match(r"^\[\s*(\d+)\]\s+(.*)$", line)
                    if m:
                        cols = re.split(r"\s{2,}", m.group(2).strip())
                        rules.append((m.group(1), cols[0], cols[1] if len(cols) > 1 else "",
                                      cols[2] if len(cols) > 2 else ""))
            self.fw_status = status
            self.fw_state.config(text=f"UFW status: {status}",
                                 foreground=self.T["ok"] if status == "active" else self.T["warn"])
            self.fill(self.fw_tv, rules, ["bad" if "DENY" in r[2] else None for r in rules])
            rows, jail = [], None
            if "NOF2B" in f2b:
                pass
            else:
                for line in f2b.splitlines():
                    if line.startswith("JAIL:"):
                        jail = line[5:].strip()
                    elif jail:
                        rows.extend((jail, ip) for ip in line.split())
            self.fill(self.f2b_tv, rows)
            self.set_status("Firewall info updated", "ok")

        self.bg(fn, done, "Reading firewall...")

    def fw_add(self):
        r = self.form("Add firewall rule", [
            ("action", "Action", "allow", "choice", ["allow", "deny"]),
            ("port", "Port (e.g. 80  or  8000:8100)", "", "text"),
            ("proto", "Protocol", "tcp", "choice", ["tcp", "udp", "any"]),
            ("src", "From IP (optional, blank = anywhere)", "", "text")], ok_text="Add rule")
        if not r:
            return
        port, proto, src = r["port"].strip(), r["proto"], r["src"].strip()
        if not re.match(r"^\d{1,5}(:\d{1,5})?$", port):
            messagebox.showerror("Invalid", "Invalid port.")
            return
        if src and not re.match(r"^[0-9a-fA-F:./]+$", src):
            messagebox.showerror("Invalid", "Invalid IP address.")
            return
        p = "" if proto == "any" else f" proto {proto}"
        if src:
            cmd = f"ufw {r['action']} from {shlex.quote(src)} to any port {port}{p}"
        else:
            cmd = f"ufw {r['action']} {port}" + ("" if proto == "any" else f"/{proto}")
        self.do_cmd(cmd, "Rule added", then=self.refresh_firewall)

    def fw_delete(self):
        v = self.selected_values(self.fw_tv)
        if not v:
            messagebox.showinfo("Firewall", "Select a rule first.")
            return
        if not messagebox.askyesno("Delete rule", f"Delete rule #{v[0]}  ({v[1]}  {v[2]}  {v[3]})?"):
            return
        self.do_cmd(f"ufw --force delete {int(v[0])}", "Rule deleted", then=self.refresh_firewall)

    def fw_enable(self):
        def fn():
            out, _e, _r = self.run_priv("grep -iE '^[[:space:]]*Port[[:space:]]+[0-9]+' /etc/ssh/sshd_config | "
                                        "awk '{print $2}' | head -1")
            return (out.strip() or "22")

        def done(port, err):
            port = port if (not err and port.isdigit()) else "22"
            if not messagebox.askyesno(
                    "Enable firewall",
                    f"This will first allow SSH (port {port}/tcp) so you don't lock yourself out, then enable UFW.\n\n"
                    "Make sure you also allow the other ports you need (80, 443 and your panel port, e.g. 8888).\n\n"
                    "Continue?", icon="warning"):
                return
            self.do_cmd(f"ufw allow {port}/tcp && ufw --force enable", "Firewall enabled", then=self.refresh_firewall)

        self.bg(fn, done)

    def fw_disable(self):
        if messagebox.askyesno("Disable firewall", "Disable UFW? The server will accept connections on all ports."):
            self.do_cmd("ufw disable", "Firewall disabled", then=self.refresh_firewall)

    def f2b_unban(self):
        v = self.selected_values(self.f2b_tv)
        if not v:
            messagebox.showinfo("Fail2ban", "Select a banned IP first.")
            return
        if not re.match(r"^[0-9a-fA-F:.]+$", v[1]) or not re.match(r"^[\w-]+$", v[0]):
            return
        self.do_cmd(f"fail2ban-client set {v[0]} unbanip {v[1]}", f"{v[1]} unbanned", then=self.refresh_firewall)

    # ==================================================================== SSL
    def build_ssl(self, parent):
        self.header(parent, "SSL Certificates", "Checks the certificate each site really serves on port 443")
        self.toolbar(parent, [("Check all sites", self.ssl_check_all, "Accent.TButton"),
                              ("Check selected", self.ssl_check_selected)])
        self.ssl_tv = self.table(parent, [("domain", "Domain", 280, "w"), ("exp", "Expires", 150, "center"),
                                          ("days", "Days left", 90, "center"), ("issuer", "Issuer", 260, "w"),
                                          ("state", "Status", 190, "w")])

    def ssl_init(self):
        def then():
            doms = sorted({n.split()[0] for n in self.sites if DOMAIN_RE.match(n.split()[0])})
            self.fill(self.ssl_tv, [(d, "—", "—", "—", "not checked") for d in doms])
            self.set_status(f"{len(doms)} domains. Click “Check all sites”.", "ok")

        self.load_sites(then=then)

    def ssl_check(self, domain):
        out, _e, _r = self.R.run(
            f"timeout 10 openssl s_client -servername {shlex.quote(domain)} -connect {shlex.quote(domain)}:443 "
            f"</dev/null 2>/dev/null | openssl x509 -noout -enddate -issuer 2>/dev/null", timeout=30)
        m = re.search(r"notAfter=(.+)", out)
        if not m:
            return (domain, "—", "—", "—", "no HTTPS / unreachable"), "bad"
        raw = m.group(1).strip().replace(" GMT", "")
        try:
            end = dt.datetime.strptime(re.sub(r"\s+", " ", raw), "%b %d %H:%M:%S %Y")
        except ValueError:
            return (domain, raw, "?", "?", "unreadable date"), "warn"
        days = (end - dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)).days
        im = re.search(r"issuer=.*?(?:CN\s*=\s*|O\s*=\s*)([^,/\n]+)", out)
        issuer = im.group(1).strip() if im else "?"
        state = "expired" if days < 0 else "renew soon" if days < 14 else "expiring" if days < 30 else "OK"
        tag = "bad" if days < 14 else "warn" if days < 30 else "ok"
        return (domain, end.strftime("%Y-%m-%d"), str(days), issuer, state), tag

    def ssl_run(self, domains):
        def fn():
            for d in domains:
                self.post(lambda d=d: self.set_status(f"Checking {d} ..."))
                row, tag = self.ssl_check(d)
                self.post(lambda row=row, tag=tag: self.ssl_update_row(row, tag))
            return len(domains)

        self.bg(fn, lambda res, err: self.set_status(f"✖ {err}" if err else f"✓ Checked {res} domain(s)",
                                                     "err" if err else "ok"))

    def ssl_update_row(self, row, tag):
        for iid in self.ssl_tv.get_children():
            if self.ssl_tv.item(iid, "values")[0] == row[0]:
                self.ssl_tv.item(iid, values=row, tags=(tag,))
                return

    def ssl_check_all(self):
        self.ssl_run([self.ssl_tv.item(i, "values")[0] for i in self.ssl_tv.get_children()])

    def ssl_check_selected(self):
        v = self.selected_values(self.ssl_tv)
        if v:
            self.ssl_run([v[0]])
        else:
            messagebox.showinfo("SSL", "Select a domain first.")

    # =================================================================== LOGS
    def build_logs(self, parent):
        head = self.header(parent, "Logs", "Read and filter log files, or follow them live")
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=24, pady=(6, 6))
        self.v_log_file = tk.StringVar()
        self.log_combo = ttk.Combobox(bar, textvariable=self.v_log_file, state="readonly", width=52)
        self.log_combo.pack(side="left")
        self.log_combo.bind("<<ComboboxSelected>>", lambda e: self.logs_refresh())
        ttk.Label(bar, text="Lines:").pack(side="left", padx=(12, 4))
        self.v_log_n = tk.StringVar(value="300")
        ttk.Combobox(bar, textvariable=self.v_log_n, values=["100", "300", "1000", "3000"], width=6,
                     state="readonly").pack(side="left")
        ttk.Label(bar, text="Filter:").pack(side="left", padx=(12, 4))
        self.v_log_grep = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.v_log_grep, width=22)
        ent.pack(side="left")
        ent.bind("<Return>", lambda e: self.logs_refresh())
        ttk.Button(bar, text="↻ Refresh", command=self.logs_refresh, style="Accent.TButton").pack(side="left", padx=8)
        self.v_log_follow = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Follow (every 3 s)", variable=self.v_log_follow).pack(side="left", padx=6)
        self.log_mode = ("file", None)
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        self.logs_text = tk.Text(frame, font=self.tfont, wrap="none", state="disabled", bd=0, highlightthickness=0,
                                 padx=10, pady=8)
        ys = ttk.Scrollbar(frame, command=self.logs_text.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.logs_text.xview)
        self.logs_text.config(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.logs_text.pack(side="left", fill="both", expand=True)

    def logs_init(self):
        def fn():
            out, _e, _r = self.run_priv(
                "ls -1 /var/log/nginx/*.log /var/log/apache2/*.log /var/log/mysql/*.log /var/log/mariadb/*.log "
                "/var/log/fastpanel2/*.log /var/log/syslog /var/log/auth.log /var/log/fail2ban.log /var/log/ufw.log "
                "/var/www/*/logs/* /var/www/*/data/logs/* 2>/dev/null | sort -u")
            return [l.strip() for l in out.splitlines() if l.strip()]

        def done(res, err):
            if err:
                self.set_status(f"✖ {err}", "err")
                return
            self.log_combo.config(values=res)
            if res and not self.v_log_file.get():
                pref = next((x for x in res if x.endswith("nginx/error.log")), res[0])
                self.v_log_file.set(pref)
                self.logs_refresh()

        self.bg(fn, done, "Looking for log files...")

    def logs_set_journal(self, unit):
        self.log_mode = ("journal", unit)
        self.v_log_file.set(f"journalctl -u {unit}")
        self.logs_refresh()

    def logs_refresh(self):
        if self.logs_busy:
            return
        mode, unit = self.log_mode
        if mode == "journal" and self.v_log_file.get().startswith("journalctl"):
            src = f"journalctl -u {shlex.quote(unit)} --no-pager -n {int(self.v_log_n.get())}"
        else:
            self.log_mode = ("file", None)
            f = self.v_log_file.get()
            if not f:
                return
            src = f"tail -n {int(self.v_log_n.get())} -- {shlex.quote(f)}"
        pat = self.v_log_grep.get().strip()
        cmd = src + (f" | grep -iF -- {shlex.quote(pat)}" if pat else "")
        self.logs_busy = True

        def fn():
            out, err, rc = self.run_priv(cmd, timeout=60)
            return out if (rc == 0 or out) else (err or "(empty)")

        def done(res, err):
            self.logs_busy = False
            if err:
                self.set_status(f"✖ {err}", "err")
                return
            t = self.logs_text
            t.config(state="normal")
            t.delete("1.0", "end")
            for line in res.splitlines()[-3000:]:
                low = line.lower()
                tag = "err" if any(k in low for k in ("error", "fail", "crit", "denied", "fatal")) else \
                    "warn" if "warn" in low else ()
                t.insert("end", line + "\n", tag)
            t.config(state="disabled")
            t.see("end")
            self.set_status(f"Log updated {time.strftime('%H:%M:%S')}", "ok")

        self.bg(fn, done)

    def logs_tick(self):
        if self.current_page == "logs" and self.ssh:
            if self.v_log_follow.get():
                self.logs_refresh()
            self.schedule("logs_job", 3000, self.logs_tick)

    # =============================================================== TERMINAL
    def build_terminal(self, parent):
        self.header(parent, "Terminal", "Run commands on the server (non-interactive: no top / vim / nano)")
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=24, pady=(6, 6))
        self.term_out = tk.Text(frame, font=self.tfont, wrap="word", state="disabled", bd=0, highlightthickness=0,
                                padx=12, pady=10)
        sb = ttk.Scrollbar(frame, command=self.term_out.yview)
        self.term_out.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.term_out.pack(side="left", fill="both", expand=True)
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=24, pady=(0, 16))
        self.lbl_cwd = ttk.Label(row, text="~ $", style="Muted.TLabel")
        self.lbl_cwd.pack(side="left", padx=(0, 8))
        self.v_cmd = tk.StringVar()
        self.term_entry = ttk.Entry(row, textvariable=self.v_cmd)
        self.term_entry.pack(side="left", fill="x", expand=True)
        self.term_entry.bind("<Return>", lambda e: self.term_run())
        self.term_entry.bind("<Up>", lambda e: self.term_history(-1))
        self.term_entry.bind("<Down>", lambda e: self.term_history(1))
        ttk.Button(row, text="▶  Run", style="Accent.TButton", command=self.term_run).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Clear", command=self.term_clear).pack(side="left", padx=(6, 0))

    def term_focus(self):
        self.term_entry.focus_set()

    def term_write(self, s, tag=None):
        self.term_out.config(state="normal")
        self.term_out.insert("end", s, tag or ())
        self.term_out.see("end")
        self.term_out.config(state="disabled")

    def term_clear(self):
        self.term_out.config(state="normal")
        self.term_out.delete("1.0", "end")
        self.term_out.config(state="disabled")

    def term_history(self, d):
        if not self.term_hist:
            return "break"
        self.term_hidx = max(0, min(len(self.term_hist), self.term_hidx + d))
        self.v_cmd.set(self.term_hist[self.term_hidx] if self.term_hidx < len(self.term_hist) else "")
        self.term_entry.icursor("end")
        return "break"

    def term_run(self):
        cmd = self.v_cmd.get().strip()
        if not cmd:
            return
        self.v_cmd.set("")
        self.term_hist.append(cmd)
        self.term_hidx = len(self.term_hist)
        cwd = self.term_cwd
        self.term_write(f"\n{self.lbl_cwd.cget('text')} {cmd}\n", "cmd")
        full = (f"cd {shlex.quote(cwd)} && " if cwd else "") + cmd + '\n__rc=$?; echo "::CWD::$PWD"; exit $__rc'

        def fn():
            return self.R.run(self.priv(full) if not self.is_root else full, timeout=120)

        def done(res, err):
            if err:
                self.term_write(f"Error: {err}\n", "err")
                return
            out, e, rc = res
            lines = out.splitlines()
            if lines and lines[-1].startswith("::CWD::"):
                self.term_cwd = lines.pop()[7:]
                self.lbl_cwd.config(text=f"{self.term_cwd} $")
            if lines:
                self.term_write("\n".join(lines) + "\n")
            if e:
                self.term_write(e, "err")

        self.bg(fn, done)

    # ================================================================== close
    def on_close(self):
        dirty = [p for p, i in self.tabs.items() if i["dirty"]]
        if dirty and not messagebox.askyesno("Exit", f"{len(dirty)} file(s) have unsaved changes. Exit anyway?"):
            return
        self.remove_my_cnf()
        try:
            self.sftp.close()
            self.ssh.close()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
