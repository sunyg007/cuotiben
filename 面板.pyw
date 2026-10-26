# -*- coding: utf-8 -*-
"""
错题本 · 控制面板（tkinter，纯标准库零依赖）
================================================================
双击本文件（.pyw 不弹黑窗口）或在 exe 里启动。它做三件事：
  1. 后台线程里起本地服务（复用 server.py 的全部逻辑）；
  2. 窗口里显示错题 / 复习的实时统计（每 5 秒自动刷新）；
  3. 显示手机访问二维码（局域网 / Tailscale 可切换），扫码即用。

关闭窗口 = 停止服务并退出（和原来关黑窗口一个意思）。
"""
import os
import sys
import threading
import webbrowser

# exe（--windowed）下没有控制台：把 stdout/stderr 引到日志文件，
# 服务端偶尔 print 的 [llm] tokens 之类的信息也不会丢、更不会崩。
if getattr(sys, "frozen", False):
    try:
        if sys.stdout is None or sys.stderr is None:
            _logdir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "data")
            os.makedirs(_logdir, exist_ok=True)
            _logf = open(os.path.join(_logdir, "panel-out.log"), "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or _logf
            sys.stderr = sys.stderr or _logf
    except Exception:
        pass

import tkinter as tk
from tkinter import ttk, messagebox

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)          # 让同目录的 server.py 能被 import（源码运行时）
import server                              # noqa: E402  常量/逻辑全复用

import segno                              # 随包自带（源码目录或 _internal 里）


def make_qr_png(url, path, scale=8):
    """URL → 二维码 PNG（segno 纯 Python 生成，写临时文件给 tk 显示）"""
    segno.make(url, error="m").save(path, scale=scale, border=2)


