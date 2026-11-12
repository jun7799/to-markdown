---
name: to-markdown
description: 将文件和URL转换为Markdown格式。支持PDF、DOCX、PPTX、XLSX、CSV、EPUB、HTML、图片、音频、Jupyter笔记本、ZIP、Outlook .msg、纯文本，以及微信文章、小红书、X/Twitter、YouTube、B站、小宇宙、抖音、TED、微博视频、喜马拉雅、Vimeo等URL，视频/音频可自动转文字稿（官方字幕优先，无字幕本地whisper转写）。当用户说"转成markdown"、"转换成md"、"转markdown"、"把这个文件/网页/链接转成文本"、"视频转文字"、"抓字幕"、"播客转文字"时使用此skill。
metadata:
  version: "2.0.0"
---

# to-markdown

将任何文件或URL转换为Markdown格式。

## 支持的格式

### 本地文件
PDF, DOCX, PPTX, XLSX, XLS, CSV, EPUB, HTML, JPG/PNG图片, **音视频(mp3/m4a/wav/mp4等，本地whisper转写)**, Jupyter Notebook(.ipynb), Outlook .msg, ZIP, 纯文本

### URL（文章类）
微信公众号 (mp.weixin.qq.com), 小红书 (xiaohongshu.com), X/Twitter (x.com), Wikipedia, RSS

### URL（视频/音频类 → 文字稿）
YouTube, B站 (bilibili.com/b23.tv), 小宇宙播客 (xiaoyuzhoufm.com), 抖音 (douyin.com), **微信视频号 (weixin.qq.com/sph，走元宝解析方案)**, TED, 微博视频, 喜马拉雅, 网易云音乐, Vimeo, SoundCloud, 以及**任何 yt-dlp 能读的音视频站**（不在此列表的也可以试）

## 工作流程

### 步骤1: 确定输入类型

- 本地文件路径 → 走 convert.py（常规转换）
- URL → 判断是**文章类**还是**音视频类**：
  - 文章类（微信/小红书/X文章/Wikipedia/RSS）→ 走 convert.py
  - 音视频类（视频页/播客页/任何 yt-dlp 站）→ 走 **video_transcript.py**（见下）
- 多个输入逐个处理

### 步骤2: 确定输出路径

默认输出目录: `D:/信息总结/raw/`
文件名从内容标题或源文件名生成，替换特殊字符为 `-`，用 `.md` 扩展名。

### 步骤3A: 常规转换（文章/文件）

```bash
PYTHONUTF8=1 python "${SKILL_DIR}/scripts/convert.py" --input "<输入路径或URL>" --output "<输出.md路径>"
```

可选参数：`--title "自定义标题"`、`--keep-data-uris`

### 步骤3B: 视频/音频转文字稿

```bash
PYTHONUTF8=1 python "${SKILL_DIR}/scripts/video_transcript.py" --url "<视频URL>" --output "<输出.md路径>"
```

常用参数：

| 参数 | 说明 |
|------|------|
| `--url` / `--file` | 视频页 URL / 本地音视频文件（二选一） |
| `--lang zh` | 语言（zh/en/auto，默认 zh） |
| `--model small` | whisper 模型：tiny快/small均衡/medium准（专有名词多时用 medium） |
| `--timestamps` | 输出带时间戳逐行稿（默认合并成自然段，适合wiki摄入） |
| `--cookies-from-browser chrome` | 借浏览器登录态（B站字幕/抖音/YouTube风控时） |
| `--force-asr` | 跳过字幕直接转写 |

**脚本自动走四层降级**（方法论来自 qiaomu-clipper）：

1. **L1 平台官方字幕 API** — B站 view→player/wbi/v2（免登录可拿部分视频）
2. **L2 yt-dlp 字幕** — 手动字幕 > 自动字幕；YouTube 等外网站自动走代理 7897，国内站直连
3. **L3 页面媒体直链** — 小宇宙节目页音频地址提取
4. **L4 音频下载 + faster-whisper 本地转写** — 兜底，永远可用；带幻觉行过滤、简体引导、音频临时文件用完即删

输出 frontmatter 含 `transcript_source`（official_subtitle/auto_subtitle/asr）、时长、语言等元信息。

### 步骤4: 处理结果

**成功**（输出 `SUCCESS:`）：报告输出路径、标题、转写来源（字幕/ASR）、字数。

**失败**：脚本会打印 `[ERROR]` + `[HINT]`，按 HINT 操作：

