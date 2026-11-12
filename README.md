# to-markdown

把任何东西转成 Markdown——文件、网页、视频、播客。Claude Code skill，两条管线一次装全。

## 功能

### 管线一：文件 / 文章 URL → Markdown

基于 [markitdown](https://github.com/microsoft/markitdown) 封装，解决三个实战问题：

- **图片本地化** — 社交平台文章的图片自动下载到 `images/` 子目录，md 内改相对引用，外链失效不丢图
- **Windows 编码保护** — UTF-8 输出，中文标题不乱码
- **自动元数据头** — frontmatter 含标题 / 来源 / 转换日期

支持：PDF、DOCX、PPTX、XLSX、CSV、EPUB、HTML、图片、Jupyter Notebook、Outlook .msg、ZIP、纯文本，以及微信公众号、小红书、X/Twitter、Wikipedia、RSS 等 URL。

### 管线二：视频 / 音频 → 文字稿

`scripts/video_transcript.py`，四层降级（方法论参考 [qiaomu-clipper](https://github.com/joeseesun/qiaomu-clipper) 乔木剪藏）：

| 层 | 方法 | 适用 |
|----|------|------|
| L1 | 平台官方字幕 API | B站（view → player/wbi/v2） |
| L2 | yt-dlp 字幕下载（手动 > 自动） | YouTube / TED / 等所有 yt-dlp 支持站 |
| L3 | 页面媒体直链提取 | 小宇宙播客 |
| L4 | 音频下载 + faster-whisper 本地转写 | 兜底，永远可用 |

工程细节：

- **字幕 > ASR**：有官方字幕零错误，ASR 只做兜底
- **幻觉行过滤**：whisper 对静音/音乐段编造的"谢谢观看 / amara.org"等孤立短行自动删除
- **简体引导**：initial_prompt 解决 whisper 中文转写出繁体的经典问题
- **错误分类**：`NEEDS_LOGIN`（该带 cookie 了）/ `STALE_TOOL`（该升级 yt-dlp 了）/ 私有视频，报错自带可操作建议
- **临时文件用完即删**，音频不上传任何云端
- **微信视频号支持**：借腾讯元宝登录态解析分享链接 → 官方播放页拿 mp4 直链 → 转写（需配合浏览器 CDP，流程见 SKILL.md）

## 安装

```bash
git clone https://github.com/jun7799/to-markdown.git ~/.claude/skills/to-markdown
```

## 依赖

```
pip install markitdown yt-dlp faster-whisper
```

- Python 3.10+（脚本对 Windows UTF-8 有内置处理）
- ffmpeg（yt-dlp 下载音频、格式转换需要；Windows 下记得让脚本找得到：PATH 或改脚本里的探测路径）

> SKILL.md 里有少量本机环境路径（Python/ffmpeg 完整路径），clone 后按你的环境调整。

## 使用

对 Claude Code 说：

```
把这个视频转成文字 https://www.bilibili.com/video/BVxxxx
这期播客讲了什么，转成md  https://www.xiaoyuzhoufm.com/episode/xxx
把这个 PDF 转成 markdown
```

或直接跑脚本：

```bash
# 视频 URL → 文字稿（自动四层降级）
python scripts/video_transcript.py --url "https://..." --output out.md

# 本地音频/视频文件转写
python scripts/video_transcript.py --file recording.m4a --output out.md

# 带时间戳的逐行稿（默认合并自然段）
python scripts/video_transcript.py --url "..." --output out.md --timestamps

# 专有名词多的内容换 medium 模型（更准，慢约3倍）
python scripts/video_transcript.py --url "..." --output out.md --model medium

# 文章/文件 → markdown
python scripts/convert.py --input "文件或URL" --output out.md
```

## 质量预期

- whisper `small`（默认）：白话内容可用，专有名词（人名/品牌/术语）易错，重要资料用 `--model medium`
- 字幕路线（L1/L2）零错误，能走字幕的都会自动先走字幕
- ASR 稿建议对重要名词人工过一遍

## 致谢

- 视频转写的方法论（四层降级、幻觉过滤、错误分类、B站 API 细节、视频号元宝解析）大量参考 [joeseesun/qiaomu-clipper](https://github.com/joeseesun/qiaomu-clipper)（乔木剪藏），推荐了解这个项目
- YouTube 字幕路线的经验来自长期实战踩坑

## License

MIT
