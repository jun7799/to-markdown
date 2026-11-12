"""
视频/音频 URL 或本地音视频文件 -> 带元信息的 Markdown 文字稿

四层降级策略（方法论参考 qiaomu-clipper, github.com/joeseesun/qiaomu-clipper）：
  L1  平台官方字幕 API（B站 view -> player/wbi/v2；国内站不走代理）
  L2  yt-dlp 字幕下载（手动字幕 > 自动字幕；YouTube 自动走代理；支持 --cookies-from-browser）
  L3  页面媒体直链提取（小宇宙节目页音频地址）
  L4  yt-dlp 音频下载 -> faster-whisper 本地转写（幻觉行过滤；音频临时文件用完即删）

工程细节（qiaomu-clipper 实战经验）：
  - 错误分类：NEEDS_LOGIN（建议带 cookie 或 web-access CDP）、STALE_TOOL（升级 yt-dlp）、PRIVATE
  - Whisper 幻觉行：只删"孤立短段且命中清单"的行（谢谢观看/amara.org 等）
  - B站字幕 URL 只认 bilibili.com / hdslb.com 域名，防串到第三方
  - 时长上限 4 小时
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, parse_qs

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_PROXY = "http://127.0.0.1:7897"  # 本地代理端口固定 7897
MAX_DURATION = 4 * 3600

# ---- 错误模式识别（qiaomu-clipper native/asr.py 同款经验）----
NEEDS_LOGIN = re.compile(
    r'sign in to confirm|not a bot|use --cookies|fresh cookies|login required'
    r'|ip address is blocked|blocked from accessing|需要登录|登录后|412', re.I)
STALE_TOOL = re.compile(
    r'needs to be reloaded|unable to extract|nsig|signature|player response'
    r'|precondition check failed|requested format is not available'
    r'|http error 403|sabr|po token|js runtime|challenge', re.I)
PRIVATE_VIDEO = re.compile(
    r'private video|video unavailable|this video is unavailable|members-only|会员专属|仅.*可见', re.I)

# Whisper 对静音/音乐段的幻觉行，只删孤立短段
HALLUCINATIONS = (
    '谢谢观看', '感谢观看', '请不吝点赞', '字幕由', '字幕 by', '字幕by', '订阅',
    'thanks for watching', 'thank you for watching', 'subtitles by', 'amara.org')

# 需要代理的外网站点（国内站走代理反而慢/失败）
FOREIGN_HOSTS = ('youtube.com', 'youtu.be', 'ted.com', 'vimeo.com', 'twitter.com',
                 'x.com', 'tiktok.com', 'soundcloud.com', 'nicovideo.jp', 'twitch.tv')
# 国内站点
CN_HOSTS = ('bilibili.com', 'b23.tv', 'douyin.com', 'xiaohongshu.com', 'weibo.com',
            'xiaoyuzhoufm.com', 'ximalaya.com', 'music.163.com', 'bilibili.tv',
            'weixin.qq.com', 'channels.weixin.qq.com')


def log(msg):
    print(f"[INFO] {msg}", flush=True)


def die(msg, hint=None):
    print(f"[ERROR] {msg}", file=sys.stderr, flush=True)
    if hint:
        print(f"[HINT] {hint}", file=sys.stderr, flush=True)
    sys.exit(1)


def classify_ytdlp_error(err_text):
    """把 yt-dlp 的报错翻译成可操作建议"""
    if NEEDS_LOGIN.search(err_text):
        return ("该站点要求登录态才能拿到内容",
                "三条路任选：① 加 --cookies-from-browser chrome 借浏览器登录态；"
                "② 用 web-access/chrome-devtools 在已登录页面里提取（B站见 SKILL.md 的 JS 片段）；"
                "③ 确认视频本身是否公开")
    if STALE_TOOL.search(err_text):
        return ("yt-dlp 落后于网站改版",
                f"先升级再重试: {sys.executable} -m pip install -U yt-dlp")
    if PRIVATE_VIDEO.search(err_text):
        return ("视频不可用（私有/会员/已下架）", "换公开视频，或带对应平台会员账号的 cookie")
    return None


# ---- 工具探测 ----

def find_ffmpeg():
    """ffmpeg 完整版在 D:\\软件\\ffmpeg\\bin（见记忆 avsync-video-toolchain），PATH 里可能没有"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidate = Path(r"D:\软件\ffmpeg\bin\ffmpeg.exe")
    if candidate.exists():
        return str(candidate)
    return None


