# 超哥视频下载 · chaoge-video-downloader

粘贴链接 → 提取信息并生成预览 → 选画质和保存目录 → 下载成 MP4。
全程在本机运行，不经过任何第三方服务器。提供两个界面：

- **网页版**：`python app.py` 后浏览器打开 http://127.0.0.1:8765
- **桌面程序版**：`gui.py`（Tkinter 原生窗口，不开浏览器、不开 HTTP 端口），可打包成单文件 exe 双击运行

## 功能

- 提取标题、时长、分辨率、可用画质列表，并生成可直接播放的预览
- 预览文件超过 6 MB 时自动转成 480p `faststart` 轻量预览（网页/GUI 秒开），下载用的仍是完整画质文件
- 画质下拉选择：最佳画质 / 2160p / 1080p / 720p …
- 保存目录可选（网页版目录弹窗，GUI 的「浏览」按钮）
- 每条任务独立显示进度百分比 + 实时速度，可单独**暂停 / 继续**（yt-dlp 用 `.part` 断点续传，抖音拷贝路径用字节偏移续传）
- 下载完成后校验成品是否含音频流，无音轨时在任务下方提示
- 刷新页面 / 重开程序后下载记录仍在（记录持久化在数据目录）
- 诊断日志 `.douyin_debug.log`，便于排查提取失败

## 支持站点

| 站点 | 方式 | 是否需要登录 |
|---|---|---|
| 抖音（公开视频） | 本机 Microsoft Edge 无头渲染 + CDP 网络捕获播放地址（整文件 mp4 与 MSE 分段流都支持） | 不需要 cookies（程序自动向 bytedance 注册端点取 `ttwid`） |
| YouTube / Twitter / TikTok / Reddit / Pinterest / Vimeo / Tumblr / AcFun / B 站 等 | yt-dlp 原生提取器 | 不需要 |
| Instagram 单条 reel/帖子 | yt-dlp + 你自己的登录 cookie | **需要**（见下） |

抖音支持的链接形式：`https://v.douyin.com/xxx/` 短链、`https://www.douyin.com/video/<id>`、带 `modal_id=` 的收藏页链接、以及整段分享文字（程序会自动找出其中的链接）。

## 外部工具与配置

程序不写死任何本机路径。外部工具按以下优先级解析：

1. 环境变量 `CHAOGE_YTDLP` / `CHAOGE_FFMPEG_DIR` / `CHAOGE_TOOLS_DIR` / `CHAOGE_OUTDIR` / `CHAOGE_PROJECT_DIR`
2. 数据目录下的 `config.json`（键名小写，参考 `config.example.json`）
3. 系统 `PATH` 里的 `yt-dlp` / `ffmpeg`
4. 数据目录（或 `tools_dir`）下的 `tools\yt-dlp\`、`tools\ffmpeg\bin\`、`tools\deno\`
5. 都找不到时回退裸命令名，交给系统 `PATH`

```json
{
  "ytdlp": "C:\\tools\\yt-dlp.exe",
  "ffmpeg_dir": "C:\\tools\\ffmpeg\\bin",
  "tools_dir": "C:\\path\\to\\project",
  "outdir": "C:\\Users\\you\\Downloads"
}
```

`config.json` 已被 `.gitignore` 排除（本机私有，不会进仓库）。抖音的 Edge 安装路径用通用候选值自动探测（`C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` 等）。

## 运行

### 网页版

```bash
pip install -r requirements.txt      # 需要 yt-dlp
python app.py                        # 然后打开 http://127.0.0.1:8765
```

Windows 下也可以直接双击 `启动下载器.bat`（会用 PATH 里的 `python`，找不到时尝试 `py`）。

### 桌面版（源码）

```bash
python gui.py
python gui.py --check                # 打印解析到的 yt-dlp/ffmpeg/deno/Edge 路径后退出
```

### 打包成 exe

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --name 超哥视频下载 --clean gui.py
# 产物 dist\超哥视频下载.exe；拷到任意位置双击运行
超哥视频下载.exe --check             # 自检外部工具是否找得到
```

exe 版运行时会在 exe 旁边自动建 `超哥视频下载数据\` 存放下载成品、预览、缓存和 Edge profile；把 `config.json` 放在这个目录里即可指定外部工具路径。窗口关闭即停止（exe 版不开 HTTP 服务）。

## 已知限制（如实说明）

- **抖音**：只支持公开视频。私密、仅朋友可见、已删除的视频会提取失败。抖音反爬较强，程序依赖本机 Edge 无头浏览器起播，极端风控时段偶发失败会自动重试（dump 渲染失败时回退到 CDP 播放捕获）。下载的是抖音服务器提供的最高可用清晰度，带抖音片尾水印。
- **Instagram**：对未登录请求返回空媒体数据。需要你自己的登录 cookie —— 把 Netscape 格式 cookies 存成程序目录下的 `instagram_cookies.txt`（也支持直接粘贴浏览器请求头里那一行 `k=v; k=v`），或在完全退出 Edge（含后台驻留进程）后让程序读取浏览器 cookie。浏览器正在运行时 cookie 数据库被独占锁定，读不到。
- 网页版监听 8765（被占用时自动改用 8766）；桌面版不开任何网络端口。
- 打包 exe 只含 Python 程序与界面文件，`yt-dlp` / `ffmpeg` / `deno` 仍是外部工具，换电脑需一并携带或在 `config.json` 指路径。

## 法律与使用提示

本项目仅供个人学习、备份自己有权保存的内容。请遵守各站点的服务条款和版权规定，不要分发未授权的他人作品。作者不对使用本工具造成的后果负责。

## 仓库内容

```
app.py            后端：提取 / 预览 / 下载 / 暂停继续 / 抖音 Edge+CDP 抓取 / 外部工具路径解析
gui.py            桌面 Tkinter 界面（含 --check 自检）
index.html        网页版前端
启动下载器.bat    Windows 一键启动网页版
config.example.json  配置样例（复制成 config.json 后填自己的路径）
requirements.txt  依赖说明
抖音使用说明.txt   抖音链接形式与限制说明
超哥视频下载.spec  PyInstaller 配置
```
