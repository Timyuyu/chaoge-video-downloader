#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超哥视频下载 v2 —— 提取预览 + 选画质选路径下载
流程: 粘贴链接 -> 提取(元数据+低清预览片) -> 预览播放 -> 选画质/路径 -> 下载
支持: yt-dlp 原生支持的站点 (YouTube/Twitter/TikTok/Reddit/Pinterest/Vimeo/
      Tumblr/AcFun/B站/Instagram 单条等)
启动: python app.py  然后浏览器打开 http://127.0.0.1:8765
"""
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import glob
import shutil
import webbrowser
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

HOST, PORT = "127.0.0.1", 8765
FROZEN = getattr(sys, "frozen", False)          # PyInstaller 打包后为 True
RES_DIR = getattr(sys, "_MEIPASS", "") or os.path.dirname(os.path.abspath(__file__))
if FROZEN:
    # exe 版：运行时数据（downloads/.previews/缓存/Edge profile）放 exe 旁的独立子目录，
    # 不写进 PyInstaller 的一次性解包临时目录
    BASE = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "超哥视频下载数据")
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
DL_DIR = os.path.join(BASE, "downloads")
PREVIEW_DIR = os.path.join(BASE, ".previews")
DEFAULT_OUTDIR = os.path.join(os.path.expanduser("~"), "Downloads")
COOKIE_FILE = os.path.join(BASE, "douyin_cookies.txt")  # 用户手动导出的抖音 cookies（存在即用于抖音）
IG_COOKIE_FILE = os.path.join(BASE, "instagram_cookies.txt")  # 用户手动导出的 Instagram cookies（存在即用于 Instagram）

# ---- 外部工具路径解析（不写死本机路径）----
# 优先级：环境变量 CHAOGE_YTDLP / CHAOGE_FFMPEG_DIR / CHAOGE_TOOLS_DIR /
#          CHAOGE_OUTDIR / CHAOGE_PROJECT_DIR
#       -> <BASE>/config.json（本机私有配置，键名小写，见 config.example.json）
#       -> PATH 里的 yt-dlp / ffmpeg
#       -> <BASE>/tools/ 或 <PROJECT_DIR>/tools/ 下的便携工具
#       -> 裸命令名（交给系统 PATH 解析）
CFG_FILE = os.path.join(BASE, "config.json")


def _load_cfg():
    try:
        with open(CFG_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


CFG = _load_cfg()


def _cfg(key):
    v = os.environ.get("CHAOGE_" + key) or CFG.get(key.lower())
    v = str(v).strip() if v else ""
    return v or None


def _first_existing(*paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


PROJECT_DIR = _cfg("PROJECT_DIR") or BASE
TOOLS_DIR = _cfg("TOOLS_DIR") or ""


def resolve_ytdlp():
    p = _cfg("YTDLP")
    if p:
        return p
    w = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if w:
        return w
    cands = []
    for base in [b for b in (BASE, TOOLS_DIR, PROJECT_DIR) if b]:
        cands += [os.path.join(base, "tools", "yt-dlp", "yt-dlp.exe"),
                  os.path.join(base, "tools", "yt-dlp.exe"),
                  os.path.join(base, "yt-dlp.exe")]
    return _first_existing(*cands) or "yt-dlp.exe"


def resolve_ffmpeg_dir():
    d = _cfg("FFMPEG_DIR")
    if d:
        return d
    w = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if w:
        return os.path.dirname(os.path.abspath(w))
    for base in [b for b in (BASE, TOOLS_DIR, PROJECT_DIR) if b]:
        for sub in ("tools/ffmpeg/bin", "tools/ffmpeg", "tools", "."):
            cand = os.path.join(base, *sub.split("/"))
            if os.path.isfile(os.path.join(cand, "ffmpeg.exe")):
                return cand
    return ""


YTDLP = resolve_ytdlp()
FFMPEG_DIR = resolve_ffmpeg_dir()
DEFAULT_OUTDIR = _cfg("OUTDIR") or DEFAULT_OUTDIR


def ffbin(name):
    """ffmpeg / ffprobe 可执行文件绝对路径；配置目录没有时回退 PATH。"""
    if FFMPEG_DIR:
        p = os.path.join(FFMPEG_DIR, name)
        if os.path.isfile(p):
            return p
    return shutil.which(name) or name


os.makedirs(DL_DIR, exist_ok=True)
os.makedirs(PREVIEW_DIR, exist_ok=True)

MEDIA = {}        # extract id -> media dict
MEDIA_ORDER = []  # newest first
TASKS = {}        # download task id -> task dict
TASK_ORDER = []   # newest first
LOCK = threading.Lock()

PROG_RE = re.compile(r"\[download\]\s+([0-9.]+)% of\s+([0-9.]+)\s*(B|KiB|MiB|GiB)")
SPEED_RE = re.compile(r"at\s+([0-9.]+)\s*(B|KiB|MiB|GiB)/s")
UNIT = {"B": 1.0, "KiB": 1024.0, "MiB": 1024.0 ** 2, "GiB": 1024.0 ** 3}
DEST_RE = re.compile(r"\[download\] Destination: (.+?)\s*$")
MERGE_RE = re.compile(r'\[Merger\] Merging formats into "(.+?)"')
ALREADY_RE = re.compile(r"\[download\] (.+?) has already been downloaded")
ERROR_RE = re.compile(r"^\s*ERROR:")


def ytdlp_env():
    env = os.environ.copy()
    extra = [d for d in (FFMPEG_DIR,
                         os.path.join(TOOLS_DIR, "deno") if TOOLS_DIR else "",
                         os.path.join(BASE, "tools", "deno"),
                         os.path.join(PROJECT_DIR, "tools", "deno")) if d]
    env["PATH"] = os.pathsep.join(extra) + os.pathsep + env.get("PATH", "")
    return env


def _cookies_file_args(path, domain):
    """cookies 文件 -> yt-dlp --cookies 参数；文件不存在返回 None。
    兼容两种格式：Netscape cookies.txt 或浏览器请求头里的 'k=v; k=v' 一行。"""
    if not os.path.isfile(path):
        return None
    raw = open(path, encoding="utf-8", errors="replace").read().strip()
    if raw.startswith("#") or "\t" in raw.splitlines()[0]:
        return ["--cookies", path]
    out = os.path.join(BASE, ".%s_cookies_gen.txt" % domain)
    lines = ["# Netscape HTTP Cookie File"]
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, val = pair.split("=", 1)
        lines.append(".%s.com\tTRUE\t/\tFALSE\t1893456000\t%s\t%s"
                     % (domain, name.strip(), val.strip()))
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return ["--cookies", out]


def douyin_cookie_args(url):
    """抖音链接且用户提供了 cookies 文件时返回 --cookies 参数。"""
    if "douyin.com" not in url:
        return []
    args = _cookies_file_args(COOKIE_FILE, "douyin")
    return args or ["--cookies", os.devnull]


def instagram_cookie_args(url):
    """Instagram 链接：用户导出的 instagram_cookies.txt 存在即用（两种格式都支持）。
    浏览器登录态是提取失败后的回退，见 run_extract。"""
    if "instagram.com" not in url:
        return []
    return _cookies_file_args(IG_COOKIE_FILE, "instagram") or []


def browser_cookie_tries(url):
    """需要登录态的站点：依次尝试读浏览器登录态。
    浏览器正在运行时 cookie 数据库被独占锁定，必须完全退出浏览器（含后台驻留）才读得到。"""
    if "instagram.com" in url:
        return ["edge", "chrome"]
    return []


def fmt_dur(s):
    if not s:
        return None
    try:
        s = int(s)
    except Exception:
        return None
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return "%d:%02d:%02d" % (h, m, sec) if h else "%d:%02d" % (m, sec)


def quality_options(formats):
    heights = sorted({f.get("height") for f in formats
                      if f.get("height") and f.get("vcodec") != "none"}, reverse=True)
    opts = [{"label": "最佳画质", "fmt": "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/bv*+ba/b"}]
    for h in heights:
        if h <= 2160:
            opts.append({"label": "%dp" % h,
                         "fmt": "bv*[vcodec^=avc1][height<=%d]+ba[acodec^=mp4a]"
                                "/bv*[height<=%d]+ba/b[height<=%d]" % (h, h, h)})
    return opts[:7]


EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
EDGE_PROFILE = os.path.join(BASE, ".edge_harness")
# CDP 捕获用独立 profile：避免与 dump 渲染的 Edge 实例互相委托导致调试端口失效
EDGE_PROFILE_CDP = os.path.join(BASE, ".edge_harness_cdp")
# 抖音反爬会识别默认 HeadlessChrome UA 导致部分视频拒绝起播，伪装成正常 Edge UA
EDGE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0")
# CDP 捕获路径用与实测可播放会话一致的 UA
EDGE_UA_CDP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) HeadlessChrome/145.0.0.0 Safari/537.36")
# CDP 会话顺手取到的页面标题：dump 渲染失败时的回退路径用它兜标题
CDP_TITLES = {}


def get_ttwid():
    """向 bytedance 注册端点取真实 ttwid cookie（抖音无 cookie 会话拒绝起播），缓存 12 小时。"""
    cache = os.path.join(BASE, ".ttwid_cache")
    try:
        if os.path.isfile(cache) and time.time() - os.path.getmtime(cache) < 12 * 3600:
            v = open(cache, encoding="utf-8").read().strip()
            if v:
                return v
    except OSError:
        pass
    try:
        body = json.dumps({"region": "cn", "aid": 1768, "needFid": False,
                           "service": "www.ixigua.com",
                           "migrate_info": {"ticket": "", "source": "node"},
                           "cbUrlProtocol": "https", "union": True}).encode()
        req = urllib.request.Request("https://ttwid.bytedance.com/ttwid/union/register/",
                                     data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            sc = r.headers.get("Set-Cookie", "")
        m = re.search(r"ttwid=([^;]+)", sc)
        if m:
            try:
                open(cache, "w", encoding="utf-8").write(m.group(1))
            except OSError:
                pass
            return m.group(1)
    except Exception:
        pass
    return None


def find_edge():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def probe_duration(path):
    """ffprobe 取时长（秒，整数）；文件或 ffprobe 不可用返回 None。"""
    probe = ffbin("ffprobe.exe")
    if not (os.path.isfile(probe) and os.path.isfile(path)):
        return None
    try:
        pr = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                             "-of", "csv=p=0", path], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=30)
        if pr.returncode != 0:
            return None
        v = pr.stdout.decode("utf-8", "replace").strip()
        return int(float(v)) if v else None
    except Exception:
        return None


def douyin_render(url):
    """用 Edge 无头模式渲染抖音视频页，返回 (dump_html, video_id) 或 (None, None)"""
    edge = find_edge()
    if not edge:
        return None, None
    vid = None
    m = re.search(r"/video/(\d{15,20})", url)
    if m:
        vid = m.group(1)
    if not vid:
        return None, None
    for attempt in range(2):
        # 每次尝试用全新独立 profile：避开残留 Edge 实例的 profile 锁与委托；
        # 固定 profile 会让重复点击的两次提取互相清掉对方的 Edge 进程（抖音侧偶发空页时尤其明显）
        pname = ".edge_dump_%d_%d" % (int(time.time() * 1000), attempt)
        profile = os.path.join(BASE, pname)
        html = ""
        try:
            proc = subprocess.run(
                [edge, "--headless=new", "--disable-gpu", "--no-first-run",
                 "--no-default-browser-check",
                 "--user-agent=" + EDGE_UA,
                 "--user-data-dir=" + profile,
                 "--virtual-time-budget=12000", "--timeout=30000",
                 "--dump-dom", url],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=90)
            html = proc.stdout.decode("utf-8", "replace")
        except Exception as exc:
            douyin_dbg("render EXC: %r vid=%s" % (exc, vid))
        _cleanup_edge(pname)
        try:
            import shutil
            shutil.rmtree(profile, ignore_errors=True)
        except Exception:
            pass
        if "douyinvod" in html:
            return html, vid
        douyin_dbg("render: no media in dom attempt=%d bytes=%d vid=%s"
                   % (attempt, len(html), vid))
        time.sleep(1.5)
    return None, vid


def douyin_dbg(msg):
    """抖音路径诊断日志（定位间歇性失败原因用）。"""
    try:
        with open(os.path.join(BASE, ".douyin_debug.log"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


def _cleanup_edge(profile_dir_name):
    """清理命令行里带指定 profile 目录名的残留 msedge 进程（只匹配该唯一目录名，不碰用户浏览器）。"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
             "Where-Object { $_.CommandLine -match '%s' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % profile_dir_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
    except Exception:
        pass