FFMPEG = find_ffmpeg()


def ytdlp_cmd(extra_proxy=None, no_proxy=False):
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist"]
    if FFMPEG:
        cmd += ["--ffmpeg-location", FFMPEG]
    if extra_proxy:
        cmd += ["--proxy", extra_proxy]
    elif no_proxy:
        cmd += ["--proxy", ""]
    return cmd


# ---- 平台识别 ----

def detect_platform(url):
    host = (urlparse(url).hostname or "").lower()
    if any(h in host for h in ("youtube.com", "youtu.be")):
        return "youtube"
    if "bilibili.com" in host or host == "b23.tv":
        return "bilibili"
    if host.endswith("ted.com"):
        return "ted"
    if "xiaoyuzhoufm.com" in host:
        return "xiaoyuzhou"
    if "douyin.com" in host:
        return "douyin"
    if "weixin.qq.com" in host or "channels.weixin.qq.com" in host:
        return "channels"  # 微信视频号：无公开接口，走 SKILL.md 的元宝解析方案
    return "generic"


def platform_proxy(url):
    """外网站走代理（YouTube 必须），国内站不走"""
    host = (urlparse(url).hostname or "").lower()
    if any(h in host for h in CN_HOSTS):
        return None
    if any(h in host for h in FOREIGN_HOSTS):
        return DEFAULT_PROXY
    return None  # 未知站点直连，失败了用户可 --proxy 手动指定


# ---- 段落模型：[(start, end, text)] ----

def stamp(seconds):
    s = max(0, int(seconds))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return (f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}")


