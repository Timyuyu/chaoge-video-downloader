#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超哥视频下载 —— 桌面程序版（tkinter 原生窗口，不打开浏览器）

复用 app.py 的提取/下载逻辑（同一套后端：抖音 CDP/dump + yt-dlp）。
界面：粘贴链接 -> 提取 -> 预览封面/播放 -> 选画质和目录 -> 开始下载
下载记录里每条独立显示 进度条 + 百分比 + 实时速度 + 暂停/继续。
"""
import os
import subprocess
import sys
import shutil
import tempfile
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk, filedialog

import app  # 同一目录的后端模块

FONT_NAME = "Microsoft YaHei UI"
TITLE_FONT = (FONT_NAME, 12, "bold")
SMALL_FONT = (FONT_NAME, 10)
# 2026 风格配色：浅灰底 + 蓝色主色 + 细边框卡片
BG, CARD, FG, MUTED = "#f6f7fb", "#ffffff", "#111827", "#566070"
BORDER, ACCENT, ACCENT_DARK = "#dfe4ee", "#2f6bff", "#2a5ee0"
OK_GREEN, ERR_RED = "#15803d", "#b42318"
STATUS_TEXT = {"queued": "排队中", "downloading": "下载中", "processing": "合并处理中",
               "paused": "已暂停", "done": "已完成", "error": "失败", "extracting": "提取中"}


def fmt_size(n):
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return ""


def fmt_speed(bps):
    """下载速度文本，例如 '1.2 MB/s'。"""
    if not bps:
        return ""
    return fmt_size(bps) + "/s"


def make_cover(preview_path, width=320):
    """用 ffmpeg 从预览视频抽一帧作封面，返回 PNG 路径（失败返回 None）。"""
    if not preview_path or not os.path.isfile(preview_path):
        return None
    out = os.path.join(tempfile.gettempdir(), "cover_%s.png" % uuid.uuid4().hex[:8])
    for ss in ("2", "0"):
        cmd = [app.ffbin("ffmpeg.exe"), "-y", "-v", "error",
               "-ss", ss, "-i", preview_path, "-frames:v", "1",
               "-vf", "scale=%d:-2" % width, out]
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=app.ytdlp_env(), timeout=40)
        except Exception:
            r = None
        if r and r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 512:
            return out
    return None


class DownloaderGUI:
    def __init__(self, root):
        self.root = root
        self._photo = None
        self._cover_path = None
        self._media_id = None
        self._rows = {}          # task id -> widget dict
        self._last_media_status = None
        root.title("超哥视频下载")
        root.geometry("1000x720")
        root.minsize(820, 560)
        self._build()
        self._refresh()

    # ---------------- 界面构建 ----------------
    def _build(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        self.root.configure(bg=BG)
        style.configure(".", background=BG, foreground=FG, font=SMALL_FONT,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Head.TLabel", font=TITLE_FONT, background=BG)
        style.configure("Brand.TLabel", font=(FONT_NAME, 15, "bold"), background=BG)
        style.configure("Sub.TLabel", foreground=MUTED, background=BG, font=(FONT_NAME, 9))
        style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                        relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED,
                        font=(FONT_NAME, 9, "bold"))
        style.configure("TEntry", fieldbackground=CARD, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, padding=8,
                        focusthickness=2, focuscolor=ACCENT)
        style.configure("TCombobox", fieldbackground=CARD, background=CARD,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        padding=6, arrowsize=14)
        style.configure("TButton", font=SMALL_FONT, padding=(12, 7), relief="flat",
                        background="#eef1f6", foreground=FG, bordercolor=BORDER,
                        lightcolor="#eef1f6", darkcolor="#eef1f6")
        style.map("TButton",
                  background=[("active", "#e2e8f2"), ("disabled", "#f1f3f7")],
                  foreground=[("disabled", "#9aa4b1")])
        style.configure("Accent.TButton", font=(FONT_NAME, 10, "bold"), padding=(14, 7),
                        background=ACCENT, foreground="#ffffff", bordercolor=ACCENT,
                        lightcolor=ACCENT, darkcolor=ACCENT)
        style.map("Accent.TButton",
                  background=[("active", ACCENT_DARK), ("disabled", "#a9c3f5")],
                  foreground=[("disabled", "#ffffff")])
        TROUGH = "#e6eaf1"
        style.configure("Horizontal.TProgressbar", thickness=12, troughcolor=TROUGH)
        # 进度条按状态换色：下载中=琥珀、已暂停=灰、已完成=蓝、失败=红
        for _nm, _col in (("Run", "#f59e0b"), ("Pause", "#94a3b8"),
                          ("Done", "#2563eb"), ("Err", "#b91c1c")):
            style.configure("%s.Horizontal.TProgressbar" % _nm, thickness=12,
                            troughcolor=TROUGH, background=_col,
                            bordercolor=TROUGH, lightcolor=_col, darkcolor=_col)
        style.configure("TScrollbar", background="#d7dbe4", troughcolor=BG,
                        bordercolor=BORDER, lightcolor="#d7dbe4", darkcolor="#d7dbe4",
                        arrowsize=12, relief="flat")

        head = ttk.Frame(self.root, padding=(14, 14, 14, 2))
        head.pack(fill="x")
        ttk.Label(head, text="超哥视频下载", style="Brand.TLabel").pack(side="left")
        ttk.Label(head, text="  提取预览 → 选择画质 → 下载（每条可单独暂停）",
                  style="Sub.TLabel").pack(side="left", pady=(4, 0))

        top = ttk.Frame(self.root, padding=(14, 10, 14, 6))
        top.pack(fill="x")
        ttk.Label(top, text="视频链接", font=TITLE_FONT).pack(side="left", padx=(0, 8))
        self.url = ttk.Entry(top, font=SMALL_FONT)
        self.url.pack(side="left", fill="x", expand=True)
        self.url.bind("<Return>", lambda e: self.on_extract())
        self.btn_extract = ttk.Button(top, text="提取视频", style="Accent.TButton",
                                      command=self.on_extract)
        self.btn_extract.pack(side="left", padx=(8, 0))

        self.status = ttk.Label(self.root, text="就绪：粘贴链接后点「提取视频」",
                                foreground="#5b6470")
        self.status.pack(fill="x", padx=14)

        # 提取结果卡片
        self.media_card = ttk.LabelFrame(self.root, text=" 提取结果 ", padding=12)
        self.media_card.pack(fill="x", padx=14, pady=(10, 6))
        self.cover = ttk.Label(self.media_card, text="（尚无预览）", foreground="#8a929d")
        self.cover.pack(side="left", padx=(0, 14))
        right = ttk.Frame(self.media_card)
        right.pack(side="left", fill="both", expand=True)
        self.m_title = ttk.Label(right, text="先提取一条视频", font=TITLE_FONT,
                                 wraplength=560, justify="left")
        self.m_title.pack(anchor="w")
        self.m_chips = ttk.Label(right, text="", foreground="#5b6470")
        self.m_chips.pack(anchor="w", pady=(4, 8))
        row1 = ttk.Frame(right)
        row1.pack(fill="x", pady=(2, 8))
        ttk.Label(row1, text="画质", style="Sub.TLabel").pack(side="left", padx=(0, 6))
        self.quality = ttk.Combobox(row1, width=12, state="readonly", font=SMALL_FONT,
                                    values=["最佳画质"])
        self.quality.pack(side="left")
        self.quality.set("最佳画质")
        ttk.Label(row1, text="保存到", style="Sub.TLabel").pack(side="left", padx=(14, 6))
        self.outdir = ttk.Entry(row1, font=SMALL_FONT)
        self.outdir.insert(0, app.DEFAULT_OUTDIR)
        self.outdir.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(row1, text="浏览…", command=self.on_browse).pack(side="left")

        row2 = ttk.Frame(right)
        row2.pack(fill="x")
        self.btn_play = ttk.Button(row2, text="播放预览", command=self.on_play)
        self.btn_play.pack(side="left")
        self.btn_dl = ttk.Button(row2, text="开始下载", style="Accent.TButton",
                                 command=self.on_download)
        self.btn_dl.pack(side="left", padx=(8, 0))
        ttk.Label(row2, text="每条下载任务都能单独暂停 / 继续",
                  style="Sub.TLabel").pack(side="left", padx=(12, 0))

        # 下载记录（可滚动）
        box = ttk.LabelFrame(self.root, text=" 下载记录（每条可单独暂停） ", padding=6)
        box.pack(fill="both", expand=True, padx=14, pady=(6, 10))
        self.canvas = tk.Canvas(box, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(box, orient="vertical", command=self.canvas.yview)
        self.rows_frame = ttk.Frame(self.canvas)
        self.rows_frame.bind("<Configure>",
                             lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfig("all", width=e.width))
        self.empty_hint = ttk.Label(box, text="还没有下载任务", foreground="#8a929d")

    def _on_wheel(self, ev):
        self.canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units")

    def _set_status(self, text, color="#5b6470"):
        self.status.config(text=text, foreground=color)

    # ---------------- 提取 ----------------
    def on_extract(self):
        url = self.url.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            self._set_status("请输入有效的 http(s) 链接", "#b42318")
            return
        mid = uuid.uuid4().hex[:12]
        app.MEDIA[mid] = {"id": mid, "url": app.normalize_douyin(url), "status": "queued"}
        with app.LOCK:
            app.MEDIA_ORDER.insert(0, mid)
        self._media_id = mid
        self._last_media_status = None
        self._cover_path = None
        self.m_title.config(text="正在提取…")
        self.m_chips.config(text="")
        self.cover.config(image="", text="提取中…")
        self.btn_dl.config(state="disabled")
        self._set_status("正在提取视频信息并生成预览…")
        threading.Thread(target=app.run_extract, args=(mid,), daemon=True).start()

    def _show_media(self, m):
        self._media_id = m["id"]
        self.m_title.config(text=m.get("title") or "(无标题)")
        chips = [m.get("source") or "", "时长 " + m["duration"] if m.get("duration") else "",
                 ("无音轨 ⚠" if m.get("has_audio") is False else "有音轨"),
                 "画质 " + "/".join(q["label"] for q in (m.get("qualities") or []))]
        self.m_chips.config(text="   ".join(c for c in chips if c))
        self.quality.config(values=[q["label"] for q in (m.get("qualities") or ["最佳画质"])] or ["最佳画质"])
        self.quality.set("最佳画质")
        self.btn_dl.config(state="normal")
        self._set_status("提取完成，可开始下载（下载记录里每条都能单独暂停）", "#166534")
        if self._cover_path is None and m.get("preview_path"):
            self._cover_path = "pending"
            threading.Thread(target=self._load_cover, args=(m["preview_path"],),
                             daemon=True).start()
        elif not m.get("preview_path"):
            # 没有预览文件时明确说明，不让界面一直停在“提取中…”
            self._cover_path = "none"
            self.cover.config(image="", text="该视频没有可播放的预览\n可直接下载后本地播放")
            self.btn_play.config(state="disabled")
        else:
            self.btn_play.config(state="normal")

    def _load_cover(self, preview_path):
        png = make_cover(preview_path)
        if not png:
            def failed():
                if self._cover_path == "pending":
                    self._cover_path = "none"
                    self.cover.config(image="", text="预览封面生成失败\n可点「播放预览」直接播放")
            self.root.after(0, failed)
            return
        try:
            from PIL import Image, ImageTk
            img = Image.open(png)
            img.thumbnail((320, 200))
            photo = ImageTk.PhotoImage(img)
        except Exception:
            return

        def apply():
            if self._media_id and self._cover_path in ("pending", png):
                self._cover_path = png
                self._photo = photo
                self.cover.config(image=photo, text="")
        self.root.after(0, apply)

    def on_play(self):
        m = app.MEDIA.get(self._media_id or "")
        pv = (m or {}).get("preview_path")
        if not pv or not os.path.isfile(pv):
            self._set_status("没有可播放的预览文件", "#b42318")
            return
        try:
            os.startfile(pv)
        except Exception as e:
            self._set_status("打开预览失败：%s" % e, "#b42318")

    def on_browse(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or app.DEFAULT_OUTDIR)
        if d:
            self.outdir.delete(0, "end")
            self.outdir.insert(0, os.path.normpath(d))

    # ---------------- 下载 ----------------
    def on_download(self):
        m = app.MEDIA.get(self._media_id or "")
        if not m or m.get("status") != "ready":
            self._set_status("请先提取视频", "#b42318")
            return
        outdir = self.outdir.get().strip()
        if not os.path.isdir(outdir):
            self._set_status("下载目录不存在，请先选择", "#b42318")
            return
        label = self.quality.get() or "最佳画质"
        qfmt = "bv*+ba/b"
        for o in (m.get("qualities") or []):
            if o["label"] == label:
                qfmt = o["fmt"]
                break
        tid = uuid.uuid4().hex[:12]
        task = {"id": tid, "url": m.get("webpage") or m["url"], "outdir": outdir,
                "media_id": m["id"], "quality_fmt": qfmt, "quality_label": label,
                "status": "queued", "progress": 0.0, "title": m.get("title"),
                "filepath": None, "error": None, "size": None,
                "speed": None, "copied": 0, "pause": False}
        with app.LOCK:
            app.TASKS[tid] = task
            app.TASK_ORDER.insert(0, tid)
        threading.Thread(target=app.run_task, args=(tid,), daemon=True).start()
        self._set_status("已加入下载队列（可继续提取其它链接并行下载）")

    def on_pause(self, tid):
        t = app.TASKS.get(tid)
        if not t:
            return
        if t.get("status") == "paused":
            app.resume_task(tid)
            self._set_status("已继续该任务")
        else:
            app.pause_task(tid)
            self._set_status("已暂停该任务（其它任务不受影响）")

    def on_open(self, tid):
        t = app.TASKS.get(tid) or {}
        path = t.get("filepath") or t.get("outdir") or app.DEFAULT_OUTDIR
        target = os.path.dirname(path) if os.path.isfile(path) else path
        try:
            os.startfile(target)
        except Exception as e:
            self._set_status("打开目录失败：%s" % e, "#b42318")

    def on_delete(self, tid):
        with app.LOCK:
            app.TASKS.pop(tid, None)
            if tid in app.TASK_ORDER:
                app.TASK_ORDER.remove(tid)
        row = self._rows.pop(tid, None)
        if row:
            row["frame"].destroy()

    # ---------------- 任务行渲染 ----------------
    def _new_row(self, tid, t):
        f = ttk.Frame(self.rows_frame, padding=(8, 6))
        f.pack(fill="x", padx=4, pady=3)
        head = ttk.Frame(f)
        head.pack(fill="x")
        title = ttk.Label(head, text=t.get("title") or t.get("url") or "",
                          font=SMALL_FONT, width=78)
        title.pack(side="left")
        state = ttk.Label(head, text="", width=13)
        state.pack(side="right")
        bar = ttk.Progressbar(f, orient="horizontal", mode="determinate",
                              maximum=100.0, style="Run.Horizontal.TProgressbar")
        bar.pack(fill="x", pady=(4, 2))
        info = ttk.Label(f, text="", foreground="#5b6470")
        info.pack(anchor="w")
        btns = ttk.Frame(f)
        btns.pack(anchor="w", pady=(2, 0))
        pause = ttk.Button(btns, text="暂停", width=8, command=lambda: self.on_pause(tid))
        pause.pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="打开目录", width=10,
                   command=lambda: self.on_open(tid)).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="删除记录", width=10,
                   command=lambda: self.on_delete(tid)).pack(side="left")
        self._rows[tid] = {"frame": f, "title": title, "state": state, "bar": bar,
                           "info": info, "pause": pause}
        if self.empty_hint.winfo_ismapped():
            self.empty_hint.pack_forget()

    def _refresh(self):
        # 提取结果
        m = app.MEDIA.get(self._media_id or "")
        if m:
            st = m.get("status")
            if st == "ready" and self._last_media_status != "ready":
                self.root.after(0, lambda: self._show_media(dict(m)))
            elif st == "error" and self._last_media_status != "error":
                err = m.get("error") or "提取失败"
                self.root.after(0, lambda e=err: self._set_status("提取失败：" + e, "#b42318"))
            self._last_media_status = st
        # 下载任务
        for tid in list(app.TASK_ORDER):
            t = app.TASKS.get(tid)
            if not t:
                continue
            row = self._rows.get(tid)
            if row is None:
                self._new_row(tid, t)
                row = self._rows[tid]
            st = t.get("status") or ""
            pct = float(t.get("progress") or 0.0)
            row["bar"]["value"] = pct
            row["bar"].configure(style="%s.Horizontal.TProgressbar" % {
                "done": "Done", "error": "Err", "paused": "Pause"}.get(st, "Run"))
            row["state"].config(text=STATUS_TEXT.get(st, st))
            parts = ["%.1f%%" % pct]
            if t.get("speed"):
                parts.append(fmt_speed(t["speed"]))
            if st == "done" and t.get("size"):
                parts.append(fmt_size(t["size"]))
            if st == "error" and t.get("error"):
                parts.append(str(t["error"])[:60])
            if t.get("note"):
                parts.append(t["note"])
            row["info"].config(text="  ·  ".join(parts))
            row["pause"].config(text="继续" if st == "paused" else "暂停",
                                state="normal" if (st in ("queued", "downloading",
                                                          "processing", "paused")) else "disabled")
        if not self._rows and not self.empty_hint.winfo_ismapped():
            self.empty_hint.pack(anchor="w", padx=8, pady=8)
        self._refresh_id = self.root.after(700, self._refresh)


def _check():
    """打印解析到的外部工具路径后退出（exe 无命令行界面时唯一的自检入口）。"""
    deno = None
    for base in (app.BASE, app.TOOLS_DIR, app.PROJECT_DIR):
        if base:
            cand = os.path.join(base, "tools", "deno", "deno.exe")
            if os.path.isfile(cand):
                deno = cand
                break
    deno = deno or shutil.which("deno")
    print("数据目录      :", app.BASE)
    print("config.json   :", app.CFG_FILE,
          "[已加载]" if os.path.isfile(app.CFG_FILE) else "[无此文件，用默认解析]")
    print("yt-dlp        :", app.YTDLP,
          "[OK]" if os.path.isfile(app.YTDLP) else "[未找到，交给 PATH 解析]")
    print("ffmpeg 目录   :", app.FFMPEG_DIR or "[未配置，交给 PATH 解析]")
    print("ffmpeg        :", app.ffbin("ffmpeg.exe"))
    print("ffprobe       :", app.ffbin("ffprobe.exe"))
    print("deno          :", deno or "[未找到]")
    print("Edge          :", next((p for p in app.EDGE_CANDIDATES
                                   if os.path.isfile(p)), "[未找到]"))
    print("默认下载目录  :", app.DEFAULT_OUTDIR)


def main():
    if "--check" in sys.argv:
        _check()
        return
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except Exception:
        pass
    DownloaderGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