class Panel(object):
    REFRESH_MS = 5000

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("错题本 · 控制面板")
        self.root.geometry("440x640")
        self.root.minsize(400, 560)
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # 高分屏不发糊
        except Exception:
            pass
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))

        self.port = server.PORT
        self.httpd = None
        self.urls = []            # [(标签, url), ...] 局域网在前、Tailscale 其后
        self.url_idx = 0
        self._qr_photo = None

        self._build_ui()
        self._start_server()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(self.REFRESH_MS, self._tick)

    # ---------------- UI ----------------
    def _build_ui(self):
        r = self.root
        r.configure(bg="#f2f4f9")

        head = tk.Frame(r, bg="#2f6bed")
        head.pack(fill="x")
        tk.Label(head, text="错题本", font=("Microsoft YaHei UI", 15, "bold"),
                 bg="#2f6bed", fg="#fff").pack(side="left", padx=14, pady=10)
        self.status_lbl = tk.Label(head, text="正在启动服务…", bg="#2f6bed", fg="#dbe7ff")
        self.status_lbl.pack(side="right", padx=14)

        # ---- 统计卡 ----
        cards = tk.Frame(r, bg="#f2f4f9")
        cards.pack(fill="x", padx=12, pady=(12, 0))
        self.card_vars = {}
        rows = [[("错题总数", "total"), ("今天待复习", "due"), ("已掌握", "mastered")],
                [("复习记录", "reviews"), ("未掌握", "pending"), ("原图/音频", "imgs")]]
        for row in rows:
            f = tk.Frame(cards, bg="#f2f4f9")
            f.pack(fill="x", pady=3)
            for name, key in row:
                box = tk.Frame(f, bg="#fff")
                box.pack(side="left", expand=True, fill="both", padx=3, ipady=2)
                v = tk.Label(box, text="—", font=("Microsoft YaHei UI", 17, "bold"),
                             bg="#fff", fg="#2f6bed")
                v.pack(pady=(8, 0))
                tk.Label(box, text=name, font=("Microsoft YaHei UI", 9),
                         bg="#fff", fg="#6b7787").pack(pady=(0, 8))
                self.card_vars[key] = v
        tip = tk.Frame(r, bg="#f2f4f9")
        tip.pack(fill="x")
        self.tip_lbl = tk.Label(tip, text="", bg="#f2f4f9", fg="#6b7787",
                                font=("Microsoft YaHei UI", 9), justify="left")
        self.tip_lbl.pack(pady=(4, 0))

        # ---- 二维码 ----
        qrbox = tk.Frame(r, bg="#fff")
        qrbox.pack(fill="both", expand=True, padx=12, pady=10)
        tk.Label(qrbox, text="手机扫码使用（同一 WiFi / Tailscale）",
                 bg="#fff", fg="#1b2430", font=("Microsoft YaHei UI", 10, "bold")
                 ).pack(pady=(10, 2))
        self.qr_lbl = tk.Label(qrbox, text="正在生成二维码…", bg="#fff", fg="#9aa5b5")
        self.qr_lbl.pack(pady=6)
        self.addr_lbl = tk.Label(qrbox, text="", bg="#fff", fg="#2f6bed",
                                 font=("Microsoft YaHei UI", 10, "bold"))
        self.addr_lbl.pack(pady=(0, 4))
        bar = tk.Frame(qrbox, bg="#fff")
        bar.pack(pady=(0, 6))
        self.url_combo = ttk.Combobox(bar, state="readonly", width=34, values=["（等待地址）"])
        self.url_combo.pack()
        self.url_combo.bind("<<ComboboxSelected>>", lambda e: self._show_qr(self.url_combo.current()))

        # ---- 按钮 ----
        btns = tk.Frame(r, bg="#f2f4f9")
        btns.pack(fill="x", padx=12, pady=(0, 4))
        tk.Button(btns, text="🖥 宽屏看板窗口", command=self._open_board_window,
                  relief="groove", bg="#e9f0ff", fg="#1f52c4", activebackground="#dbe7ff",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(fill="x", ipady=6, pady=(0, 4))
        for text, cmd in (("🌐 打开错题本", self._open_web),
                          ("📋 复制手机地址", self._copy_addr),
                          ("📂 数据文件夹", self._open_data)):
            tk.Button(btns, text=text, command=cmd, relief="groove", bg="#fff",
                      fg="#1b2430", activebackground="#e9f0ff",
                      font=("Microsoft YaHei UI", 10)).pack(side="left", expand=True, fill="x", padx=3, ipady=5)
        tk.Label(r, text="关闭本窗口 = 停止服务；手机和电脑要在同一个网络",
                 bg="#f2f4f9", fg="#9aa5b5", font=("Microsoft YaHei UI", 9)).pack(pady=(0, 10))

    # ---------------- 服务 ----------------
    def _start_server(self):
        def run():
            try:
                server.init_db()
                from http.server import ThreadingHTTPServer
                self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), server.Handler)
                self.root.after(0, lambda: self._on_server_up(True, ""))
                self.httpd.serve_forever()
            except OSError as exc:
                self.root.after(0, lambda: self._on_server_up(False, str(exc)))
            except Exception as exc:   # noqa: BLE001
                self.root.after(0, lambda: self._on_server_up(False, str(exc)))
        threading.Thread(target=run, daemon=True).start()

    def _on_server_up(self, ok, err):
        if ok:
            self.status_lbl.config(text="● 服务运行中 · 端口 %d · v%s" % (self.port, server.VERSION),
                                   fg="#b7f0cd")
            self._refresh_urls()
        else:
            self.status_lbl.config(text="● 端口被占用 / 启动失败", fg="#ffd2d3")
            self.tip_lbl.config(text="可能是已经开过一个错题本（窗口或黑窗口）。\n"
                                     "把另一个关掉再开本面板；或点下面按钮直接打开已有服务。")
            self.qr_lbl.config(text="（服务未启动）")
            tk.Button(self.root, text="🌐 尝试打开 http://127.0.0.1:%d" % self.port,
                      command=lambda: webbrowser.open("http://127.0.0.1:%d" % self.port)
                      ).pack(pady=4)

    def _refresh_urls(self):
        urls = [("局域网 " + ip, "http://%s:%d" % (ip, self.port))
                for ip in server.local_ips() if not ip.startswith("127.")]
        try:
            tails = [ip for info in server.local_ips() for ip in [info]
                     if ip.startswith("100.")]     # Tailscale 的 CGNAT 段
        except Exception:
            tails = []
        for ip in tails:
            urls.append(("Tailscale " + ip, "http://%s:%d" % (ip, self.port)))
        if not urls:
            urls = [("本机", "http://127.0.0.1:%d" % self.port)]
        self.urls = urls
        self.url_combo.config(values=[u[0] + "   " + u[1] for u in urls])
        self.url_combo.current(min(self.url_idx, len(urls) - 1))
        self._show_qr(self.url_combo.current())

    def _show_qr(self, idx):
        if not self.urls:
            return
        self.url_idx = max(0, min(idx, len(self.urls) - 1))
        label, url = self.urls[self.url_idx]
        self.addr_lbl.config(text=url)
        try:
            tmp = os.path.join(server.DATA_DIR, "_panel_qr.png")
            os.makedirs(server.DATA_DIR, exist_ok=True)
            make_qr_png(url, tmp)
            from tkinter import PhotoImage
            self._qr_photo = PhotoImage(file=tmp)
            self.qr_lbl.config(image=self._qr_photo, text="")
            try:
                os.remove(tmp)
            except Exception:
                pass
        except Exception as exc:   # noqa: BLE001
            self.qr_lbl.config(text="二维码生成失败：%s" % exc, image="")

    # ---------------- 统计刷新 ----------------
    def _tick(self):
        self._refresh_stats()
        self.root.after(self.REFRESH_MS, self._tick)

    def _refresh_stats(self):
        try:
            st = server.stats()
            self.card_vars["total"].config(text=str(st.get("total", 0)))
            self.card_vars["due"].config(text=str(st.get("due_count", 0)))
            self.card_vars["mastered"].config(text=str(st.get("mastered", 0)))
            self.card_vars["pending"].config(text=str(st.get("total", 0) - st.get("mastered", 0)))
            self.card_vars["reviews"].config(text=str(st.get("review_count", 0)))
            imgs = 0
            if os.path.isdir(server.IMAGE_DIR):
                imgs = len([f for f in os.listdir(server.IMAGE_DIR) if not f.startswith(".")])
            auds = 0
            if os.path.isdir(server.AUDIO_DIR):
                auds = len([f for f in os.listdir(server.AUDIO_DIR) if not f.startswith(".")])
            self.card_vars["imgs"].config(text="%d / %d" % (imgs, auds))
            wk = st.get("week") or {}
            self.tip_lbl.config(text="本周：复习 %d 次 · 正确率 %d%% · 新收录 %d 题 · 练了 %d 天" % (
                wk.get("reviewed", 0), wk.get("accuracy", 0), wk.get("new", 0), wk.get("active_days", 0)))
        except Exception as exc:   # noqa: BLE001
            self.tip_lbl.config(text="统计读取失败：%s" % exc)

    # ---------------- 按钮 ----------------
    def _open_board_window(self):
        """用 Edge/Chrome 应用模式弹无地址栏的独立看板窗口（探测代码在 错题本看板.pyw）"""
        url = "http://127.0.0.1:%d/" % self.port
        try:
            from 错题本看板 import find_browser_exe, open_dashboard_window, wait_server_ready  # noqa: E402
        except Exception:          # exe 场景或单文件部署找不到模块：退化为普通标签页
            webbrowser.open(url)
            return
        threading.Thread(
            target=lambda: (wait_server_ready(url), open_dashboard_window(url)),
            daemon=True).start()

    def _open_web(self):
        webbrowser.open("http://127.0.0.1:%d" % self.port)

    def _copy_addr(self):
        if not self.urls:
            return
        url = self.urls[self.url_idx][1]
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        messagebox.showinfo("已复制", "手机访问地址已复制：\n\n" + url
                            + "\n\n手机和电脑连同一个 WiFi，用手机浏览器打开即可。")

    def _open_data(self):
        try:
            os.makedirs(server.DATA_DIR, exist_ok=True)
            os.startfile(server.DATA_DIR)   # noqa: S606  Windows 专属
        except Exception as exc:   # noqa: BLE001
            messagebox.showerror("打开失败", str(exc))

    def _on_close(self):
        try:
            if self.httpd:
                threading.Thread(target=self.httpd.shutdown, daemon=True).start()
        except Exception:
            pass
        self.root.destroy()
        os._exit(0)     # 干净利落地结束（守护线程随之而死，进程不残留）

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Panel().run()
