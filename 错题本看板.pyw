# -*- coding: utf-8 -*-
"""
错题本 · 宽屏看板（Windows 桌面窗口，纯标准库零依赖）
================================================================
双击本文件（.pyw 不弹黑窗口）：
  1. 后台线程起本地服务（复用 server.py 全部逻辑，与面板/黑窗口同一个库）；
  2. 等服务真正就绪后，用系统自带的 Edge/Chrome 以「应用模式」弹出一个
     无地址栏的独立桌面窗口（1440×900，可最大化）——里面就是宽屏看板：
     问候横幅、数字卡、各科进度、优先补练、内嵌复习日历、手机扫码二维码。

**单窗口模式**：屏幕上始终只有看板一个窗口。看板窗口一关，服务自动
停止、本进程安静退出（背后有个完全隐藏的心跳泵在盯着窗口）。
端口被占用时不报死：多半是已经开过一个错题本（面板/黑窗口/exe），
此时不再重复起服务，直接把已有服务用看板窗口打开。
"""
import os
import sys
import threading
import time
import urllib.request
import webbrowser

# exe（--windowed）下没有控制台：stdout/stderr 引到日志文件（与 面板.pyw 同套路）
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

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
import server                              # noqa: E402  常量/逻辑全复用


# ---------------- 浏览器「应用模式」拉起 ----------------
# Edge/Chrome 的 --app=URL 会开一个无地址栏、无标签页的独立窗口，
# 尺寸自定、可最大化——观感就是桌面软件。都没有时退回普通浏览器标签页。
def find_browser_exe():
    cands = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lc = os.environ.get("LOCALAPPDATA", "")
    for root in (pf86, pf):
        cands.append(os.path.join(root, r"Microsoft\Edge\Application\msedge.exe"))
    for root in (pf, pf86):
        cands.append(os.path.join(root, r"Google\Chrome\Application\chrome.exe"))
    if lc:
        cands.append(os.path.join(lc, r"Google\Chrome\Application\chrome.exe"))
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return ""


def open_dashboard_window(url, size="1440,900"):
    """用应用模式弹独立看板窗口；返回 True=弹成功，False=已退回普通浏览器"""
    exe = find_browser_exe()
    if exe:
        try:
            import subprocess
            subprocess.Popen(          # noqa: S603  仅拉起本机浏览器，参数是固定 URL
                [exe, "--app=" + url, "--window-size=" + size],
                close_fds=True)
            return True
        except Exception:
            pass
    webbrowser.open(url)
    return False


def wait_server_ready(url, tries=40, gap=0.25):
    """等页面真的可打开：health 200 之后再把首页 HTML 拉到手才算数。
    只探 health 会出现竞态——bind 刚成功、静态资源还没伺候好，浏览器抢跑
    渲染了空响应，窗口就是一整块白屏（实测踩过）。"""
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=2.5) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
        time.sleep(gap)
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url + "/", timeout=2.5) as r:
                head = r.read(100)
                if r.status == 200 and b"<!DOCTYPE" in head.upper():
                    return True
        except Exception:
            pass
        time.sleep(gap)
    return False