- `该站点要求登录态` → 三条路：① `--cookies-from-browser chrome`；② B站用下方的 CDP 提取方案；③ web-access 路线
- `yt-dlp 落后于网站改版` → `{python} -m pip install -U yt-dlp` 后重试
- `视频不可用` → 私有/会员/下架视频，换源
- Chrome 运行中导致 `Could not copy Chrome cookie database` → 这是 yt-dlp issue #7271（Chrome 锁库），改用下方 CDP 方案或 Edge
- 文章类 URL 失败 → 回退 web reader MCP：`mcp__web_reader__webReader(url=...)`

### 步骤5: 批量处理

多个输入逐个处理，各自生成 .md，存同一目录。

## B站字幕需要登录时的 CDP 提取方案

B站字幕（含 AI 字幕）只给登录用户（`need_login_subtitle`）。用户 Chrome 已登录 B站时，用 chrome-devtools MCP 在视频页里执行（页面内 fetch 自动带 cookie）：

```javascript
(async () => {
  const bvid = location.pathname.match(/BV[0-9A-Za-z]{10}/)[0];
  const view = await (await fetch(`https://api.bilibili.com/x/web-interface/view?bvid=${bvid}`, {credentials:'include'})).json();
  const {aid, cid, title} = view.data;
  const player = await (await fetch(`https://api.bilibili.com/x/player/wbi/v2?aid=${aid}&cid=${cid}`, {credentials:'include'})).json();
  const subs = player.data?.subtitle?.subtitles || [];
  if (!subs.length) return {error: '无字幕轨道', needLogin: player.data?.need_login_subtitle};
  const track = subs.find(s => s.lan.startsWith('zh')) || subs[0];
  const subUrl = track.subtitle_url.startsWith('//') ? 'https:' + track.subtitle_url : track.subtitle_url;
  const file = await (await fetch(subUrl)).json();
  return {title, lan: track.lan, body: file.body};
})()
```

把返回的 JSON 存成文件，再转：

```bash
python "${SKILL_DIR}/scripts/video_transcript.py" --from-bilibili-json "<字幕.json>" --url "<视频URL>" --title "<标题>" --output "<输出.md>"
```

## 视频号（微信 Channels）元宝解析方案（2026-11-12 全链路实测通过）

视频号分享链接（`weixin.qq.com/sph/<id>`）在普通浏览器只显示"请在微信打开"，没有公开接口，yt-dlp 也不支持。解法是**借腾讯元宝的解析 API**（方法来自 qiaomu-clipper 的 channels-study.ts）。视频号没有字幕系统，拿到视频后必然走 ASR 转写。

> 两个实测要点：① 元宝登录态判断用 `/api/userinfo/getuserconfig` 返回 200/401，**别信页面的会话列表**（登录态过期后仍显示历史会话）；② chrome-devtools MCP 的浏览器实例与用户日常 Chrome profile 隔离，**元宝登录态在用户 Chrome 里，必须走 web-access（CDP proxy）**，别用 chrome-devtools MCP。站点经验详见 web-access 的 `references/site-patterns/yuanbao.tencent.com.md`。

**第 1 步：元宝解析分享链接**。用 chrome-devtools 打开 `https://yuanbao.tencent.com/`（需登录；用户 Chrome 有元宝登录态就直接用），在页面里执行（把 sph 链接换成用户的）：

```javascript
(async () => {
  const r = await fetch('/api/weixin/get_parse_result', {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({type: 'video_channel_url', url: 'https://weixin.qq.com/sph/XXXX', scene: 1})
  });
  const data = await r.json();
  return {status: r.status, code: data?.code, playable_url: data?.data?.playable_url};
})()
```

- `status 401/403` = 元宝没登录，让用户在页面上登录后重试
- 成功则拿到 `playable_url`（`channels.weixin.qq.com/finder-preview/pages/feed?token=xx&eid=xx`，token 短时效，几分钟内用掉）

**第 2 步：播放页读视频直链**。新标签打开 playable_url，加载完（出现 video 元素）后在**该页面**执行：

```javascript
(async () => {
  const u = new URL(location.href);
  const r = await fetch('/finder-preview/api/feed/get_feed_info', {
    method: 'POST', credentials: 'omit',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({baseReq: {generalToken: u.searchParams.get('token')}, exportId: u.searchParams.get('eid')})
  });
  const data = await r.json();
  const feed = data?.data?.feedInfo;
  return {
    errCode: data?.errCode,
    title: feed?.description, author: data?.data?.authorInfo?.nickname,
    date: feed?.createtime ? new Date(feed.createtime * 1000).toISOString().slice(0, 10) : null,
    videoUrl: feed?.h264VideoInfo?.videoUrl,
    duration: document.querySelector('video')?.duration
  };
})()
```

**校验**：`videoUrl` 必须是 `https://finder.video.qq.com/<数字>/<数字>/stodownload` 开头，其他域名一律不下载。

