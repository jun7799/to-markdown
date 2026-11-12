"""
markitdown 包装脚本

解决问题：
1. 图片路径错位 - 社交平台转换器用 os.getcwd()/images/ 下载图片，
   本脚本通过 chdir 到输出目录确保图片在 markdown 文件旁边
2. Windows 编码 - 确保 UTF-8 输出
3. 元数据头 - 自动生成标题、来源、日期信息
4. EPUB 图片提取 - 从 EPUB zip 中提取图片并修正 md 中的路径
"""

import argparse
import os
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# markitdown 本地开发版本路径
MARKITDOWN_SRC = r"D:\归档项目\新技术试验\markitdown\packages\markitdown\src"


def main():
    parser = argparse.ArgumentParser(description="Convert files/URLs to Markdown")
    parser.add_argument("--input", "-i", required=True, help="Input file path or URL")
    parser.add_argument("--output", "-o", required=True, help="Output .md file path")
    parser.add_argument("--title", "-t", default=None, help="Custom title")
    parser.add_argument("--keep-data-uris", action="store_true",
                        help="Keep base64 data URIs in output")
    args = parser.parse_args()

    source = args.input
    output_path = Path(args.output)
    output_dir = output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    original_cwd = os.getcwd()

    try:
        # 切换到输出目录，让图片下载到正确位置
        os.chdir(output_dir)

        sys.path.insert(0, MARKITDOWN_SRC)
        from markitdown import MarkItDown

        md = MarkItDown()
        kwargs = {}
        # 始终保留 base64 数据，由后处理步骤提取为本地图片
        kwargs["keep_data_uris"] = True

        # 微信转换器支持 wechat_image_dir kwarg
        image_dir = str(output_dir / "images")
        kwargs["wechat_image_dir"] = image_dir

        # EPUB 转换器支持 epub_image_dir kwarg
        if not source.startswith("http") and source.lower().endswith(".epub"):
            safe_name = re.sub(r'[\\/:*?"<>|\s]+', '-', output_path.stem)[:40].strip('-')
            epub_img_dir = str(output_dir / "images" / safe_name)
            kwargs["epub_image_dir"] = epub_img_dir

        result = md.convert(source, **kwargs)
        markdown = result.markdown
        title = result.title or ""

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        os.chdir(original_cwd)

    if not title:
        if source.startswith("http"):
            title = source.split("/")[-1][:50] or "Untitled"
        else:
            title = Path(source).stem

    # 元数据头
    now = datetime.now().strftime("%Y-%m-%d")
    source_type = "URL" if source.startswith("http") else "file"
    header = (
        f"---\n"
        f"title: \"{title}\"\n"
        f"source: {source}\n"
        f"source_type: {source_type}\n"
        f"date_converted: {now}\n"
        f"---\n\n"
    )

    # 提取 base64 data URI 为本地图片文件
    img_dir = output_dir / "images"
    markdown, extracted_count = _extract_data_uri_images(markdown, img_dir)

    output_path.write_text(header + markdown, encoding="utf-8")

    # EPUB 图片后处理：从 zip 中提取图片并修正 md 路径
    if not source.startswith("http") and source.lower().endswith(".epub"):
        _extract_epub_images(source, output_path, output_dir)

    # 检测转换结果是否为空（正文太短说明转换失败）
    content_len = len(markdown.strip())
    if source.startswith("http") and content_len < 200:
        print(f"ERROR: Converted content is too short ({content_len} bytes), likely empty", file=sys.stderr)
        sys.exit(1)

    # 输出结果信息
    images_dir = output_dir / "images"
    image_count = 0
    if images_dir.exists():
        image_count = sum(1 for d in images_dir.iterdir() if d.is_dir()
                          for f in d.iterdir() if f.is_file())
        image_count += sum(1 for f in images_dir.iterdir() if f.is_file())

    print(f"SUCCESS: {output_path}")
    print(f"Title: {title}")
    print(f"Images: {image_count}")
    if image_count > 0:
        print(f"ImagesDir: {images_dir}")


def _extract_data_uri_images(markdown: str, img_dir: Path):
    """将 markdown 中的 base64 data URI 提取为本地图片文件，替换为相对路径引用。"""
    import base64

    img_dir.mkdir(parents=True, exist_ok=True)

    pattern = re.compile(
        r'!\[([^\]]*)\]\(data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/= \t\n\r]+)\)'
    )

    counter = 0
    def _replace(m):
        nonlocal counter
        counter += 1
        alt = m.group(1)
        ext = m.group(2)
        if ext == "jpeg":
            ext = "jpg"
        b64_data = re.sub(r'\s+', '', m.group(3))
        try:
            img_bytes = base64.b64decode(b64_data)
        except Exception:
            return m.group(0)
        fname = f"img_{counter}.{ext}"
        (img_dir / fname).write_bytes(img_bytes)
        return f"![{alt}](images/{fname})"

    markdown = pattern.sub(_replace, markdown)
    return markdown, counter


def _extract_epub_images(epub_path: str, md_path: Path, output_dir: Path):
    """从 EPUB zip 中提取图片，并修正 md 文件中的图片路径。"""
    img_extensions = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"}

    # 用文件名（去特殊字符）作为图片子目录名
    safe_name = re.sub(r'[\\/:*?"<>|\s]+', '-', md_path.stem)[:40].strip('-')
    images_subdir = output_dir / "images" / safe_name
    images_subdir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(epub_path, "r") as z:
        img_files = [
            f for f in z.namelist()
            if Path(f).suffix.lower() in img_extensions
            and not Path(f).name.startswith(".")
        ]
        if not img_files:
            return

        # 提取图片，并记录文件名集合用于路径修正
        extracted_names = set()
        for img in img_files:
            fname = Path(img).name
            (images_subdir / fname).write_bytes(z.read(img))
            extracted_names.add(fname)

    # 修正 md 中的图片路径：基于"文件名匹配"重写引用
    # markitdown 会原样保留 HTML 里的相对引用（如 ../Images/xxx.jpg），而不同
    # EPUB 的图片目录大小写不一（Images/ 或 images/），用正则硬匹配会漏掉大写。
    # 这里取每个引用的 basename，命中提取集合就重写为 images/子目录/文件名，
    # 从而无视大小写和 ../ 前缀；base64 提取的 images/img_N.ext 不在集合里，不受影响
    content = md_path.read_text(encoding="utf-8")
    new_prefix = f"images/{safe_name}/"

    def _fix_ref(m):
        alt, path = m.group(1), m.group(2)
        fname = Path(path).name
        if fname in extracted_names:
            return f"![{alt}]({new_prefix}{fname})"
        return m.group(0)

    content = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', _fix_ref, content)

    md_path.write_text(content, encoding="utf-8")
    print(f"[EPUB] Extracted {len(img_files)} images to {images_subdir}")


if __name__ == "__main__":
    main()