def main():
    """起服务 → 等就绪 → 弹看板窗口 → 等窗口关掉 → 停服务。"""
    import subprocess
    port = server.PORT
    base = "http://127.0.0.1:%d" % port
    httpd = None

    # 端口被占用 = 已经有错题本在跑（面板/黑窗口/exe）：复用它，不重复起服务
    already = False
    try:
        server.init_db()
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("0.0.0.0", port), server.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    except OSError:
        already = True
    except Exception:
        already = True

    if not already:
        wait_server_ready(base)
    else:
        # 复用已有服务：也给它一点点时间（可能正在忙），探一下更稳
        wait_server_ready(base, tries=10)

    # 弹看板窗口，然后守着：应用模式的 --app 窗口关掉后，若再没有错题本的
    # 页面活着（这里用 health 探针无法区分窗口，改用「窗口进程探活」不可靠——
    # 简单可靠的做法：守一个 stdin/事件，窗口关闭交给用户按 Ctrl+C 或直接结束进程。
    # 但 .pyw 没有控制台、也没窗口可等 —— 所以起一个隐形 tk 窗口当「服务开关」：
    # 用户关掉 Edge 看板窗口后，托盘无图标不好感知 —— 折中：隐形窗口仅在服务
    # 是本进程起的时候存在，关掉它（或 Edge 窗口后再关它）即停服务退出。
    if httpd is None:
        # 复用别人的服务：弹完窗口就退出本进程，服务留给原进程管
        open_dashboard_window(base)
        return

    # 本进程持有服务：**单窗口模式**——屏幕上只有看板一个窗口，不再弹服务小窗。
    # tk 只作隐藏的心跳泵：轮询「看板窗口还在不在」，关了就停服务收摊。
    # 兜底：万一回退成了普通浏览器标签页（枚举不到 --app 窗），或用户从任务
    # 管理器杀不掉，服务会在 pythonw 进程结束时随进程消失（守护线程）；也可以
    # 直接结束本进程（任务管理器里叫 pythonw.exe）。
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()                    # 完全隐藏：不进任务栏，用户看不到它

    launch_t = [None]      # 浏览器拉起时刻（探活的宽限期起点）
    miss_t = [None]        # 第一次探不到看板窗口的时刻（连续 6 秒才判退）

    def app_window_alive():
        """以「屏幕上还有没有看板窗口」为准的探活：枚举所有顶层窗口，
        找 --app 模式窗口（标题以「错题本」开头 + Chrome 系窗口类名）。
        不依赖 pid——Edge 单实例复用时 Popen 拿到的常是转交参数后立即
        退出的「传令兵」，pid 探活会误判窗口已关而错杀服务。"""
        try:
            import ctypes
            import ctypes.wintypes as wt
            user32 = ctypes.windll.user32
            found = [False]
            CBENUMPROC = ctypes.WINFUNCTYPE(
                ctypes.c_bool, wt.HWND, wt.LPARAM)
            TITLES = ("错题本",)          # 看板页 <title> 以「错题本」开头
            def _cb(hwnd, _lparam):
                if not user32.IsWindowVisible(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value
                if any(t.startswith(x) for x in TITLES):
                    cls = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(hwnd, cls, 64)
                    # Edge/Chrome 应用模式主窗口类名：Chrome_WidgetWin_1
                    if cls.value.startswith("Chrome_WidgetWin"):
                        found[0] = True
                        return False
                return True
            user32.EnumWindows(CBENUMPROC(_cb), 0)
            return found[0]
        except Exception:
            return True           # 探测失败宁可多等，不要误杀服务

    def poll_closed():
        """看板窗口被用户关掉 → 本进程没有存在意义了，停服务退出。
        宽限 25 秒（窗口肯定已打开或已回退普通浏览器），之后每 3 秒枚举
        一次窗口；连续 6 秒都找不到看板窗口才认定真关了。"""
        born = launch_t[0]
        grace_ok = born and (time.time() - born > 25)
        if grace_ok and not app_window_alive():
            if not miss_t[0]:
                miss_t[0] = time.time()          # 第一次探不到：先记下，别急着杀
            elif time.time() - miss_t[0] > 6:    # 连续 6 秒都没有 → 真退了
                try:
                    httpd.shutdown()
                except Exception:
                    pass
                root.destroy()
                os._exit(0)
                return
        else:
            miss_t[0] = None
        root.after(3000, poll_closed)

    def launch():
        exe = find_browser_exe()
        launch_t[0] = time.time()
        if exe:
            try:
                subprocess.Popen(       # noqa: S603  仅拉起本机浏览器，参数是固定 URL
                    [exe, "--app=" + base + "/", "--window-size=1440,900"],
                    close_fds=True)
                return
            except Exception:
                pass
        webbrowser.open(base)           # 回退普通标签页：探不到独立窗，宽限后靠「页面标题枚举」兜不住——
                                        # 这种极少数环境（连 Edge/Chrome 都没有）下服务随本进程存活，
                                        # 用户重启电脑/结束 pythonw.exe 即收摊。

    root.after(300, launch)
    root.after(5000, poll_closed)
    root.mainloop()

    # mainloop 退出（用户通过任务管理器/其它途径关闭隐藏窗口）也兜底停服务
    try:
        httpd.shutdown()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