**第 3 步：下载转写**：

```bash
curl -o "$TEMP/channels.mp4" "<videoUrl>"
python "${SKILL_DIR}/scripts/video_transcript.py" --file "$TEMP/channels.mp4" --title "<标题>" --output "<输出.md>"
```

（`--file` 模式的 frontmatter 里 source 是本地路径，建议手动把分享链接补进 frontmatter。）

> 备注：若用户在 Chrome 登录了视频号网页版（channels.weixin.qq.com/web/pages/feed/xxx），也可以直接在该播放页执行第 2 步同款 `get_feed_info` 或直接读 `document.querySelector('video').src`，跳过元宝。

## 通用兜底：yt-dlp 不认识的站点（小宇宙等）

任何脚本四层全失败的站点（yt-dlp 无 extractor、页面直链也提不到），用 chrome-devtools MCP 打开页面取媒体直链，再走 `--file` 转写：

```javascript
// 在视频/播客页执行，拿 <audio>/<video> 元素的 src（小宇宙网页版的 audio.src 即 m4a 直链）
(() => {
  const a = document.querySelector('audio[src], audio source[src]');
  const v = document.querySelector('video[src], video source[src]');
  return {audio: a?.src || null, video: v?.src || null, title: document.title};
})()
```

拿到直链后：`curl -o temp.m4a "<直链>"` 再 `--file temp.m4a` 转写。注意 blob: 开头的 src 是 MSE 流拿不到直链，此路不通就放弃该站。

> 小宇宙的 L3 页面直链提取（media.xyzcdn.net 正则）2026-11-12 未实测——遇到小宇宙链接优先试脚本，失败即走本节 CDP 方案。

## 图片处理（convert.py 路线）

脚本自动将图片下载到输出文件旁边的 `images/` 子目录，引用为相对路径 `images/filename.jpg`。转换后检查图片是否实际下载成功。

## 错误处理

| 场景 | 处理 |
|------|------|
| 文件不存在 | 报告错误，跳过 |
| URL无法访问 | 重试一次，文章类用 web reader 回退 |
| markitdown不支持 | URL 用 web reader，文件用直接读取 |
| 转换结果为空（正文<200字节） | 脚本退出码1，自动触发 web reader 回退 |
| 视频无字幕 | 自动降级 ASR 转写，无需干预 |
| 视频超4小时 | 拒绝处理（whisper 转写不现实） |
| 编码错误 | 脚本内置 UTF-8 保护 |

## 依赖

- Python 3.12（`C:\Users\44162\AppData\Local\Programs\Python\Python312\python.exe`）
- markitdown 包 (D:/归档项目/新技术试验/markitdown/)
- yt-dlp + faster-whisper（Python312 里已装）+ ffmpeg（D:\软件\ffmpeg\bin）
- web reader MCP 工具（文章类回退）

## 质量注意事项（视频转写）

- **字幕 > ASR**：有官方/AI字幕的视频优先用字幕（零错误）；ASR 是兜底
- **whisper small 的水平**：白话内容可用，专有名词（人名/品牌/术语）易错；重要资料加 `--model medium`（慢约3倍但明显更准）
- 中文已用 initial_prompt 引导简体输出（2026-11-12 修复，此前会出繁体）
- ASR 稿建议过一遍术语：重要名词人工核对或喂给 glm 纠错（参考 jianying-subtitle-fixer 的双层纠错思路）
- YouTube 遇 "Sign in to confirm you're not a bot" = 代理出口 IP 被 YouTube 风控，带 cookie 或提醒用户

## 示例

```
用户：把这个视频转成文字 https://www.bilibili.com/video/BVxxx
用户：这期播客讲什么，转成md  https://www.xiaoyuzhoufm.com/episode/xxx
用户：把这个 PDF 转成 markdown
用户：转 markdown  https://www.xiaohongshu.com/explore/xxx
用户：这个音频帮我转成文字  D:/录音.m4a
用户：抓一下这个 TED 演讲的文稿  https://www.ted.com/talks/xxx
```

## 维护记录

- 2026-11-12 v2.0：新增 video_transcript.py 四层降级管线（B站API/yt-dlp字幕/小宇宙直链/whisper ASR），方法论参考 qiaomu-clipper。已实测：B站无字幕视频全链路 ASR 兜底通过、简体修复、标题自动带入、错误分类（NEEDS_LOGIN/STALE_TOOL）生效
- YouTube 专用坑档案在 `youtube-transcript` skill（timedtext API 等4条死路），本 skill 的 yt-dlp 路线与其同源，遇到 YouTube 深度问题先查那边