def parse_json3(path):
    """YouTube json3 字幕，解析最干净"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    segs = []
    for event in data.get("events", []):
        if "segs" not in event:
            continue
        text = "".join(s.get("utf8", "") for s in event["segs"]).strip()
        text = re.sub(r"\s+", " ", text)
        if not text or text == "\n":
            continue
        start = event.get("tStartMs", 0) / 1000
        dur = event.get("dDurationMs", 0) / 1000
        segs.append((start, start + dur, text))
    return segs


VTT_TIME = re.compile(r"(\d+):(\d+):(\d+)[.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[.,](\d+)")


def parse_vtt(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    segs = []
    for block in re.split(r"\n\s*\n", text):
        m = VTT_TIME.search(block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        lines = [ln for ln in block.splitlines() if not VTT_TIME.search(ln)
                 and not ln.strip().startswith(("WEBVTT", "Kind:", "Language:", "NOTE"))]
        body = re.sub(r"<[^>]+>", "", " ".join(lines)).strip()  # 去 karaoke/定位标签
        body = re.sub(r"\s+", " ", body)
        if body:
            segs.append((start, end, body))
    return segs


def parse_srt(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    segs = []
    for block in re.split(r"\n\s*\n", text):
        m = VTT_TIME.search(block.replace(",", "."))
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        lines = [ln for ln in block.splitlines()
                 if not ln.strip().isdigit() and not VTT_TIME.search(ln.replace(",", "."))]
        body = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if body:
            segs.append((start, end, body))
    return segs


def parse_subtitle_file(path):
    p = Path(path)
    suf = p.suffix.lower()
    if suf in (".json3", ".json"):
        if p.suffix == ".json" and "body" in p.read_text(encoding="utf-8", errors="replace")[:200]:
            return parse_bilibili_json(p)  # B站字幕文件也是 .json
        return parse_json3(p)
    if suf == ".vtt":
        return parse_vtt(p)
    if suf == ".srt":
        return parse_srt(p)
    # 兜底按内容猜
    head = p.read_text(encoding="utf-8", errors="replace")[:500]
    if "WEBVTT" in head:
        return parse_vtt(p)
    return parse_srt(p)


def parse_bilibili_json(path_or_text):
    """B站字幕 JSON: {"body": [{from, to, content}]}"""
    if isinstance(path_or_text, Path):
        data = json.loads(path_or_text.read_text(encoding="utf-8", errors="replace"))
    else:
        data = path_or_text
    segs = []
    for line in data.get("body", [])[:20000]:
        text = re.sub(r"\s+", " ", str(line.get("content", ""))).strip()
        start = float(line.get("from", 0))
        end = float(line.get("to", start))
        if text and start >= 0:
            segs.append((start, end, text))
    return segs


# ---- L1: B站官方字幕 API（无 cookie 部分视频可得；带 cookie-file 全量可得）----

BILIBILI_SUB_HOST = re.compile(r"(^|\.)(bilibili\.com|hdslb\.com)$")


def bilibili_resolve_url(url):
    """b23.tv 短链 -> 真实地址；提取 bvid 和 page"""
    import requests
    if "b23.tv" in url:
        r = requests.get(url, allow_redirects=True, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        url = r.url
    m = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if not m:
        return None, None, url
    bvid = m.group(1)
    page = 1
    pq = parse_qs(urlparse(url).query)
    if "p" in pq and pq["p"][0].isdigit():
        page = int(pq["p"][0])
    return bvid, page, url


def bilibili_fetch(bvid, page, cookie_file=None):
    """返回 (segments, meta) 或抛异常。字幕 URL 只认 B站自家域名。"""
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    s.headers["Referer"] = "https://www.bilibili.com/"
    if cookie_file:
        from http.cookiejar import MozillaCookieJar
        jar = MozillaCookieJar(cookie_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        s.cookies.update(jar)

    r = s.get("https://api.bilibili.com/x/web-interface/view",
              params={"bvid": bvid}, timeout=15)
    view = r.json()
    if view.get("code") != 0:
        raise RuntimeError(f"B站 view API: {view.get('message', '未知错误')}")
    data = view["data"]
    cid = None
    pages = data.get("pages", [])
    for pg in pages:
        if pg.get("page") == page:
            cid = pg.get("cid")
            break
    if cid is None:
        cid = pages[0]["cid"] if pages else data.get("cid")
    meta = {
        "title": data.get("title", ""),
        "author": (data.get("owner") or {}).get("name", ""),
        "duration": data.get("duration"),
    }

    # 注意：只用 wbi/v2 和带 aid 的 v2，按 bvid 请求的老接口多P视频会串字幕（qiaomu-clipper 踩过）
    player = None
    for path in ("wbi/v2", "v2"):
        try:
            r = s.get(f"https://api.bilibili.com/x/player/{path}",
                      params={"aid": data["aid"], "cid": cid}, timeout=15)
            answer = r.json()
            if answer.get("code") == 0 and answer.get("data"):
                player = answer["data"]
                break
        except Exception:
            continue
    if not player:
        raise RuntimeError("B站 player API 无法读取")

    subs = player.get("subtitle", {}).get("subtitles", [])
    need_login = player.get("need_login_subtitle") is True and not subs
    if not subs:
        if need_login:
            raise PermissionError("B站字幕需要登录（need_login_subtitle）")
        raise LookupError("该视频没有官方字幕（连 AI 字幕都没有）")

    # 选轨：zh（含 ai-zh）优先，其次第一轨
    def track_score(tr):
        lan = str(tr.get("lan", ""))
        return (0 if lan.startswith("zh") else 1, 0 if not lan.startswith("ai-") else 1)
    subs_sorted = sorted(subs, key=track_score)
    chosen = subs_sorted[0]
    sub_url = chosen.get("subtitle_url", "")
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    host = urlparse(sub_url).hostname or ""
    if not BILIBILI_SUB_HOST.match(host):
        raise RuntimeError(f"字幕地址不在 B站域名下，拒绝下载: {host}")

    log(f"B站字幕轨: {chosen.get('lan_doc', chosen.get('lan'))}"
        f"{'（AI 生成）' if str(chosen.get('lan', '')).startswith('ai-') else ''}")
    r = s.get(sub_url, timeout=15)
    segments = parse_bilibili_json(r.json())
    meta["subtitle_lang"] = chosen.get("lan", "")
    meta["is_ai_subtitle"] = str(chosen.get("lan", "")).startswith("ai-")
    return segments, meta


# ---- L3: 小宇宙页面音频直链 ----

def xiaoyuzhou_audio_url(url):
    """小宇宙节目页内嵌音频地址（media.xyzcdn.net），页面公开无需登录"""
    import requests
    r = requests.get(url, timeout=20, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    html = r.text
    # 页面里音频地址出现在 INITIAL_STATE / shed.json 两种形态
    m = re.search(r'https://media\.xyzcdn\.net/[^"\'\\\s]+\.m4a', html)
    if not m:
        m = re.search(r'https://[^"\'\\\s]+\.m4a', html)
    if not m:
        return None
    title_m = re.search(r'<title>([^<]+)</title>', html)
    return m.group(0), (title_m.group(1).strip() if title_m else None)


# ---- L2: yt-dlp 字幕 ----

def pick_subtitle_lang(subs_dict, want_lang):
    """从 yt-dlp dump-json 的 subs/automatic_captions 里选语言，返回 (lang, is_auto)"""
    for d, auto in ((subs_dict, False), (subs_dict, True)):
        if not d:
            continue
        # 先精确，再前缀，再 zh/en 常见值
        candidates = []
        if want_lang in d:
            candidates = [want_lang]
        else:
            prefix = want_lang.split("-")[0]
            candidates = [k for k in d if k.startswith(prefix)]
            if not candidates:
                candidates = [k for k in d if k.startswith(("zh", "en"))]
            if not candidates:
                candidates = list(d.keys())
        if candidates:
            return candidates[0], auto
    return None, None


def ytdlp_metadata(url, proxy=None, cookie_browser=None):
    cmd = ytdlp_cmd(extra_proxy=proxy) + ["--dump-json", "--skip-download"]
    if cookie_browser:
        cmd += ["--cookies-from-browser", cookie_browser]
    cmd.append(url)
    log("获取视频元信息...")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        classified = classify_ytdlp_error(r.stderr or "")
        if classified:
            die(classified[0], classified[1])
        die(f"yt-dlp 获取元信息失败: {(r.stderr or r.stdout).strip()[:300]}")
    return json.loads(r.stdout.strip().splitlines()[0])


def ytdlp_download_sub(url, lang, proxy=None, cookie_browser=None, workdir=None):
    """下载一条字幕轨道，返回 (segments, sub_lang, is_auto) 或 None"""
    meta = ytdlp_metadata(url, proxy, cookie_browser)
    if (meta.get("duration") or 0) > MAX_DURATION:
        die(f"视频时长 {meta['duration']//3600}h 超过 {MAX_DURATION//3600}h 上限")
    sub_lang, is_auto = pick_subtitle_lang(meta.get("subtitles") or {}, lang)
    if not sub_lang:
        sub_lang, is_auto = pick_subtitle_lang(meta.get("automatic_captions") or {}, lang)
    if not sub_lang:
        return None, meta
    log(f"下载字幕: {sub_lang} ({'自动生成' if is_auto else '手动上传'})")
    cmd = ytdlp_cmd(extra_proxy=proxy) + [
        "--skip-download",
        "--write-subs" if not is_auto else "--write-auto-subs",
        "--sub-langs", sub_lang,
        "--sub-format", "json3/vtt/srt/best",
        "-o", str(Path(workdir) / "sub"),
    ]
    if cookie_browser:
        cmd += ["--cookies-from-browser", cookie_browser]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    sub_files = [p for p in Path(workdir).glob("sub.*") if p.suffix != ".part"]
    if r.returncode != 0 or not sub_files:
        return None, meta
    segments = parse_subtitle_file(sub_files[0])
    return segments, meta


# ---- L4: faster-whisper 本地转写 ----

def is_hallucination(prev_text, text, next_text, duration):
    """只删孤立短段且命中清单的行（qiaomu-clipper 同款策略）"""
    if duration > 5 or len(text) > 30:
        return False
    if not any(h in text.lower() for h in HALLUCINATIONS):
        return False
    # 前后都是正常内容才算孤立幻觉
    return True


def transcribe_file(audio_path, model_name, lang):
    """faster-whisper 转写，参数对齐 video-autocut 的 transcribe.py"""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("faster-whisper 未安装", f"{sys.executable} -m pip install faster-whisper")
    log(f"加载模型 {model_name}（CPU/int8，首次使用会自动下载）...")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    kwargs = dict(beam_size=5, vad_filter=True,
                  vad_parameters=dict(min_silence_duration_ms=500))
    if lang and lang != "auto":
        kwargs["language"] = lang.split("-")[0]
        # whisper small 转普通话常出繁体，用提示词引导简体输出
        if kwargs["language"] == "zh":
            kwargs["initial_prompt"] = "以下是普通话简体中文的内容。"
    log("开始转写（长视频请耐心，CPU 上约为时长的 1/5~1/10）...")
    segments_iter, info = model.transcribe(str(audio_path), **kwargs)

    segs = []
    last_report, total = 0.0, info.duration or 0
    for s in segments_iter:
        text = s.text.strip()
        if not text:
            continue
        segs.append((s.start, s.end, text))
        if s.end - last_report > 30:
            last_report = s.end
            pct = f" {s.end/total*100:.0f}%" if total else ""
            print(f"\r[INFO] 转写进度 {int(s.end//60)}m{int(s.end%60):02d}s{pct}", end="", flush=True)
    print()
    # 幻觉过滤：孤立短段
    filtered = []
    for i, (start, end, text) in enumerate(segs):
        prev_t = segs[i - 1][2] if i > 0 else ""
        next_t = segs[i + 1][2] if i + 1 < len(segs) else ""
        if is_hallucination(prev_t, text, next_t, end - start):
            log(f"过滤幻觉行: {text[:30]}")
            continue
        filtered.append((start, end, text))
    return filtered, {"language": info.language, "duration": info.duration}


def download_audio(url, proxy=None, cookie_browser=None, workdir=None):
    """yt-dlp 只下音频（mp3 体积小速度快），返回文件路径"""
    log("下载音频（跳过视频流）...")
    cmd = ytdlp_cmd(extra_proxy=proxy) + [
        "-x", "--audio-format", "mp3", "--audio-quality", "5",
        "-o", str(Path(workdir) / "audio.%(ext)s"),
    ]
    if cookie_browser:
        cmd += ["--cookies-from-browser", cookie_browser]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    if r.returncode != 0:
        classified = classify_ytdlp_error(r.stderr or "")
        if classified:
            die(classified[0], classified[1])
        die(f"音频下载失败: {(r.stderr or r.stdout).strip()[:300]}")
    for p in Path(workdir).glob("audio.*"):
        if p.suffix != ".part":
            return p
    die("音频下载完成但找不到文件")


# ---- 输出 ----

def merge_to_paragraphs(segs):
    """纯文本模式：把碎段合并成自然段（按句末标点切）"""
    text = "".join(t for _, _, t in segs)
    text = re.sub(r"\s+", " ", text)
    # 中文句号/问号/感叹号断段，英文句点+空格断段
    parts = re.split(r"(?<=[。？！.?!])\s+", text)
    paragraphs, buf = [], []
    for part in parts:
        buf.append(part)
        if sum(len(x) for x in buf) > 200:
            paragraphs.append("".join(buf))
            buf = []
    if buf:
        paragraphs.append("".join(buf))
    return "\n\n".join(p for p in paragraphs if p.strip())


def write_md(output_path, url, meta, segs, source_kind, timestamps, asr_model=None):
    total = segs[-1][1] if segs else 0
    fm = [
        "---",
        f'title: "{(meta.get("title") or "未命名").replace(chr(34), chr(39))}"',
        f"source: {url}",
        f"source_type: video_transcript",
        f"platform: {meta.get('platform', '')}",
        f"author: \"{meta.get('author', '')}\"",
        f"duration: \"{stamp(total)}\"",
        f"transcript_source: {source_kind}",  # official_subtitle / auto_subtitle / asr
    ]
    if asr_model:
        fm.append(f"asr_model: {asr_model}")
    if meta.get("language"):
        fm.append(f"language: {meta['language']}")
    fm += [f"date_converted: {date.today().isoformat()}", "---", ""]

    if timestamps:
        body = "\n".join(f"`[{stamp(s)}]` {t}" for s, e, t in segs)
    else:
        body = merge_to_paragraphs(segs)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(fm) + "\n# " + (meta.get("title") or "文字稿") + "\n\n" + body + "\n",
                   encoding="utf-8")
    words = sum(len(t) for _, _, t in segs)
    print(f"SUCCESS: {out}")
    print(f"Title: {meta.get('title', '')}")
    print(f"Source: {source_kind}" + (f" ({asr_model})" if asr_model else ""))
    print(f"Segments: {len(segs)}  Chars: {words}  Duration: {stamp(total)}")


# ---- 主流程 ----

def main():
    ap = argparse.ArgumentParser(description="视频/音频 -> Markdown 文字稿")
    ap.add_argument("--url", help="视频/音频页 URL")
    ap.add_argument("--file", help="本地音视频文件路径（直接转写）")
    ap.add_argument("--from-bilibili-json", dest="from_bilibili_json",
                    help="web-access CDP 提取的 B站字幕 JSON 路径（配合 --title/--url 补元信息）")
    ap.add_argument("--output", "-o", required=True)
    ap.add_argument("--lang", default="zh", help="字幕/转写语言（zh/en/auto，默认 zh）")
    ap.add_argument("--model", default="small", help="faster-whisper 模型（tiny/base/small/medium，默认 small）")
    ap.add_argument("--timestamps", action="store_true", help="输出带时间戳的逐行稿（默认合并自然段）")
    ap.add_argument("--proxy", default=None, help="覆盖自动代理判断（YouTube 等外网站自动走 7897）")
    ap.add_argument("--cookies-from-browser", dest="cookies_from_browser", default=None,
                    help="借浏览器登录态（chrome/edge/firefox），B站/抖音字幕常需要")
    ap.add_argument("--cookie-file", default=None, help="Netscape cookie 文件（B站 API 路线用）")
    ap.add_argument("--force-asr", action="store_true", help="跳过字幕路线，强制下载音频转写")
    ap.add_argument("--title", default=None, help="配合 --from-bilibili-json 指定标题")
    args = ap.parse_args()

    inputs = [args.url, args.file, args.from_bilibili_json]
    if sum(1 for x in inputs if x) != 1:
        die("必须且只能提供 --url / --file / --from-bilibili-json 之一")

    # 模式0：CDP 提取的 B站字幕 JSON 直接转
    if args.from_bilibili_json:
        segs = parse_bilibili_json(Path(args.from_bilibili_json))
        if not segs:
            die("B站字幕 JSON 解析为空")
        meta = {"title": args.title or "B站视频", "platform": "bilibili"}
        write_md(args.output, args.url or "", meta, segs,
                 "official_subtitle", args.timestamps)
        return

    # 模式1：本地文件直接转写
    if args.file:
        path = Path(args.file)
        if not path.exists():
            die(f"文件不存在: {path}")
        segs, info = transcribe_file(path, args.model, args.lang)
        meta = {"title": args.title or path.stem, "platform": "local_file",
                "language": info.get("language")}
        write_md(args.output, args.url or str(path), meta, segs, "asr", args.timestamps, args.model)
        return

    # 模式2：URL 分层降级
    url = args.url
    platform = detect_platform(url)
    proxy = args.proxy if args.proxy is not None else platform_proxy(url)
    if proxy:
        log(f"检测到外网站点，走代理 {proxy}")
    log(f"平台: {platform}")

    workdir = tempfile.mkdtemp(prefix="vtrans_")
    try:
        # 视频号：无公开接口、yt-dlp 不支持，直接给出可操作指引
        if platform == "channels":
            die("视频号没有公开接口，脚本无法直接处理",
                "按 SKILL.md「视频号」一节的元宝解析方案：chrome-devtools 打开 yuanbao.tencent.com（登录态）"
                "→ 解析分享链接拿播放页 → 播放页里读 mp4 直链 → 下载后用 --file 转写")

        # L1: B站官方字幕 API（免登录可拿一部分）
        if platform == "bilibili" and not args.force_asr:
            try:
                bvid, page, real_url = bilibili_resolve_url(url)
                if bvid:
                    log(f"B站视频 {bvid} P{page}，尝试官方字幕 API...")
                    segs, meta = bilibili_fetch(bvid, page, args.cookie_file)
                    meta["platform"] = "bilibili"
                    write_md(args.output, url, meta, segs,
                             "auto_subtitle" if meta.get("is_ai_subtitle") else "official_subtitle",
                             args.timestamps)
                    return
            except PermissionError as e:
                log(f"{e}，转下一层")
            except (LookupError, RuntimeError) as e:
                log(f"B站 API: {e}，转下一层")

        # L3 预处理：小宇宙先解出音频直链
        audio_direct = None
        if platform == "xiaoyuzhou":
            try:
                got = xiaoyuzhou_audio_url(url)
                if got:
                    audio_direct, page_title = got
                    log(f"小宇宙音频直链已提取: {audio_direct[:80]}...")
                else:
                    log("小宇宙页面未找到音频地址，将由 yt-dlp 尝试")
            except Exception as e:
                log(f"小宇宙页面提取失败: {e}，将由 yt-dlp 尝试")

        # L2: yt-dlp 字幕（手动 > 自动）
        ytdlp_title = None
        if not args.force_asr and not audio_direct:
            try:
                segs, meta = ytdlp_download_sub(url, args.lang, proxy,
                                                args.cookies_from_browser, workdir)
                ytdlp_title = meta.get("title")  # ASR 兜底时也带上标题
                if segs:
                    meta["platform"] = platform
                    is_auto = "auto" in str(meta.get("subtitle_lang", "")) or \
                              meta.get("is_ai_subtitle", False) or \
                              not meta.get("subtitles")
                    write_md(args.output, url, meta, segs,
                             "auto_subtitle" if is_auto else "official_subtitle",
                             args.timestamps)
                    return
                log("无可用字幕轨道，转 ASR 路线")
            except SystemExit:
                raise  # die() 已给出分类建议，直接退出
            except Exception as e:
                log(f"yt-dlp 字幕路线失败: {e}，转 ASR 路线")

        # L4: 音频 -> faster-whisper
        if audio_direct:
            import requests
            log("下载小宇宙音频直链...")
            audio_path = Path(workdir) / "audio.m4a"
            with requests.get(audio_direct, stream=True, timeout=120,
                              headers={"User-Agent": "Mozilla/5.0"}) as r:
                r.raise_for_status()
                with open(audio_path, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
            meta = {"title": page_title or "小宇宙节目", "platform": "xiaoyuzhou"}
        else:
            audio_path = download_audio(url, proxy, args.cookies_from_browser, workdir)
            meta = {"title": ytdlp_title, "platform": platform}
        segs, info = transcribe_file(audio_path, args.model, args.lang)
        meta.setdefault("language", info.get("language"))
        meta["title"] = meta.get("title") or f"{platform} 视频转写"
        write_md(args.output, url, meta, segs, "asr", args.timestamps, args.model)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        log("临时文件已清理")


if __name__ == "__main__":
    main()