def probe_has_audio(path):
    """ffprobe 检查文件是否含音频流：True/False，探测失败返回 None（不下结论）。"""
    try:
        pr = subprocess.run(
            [ffbin("ffprobe.exe"), "-v", "error",
             "-select_streams", "a", "-show_entries", "stream=codec_name",
             "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=ytdlp_env(), timeout=30)
        if pr.returncode != 0:
            return None  # 探测失败：不下「无音轨」的结论
        return bool(pr.stdout.strip())
    except Exception:
        return None


def douyin_cdp_media(url, vid):
    """分段流(MSE)抖音视频：Edge 无头 + CDP 捕获真实播放请求，下载并合成 mp4。
    返回合成后的 mp4 路径，失败返回 None。"""
    for attempt in (1, 2):
        r = _douyin_cdp_attempt(url, vid, attempt)
        if r:
            return r
    return None


def _douyin_cdp_attempt(url, vid, attempt):
    import tempfile
    import socket
    try:
        import websocket  # websocket-client，venv 已内置
    except ImportError:
        return None
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    # 每次尝试用全新独立 profile 目录：彻底避开残留 Edge 实例的 profile 锁与委托
    pname = ".edge_cdp_%d_%d" % (int(time.time() * 1000), attempt)
    profile = os.path.join(BASE, pname)
    # 不带 --dump-dom：dump 完成即退出会抢在播放器起播前杀进程；纯 URL 加载存活更久
    proc = subprocess.Popen(
        [find_edge(), "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--autoplay-policy=no-user-gesture-required",
         "--user-agent=" + EDGE_UA_CDP,
         "--user-data-dir=" + profile,
         "--remote-debugging-port=%d" % port, "--timeout=30000",
         url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    media, ws = [], None
    try:
        browser_ws = None
        for _ in range(40):  # 等 CDP 端口就绪（浏览器级 ws 地址）
            try:
                ver = json.load(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/version" % port, timeout=2))
                browser_ws = ver.get("webSocketDebuggerUrl")
                if browser_ws:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not browser_ws:
            douyin_dbg("cdp: no browser ws (vid=%s port=%d attempt=%d)" % (vid, port, attempt))
            return None
        ws = websocket.create_connection(browser_ws, timeout=60)
        mid = [0]

        def send(method, sid=None, **params):
            mid[0] += 1
            m = {"id": mid[0], "method": method, "params": params}
            if sid:
                m["sessionId"] = sid
            ws.send(json.dumps(m))
            return mid[0]

        tt = get_ttwid()
        douyin_dbg("cdp: connected, ttwid=%s attempt=%d" % ("ok" if tt else "MISSING", attempt))
        COOKIES = [
            {"name": "ttwid", "value": tt, "domain": ".douyin.com",
             "path": "/", "secure": True},
            {"name": "IsDouyinActive", "value": "true",
             "domain": ".douyin.com", "path": "/"}] if tt else []
        if COOKIES:
            send("Storage.setCookies", cookies=COOKIES)  # 浏览器级注入，先于播放器初始化 API
        # 自动附着所有现有/新建 target：抖音视频页 SPA 换 target 也不漏请求
        # Edge CDP 要求 waitForDebuggerOnStart 必填，缺失会导致整个连接被重置
        send("Target.setAutoAttach", autoAttach=True, flatten=True,
             waitForDebuggerOnStart=False)
        t0 = time.time()
        enabled = set()

        def note_title(ti):
            # targetInfo 自带 title：不额外发 CDP 命令（发 Runtime.evaluate 会让连接提前重置，媒体还没起播就断）
            ti = ti or {}
            ttl = (ti.get("title") or "").strip()
            if (not ttl or ttl.lower() in ("about:blank", "douyin", "www.douyin.com")
                    or "douyin.com/" in ttl.lower()):
                return  # attach 时的占位标题（about:blank / 直接显示 URL），等 targetInfoChanged 给真实标题
            if vid and "douyin.com" in (ti.get("url") or "") and not CDP_TITLES.get(vid):
                CDP_TITLES[vid] = ttl
                douyin_dbg("cdp: title=%s" % ttl[:60])

        while time.time() - t0 < 28:
            ws.settimeout(max(0.5, 28 - (time.time() - t0)))
            try:
                msg = json.loads(ws.recv())
            except Exception as e:
                douyin_dbg("cdp recv end: %s after %.1fs (got %d media)" % (
                    type(e).__name__, time.time() - t0, len(media)))
                break
            mth = msg.get("method")
            if mth == "Target.attachedToTarget":
                s2 = msg["params"]["sessionId"]
                ti = msg["params"]["targetInfo"]
                note_title(ti)
                # 只对 page 类 target 发 Network 命令：对 Edge 内部页/扩展 target 发会导致连接被重置
                if s2 not in enabled and ti.get("type") == "page":
                    enabled.add(s2)
                    if COOKIES:
                        send("Network.setCookies", sid=s2, cookies=COOKIES)
                    send("Network.enable", sid=s2)
            elif mth == "Target.targetInfoChanged":
                note_title(msg["params"].get("targetInfo"))
            elif mth == "Network.requestWillBeSent":
                u = msg["params"]["request"]["url"]
                if "douyinvod" in u and "video/tos" in u:
                    media.append(u)
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _cleanup_edge(pname)
        try:
            import shutil
            shutil.rmtree(profile, ignore_errors=True)
        except Exception:
            pass
    media = list(dict.fromkeys(media))
    douyin_dbg("cdp: captured %d media urls (vid=%s)" % (len(media), vid))
    if not media:
        return None
    tmpdir = tempfile.mkdtemp(dir=BASE)
    try:
        segs = []
        for i, u in enumerate(media[:6]):
            req = urllib.request.Request(u, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
                "Referer": "https://www.douyin.com/"})
            with urllib.request.urlopen(req, timeout=60) as r, \
                    open(os.path.join(tmpdir, "s%d.mp4" % i), "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        probe = ffbin("ffprobe.exe")
        env = ytdlp_env()
        vids, auds = [], []
        for fn in glob.glob(os.path.join(tmpdir, "s*.mp4")):
            try:
                pr = subprocess.run([probe, "-v", "error", "-select_streams", "v",
                                     "-show_entries", "stream=width", "-of", "csv=p=0", fn],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    env=env, timeout=30)
                if pr.stdout.strip():
                    vids.append(fn)
                else:
                    auds.append(fn)
            except Exception:
                pass
        if not vids:
            return None
        out = os.path.join(PREVIEW_DIR, "douyin_%s_preview.mp4" % vid)
        cmd = [ffbin("ffmpeg.exe"), "-y", "-v", "error"]
        for f in sorted(vids, key=os.path.getsize)[-1:] + \
                sorted(auds, key=os.path.getsize)[-1:]:
            cmd += ["-i", f]
        cmd += ["-c", "copy", "-f", "mp4", out]
        if subprocess.run(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, env=env, timeout=120).returncode != 0:
            return None
        return out if os.path.isfile(out) and os.path.getsize(out) > 1024 else None
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except OSError:
            pass


def douyin_extract(m):
    """抖音分支：Edge 无头渲染 -> 元数据 + 直接下载完整视频作预览。成功返回 True。"""
    import html as htmlmod
    import urllib.request
    try:
        dump, vid = douyin_render(m["url"])
        douyin_dbg("extract: render dump=%s vid=%s" % (bool(dump), vid))
        if not dump:
            # 渲染两次都拿不到播放页（抖音反爬空页/挑战页）：改走 CDP 播放捕获。
            # 它用全新 profile 且注入 ttwid，实测比 --dump-dom 稳；成品预览已生成即算提取成功。
            merged = douyin_cdp_media(m["url"], vid) if vid else None
            douyin_dbg("extract: dump-fail cdp fallback merged=%s vid=%s" % (bool(merged), vid))
            if not merged:
                return False
            title = (CDP_TITLES.get(vid or "") or "").strip() or ("douyin_" + (vid or m["id"]))
            safe = re.sub(r'[<>:"/\\|?*\s]+', "_", title)[:80].strip("_")
            m["title"] = title
            m["duration"] = fmt_dur(probe_duration(merged))
            m["source"] = "Douyin"
            m["webpage"] = m["url"]
            m["qualities"] = [{"label": "最佳画质", "fmt": "douyin"}]
            m["preview_path"] = merged
            m["has_audio"] = probe_has_audio(merged)
            finalize_douyin_preview(m)
            m["douyin_play"] = None
            m["douyin_id"] = vid
            m["douyin_filename"] = (safe or ("douyin_" + (vid or m["id"]))) + ".mp4"
            m["status"] = "ready"
            return True
        t = re.search(r"<title>([^<]+)</title>", dump)
        title = htmlmod.unescape(t.group(1)).strip() if t else ""
        title = re.sub(r"\s*[-–]\s*抖音\s*$", "", title) or ("douyin_" + (vid or m["id"]))
        srcs = re.findall(r'<source[^>]*src="([^"]+douyinvod[^"]+)"', dump)
        douyin_dbg("extract: srcs=%d vid=%s" % (len(srcs), vid))
        if srcs:
            play_url = htmlmod.unescape(srcs[0])
        else:
            # 分段流(MSE)视频：DOM 里没有 <source>，走 CDP 捕获真实播放请求
            merged = douyin_cdp_media(m["url"], vid)
            douyin_dbg("extract: cdp merged=%s vid=%s" % (bool(merged), vid))
            if not merged:
                return False
            play_url = None
        dur = re.search(r'"duration":(\d+)', dump)
        m["title"] = title
        m["duration"] = fmt_dur(int(dur.group(1)) if dur else None)
        m["source"] = "Douyin"
        m["webpage"] = m["url"]
        m["qualities"] = [{"label": "最佳画质", "fmt": "douyin"}]
        safe = re.sub(r'[<>:"/\\|?*\s]+', "_", title)[:80].strip("_") or ("douyin_" + vid)
        pv = os.path.join(PREVIEW_DIR, "douyin_%s_preview.mp4" % vid)
        if play_url:
            req = urllib.request.Request(play_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
                "Referer": "https://www.douyin.com/"})
            with urllib.request.urlopen(req, timeout=60) as r, open(pv, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        if not os.path.isfile(pv) or os.path.getsize(pv) < 1024:
            return False
        m["preview_path"] = pv
        m["has_audio"] = probe_has_audio(pv)
        finalize_douyin_preview(m)
        douyin_dbg("extract: has_audio=%s vid=%s" % (m["has_audio"], vid))
        m["douyin_play"] = play_url
        m["douyin_id"] = vid
        m["douyin_filename"] = safe + ".mp4"
        m["status"] = "ready"
        return True
    except Exception:
        import traceback
        douyin_dbg("extract EXC: " + traceback.format_exc().replace("\n", " | ")[-400:])
        return False


def normalize_douyin(url):
    """抖音分享文本/短链/modal_id 链接 -> 标准 /video/<id> 链接"""
    if "douyin.com" not in url:
        return url
    m = re.search(r"/video/(\d{15,20})", url)
    if m:
        return "https://www.douyin.com/video/%s" % m.group(1)
    m = re.search(r"modal_id=(\d{15,20})", url)
    if m:
        return "https://www.douyin.com/video/%s" % m.group(1)
    if re.search(r"v\.douyin\.com/", url):
        try:  # 短链跟随重定向取最终 video id
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                final = r.geturl()
            m = re.search(r"/video/(\d{15,20})|modal_id=(\d{15,20})", final)
            if m:
                return "https://www.douyin.com/video/%s" % (m.group(1) or m.group(2))
        except Exception:
            pass
    return url


def run_extract(mid):
    m = MEDIA[mid]
    m["status"] = "extracting"
    if "douyin.com" in m["url"]:
        if douyin_extract(m):
            return
        # Edge 无头失败（如私密视频/需登录）时回退 yt-dlp 流程，报常规错误
    try:
        base_cmd = [YTDLP, m["url"], "--no-playlist", "--skip-download",
                    "--dump-single-json", "--js-runtimes", "deno",
                    "--remote-components", "ejs:github"]
        cookie_args = douyin_cookie_args(m["url"]) + instagram_cookie_args(m["url"])

        def ytdlp_try(extra):
            return subprocess.run(base_cmd + cookie_args + extra,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8", errors="replace",
                                  env=ytdlp_env(), timeout=120)

        proc = ytdlp_try([])
        if proc.returncode != 0:
            # 需要登录态的站点（Instagram）：改用浏览器登录 cookie 再试
            for br in browser_cookie_tries(m["url"]):
                proc = ytdlp_try(["--cookies-from-browser", br])
                if proc.returncode == 0:
                    m["cookie_args"] = ["--cookies-from-browser", br]
                    break
        if proc.returncode != 0:
            douyin_dbg("ytdlp extract fail rc=%s url=%s err=%s" % (
                proc.returncode, m["url"],
                (proc.stderr or "").replace("\n", " | ")[-300:]))
            m["status"] = "error"
            if "douyin.com" in m["url"]:
                m["error"] = ("抖音提取失败：该视频可能为私密、仅朋友可见或已被删除；"
                              "公开视频请重试一次（详见 抖音使用说明.txt）")
            elif "instagram.com" in m["url"]:
                m["error"] = ("Instagram 提取失败：Instagram 对未登录的请求返回空媒体数据，需要登录态。"
                              "办法一：把浏览器导出的 Instagram cookies 存成 " + IG_COOKIE_FILE +
                              " 然后重试。办法二：先在 Edge 里登录 Instagram，再完全退出 Edge"
                              "（含后台驻留）后重试——Edge 运行时它的 cookie 数据库被独占锁定，读不到。")
            else:
                m["error"] = "提取失败：该链接可能不受支持、需要登录或已失效"
            return
        info = json.loads(proc.stdout)
        m["title"] = info.get("title")
        m["duration"] = fmt_dur(info.get("duration"))
        m["thumbnail"] = info.get("thumbnail")
        m["uploader"] = info.get("uploader") or info.get("channel") or info.get("uploader_id")
        m["source"] = info.get("extractor_key") or info.get("extractor")
        m["view_count"] = info.get("view_count")
        m["webpage"] = info.get("webpage_url") or m["url"]
        m["qualities"] = quality_options(info.get("formats") or [])
        # 低清预览片：优先渐进式单文件，尽量无需合并直接可播
        pid = re.sub(r"\W", "_", str(info.get("id") or mid))[:80]
        pv = os.path.join(PREVIEW_DIR, pid + "_preview.%(ext)s")
        for old in glob.glob(os.path.join(PREVIEW_DIR, pid + "_preview*")):
            try:
                os.remove(old)  # 清掉残留中间文件，避免 416 续传错误
            except OSError:
                pass
        pr = subprocess.run([YTDLP, m["url"], "--no-playlist",
                             "-f", "b[height<=360]/bv*[height<=360]+ba/b",
                             "--merge-output-format", "mp4",
                             "--js-runtimes", "deno",
                             "--remote-components", "ejs:github",
                             "-o", pv, "--newline", "--no-part"]
                            + cookie_args + (m.get("cookie_args") or []),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace",
                            env=ytdlp_env(), timeout=180)
        if pr.returncode == 0:
            cands = []
            for line in (pr.stdout or "").splitlines():
                mm = MERGE_RE.search(line)
                if mm:
                    cands.append(mm.group(1))
                    continue
                mm = ALREADY_RE.search(line) or DEST_RE.search(line)
                if mm:
                    cands.append(mm.group(1))
            for p in reversed(cands):  # 合并产物最后生成，优先取
                if os.path.isfile(p):
                    m["preview_path"] = p
                    break
        m["status"] = "ready"
    except subprocess.TimeoutExpired:
        m["status"] = "error"
        m["error"] = "提取超时"
    except Exception as e:
        m["status"] = "error"
        m["error"] = str(e)


def _report_progress(t, done, total, pct=None):
    """更新进度百分比 + 实时速度（bytes/s，指数滑动平均，采样间隔 >=0.5s）。"""
    now = time.time()
    if pct is not None:
        t["progress"] = max(t.get("progress") or 0.0, pct)
    elif total:
        t["progress"] = min(99.0, done / total * 100.0) if total else 99.0
    prev_t, prev_b = t.get("_tick") or (now, done)
    dt = now - prev_t
    if dt >= 0.5:
        inst = max(0.0, done - prev_b) / dt
        prev = t.get("speed")
        t["speed"] = inst if not prev else (prev * 0.6 + inst * 0.4)
        t["_tick"] = (now, done)
    else:
        t["_tick"] = (prev_t, prev_b)


def pause_task(tid):
    """请求暂停某一条下载任务（只影响这一条，其它任务继续）。"""
    t = TASKS.get(tid)
    if not t:
        return None
    if t.get("status") in ("queued", "downloading", "processing"):
        t["pause"] = True
    return t


def resume_task(tid):
    """继续一条已暂停的任务；yt-dlp 任务靠 .part 断点续传，抖音复制任务从已复制偏移续。"""
    t = TASKS.get(tid)
    if not t:
        return None
    t["pause"] = False
    if t.get("status") == "paused":
        t["status"] = "queued"
        t["_tick"] = None
        threading.Thread(target=run_task, args=(tid,), daemon=True).start()
    return t


def public_media(mid):
    m = MEDIA[mid]
    keys = ("id", "url", "status", "title", "duration", "thumbnail",
            "uploader", "source", "qualities", "error", "view_count", "webpage",
            "has_audio")
    d = {k: m.get(k) for k in keys}
    pp = m.get("preview_path")
    # 预览文件不存在时不能让前端挂一个必然失败的 <video>（表现为预览一直卡住）
    d["has_preview"] = bool(pp) and os.path.isfile(pp)
    return d


LIGHT_PREVIEW_MAX = 6_000_000   # 超过这个体积就转一版轻量预览，避免网页预览一直加载不出来


def make_light_preview(src, dst, height=480):
    """把大文件转成 480p faststart 轻量预览（供网页/GUI 快速播放），失败返回 None。"""
    if not src or not os.path.isfile(src):
        return None
    cmd = [ffbin("ffmpeg.exe"), "-y", "-v", "error", "-i", src,
           "-vf", "scale=-2:'min(ih\\,%d)'" % height, "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k",
           "-movflags", "+faststart", dst]
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env=ytdlp_env(), timeout=420)
    except Exception:
        return None
    if r.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 1024:
        return dst
    try:
        if os.path.isfile(dst):
            os.remove(dst)
    except OSError:
        pass
    return None


def finalize_douyin_preview(m):
    """完整视频留作下载源，预览改用轻量版（体积大时才转）。"""
    full = m.get("preview_path")
    if not full or not os.path.isfile(full):
        return
    m["full_path"] = full
    if os.path.getsize(full) <= LIGHT_PREVIEW_MAX:
        return
    light = re.sub(r"_preview\.mp4$", "_prev480.mp4", full)
    if light == full:
        light = full[:-4] + "_prev480.mp4"
    lp = make_light_preview(full, light)
    if lp:
        m["preview_path"] = lp
        douyin_dbg("preview: light=%s (%d bytes) full=%d" % (
            os.path.basename(lp), os.path.getsize(lp), os.path.getsize(full)))


def pick_output_file(filepath, dests):
    """yt-dlp 没有 [Merger] 行时（单一渐进格式），从打印过的 Destination 里找出真实产物。"""
    for p in [filepath] + list(dests):
        if p and os.path.isfile(p):
            return p
    for p in [filepath] + list(dests):
        if not p:
            continue
        if p.endswith(".part") and os.path.isfile(p[:-5]):
            return p[:-5]
        m = re.search(r"\.f\d+(\.[A-Za-z0-9]+)$", p)  # 片段名 X.f399.mp4 -> X.mp4
        if m:
            alt = p[:m.start()] + m.group(1)
            if os.path.isfile(alt):
                return alt
    return filepath


def run_task_douyin(t, m):
    """抖音：提取时已把完整视频下到预览目录，这里分块复制过去并推进进度。
    支持暂停/继续：暂停时记录已复制偏移，继续时从该偏移接着写。"""
    try:
        src = m.get("full_path") or m["preview_path"]
        total = os.path.getsize(src)
        outdir = t.get("outdir") or DL_DIR
        resuming = bool(t.get("filepath")) and os.path.isfile(t["filepath"]) and (t.get("copied") or 0) > 0
        if resuming:
            dst = t["filepath"]
        else:
            dst = os.path.join(outdir, m["douyin_filename"])
            if os.path.exists(dst):
                base, ext = os.path.splitext(m["douyin_filename"])
                dst = os.path.join(outdir, "%s [%s]%s" % (base, m.get("douyin_id") or "dy", ext))
            t["filepath"] = dst
            t["copied"] = 0
        done = int(t.get("copied") or 0)
        t["status"] = "downloading"
        t["_tick"] = None
        with open(src, "rb") as f, open(dst, "r+b" if resuming else "wb") as g:
            if resuming:
                f.seek(done)
                g.seek(done)
            while True:
                if t.get("pause"):
                    g.flush()
                    os.fsync(g.fileno())
                    t["copied"] = done
                    t["speed"] = None
                    t["status"] = "paused"
                    return
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                g.write(chunk)
                done += len(chunk)
                t["copied"] = done
                _report_progress(t, done, total)
        t["filepath"] = dst
        t["title"] = os.path.splitext(os.path.basename(dst))[0]
        t["size"] = os.path.getsize(dst)
        t["copied"] = done
        t["status"] = "done"
        t["progress"] = 100.0
        t["speed"] = None
        if probe_has_audio(dst) is False:
            t["note"] = "该视频无音轨（抖音源未提供音频流）"
            douyin_dbg("task: NO AUDIO %s" % os.path.basename(dst))
    except Exception as e:
        t["status"] = "error"
        t["error"] = str(e)


def run_task(tid):
    t = TASKS[tid]
    if t.get("pause"):
        t["status"] = "paused"
        return
    m = MEDIA.get(t.get("media_id") or "")
    if m and m.get("douyin_filename") and m.get("preview_path") and os.path.isfile(m["preview_path"]):
        run_task_douyin(t, m)
        return
    env = ytdlp_env()
    out_tmpl = os.path.join(t.get("outdir") or DL_DIR, "%(title).120B [%(id)s].%(ext)s")
    cmd = [YTDLP, t["url"], "--no-playlist", "-f", t.get("quality_fmt") or "bv*+ba/b",
           "--js-runtimes", "deno", "--remote-components", "ejs:github",
           "--merge-output-format", "mp4", "--remux-video", "mp4",
           "-o", out_tmpl, "--newline", "--restrict-filenames"]
    cmd += douyin_cookie_args(t["url"]) + ((m or {}).get("cookie_args") or [])
    t["status"] = "downloading"
    filepath = None
    err_tail = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env=env)
        files = {}  # dest -> [total_bytes, done_bytes]
        dests = []  # yt-dlp 打印过的 Destination（单一渐进格式时这就是最终文件）
        cur = None
        for line in proc.stdout:
            if t.get("pause"):
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except Exception:
                    proc.kill()
                t["speed"] = None
                t["status"] = "paused"
                return
            line = line.rstrip()
            if not line:
                continue
            m = DEST_RE.search(line)
            if m:
                cur = m.group(1).strip()
                if cur not in dests:
                    dests.append(cur)
                files.setdefault(cur, [0.0, 0.0])
            m = PROG_RE.search(line)
            if m and cur:
                total = float(m.group(2)) * UNIT[m.group(3)]
                files[cur][0] = max(files[cur][0], total)
                files[cur][1] = total * float(m.group(1)) / 100.0
                tot = sum(v[0] for v in files.values())
                done = sum(v[1] for v in files.values())
                if tot > 0:
                    pct = min(99.0, done / tot * 100.0)
                    _report_progress(t, done, tot, pct=pct)
                ms = SPEED_RE.search(line)  # yt-dlp 自报速度，比本地采样更准
                if ms:
                    t["speed"] = float(ms.group(1)) * UNIT[ms.group(2)]
            m = ALREADY_RE.search(line)
            if m:
                filepath = m.group(1)
                sz = os.path.getsize(filepath) if os.path.isfile(filepath) else 0
                files[filepath] = [float(sz), float(sz)]
                cur = filepath
            m = MERGE_RE.search(line)
            if m:
                filepath = m.group(1)
            if "Merging" in line or "ffmpeg" in line:
                t["status"] = "processing"
            if ERROR_RE.search(line):
                err_tail.append(line)
        rc = proc.wait()
        if rc == 0 and not (filepath and os.path.isfile(filepath)):
            filepath = pick_output_file(filepath, dests)
        if rc == 0 and filepath and os.path.isfile(filepath):
            t["filepath"] = filepath
            t["title"] = os.path.splitext(os.path.basename(filepath))[0]
            t["size"] = os.path.getsize(filepath)
            t["status"] = "done"
            t["progress"] = 100.0
            t["speed"] = None
        else:
            t["status"] = "error"
            t["error"] = (err_tail[-1] if err_tail
                          else "yt-dlp 退出码 %s，未产生文件" % rc)
    except Exception as e:
        t["status"] = "error"
        t["error"] = str(e)


def public_task(tid):
    t = TASKS[tid]
    keys = ("id", "url", "status", "progress", "title", "error", "outdir", "size",
            "note", "speed")
    d = {k: t.get(k) for k in keys}
    d["progress"] = round(float(t.get("progress") or 0), 1)
    if d.get("speed") is not None:
        d["speed"] = round(float(d["speed"]), 1)
    d["has_file"] = bool(t.get("filepath"))
    d["can_pause"] = bool(t.get("status") in ("queued", "downloading", "processing"))
    d["can_resume"] = bool(t.get("status") == "paused")
    return d


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, download=True):
        try:
            if not os.path.isfile(path):
                self._send_json({"error": "file not found"}, 404)
                return
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            size = os.path.getsize(path)
            name = os.path.basename(path)
            rng = self.headers.get("Range")
            start, end, code = 0, size - 1, 200
            if rng:
                mm = re.match(r"bytes=(\d*)-(\d*)", rng)
                if mm:
                    if mm.group(1):
                        start = int(mm.group(1))
                        end = int(mm.group(2)) if mm.group(2) else size - 1
                    elif mm.group(2):
                        start = max(0, size - int(mm.group(2)))
                    start, end = min(start, size - 1), min(end, size - 1)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", "bytes */%d" % size)
                        self.end_headers()
                        return
                    code = 206
            length = end - start + 1
            with open(path, "rb") as f:
                f.seek(start)
                data = f.read(length)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            disp = "attachment" if download else "inline"
            self.send_header("Content-Disposition",
                             "%s; filename*=UTF-8''%s" % (disp, quote(name)))
            self.send_header("Content-Length", str(length))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 播放器中止预取属正常

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(RES_DIR, "index.html"), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        q = parse_qs(u.query)
        if u.path == "/api/task":
            tid = q.get("id", [""])[0]
            with LOCK:
                if tid not in TASKS:
                    self._send_json({"error": "no such task"}, 404)
                    return
                self._send_json(public_task(tid))
            return
        if u.path == "/api/tasks":
            with LOCK:
                self._send_json([public_task(i) for i in TASK_ORDER])
            return
        if u.path == "/api/media":
            with LOCK:
                m = MEDIA.get(q.get("id", [""])[0])
            if not m:
                self._send_json({"error": "no such media"}, 404)
                return
            self._send_json(public_media(m["id"]))
            return
        if u.path == "/api/media/latest":
            with LOCK:
                mid = MEDIA_ORDER[0] if MEDIA_ORDER else None
                m = MEDIA.get(mid) if mid else None
            if not m:
                self._send_json({"error": "no media"}, 404)
                return
            self._send_json(public_media(m["id"]))
            return
        if u.path == "/api/listdirs":
            p = q.get("path", [""])[0] or DEFAULT_OUTDIR
            p = os.path.abspath(p)
            if not os.path.isdir(p):
                self._send_json({"error": "目录不存在"}, 404)
                return
            dirs = []
            try:
                for name in sorted(os.listdir(p)):
                    fp = os.path.join(p, name)
                    try:
                        if os.path.isdir(fp) and not name.startswith("."):
                            dirs.append({"name": name, "path": fp})
                    except OSError:
                        pass
            except OSError as e:
                self._send_json({"error": str(e)}, 403)
                return
            parent = os.path.dirname(p)
            self._send_json({"current": p,
                             "parent": parent if parent != p else None,
                             "dirs": dirs})
            return
        if u.path == "/preview":
            with LOCK:
                m = MEDIA.get(q.get("id", [""])[0])
            if not m or not m.get("preview_path"):
                self._send_json({"error": "no preview"}, 404)
                return
            fp = os.path.abspath(m["preview_path"])
            if not fp.startswith(os.path.abspath(PREVIEW_DIR)):
                self._send_json({"error": "bad path"}, 400)
                return
            self._send_file(fp, download=False)
            return
        if u.path == "/file":
            tid = q.get("id", [""])[0]
            with LOCK:
                t = TASKS.get(tid)
            if not t or not t.get("filepath"):
                self._send_json({"error": "no file"}, 404)
                return
            fp = os.path.abspath(t["filepath"])
            if not fp.startswith(os.path.abspath(DL_DIR)):
                self._send_json({"error": "bad path"}, 400)
                return
            self._send_file(fp, download=(q.get("inline", ["0"])[0] != "1"))
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            self._send_json({"error": "bad request"}, 400)
            return
        if u.path == "/api/extract":
            url = (body.get("url") or "").strip()
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.netloc:
                self._send_json({"error": "请输入有效的 http(s) 链接"}, 400)
                return
            mid = uuid.uuid4().hex[:12]
            MEDIA[mid] = {"id": mid, "url": normalize_douyin(url), "status": "queued"}
            with LOCK:
                MEDIA_ORDER.insert(0, mid)
            threading.Thread(target=run_extract, args=(mid,), daemon=True).start()
            self._send_json({"id": mid})
            return
        if u.path == "/api/download":
            mid = body.get("media_id") or ""
            with LOCK:
                m = MEDIA.get(mid)
            if not m or m.get("status") != "ready":
                self._send_json({"error": "请先提取视频"}, 400)
                return
            outdir = (body.get("outdir") or DEFAULT_OUTDIR).strip()
            if not os.path.isdir(outdir):
                self._send_json({"error": "下载目录不存在，请先选择"}, 400)
                return
            fmt_label = body.get("quality") or "最佳画质"
            qfmt = "bv*+ba/b"
            for o in (m.get("qualities") or []):
                if o["label"] == fmt_label:
                    qfmt = o["fmt"]
                    break
            tid = uuid.uuid4().hex[:12]
            task = {"id": tid, "url": m.get("webpage") or m["url"], "outdir": outdir,
                    "media_id": mid,
                    "quality_fmt": qfmt, "quality_label": fmt_label,
                    "status": "queued", "progress": 0.0, "title": m.get("title"),
                    "filepath": None, "error": None, "size": None,
                    "speed": None, "copied": 0, "pause": False}
            with LOCK:
                TASKS[tid] = task
                TASK_ORDER.insert(0, tid)
            threading.Thread(target=run_task, args=(tid,), daemon=True).start()
            self._send_json({"id": tid})
            return
        if u.path == "/api/pause":
            tid = body.get("id") or ""
            t = pause_task(tid)
            if not t:
                self._send_json({"error": "no such task"}, 404)
                return
            self._send_json(public_task(tid))
            return
        if u.path == "/api/resume":
            tid = body.get("id") or ""
            t = resume_task(tid)
            if not t:
                self._send_json({"error": "no such task"}, 404)
                return
            self._send_json(public_task(tid))
            return
        if u.path == "/api/clear":
            with LOCK:
                if body.get("all"):
                    ids = list(TASK_ORDER)
                else:
                    ids = [i for i in TASK_ORDER
                           if TASKS[i]["status"] in ("done", "error")]
                for i in ids:
                    TASK_ORDER.remove(i)
                    TASKS.pop(i, None)
            self._send_json({"cleared": len(ids)})
            return
        if u.path == "/api/delete":
            tid = body.get("id") or ""
            with LOCK:
                t = TASKS.pop(tid, None)
                if tid in TASK_ORDER:
                    TASK_ORDER.remove(tid)
            if not t:
                self._send_json({"error": "no such task"}, 404)
                return
            self._send_json({"deleted": tid})
            return
        if u.path == "/api/openfolder":
            p = (body.get("path") or "").strip()
            if not os.path.isdir(p):
                self._send_json({"error": "目录不存在"}, 404)
                return
            try:
                os.startfile(p)  # 打开系统资源管理器
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
                return
            self._send_json({"opened": p})
            return
        self._send_json({"error": "not found"}, 404)


def main():
    port = PORT
    for _ in range(6):
        try:
            srv = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            port += 1
    else:
        print("端口不可用", file=sys.stderr)
        sys.exit(1)
    print("超哥视频下载: http://%s:%d  默认下载目录: %s" % (HOST, port, DEFAULT_OUTDIR))

    def opener():
        try:
            with urllib.request.urlopen("http://%s:%d/" % (HOST, port), timeout=2):
                webbrowser.open("http://%s:%d/" % (HOST, port))
        except Exception:
            pass

    threading.Thread(target=opener, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
