#!/usr/bin/env python3
"""
FlashPull - fast multi-connection download manager
Run:  python flashpull.py        (Python 3.8+, no extra packages needed)
"""
import os, sys, json, time, uuid, re, threading, subprocess, datetime
import urllib.request, urllib.parse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP = "FlashPull"
UA = "Mozilla/5.0 (FlashPull/1.0)"
HOME = os.path.join(os.path.expanduser("~"), ".flashpull")
os.makedirs(HOME, exist_ok=True)
STATE_FILE = os.path.join(HOME, "downloads.json")
CFG_FILE = os.path.join(HOME, "settings.json")

CATS = {
    "Video": (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".3gp"),
    "Music": (".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma"),
    "Compressed": (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"),
    "Documents": (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".epub", ".csv"),
    "Programs": (".exe", ".msi", ".apk", ".dmg", ".deb", ".rpm", ".appimage"),
    "Images": (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".psd"),
}

# ---------- colours ----------
BG, PANEL, CARD = "#0f1220", "#161a2e", "#1c2140"
FG, MUTED = "#e8ebff", "#8a90b8"
ACCENT, ACCENT2, OK, WARN, ERR = "#00e0c6", "#7c5cff", "#2ee59d", "#ffb347", "#ff5c7a"


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def fmt_size(n):
    if not n:
        return "—"
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_time(s):
    if s is None or s < 0 or s > 86400 * 30:
        return "—"
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def category_of(name):
    ext = os.path.splitext(name)[1].lower()
    for c, exts in CATS.items():
        if ext in exts:
            return c
    return "Other"


def clean_name(n):
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", n).strip(" .")
    return n or "download"


def unique_path(folder, name):
    base, ext = os.path.splitext(name)
    p, i = os.path.join(folder, name), 1
    while os.path.exists(p) or os.path.exists(p + ".part"):
        p = os.path.join(folder, f"{base} ({i}){ext}")
        i += 1
    return p


# ---------- download engine ----------
class Download:
    def __init__(self, url, base_folder, d=None):
        self.id = uuid.uuid4().hex[:8]
        self.url = url
        self.base_folder = base_folder
        self.name = None
        self.path = None
        self.size = None
        self.resumable = True
        self.segs = []
        self.status = "Queued"
        self.error = ""
        self.added = datetime.datetime.now().strftime("%d %b %H:%M")
        self.speed = 0.0
        self.stop_ev = threading.Event()
        self.failed = False
        self.thread = None
        if d:
            self.__dict__.update({k: v for k, v in d.items() if k in (
                "id", "url", "base_folder", "name", "path", "size", "resumable", "segs", "status", "added")})
            if self.status in ("Downloading", "Queued") and self.path:
                self.status = "Paused"

    def to_dict(self):
        return {k: getattr(self, k) for k in (
            "id", "url", "base_folder", "name", "path", "size", "resumable", "segs", "status", "added")}

    @property
    def done(self):
        if self.status == "Complete" and self.size:
            return self.size
        return sum(max(0, s[2] - s[0]) for s in self.segs)

    @property
    def category(self):
        return category_of(self.name or self.url.split("?")[0])

    def probe(self, connections):
        req = urllib.request.Request(self.url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            cr = r.headers.get("Content-Range")
            size = 0
            if r.status == 206 and cr and "/" in cr:
                try:
                    size = int(cr.split("/")[-1])
                    self.resumable = True
                except ValueError:
                    self.resumable = False
            else:
                self.resumable = False
                size = int(r.headers.get("Content-Length") or 0)
            cd = r.headers.get("Content-Disposition", "")
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
            name = urllib.parse.unquote(m.group(1)) if m else \
                urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(r.geturl()).path))
            self.name = clean_name(name or "download")
        self.size = size
        folder = os.path.join(self.base_folder, category_of(self.name))
        os.makedirs(folder, exist_ok=True)
        self.path = unique_path(folder, self.name)
        self.name = os.path.basename(self.path)
        if self.resumable and size:
            n = max(1, min(connections, size // (512 * 1024) or 1))
            part = size // n
            self.segs = [[i * part, (i + 1) * part - 1 if i < n - 1 else size - 1, i * part] for i in range(n)]
        else:
            self.segs = [[0, size - 1 if size else 2 ** 62, 0]]

    def start(self, connections):
        if self.status == "Downloading":
            return
        self.stop_ev = threading.Event()
        self.failed = False
        self.error = ""
        self.status = "Downloading"
        self.thread = threading.Thread(target=self._run, args=(connections,), daemon=True)
        self.thread.start()

    def pause(self):
        if self.status in ("Downloading", "Queued"):
            self.stop_ev.set()
            self.status = "Paused"

    def _run(self, connections):
        try:
            if not self.path:
                self.probe(connections)
            part = self.path + ".part"
            if not os.path.exists(part):
                with open(part, "wb") as f:
                    if self.size and self.resumable:
                        f.seek(self.size - 1)
                        f.write(b"\0")
            workers = []
            for i, s in enumerate(self.segs):
                if s[2] <= s[1]:
                    t = threading.Thread(target=self._seg, args=(i, part), daemon=True)
                    t.start()
                    workers.append(t)
            last, t0 = self.done, time.time()
            while any(w.is_alive() for w in workers):
                time.sleep(0.4)
                now = time.time()
                cur = self.done
                inst = (cur - last) / max(now - t0, 1e-6)
                self.speed = inst if self.speed == 0 else self.speed * 0.6 + inst * 0.4
                last, t0 = cur, now
            self.speed = 0
            if self.failed:
                self.status = "Error"
            elif self.stop_ev.is_set():
                self.status = "Paused"
            elif all(s[2] > s[1] for s in self.segs):
                if not self.resumable and not self.size:
                    self.size = self.done
                os.replace(part, self.path)
                self.status = "Complete"
            else:
                self.status = "Error"
                self.error = "Incomplete"
        except Exception as e:
            self.speed = 0
            self.status = "Error"
            self.error = str(e)[:80]

    def _seg(self, i, part):
        s = self.segs[i]
        tries = 0
        while s[2] <= s[1] and not self.stop_ev.is_set():
            try:
                hdr = {"User-Agent": UA}
                if self.resumable:
                    hdr["Range"] = f"bytes={s[2]}-{s[1]}"
                elif s[2] != 0:  # cannot resume a plain stream - restart
                    s[2] = 0
                    open(part, "wb").close()
                req = urllib.request.Request(self.url, headers=hdr)
                with urllib.request.urlopen(req, timeout=25) as r:
                    if self.resumable and r.status != 206:
                        raise IOError("Server ignored range request")
                    with open(part, "r+b") as f:
                        f.seek(s[2])
                        while not self.stop_ev.is_set():
                            chunk = r.read(65536)
                            if not chunk:
                                if not self.resumable or s[1] == 2 ** 62:
                                    s[1] = s[2] - 1  # stream finished
                                break
                            f.write(chunk)
                            s[2] += len(chunk)
                tries = 0
            except Exception as e:
                tries += 1
                if tries > 6:
                    self.failed = True
                    self.error = str(e)[:80]
                    self.stop_ev.set()
                    return
                time.sleep(min(tries, 4))


# ---------- UI ----------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP} — Download Manager")
        self.geometry("1100x640")
        self.minsize(900, 520)
        self.configure(bg=BG)
        self.cfg = {"folder": os.path.join(os.path.expanduser("~"), "Downloads", APP),
                    "connections": 8, "max_active": 3}
        self.cfg.update(load_json(CFG_FILE, {}))
        os.makedirs(self.cfg["folder"], exist_ok=True)
        self.items = {}
        for d in load_json(STATE_FILE, []):
            it = Download(d["url"], d.get("base_folder", self.cfg["folder"]), d)
            self.items[it.id] = it
        self.queue_on = True
        self.schedule = None  # (datetime, shutdown_after)
        self.shutdown_after = False
        self.filter = "All"
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(500, self.tick)
        self.after(5000, self.autosave)

    # ----- styling -----
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                    rowheight=34, borderwidth=0, font=("Segoe UI", 10))
        s.configure("Treeview.Heading", background=CARD, foreground=MUTED, relief="flat",
                    font=("Segoe UI", 9, "bold"), padding=8)
        s.map("Treeview", background=[("selected", ACCENT2)], foreground=[("selected", "#fff")])
        s.map("Treeview.Heading", background=[("active", CARD)])
        s.configure("TScrollbar", background=CARD, troughcolor=PANEL, borderwidth=0, arrowcolor=MUTED)
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=CARD, borderwidth=0)

    def btn(self, parent, text, cmd, primary=False):
        b = tk.Button(parent, text=text, command=cmd, relief="flat", bd=0, cursor="hand2",
                      bg=ACCENT if primary else CARD, fg="#04140f" if primary else FG,
                      activebackground=ACCENT2, activeforeground="#fff",
                      font=("Segoe UI", 10, "bold" if primary else "normal"), padx=14, pady=8)
        b.bind("<Enter>", lambda e: b.config(bg=ACCENT2, fg="#fff"))
        b.bind("<Leave>", lambda e: b.config(bg=ACCENT if primary else CARD, fg="#04140f" if primary else FG))
        return b

    def _build(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(top, text="⚡ FlashPull", bg=BG, fg=ACCENT, font=("Segoe UI", 18, "bold")).pack(side="left", padx=(0, 18))
        for text, cmd, prim in [("＋ Add URL", self.add_dialog, True), ("▶ Resume", self.resume_sel, False),
                                ("⏸ Pause", self.pause_sel, False), ("⏹ Pause All", self.pause_all, False),
                                ("🗑 Delete", self.delete_sel, False), ("📂 Open Folder", self.open_folder, False),
                                ("⏰ Scheduler", self.sched_dialog, False), ("⚙ Settings", self.settings_dialog, False)]:
            self.btn(top, text, cmd, prim).pack(side="left", padx=3)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=6)
        side = tk.Frame(body, bg=PANEL, width=170)
        side.pack(side="left", fill="y", padx=(0, 10))
        side.pack_propagate(False)
        tk.Label(side, text="CATEGORIES", bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(14, 6))
        self.side_btns = {}
        for name in ["All", "Active", "Completed", "Video", "Music", "Compressed", "Documents", "Programs", "Images", "Other"]:
            b = tk.Label(side, text="  " + name, bg=PANEL, fg=FG, anchor="w", font=("Segoe UI", 10), cursor="hand2", pady=6)
            b.pack(fill="x")
            b.bind("<Button-1>", lambda e, n=name: self.set_filter(n))
            self.side_btns[name] = b
        self.set_filter("All")

        right = tk.Frame(body, bg=PANEL)
        right.pack(side="left", fill="both", expand=True)
        cols = ("name", "size", "progress", "speed", "eta", "status", "added")
        self.tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="extended")
        for c, w, t in [("name", 300, "File name"), ("size", 80, "Size"), ("progress", 180, "Progress"),
                        ("speed", 90, "Speed"), ("eta", 70, "Time left"), ("status", 90, "Status"), ("added", 90, "Added")]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c in ("name", "progress") else "center")
        sb = ttk.Scrollbar(right, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("Complete", foreground=OK)
        self.tree.tag_configure("Error", foreground=ERR)
        self.tree.tag_configure("Paused", foreground=WARN)
        self.tree.tag_configure("Downloading", foreground=ACCENT)
        self.tree.bind("<Double-1>", self.open_file)
        self.menu = tk.Menu(self, tearoff=0, bg=CARD, fg=FG, activebackground=ACCENT2)
        for label, cmd in [("Resume", self.resume_sel), ("Pause", self.pause_sel), ("Open file", self.open_file),
                           ("Open folder", self.open_folder), ("Copy URL", self.copy_url), ("Delete", self.delete_sel)]:
            self.menu.add_command(label=label, command=cmd)
        self.tree.bind("<Button-3>", lambda e: (self.tree.identify_row(e.y) and self.tree.selection_add(self.tree.identify_row(e.y)), self.menu.tk_popup(e.x_root, e.y_root)))

        self.status = tk.Label(self, text="", bg=CARD, fg=MUTED, anchor="w", font=("Segoe UI", 9), padx=14, pady=6)
        self.status.pack(fill="x", side="bottom")

    def set_filter(self, name):
        self.filter = name
        for n, b in self.side_btns.items():
            b.config(bg=ACCENT2 if n == name else PANEL, fg="#fff" if n == name else FG)
        if hasattr(self, "tree"):
            self.tree.delete(*self.tree.get_children())
            self.refresh()

    # ----- actions -----
    def add_dialog(self):
        w = tk.Toplevel(self, bg=BG)
        w.title("Add downloads")
        w.geometry("560x330")
        w.transient(self)
        tk.Label(w, text="Paste one or more URLs (one per line)", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=16, pady=(16, 6))
        t = tk.Text(w, height=8, bg=CARD, fg=FG, insertbackground=ACCENT, relief="flat", font=("Consolas", 10))
        t.pack(fill="x", padx=16)
        try:
            clip = self.clipboard_get().strip()
            if clip.lower().startswith(("http://", "https://")):
                t.insert("1.0", clip)
        except tk.TclError:
            pass
        start_now = tk.BooleanVar(value=True)
        tk.Checkbutton(w, text="Start immediately", variable=start_now, bg=BG, fg=FG, selectcolor=CARD,
                       activebackground=BG, activeforeground=FG).pack(anchor="w", padx=16, pady=8)

        def ok():
            urls = [u.strip() for u in t.get("1.0", "end").splitlines() if u.strip().lower().startswith(("http://", "https://", "ftp://"))]
            if not urls:
                messagebox.showwarning(APP, "Please paste a valid http(s) URL.", parent=w)
                return
            for u in urls:
                it = Download(u, self.cfg["folder"])
                it.status = "Queued" if start_now.get() else "Paused"
                self.items[it.id] = it
            w.destroy()
            self.refresh()
        self.btn(w, "Add", ok, True).pack(side="right", padx=16, pady=10)
        self.btn(w, "Cancel", w.destroy).pack(side="right")

    def sel(self):
        return [self.items[i] for i in self.tree.selection() if i in self.items]

    def resume_sel(self):
        for it in self.sel():
            if it.status in ("Paused", "Error"):
                it.status = "Queued"
        self.queue_on = True

    def pause_sel(self):
        for it in self.sel():
            it.pause()

    def pause_all(self):
        for it in self.items.values():
            it.pause()

    def delete_sel(self):
        s = self.sel()
        if not s or not messagebox.askyesno(APP, f"Remove {len(s)} download(s) from the list?\n(Unfinished temp data will be deleted.)"):
            return
        for it in s:
            it.pause()
            if it.status != "Complete" and it.path:
                try:
                    os.remove(it.path + ".part")
                except OSError:
                    pass
            self.items.pop(it.id, None)
        self.tree.delete(*self.tree.get_children())
        self.refresh()

    def open_path(self, p):
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
        except Exception as e:
            messagebox.showerror(APP, str(e))

    def open_file(self, *_):
        for it in self.sel()[:1]:
            if it.status == "Complete" and it.path and os.path.exists(it.path):
                self.open_path(it.path)

    def open_folder(self):
        s = self.sel()
        self.open_path(os.path.dirname(s[0].path) if s and s[0].path else self.cfg["folder"])

    def copy_url(self):
        s = self.sel()
        if s:
            self.clipboard_clear()
            self.clipboard_append(s[0].url)

    def settings_dialog(self):
        w = tk.Toplevel(self, bg=BG)
        w.title("Settings")
        w.geometry("480x250")
        w.transient(self)
        folder = tk.StringVar(value=self.cfg["folder"])
        conn = tk.IntVar(value=self.cfg["connections"])
        act = tk.IntVar(value=self.cfg["max_active"])
        tk.Label(w, text="Save folder", bg=BG, fg=FG).grid(row=0, column=0, padx=16, pady=14, sticky="w")
        tk.Entry(w, textvariable=folder, width=34, bg=CARD, fg=FG, relief="flat", insertbackground=ACCENT).grid(row=0, column=1)
        tk.Button(w, text="…", command=lambda: folder.set(filedialog.askdirectory() or folder.get()), bg=CARD, fg=FG, relief="flat").grid(row=0, column=2, padx=6)
        tk.Label(w, text="Connections per file (1–16)", bg=BG, fg=FG).grid(row=1, column=0, padx=16, pady=10, sticky="w")
        tk.Spinbox(w, from_=1, to=16, textvariable=conn, width=6, bg=CARD, fg=FG, relief="flat").grid(row=1, column=1, sticky="w")
        tk.Label(w, text="Max simultaneous downloads", bg=BG, fg=FG).grid(row=2, column=0, padx=16, pady=10, sticky="w")
        tk.Spinbox(w, from_=1, to=10, textvariable=act, width=6, bg=CARD, fg=FG, relief="flat").grid(row=2, column=1, sticky="w")

        def ok():
            self.cfg.update(folder=folder.get(), connections=max(1, min(16, conn.get())), max_active=max(1, act.get()))
            os.makedirs(self.cfg["folder"], exist_ok=True)
            save_json(CFG_FILE, self.cfg)
            w.destroy()
        self.btn(w, "Save", ok, True).grid(row=3, column=1, pady=18, sticky="e")

    def sched_dialog(self):
        w = tk.Toplevel(self, bg=BG)
        w.title("Scheduler")
        w.geometry("400x230")
        w.transient(self)
        tk.Label(w, text="Start the queue at (24h, HH:MM):", bg=BG, fg=FG).pack(anchor="w", padx=16, pady=(18, 6))
        e = tk.Entry(w, bg=CARD, fg=FG, relief="flat", insertbackground=ACCENT, font=("Segoe UI", 12))
        e.pack(anchor="w", padx=16)
        e.insert(0, "02:00")
        sd = tk.BooleanVar()
        tk.Checkbutton(w, text="Shut down PC when all downloads finish (Windows)", variable=sd, bg=BG, fg=FG,
                       selectcolor=CARD, activebackground=BG, activeforeground=FG).pack(anchor="w", padx=16, pady=10)

        def ok():
            try:
                h, m = map(int, e.get().split(":"))
                t = datetime.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
                if t <= datetime.datetime.now():
                    t += datetime.timedelta(days=1)
            except Exception:
                messagebox.showerror(APP, "Use HH:MM, e.g. 23:30", parent=w)
                return
            self.schedule, self.shutdown_after, self.queue_on = t, sd.get(), False
            w.destroy()
        def clear():
            self.schedule, self.queue_on = None, True
            w.destroy()
        self.btn(w, "Set", ok, True).pack(side="right", padx=16)
        self.btn(w, "Cancel schedule", clear).pack(side="right")

    # ----- loop -----
    def refresh(self):
        shown = set()
        for it in self.items.values():
            f = self.filter
            if f == "Active" and it.status not in ("Downloading", "Queued"):
                continue
            if f == "Completed" and it.status != "Complete":
                continue
            if f in CATS or f == "Other":
                if it.category != f:
                    continue
            shown.add(it.id)
            pct = (it.done / it.size * 100) if it.size else 0
            if it.status == "Complete":
                pct = 100
            bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
            prog = f"{bar} {pct:.0f}%" if it.size or it.status == "Complete" else f"{fmt_size(it.done)}"
            eta = fmt_time((it.size - it.done) / it.speed) if it.speed and it.size else "—"
            vals = (it.name or it.url[:60], fmt_size(it.size), prog,
                    fmt_size(it.speed) + "/s" if it.speed else "—", eta,
                    it.status if not it.error else f"Error", it.added)
            if self.tree.exists(it.id):
                self.tree.item(it.id, values=vals, tags=(it.status,))
            else:
                self.tree.insert("", "end", iid=it.id, values=vals, tags=(it.status,))
        for iid in self.tree.get_children():
            if iid not in shown:
                self.tree.delete(iid)

    def tick(self):
        now = datetime.datetime.now()
        if self.schedule and now >= self.schedule:
            self.queue_on, self.schedule = True, None
        active = [i for i in self.items.values() if i.status == "Downloading"]
        if self.queue_on:
            slots = self.cfg["max_active"] - len(active)
            for it in self.items.values():
                if slots <= 0:
                    break
                if it.status == "Queued":
                    it.start(self.cfg["connections"])
                    slots -= 1
        total_speed = sum(i.speed for i in active)
        pending = [i for i in self.items.values() if i.status in ("Downloading", "Queued")]
        msg = f"{len(active)} active · {len(pending) - len(active)} queued · {fmt_size(total_speed)}/s"
        if self.schedule:
            msg += f"  ·  ⏰ queue starts {self.schedule.strftime('%H:%M')}"
        errs = [i for i in self.items.values() if i.status == "Error" and i.error]
        if errs:
            msg += f"  ·  last error: {errs[-1].error}"
        self.status.config(text=msg)
        if self.shutdown_after and self.items and not pending and not self.schedule and \
                any(i.status == "Complete" for i in self.items.values()):
            self.shutdown_after = False
            if sys.platform.startswith("win"):
                os.system("shutdown /s /t 60")
                messagebox.showinfo(APP, "All downloads finished. PC will shut down in 60 seconds.\n(Run 'shutdown /a' to cancel.)")
        self.refresh()
        self.after(500, self.tick)

    def autosave(self):
        save_json(STATE_FILE, [i.to_dict() for i in self.items.values()])
        self.after(5000, self.autosave)

    def on_close(self):
        for it in self.items.values():
            it.stop_ev.set()
            if it.status in ("Downloading", "Queued"):
                it.status = "Paused"
        time.sleep(0.4)
        save_json(STATE_FILE, [i.to_dict() for i in self.items.values()])
        save_json(CFG_FILE, self.cfg)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
