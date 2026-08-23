#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Zhihu A-Share Data Fetcher and Markdown Generator
- 凭证、输出文件与日志路径始终绑定在【当前工作目录 (Current Working Directory, Cwd)】
- 凭证路径：./config/cookies.json
- 输出目录：./output/{YYYYMMDD}/
- 日志目录：./logs/fetch.log
- 支持基于 Cookie (z_c0) 的 API 认证登录与在线状态校验 (/api/v4/me)
"""

import os
import re
import sys
import time
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import quote, unquote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup
import markdownify
import markdown
from PIL import Image

# 修复 Windows 终端输出 UTF-8 编码问题
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ===== 路径与动态 CWD 配置 =====
# 动态使用当前命令执行的工作目录 (CWD)，确保 Skill 安装在任意位置均能在用户当前项目中生成文件
WORK_DIR = Path.cwd().resolve()

# 默认仅向控制台 (sys.stdout) 输出 Log，便于 Agent 实时捕获输出，避免在本地落盘无用的日志文件
logger = logging.getLogger("zhihu_fetcher")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _console_handler = logging.StreamHandler(sys.stdout)
    _console_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_console_handler)


def setup_file_logging(log_file_path: str = "logs/fetch.log"):
    """仅在用户显式开启参数时启用文件日志 Handler"""
    log_path = Path(log_file_path) if Path(log_file_path).is_absolute() else WORK_DIR / log_file_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)
    logger.info(f"已启用文件日志保存: {log_path}")


REQUIRED_COOKIES = {"z_c0"}
ZHIHU_BASE_URL = "https://www.zhihu.com"
ZHIHU_API_V4 = "https://www.zhihu.com/api/v4"
CHROME_VERSION = "145"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{CHROME_VERSION}.0.0.0 Safari/537.36"
)


# ===== 基础工具函数 =====
def get_browser_headers() -> dict[str, str]:
    """生成伪装成真实 Chrome 浏览器的请求头"""
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"{ZHIHU_BASE_URL}/",
        "sec-ch-ua": (
            f'"Not:A-Brand";v="99", '
            f'"Google Chrome";v="{CHROME_VERSION}", '
            f'"Chromium";v="{CHROME_VERSION}"'
        ),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


def parse_cookie_string(cookie_str: str) -> dict[str, str]:
    """解析 Cookie 文本字符串为字典"""
    result = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def save_last_error(msg: str, base_output_dir: str = "output"):
    """记录极简错误原因到 output/last_error.txt 供 Bark 推送准确故障"""
    try:
        out_base_path = Path(base_output_dir) if Path(base_output_dir).is_absolute() else WORK_DIR / base_output_dir
        out_base_path.mkdir(parents=True, exist_ok=True)
        err_file = out_base_path / "last_error.txt"
        with open(err_file, "w", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        pass


def parse_date(date_str=None):
    """
    解析 YYYY-MM-DD 或默认今天日期。
    返回:
    - dt: 真正的运行目标日期 datetime 对象 (决定落盘目录与页面显示日期)
    - date_iso: YYYY-MM-DD
    - date_compact: YYYYMMDD (用于落盘目录，如 20260824)
    - date_formatted: YYYY年MM月DD日
    - search_dt: 截至当前运行时间已收盘的上一个完整交易日（用于生成知乎搜索关键词）
    """
    tz_beijing = timezone(timedelta(hours=8))
    if date_str:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz_beijing)
        except ValueError:
            logger.warning(f"日期格式无效 '{date_str}'，默认使用今天日期。")
            dt = datetime.now(tz_beijing)
    else:
        dt = datetime.now(tz_beijing)

    date_compact = dt.strftime("%Y%m%d")
    date_formatted = f"{dt.year}年{dt.month:02d}月{dt.day:02d}日"
    date_iso = dt.strftime("%Y-%m-%d")

    # 计算上一个完整已收盘交易日（A股收盘时间为北京时间 15:00）
    search_dt = dt
    # 如果今天属于交易日（周一至周五），但当前北京时间还没到 15:00:00，说明今日未收盘，需回溯至前一交易日
    if search_dt.weekday() < 5 and search_dt.hour < 15:
        search_dt -= timedelta(days=1)
        logger.info(f"当前时间为交易日盘中/盘前 ({dt.strftime('%H:%M:%S')} < 15:00)，尚未收盘，检索关键词自动向前回溯 1 天。")

    # 循环向前跳过周六(5)和周日(6)
    while search_dt.weekday() >= 5:
        search_dt -= timedelta(days=1)

    if search_dt.strftime("%Y%m%d") != date_compact:
        logger.info(f"检索关键词定位至上一个完整开盘日 ({search_dt.strftime('%Y-%m-%d')})，落盘目录保持为独立日期 {date_compact}")

    return dt, date_iso, date_compact, date_formatted, search_dt


def upgrade_image_url(img_url: str) -> str:
    """将知乎缩略图/低清图 URL 提升为原图 HD 高清 URL"""
    if not img_url or img_url.startswith("data:"):
        return img_url
    # 自动替换缩略图后缀（如 _b.jpg, _720w.jpg, _hd.jpg）为原图后缀 _r.jpg
    upgraded = re.sub(r'_(?:b|720w|hd|50w|xs|s)\.(jpg|jpeg|png|webp)', r'_r.\1', img_url)
    return upgraded


# ===== 知乎 API 客户端类 =====
class ZhihuApiClient:
    """知乎 API 客户端（基于 Cookie 鉴权与 Session 复用）"""

    def __init__(self, cookies: dict[str, str] | str | None = None):
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=15, pool_maxsize=30)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(get_browser_headers())
        self.cookies: dict[str, str] = {}

        if cookies:
            if isinstance(cookies, str):
                cookies = parse_cookie_string(cookies)
            self.set_cookies(cookies)

    def set_cookies(self, cookies: dict[str, str]):
        self.cookies = cookies
        for k, v in cookies.items():
            self.session.cookies.set(k, v, domain=".zhihu.com")
        if cookies.get("_xsrf"):
            self.session.headers["x-xsrftoken"] = cookies["_xsrf"]

    def fetch_missing_cookies(self):
        """访问首页自动拉取并补全 _xsrf 和 d_c0"""
        try:
            resp = self.session.get(f"{ZHIHU_BASE_URL}/", timeout=10)
            if resp.status_code == 200:
                for c in self.session.cookies:
                    if c.name in ("_xsrf", "d_c0") and c.name not in self.cookies:
                        self.cookies[c.name] = c.value
                if self.cookies.get("_xsrf"):
                    self.session.headers["x-xsrftoken"] = self.cookies["_xsrf"]
        except Exception as e:
            logger.warning(f"自动补全 Cookie 过程出现异常: {e}")

    def verify_auth(self) -> dict | None:
        """在线校验凭证有效性 (/api/v4/me)"""
        try:
            resp = self.session.get(f"{ZHIHU_API_V4}/me", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("id") or (data.get("name") and data.get("name") != "知乎用户"):
                    return data
        except Exception as e:
            logger.debug(f"验证 Session 异常: {e}")
        return None

    def search(self, keyword: str, limit: int = 20, sort_by: str = "default") -> tuple[list[dict[str, Any]], str | None]:
        """执行知乎搜索 API (/api/v4/search_v3)，支持综合 (default)、最新发布 (created_time) 与最多赞同 (upvoted_count) 检索"""
        url = f"{ZHIHU_API_V4}/search_v3"
        search_source = "Filter" if sort_by and sort_by != "default" else "Normal"
        params = {
            "gk_version": "gz-gaokao",
            "t": "general",
            "q": keyword,
            "correction": 1,
            "offset": 0,
            "limit": limit,
            "filter_fields": "lc_idx",
            "lc_idx": 0,
            "show_all_topics": 0,
            "search_source": search_source,
            "type": "content"
        }
        if sort_by and sort_by != "default":
            params["sort"] = sort_by  # 例如 created_time 或 upvoted_count

        encoded_query = quote(keyword)
        referer_url = f"{ZHIHU_BASE_URL}/search?type=content&q={encoded_query}"
        if sort_by and sort_by != "default":
            referer_url += f"&sort={sort_by}"
        self.session.headers["Referer"] = referer_url

        
        try:
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json().get("data", []), None
            elif resp.status_code in (400, 401, 403):
                err = f"知乎 API 拒绝访问 (HTTP {resp.status_code})。知乎搜索接口依赖有效登录凭证，请检查并更新 GitHub Secrets 中的 ZHIHU_COOKIE (需包含有效 z_c0)。"
                logger.error(err)
                return [], err
            else:
                err = f"知乎 API 返回异常响应: HTTP {resp.status_code}"
                logger.error(err)
                return [], err
        except Exception as e:
            err = f"网络请求发生异常: {e}"
            logger.error(err)
            return [], err


    def get_question_answers(self, question_id: str, limit: int = 20, offset: int = 0) -> tuple[list[dict[str, Any]], str | None]:
        """抓取指定知乎问题 ID 下的高赞回答 (/api/v4/questions/{question_id}/answers)"""

        url = f"{ZHIHU_API_V4}/questions/{question_id}/answers"
        params = {
            "include": "data[*].is_normal,content,voteup_count,created_time,updated_time,author,question",
            "limit": limit,
            "offset": offset,
            "order_by": "default"
        }
        self.session.headers["Referer"] = f"{ZHIHU_BASE_URL}/question/{question_id}"
        
        try:
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json().get("data", []), None
            elif resp.status_code in (401, 403):
                err = f"知乎 API 拒绝访问 (HTTP {resp.status_code})。请确认已在环境变量或命令行传入有效 z_c0 Cookie。"
                logger.error(err)
                return [], err
            else:
                err = f"知乎 API 返回异常响应: HTTP {resp.status_code}"
                logger.error(err)
                return [], err
        except Exception as e:
            err = f"网络请求发生异常: {e}"
            logger.error(err)
            return [], err

    def get_answer_comments(self, answer_id: str, limit: int = 20, order_by: str = "score") -> tuple[list[dict[str, Any]], str | None]:
        """抓取指定回答 ID 下的知乎热评及其子评论 (/api/v4/comment_v5/answers/{answer_id}/root_comment)"""
        if not answer_id:
            return [], None

        url = f"{ZHIHU_API_V4}/comment_v5/answers/{answer_id}/root_comment"
        params = {
            "order_by": order_by,
            "limit": limit
        }
        self.session.headers["Referer"] = f"{ZHIHU_BASE_URL}/answer/{answer_id}"

        def _parse_comment(item: dict) -> dict:
            author_obj = item.get("author", {})
            author_name = "知乎用户"
            if isinstance(author_obj, dict):
                author_name = author_obj.get("name") or author_obj.get("member", {}).get("name") or "知乎用户"

            is_content_author = False
            author_tags = item.get("author_tag", [])
            if isinstance(author_tags, list):
                for tag in author_tags:
                    if isinstance(tag, dict) and tag.get("text") == "作者":
                        is_content_author = True
                        break

            reply_to_name = ""
            reply_to_obj = item.get("reply_to_author", {})
            if isinstance(reply_to_obj, dict):
                reply_to_name = reply_to_obj.get("name") or reply_to_obj.get("member", {}).get("name") or ""

            ip_location = ""
            comment_tags = item.get("comment_tag", [])
            if isinstance(comment_tags, list):
                for tag in comment_tags:
                    if isinstance(tag, dict) and tag.get("type") == "ip_info":
                        ip_location = tag.get("text", "")
                        break

            content_str = item.get("content", "")
            vote_count = item.get("vote_count", 0) or item.get("like_count", 0)
            created_time = item.get("created_time", 0)
            created_str = datetime.fromtimestamp(created_time, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if created_time else ""

            raw_children = item.get("child_comments", [])
            child_comments = []
            if isinstance(raw_children, list):
                for child_item in raw_children:
                    child_comments.append(_parse_comment(child_item))

            return {
                "id": str(item.get("id", "")),
                "author_name": author_name,
                "is_content_author": is_content_author,
                "reply_to_name": reply_to_name,
                "ip_location": ip_location,
                "content": content_str,
                "vote_count": vote_count,
                "created_time": created_str,
                "child_comments": child_comments
            }

        try:
            resp = self.session.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                raw_data = resp.json().get("data", [])
                comments = [_parse_comment(item) for item in raw_data]
                return comments, None
            else:
                logger.warning(f"获取回答 {answer_id} 热评失败 (HTTP {resp.status_code})")
                return [], f"HTTP {resp.status_code}"
        except Exception as e:
            logger.warning(f"获取回答 {answer_id} 热评出现异常: {e}")
            return [], str(e)

    def download_image(self, img_url: str, save_path: str) -> bool:
        """使用当前 session 下载图片（如文件已存在则自动跳过重复下载）"""
        try:
            if img_url.startswith("data:"):
                return False
            if img_url.startswith("//"):
                img_url = "https:" + img_url

            if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                logger.info(f"图片本地已存在，跳过重复下载: {save_path}")
                return True

            resp = self.session.get(img_url, timeout=10, stream=True)
            if resp.status_code == 200:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                logger.info(f"图片成功保存: {img_url} -> {save_path}")
                return True
            else:
                logger.warning(f"图片下载失败 (HTTP {resp.status_code}): {img_url}")
                return False
        except Exception as e:
            logger.warning(f"图片下载异常: {img_url}, 错误: {e}")
            return False

    def close(self):
        self.session.close()


def get_authenticated_client(raw_cookie_input: str | None = None) -> ZhihuApiClient:
    """获取或初始化有效认证的 ZhihuApiClient（优先使用命令行参数，其次读取 Agent 环境变量 ZHIHU_COOKIE / zhihu_cookie）"""
    cookie_str = raw_cookie_input or os.environ.get("ZHIHU_COOKIE") or os.environ.get("zhihu_cookie")
    
    if cookie_str:
        cookies = parse_cookie_string(cookie_str)
        if "z_c0" in cookies or len(cookies) > 0:
            client = ZhihuApiClient(cookies)
            user_info = client.verify_auth()
            if user_info:
                logger.info(f"Cookie 认证成功！当前登录知乎用户: {user_info.get('name', '未知')}")
                client.fetch_missing_cookies()
                return client
            else:
                logger.warning("传入的 Cookie 校验失败 (已被知乎注销或已过期)。")

    logger.info("------------------------------------------------------------")
    logger.info("提示: 未检测到有效知乎 Cookie 凭证（优先读取 Agent 环境变量 ZHIHU_COOKIE 或命令行 --cookie 参数）。")
    logger.info("将尝试匿名访问知乎 API。如遇到 HTTP 403 限流，请确保已配置 zhihu_cookie / ZHIHU_COOKIE 环境变量或传入 --cookie。")
    logger.info("------------------------------------------------------------")
    return ZhihuApiClient()



def extract_answer_details(item):
    """提取回答元数据、问题 ID、知乎真实网址与正文 HTML"""
    obj = item.get("object") or item.get("target") or item
    
    content_html = obj.get("content", "")
    created_time = obj.get("created_time") or obj.get("updated_time") or 0
    voteup_count = obj.get("voteup_count", 0)
    
    author_obj = obj.get("author", {})
    author_name = author_obj.get("name", "匿名用户") if isinstance(author_obj, dict) else "匿名用户"
    
    question_obj = obj.get("question", {})
    if isinstance(question_obj, dict):
        question_title = question_obj.get("title") or question_obj.get("name") or obj.get("title") or "A股市场动态分析"
        question_id = str(question_obj.get("id", ""))
    else:
        question_title = obj.get("title") or "A股市场动态分析"
        question_id = ""


    question_url = f"https://www.zhihu.com/question/{question_id}" if question_id else "https://www.zhihu.com"
    answer_id = obj.get("id", str(int(time.time())))
    
    return {
        "id": str(answer_id),
        "question_id": question_id,
        "question_url": question_url,
        "question_title": question_title,
        "author_name": author_name,
        "created_time": created_time,
        "voteup_count": voteup_count,
        "content_html": content_html
    }



def compress_image(image_path: str, max_dimension: int = 800, quality: int = 80):
    """使用 PIL 将图片等比例缩放到 800px 以内，并使用 80% JPEG 质量压缩，最大化节省 Vision Token"""
    try:
        if not os.path.exists(image_path):
            return
        with Image.open(image_path) as img:
            if getattr(img, "is_animated", False):
                return

            width, height = img.size
            need_resize = max(width, height) > max_dimension
            ext = os.path.splitext(image_path)[1].lower()

            if need_resize:
                ratio = max_dimension / float(max(width, height))
                new_size = (int(width * ratio), int(height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)

            if ext in [".jpg", ".jpeg"]:
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(image_path, "JPEG", quality=quality, optimize=True)
            elif ext == ".png":
                if img.mode == "P":
                    img = img.convert("RGBA")
                img.save(image_path, "PNG", optimize=True)
            elif ext == ".webp":
                img.save(image_path, "WEBP", quality=quality, method=6)
    except Exception as e:
        logger.warning(f"图片压缩异常 ({image_path}): {e}")


def process_content_images(client: ZhihuApiClient, content_html: str, answer_idx: int, images_dir: str) -> str:
    """提取 HTML 中的图片、进行降尺寸/质量压缩，清理杂质节点并转化数学公式为 KaTeX 格式"""
    try:
        soup = BeautifulSoup(content_html, "lxml")
    except Exception:
        soup = BeautifulSoup(content_html, "html.parser")

    # 1. 转换知乎公式节点 <span class="ztext-math" data-tex="..."> 为 KaTeX 识别格式
    for math_span in soup.find_all("span", class_="ztext-math"):
        tex = math_span.get("data-tex", "")
        if tex:
            classes = math_span.get("class", [])
            display = "display" in classes or "\n" in tex or r"\begin" in tex
            if display:
                math_span.replace_with(soup.new_string(f" $${tex}$$ "))
            else:
                math_span.replace_with(soup.new_string(f" \\({tex}\\) "))

    # 2. 清理知乎重定向外链 (link.zhihu.com/?target=...)
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if "link.zhihu.com/?target=" in href:
            match = re.search(r"target=([^&]+)", href)
            if match:
                real_url = unquote(match.group(1))
                a_tag["href"] = real_url

    # 3. 清理无用/杂质 HTML 节点 (优先解构 noscript 彻底避免知乎占位重复图片)
    for tag in soup.find_all("noscript"):
        tag.decompose()

    for tag in soup.find_all("figcaption"):
        if not tag.text.strip() and not tag.find_all("img"):
            tag.decompose()

    # 4. 下载图片并进行 Token 优化压缩
    img_tags = soup.find_all("img")
    download_tasks = []
    img_counter = 1
    for img in img_tags:
        src = img.get("data-actualsrc") or img.get("data-original") or img.get("src")
        if not src or src.startswith("data:"):
            continue

        src_hd = upgrade_image_url(src)
        parsed_url = urlparse(src_hd)
        ext = os.path.splitext(parsed_url.path)[1].lower()
        if ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
            ext = ".png"

        img_name = f"img_{answer_idx}_{img_counter}{ext}"
        local_relative_path = f"./images/{img_name}"
        abs_save_path = os.path.join(images_dir, img_name)

        download_tasks.append((src_hd, src, abs_save_path, local_relative_path, img))
        img_counter += 1

    if download_tasks:
        def _download_task(task_item):
            src_hd, src_orig, save_path, rel_path, img_element = task_item
            success = client.download_image(src_hd, save_path)
            if not success and src_hd != src_orig:
                success = client.download_image(src_orig, save_path)
            if success:
                # 针对多模态 Token 进行自动图片缩放与质量压缩
                compress_image(save_path, max_dimension=1280, quality=85)
            return task_item, success

        max_workers = min(len(download_tasks), 5)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_download_task, t) for t in download_tasks]
            for future in as_completed(futures):
                (src_hd, src_orig, save_path, rel_path, img_element), success = future.result()
                if success:
                    img_element["src"] = rel_path
                else:
                    img_element["src"] = src_hd

    # 5. 清理 DOM 中的知乎无用属性
    for tag in soup.find_all(True):
        for attr in ["data-draft-node", "data-draft-type", "data-rawwidth", "data-rawheight", "data-actualsrc", "data-original", "data-private-watermark"]:
            if attr in tag.attrs:
                del tag.attrs[attr]

    return str(soup)


def convert_html_to_markdown(html_content: str) -> str:
    """将 HTML 转换为排版整洁的 Markdown 文本"""
    md = markdownify.markdownify(html_content, heading_style="ATX")
    md = re.sub(r"<noscript>.*?</noscript>", "", md, flags=re.DOTALL)
    md = re.sub(r"<figcaption>.*?</figcaption>", "", md, flags=re.DOTALL)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def compile_markdown_content(md_text: str) -> tuple[dict[str, str], str]:
    """
    解析 Frontmatter 与 Callout 语法，并使用 markdown 库转换为经过 CSS 美化的 HTML 正文。
    支持语法：
    - > [!AI-SUMMARY]
    - [词汇]{注:解释}
    - > [!AI-BACKGROUND] 标题
    - > [!AI-CHART-CARD]
    - > [!AI-FINAL-SUMMARY]
    """
    frontmatter = {}
    content = md_text

    # 1. 提取 Frontmatter
    if md_text.startswith("---"):
        parts = md_text.split("---", 2)
        if len(parts) >= 3:
            fm_text = parts[1]
            content = parts[2]
            for line in fm_text.strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    frontmatter[k.strip()] = v.strip().strip('"').strip("'")

    # 2. 转换内联黑话 [词汇]{注:解释}
    def _replace_note(m):
        term = m.group(1)
        note = m.group(2)
        return (
            f'<span class="inline-note-term">{term}'
            f'<sup class="note-badge">注</sup>'
            f'<span class="note-tooltip">{note}</span></span>'
        )

    content = re.sub(r'\[([^\]]+)\]\{注:([^\}]+)\}', _replace_note, content)

    # 3. 转换 Callout 块
    lines = content.splitlines()
    new_lines = []
    in_callout = False
    callout_type = ""
    callout_title = ""
    callout_buffer = []

    def _flush_callout(c_type, c_title, c_buf):
        raw_text = "\n".join(c_buf).strip()
        html_inner = markdown.markdown(raw_text, extensions=['tables', 'fenced_code'])
        if c_type == "AI-SUMMARY":
            return (
                f'<div class="ai-card ai-summary-card">'
                f'<div class="ai-card-title">AI 文章核心概述（精简版）</div>'
                f'<div class="ai-card-body">{html_inner}</div>'
                f'</div>'
            )
        elif c_type == "AI-BACKGROUND":
            title_str = c_title or "核心背景知识"
            return (
                f'<blockquote class="ai-background-block">'
                f'<div class="ai-block-header">'
                f'<span class="ai-block-tag">核心背景知识</span>'
                f'<span class="ai-block-title">{title_str}</span>'
                f'</div>'
                f'<div class="ai-block-body">{html_inner}</div>'
                f'</blockquote>'
            )
        elif c_type == "AI-CHART-CARD":
            return (
                f'<div class="ai-card ai-chart-card">'
                f'<div class="ai-card-title">图表视觉解读</div>'
                f'{html_inner}'
                f'</div>'
            )
        elif c_type == "AI-FINAL-SUMMARY":
            return (
                f'<div class="ai-card ai-summary-card">'
                f'<div class="ai-card-title">全文总结与新手避坑指南</div>'
                f'{html_inner}'
                f'</div>'
            )
        return raw_text

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        callout_match = re.match(r'^\s*>\s*\[\!(AI-SUMMARY|AI-BACKGROUND|AI-CHART-CARD|AI-FINAL-SUMMARY)\](?:\s+(.*))?$', line)
        if callout_match:
            if in_callout:
                new_lines.append(_flush_callout(callout_type, callout_title, callout_buffer))
                callout_buffer = []
            in_callout = True
            callout_type = callout_match.group(1)
            callout_title = (callout_match.group(2) or "").strip()
            idx += 1
            continue

        if in_callout:
            if line.startswith(">"):
                callout_buffer.append(line.lstrip(">").strip())
                idx += 1
                continue
            elif not line.strip():
                new_lines.append(_flush_callout(callout_type, callout_title, callout_buffer))
                in_callout = False
                callout_buffer = []
                new_lines.append("")
                idx += 1
                continue
            else:
                new_lines.append(_flush_callout(callout_type, callout_title, callout_buffer))
                in_callout = False
                callout_buffer = []
                new_lines.append(line)
                idx += 1
                continue
        else:
            new_lines.append(line)
            idx += 1

    if in_callout:
        new_lines.append(_flush_callout(callout_type, callout_title, callout_buffer))

    final_md = "\n".join(new_lines)
    html_body = markdown.markdown(final_md, extensions=['tables', 'fenced_code'])
    # 清理正文开头与卡片标题重复的 <h1> 节点
    html_body = re.sub(r'^\s*<h1>.*?</h1>\s*', '', html_body, flags=re.DOTALL | re.IGNORECASE)
    return frontmatter, html_body


def render_full_html_page(question_title: str, question_id: str, question_url: str, author_name: str, created_datetime_str: str, voteup_count: int | str, body_html: str) -> str:
    qid_badge_html = f'<span class="copy-qid-badge" onclick="copyQuestionId(\'{question_id}\')" title="点击复制问题 ID">ID: {question_id}</span>' if question_id else ''
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{question_title}</title>
    <!-- KaTeX 数学公式渲染支持 -->
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
    <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js" onload="renderMathInElement(document.body, {{delimiters: [{{left: '$$', right: '$$', display: true}}, {{left: '\\\\(', right: '\\\\)', display: false}}, {{left: '\\\\[', right: '\\\\]', display: true}}]}});"></script>
    <style>
        :root {{
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --accent-blue: #3b82f6;
            --accent-emerald: #10b981;
            --accent-amber: #f59e0b;
            --border-color: #334155;
        }}
        @media (prefers-color-scheme: light) {{
            :root {{
                --bg-color: #f8fafc;
                --card-bg: #ffffff;
                --text-primary: #0f172a;
                --text-secondary: #64748b;
                --border-color: #e2e8f0;
            }}
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans SC", sans-serif;
            background-color: var(--bg-color);
            color: var(--text-primary);
            line-height: 1.8;
            margin: 0;
            padding: 2rem 1rem;
        }}
        .container {{
            max-width: 860px;
            margin: 0 auto;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 2.5rem;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.1);
        }}
        h1.article-title {{
            font-size: 1.75rem;
            font-weight: 700;
            margin-top: 0;
            margin-bottom: 1.25rem;
            color: var(--text-primary);
            line-height: 1.4;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 0.75rem;
        }}
        .question-link {{
            color: var(--text-primary);
            text-decoration: none;
            transition: color 0.2s ease;
        }}
        .question-link:hover {{
            color: var(--accent-blue);
            text-decoration: underline;
        }}
        .copy-qid-badge {{
            font-size: 0.8rem;
            padding: 0.25rem 0.6rem;
            border-radius: 6px;
            background: rgba(59, 130, 246, 0.12);
            color: var(--accent-blue);
            border: 1px solid rgba(59, 130, 246, 0.3);
            cursor: pointer;
            font-weight: 500;
            user-select: none;
            transition: all 0.2s ease;
        }}
        .copy-qid-badge:hover {{
            background: var(--accent-blue);
            color: #ffffff;
        }}
        .article-meta {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.75rem;
            align-items: center;
            padding-bottom: 1.5rem;
            border-bottom: 1px solid var(--border-color);
            margin-bottom: 2rem;
        }}
        .meta-badge {{
            font-size: 0.85rem;
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            background: var(--border-color);
            color: var(--text-secondary);
            font-weight: 500;
        }}
        .meta-badge.author {{ background: rgba(59, 130, 246, 0.15); color: var(--accent-blue); }}
        .meta-badge.votes {{ background: rgba(16, 185, 129, 0.15); color: var(--accent-emerald); }}
        .article-content img {{
            max-width: 100%;
            height: auto;
            border-radius: 12px;
            margin: 1.5rem 0;
            display: block;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
        }}
        .article-content p {{
            margin-bottom: 1.5rem;
        }}
        .ai-card {{
            margin: 1.5rem 0 2rem 0;
            border-radius: 12px;
            border-left: 4px solid var(--accent-blue);
            background: rgba(59, 130, 246, 0.06);
            padding: 1.25rem 1.5rem;
            border-top: 1px solid var(--border-color);
            border-right: 1px solid var(--border-color);
            border-bottom: 1px solid var(--border-color);
        }}
        .ai-card.ai-chart-card {{
            border-left-color: #2563eb;
            background: rgba(37, 99, 235, 0.07);
        }}
        .ai-card.ai-summary-card {{
            border-left-color: #60a5fa;
            background: rgba(96, 165, 250, 0.08);
        }}
        .ai-card-title {{
            font-weight: 700;
            font-size: 1.05rem;
            margin-bottom: 0.75rem;
            color: var(--text-primary);
        }}
        .ai-section {{
            margin-top: 0.75rem;
        }}
        .ai-section-label {{
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 0.25rem;
        }}
        .ai-card ul, .ai-card ol {{
            margin: 0.5rem 0;
            padding-left: 1.25rem;
        }}
        .ai-card li {{
            margin-bottom: 0.35rem;
        }}
        .inline-note-term {{
            position: relative;
            border-bottom: 1.5px dashed var(--accent-blue);
            cursor: pointer;
            display: inline-block;
        }}
        .note-badge {{
            font-size: 0.65rem;
            line-height: 1;
            padding: 0.15rem 0.35rem;
            border-radius: 9999px;
            background: var(--accent-blue);
            color: #ffffff;
            font-weight: 700;
            margin-left: 2px;
            vertical-align: super;
            user-select: none;
            transition: transform 0.2s ease, background-color 0.2s ease;
        }}
        .inline-note-term:hover .note-badge,
        .inline-note-term:focus .note-badge {{
            transform: scale(1.15);
            background: #2563eb;
        }}
        .note-tooltip {{
            position: absolute;
            bottom: 130%;
            left: 50%;
            transform: translateX(-50%);
            width: max-content;
            max-width: min(280px, 80vw);
            white-space: normal;
            word-break: break-word;
            overflow-wrap: break-word;
            background: #0f172a;
            color: #f8fafc;
            padding: 0.6rem 0.9rem;
            border-radius: 8px;
            font-size: 0.8rem;
            line-height: 1.5;
            box-shadow: 0 10px 25px rgba(0,0,0,0.4);
            border: 1px solid #334155;
            z-index: 1000;
            pointer-events: none;
            opacity: 0;
            visibility: hidden;
            transition: opacity 0.2s ease, visibility 0.2s ease;
        }}
        .inline-note-term:hover .note-tooltip,
        .inline-note-term.active .note-tooltip {{
            opacity: 1;
            visibility: visible;
        }}
        .ai-background-block {{
            margin: 1.5rem 0 2rem 0;
            padding: 1.25rem 1.5rem;
            border-left: 4px solid var(--accent-blue);
            background: rgba(59, 130, 246, 0.06);
            border-top: 1px solid var(--border-color);
            border-right: 1px solid var(--border-color);
            border-bottom: 1px solid var(--border-color);
            border-radius: 0 12px 12px 0;
        }}
        .ai-block-header {{
            display: flex;
            align-items: center;
            gap: 0.6rem;
            margin-bottom: 0.6rem;
        }}
        .ai-block-tag {{
            font-size: 0.75rem;
            font-weight: 700;
            padding: 0.15rem 0.5rem;
            border-radius: 4px;
            background: rgba(59, 130, 246, 0.18);
            color: var(--accent-blue);
        }}
        .ai-block-title {{
            font-weight: 700;
            font-size: 0.95rem;
            color: var(--text-primary);
        }}
        .ai-block-body {{
            font-size: 0.9rem;
            line-height: 1.7;
            color: var(--text-secondary);
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1 class="article-title">
            <a href="{question_url}" target="_blank" rel="noopener noreferrer" class="question-link" data-question-id="{question_id}" title="点击打开知乎原问题网页，长按可复制问题 ID">{question_title}</a>
            {qid_badge_html}
        </h1>
        <div class="article-meta">
            <span class="meta-badge author">作者：{author_name}</span>
            <span class="meta-badge time">发布时间：{created_datetime_str}</span>
            <span class="meta-badge votes">赞同数：{voteup_count}</span>
        </div>
        <div class="article-content">
            {body_html}
        </div>
    </div>

    <script>
        function copyQuestionId(qId) {{
            if (!qId) return;
            if (navigator.clipboard && navigator.clipboard.writeText) {{
                navigator.clipboard.writeText(qId).then(() => {{
                    showToast('已复制问题 ID: ' + qId);
                }}).catch(() => fallbackCopy(qId));
            }} else {{
                fallbackCopy(qId);
            }}
        }}

        function fallbackCopy(qId) {{
            const ta = document.createElement('textarea');
            ta.value = qId;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            showToast('已复制问题 ID: ' + qId);
        }}

        function showToast(msg) {{
            let toast = document.getElementById('copy-toast');
            if (!toast) {{
                toast = document.createElement('div');
                toast.id = 'copy-toast';
                toast.style.cssText = `
                    position: fixed;
                    bottom: 2rem;
                    left: 50%;
                    transform: translateX(-50%);
                    background: #0f172a;
                    color: #f8fafc;
                    padding: 0.75rem 1.5rem;
                    border-radius: 9999px;
                    font-size: 0.875rem;
                    font-weight: 500;
                    box-shadow: 0 10px 25px rgba(0,0,0,0.4);
                    border: 1px solid #334155;
                    z-index: 9999;
                    transition: opacity 0.3s ease;
                `;
                document.body.appendChild(toast);
            }}
            toast.innerText = msg;
            toast.style.opacity = '1';
            setTimeout(() => {{ toast.style.opacity = '0'; }}, 2800);
        }}
    </script>
</body>
</html>
"""


def compile_markdown_file(md_path: Path) -> tuple[str, dict]:
    """读取带有 Frontmatter 与 Callout 批注的 .md 文件，并提取 HTML body 节点及元数据"""
    with open(md_path, "r", encoding="utf-8") as f:
        md_text = f.read()

    fm, body_html = compile_markdown_content(md_text)

    question_title = fm.get("question_title", "A股市场动态分析")
    question_id = fm.get("question_id", "")
    answer_id = fm.get("id", "")
    question_url = fm.get("question_url", "https://www.zhihu.com")
    author_name = fm.get("author_name", "知乎用户")
    created_datetime_str = fm.get("created_time", "")
    voteup_count = fm.get("voteup_count", 0)

    comments = []
    if answer_id:
        comments_file = md_path.parent / "comments" / f"comments_{answer_id}.json"
        if not comments_file.exists():
            comments_file = md_path.parent / "temp" / "comments" / f"comments_{answer_id}.json"
        if comments_file.exists():
            try:
                with open(comments_file, "r", encoding="utf-8") as cf:
                    comments = json.load(cf)
            except Exception:
                comments = []

    meta = {
        "id": answer_id,
        "question_title": question_title,
        "question_id": question_id,
        "question_url": question_url,
        "author_name": author_name,
        "created_time": created_datetime_str,
        "voteup_count": voteup_count,
        "filename": md_path.name,
        "body_html": body_html,
        "comments": comments
    }
    return md_path.name, meta


def generate_index_dashboard(daily_out_dir: Path, articles_meta: list[dict], date_formatted: str, date_compact: str) -> str:
    """生成每日知乎热议全景聚合仪表盘 index.html（单页知乎信息流风格，支持直接阅读与展开/收起）"""
    index_path = daily_out_dir / "index.html"

    total_articles = len(articles_meta)
    total_votes = 0
    for item in articles_meta:
        try:
            total_votes += int(item.get("voteup_count", 0))
        except (ValueError, TypeError):
            pass

    cards_html = ""
    for idx, item in enumerate(articles_meta, start=1):
        qid = item.get("question_id", "")
        qid_badge = f'<button class="btn-copy" onclick="copyQId(\'{qid}\')">ID: {qid}</button>' if qid else ''
        body_content = item.get("body_html", "<p style='color:var(--text-secondary);'>无正文内容</p>")

        comments = item.get("comments", [])
        comments_section_html = ""
        if comments:
            def _render_comment_html(c: dict, is_child: bool = False) -> str:
                author = c.get("author_name", "知乎用户") or "知乎用户"
                avatar_char = author[0]
                c_content = c.get("content", "")
                c_votes = c.get("vote_count", 0)
                c_time = c.get("created_time", "")
                c_ip = c.get("ip_location", "")
                time_ip_str = f"{c_time} · {c_ip}" if c_ip else c_time

                author_badge_html = '<span class="author-badge">作者</span>' if c.get("is_content_author") else ''
                reply_html = f'<span class="reply-symbol">▸</span> <span class="reply-author-name">{c["reply_to_name"]}</span>' if c.get("reply_to_name") else ''

                votes_html = f'<span class="dash-comment-votes">赞同 {c_votes}</span>' if (not is_child or c_votes > 0) else ''

                child_html = ""
                children = c.get("child_comments", [])
                if children:
                    child_items = "".join([_render_comment_html(child, is_child=True) for child in children])
                    child_html = f'<div class="dash-child-comments-list">{child_items}</div>'

                item_class = "dash-child-comment-item" if is_child else "dash-comment-item"

                return f"""
                <div class="{item_class}">
                    <div class="dash-comment-header">
                        <span class="dash-comment-avatar">{avatar_char}</span>
                        <span class="dash-comment-author">{author}</span>
                        {author_badge_html}
                        {reply_html}
                        <span class="dash-comment-time">{time_ip_str}</span>
                        {votes_html}
                    </div>
                    <div class="dash-comment-body">{c_content}</div>
                    {child_html}
                </div>
                """

            comment_items_html = "".join([_render_comment_html(c, is_child=False) for c in comments])

            comments_section_html = f"""
            <div class="dash-comments-wrapper">
                <div class="dash-comments-header" onclick="toggleComments({idx})">
                    <span class="dash-comments-title">热门评论 ({len(comments)})</span>
                    <button class="btn-toggle-comments" id="btn-toggle-comments-{idx}" onclick="event.stopPropagation(); toggleComments({idx})">展开评论</button>
                </div>
                <div class="dash-comments-list collapsed" id="comments-list-{idx}">
                    {comment_items_html}
                </div>
            </div>
            """

        cards_html += f"""
        <div class="dash-card" id="card-{idx}">
            <div class="dash-card-header">
                <span class="dash-author-avatar">{item.get('author_name', '知')[0]}</span>
                <span class="dash-author">{item.get('author_name', '知乎用户')}</span>
                <span class="dash-time">· {item.get('created_time', '')}</span>
                <span class="dash-votes">赞同 {item.get('voteup_count', 0)}</span>
            </div>
            <h2 class="dash-title">
                <a href="{item.get('question_url', '#')}" target="_blank" rel="noopener noreferrer" class="question-link">{item.get('question_title', '知乎热门分析')}</a>
                {qid_badge}
            </h2>
            <div class="answer-content-wrapper collapsed" id="content-wrapper-{idx}">
                <div class="answer-body-content article-content" id="body-{idx}">
                    {body_content}
                </div>
                <div class="fade-overlay"></div>
            </div>
            <div class="card-footer-actions">
                <button class="btn-toggle-expand" onclick="toggleExpand({idx})" id="btn-toggle-{idx}">
                    <span class="btn-text">阅读完整回答与图表解析</span>
                </button>
                <a href="{item.get('question_url', '#')}" target="_blank" rel="noopener noreferrer" class="btn-zhihu">知乎原问题</a>
            </div>
            {comments_section_html}
        </div>
        """

    # 动态构建历史盘后日历下拉菜单
    base_dir = daily_out_dir.parent
    history_options_html = ""
    if base_dir.exists():
        for d in sorted([p for p in base_dir.iterdir() if p.is_dir() and re.match(r'^\d{8}$', p.name)], key=lambda x: x.name, reverse=True):
            d_name = d.name
            d_fmt = f"{d_name[:4]}年{d_name[4:6]}月{d_name[6:]}日"
            selected = "selected" if d_name == date_compact else ""
            history_options_html += f'<option value="{d_name}" {selected}>{d_fmt}</option>'

    history_selector_html = f"""
    <div class="dash-history-select">
        <label for="history-select" style="font-size:0.85rem;color:var(--text-secondary);margin-right:0.3rem;">📅 历史盘后日历:</label>
        <select id="history-select" onchange="switchDashboardDate(this.value)" style="padding:0.25rem 0.6rem;border-radius:6px;border:1px solid var(--border-color);background:var(--card-bg);color:var(--text-primary);font-size:0.85rem;cursor:pointer;">
            {history_options_html}
        </select>
    </div>
    """ if history_options_html else ""

    dashboard_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>知乎 A 股热议全景 ({date_formatted})</title>
    <!-- KaTeX 数学公式渲染支持 -->
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
    <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js" onload="renderMathInElement(document.body, {{delimiters: [{{left: '$$', right: '$$', display: true}}, {{left: '\\\\(', right: '\\\\)', display: false}}, {{left: '\\\\[', right: '\\\\]', display: true}}]}});"></script>
    <style>
        :root {{
            --bg-color: #f6f6f6;
            --card-bg: #ffffff;
            --text-primary: #121212;
            --text-secondary: #8590a6;
            --zhihu-blue: #056de8;
            --zhihu-blue-hover: #0056b3;
            --zhihu-blue-bg: rgba(5, 109, 232, 0.08);
            --border-color: #ebebeb;
            --card-shadow: 0 1px 3px rgba(18, 18, 18, 0.1);
        }}
        @media (prefers-color-scheme: dark) {{
            :root {{
                --bg-color: #121212;
                --card-bg: #1e1e1e;
                --text-primary: #f0f0f0;
                --text-secondary: #8590a6;
                --zhihu-blue: #3b82f6;
                --zhihu-blue-hover: #60a5fa;
                --zhihu-blue-bg: rgba(59, 130, 246, 0.12);
                --border-color: #2e2e2e;
                --card-shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
            }}
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", "PingFang SC", "Microsoft YaHei", "Source Han Sans SC", sans-serif;
            background-color: var(--bg-color);
            color: var(--text-primary);
            line-height: 1.67;
            margin: 0;
            padding: 1.5rem 0;
            -webkit-font-smoothing: antialiased;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
            padding: 0 1rem;
        }}
        .dash-header {{
            background: var(--card-bg);
            border-radius: 8px;
            padding: 1.5rem 2rem;
            margin-bottom: 1rem;
            box-shadow: var(--card-shadow);
            border: 1px solid var(--border-color);
        }}
        .dash-header-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 0.75rem;
        }}
        .dash-header h1 {{
            font-size: 1.5rem;
            font-weight: 600;
            margin: 0;
            color: var(--text-primary);
        }}
        .dash-meta {{
            font-size: 0.88rem;
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            gap: 1.25rem;
            margin-top: 0.75rem;
        }}
        .dash-grid {{
            display: flex;
            flex-direction: column;
            gap: 0.85rem;
        }}
        .dash-card {{
            background: var(--card-bg);
            border-radius: 8px;
            padding: 1.5rem 2rem;
            box-shadow: var(--card-shadow);
            border: 1px solid var(--border-color);
        }}
        .dash-card-header {{
            display: flex;
            align-items: center;
            gap: 0.6rem;
            margin-bottom: 0.75rem;
            font-size: 0.88rem;
        }}
        .dash-author-avatar {{
            width: 24px;
            height: 24px;
            border-radius: 50%;
            background: var(--zhihu-blue-bg);
            color: var(--zhihu-blue);
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 0.75rem;
        }}
        .dash-author {{ color: var(--text-primary); font-weight: 600; }}
        .dash-time {{ color: var(--text-secondary); }}
        .dash-votes {{
            background: var(--zhihu-blue-bg);
            color: var(--zhihu-blue);
            font-weight: 500;
            padding: 0.15rem 0.65rem;
            border-radius: 4px;
            margin-left: auto;
            font-size: 0.82rem;
        }}
        .dash-title {{
            font-size: 1.25rem;
            font-weight: 600;
            margin: 0 0 1rem 0;
            line-height: 1.4;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
        }}
        .dash-title a.question-link {{
            color: var(--text-primary);
            text-decoration: none;
        }}
        .dash-title a.question-link:hover {{
            color: var(--zhihu-blue);
        }}
        .btn-copy {{
            font-size: 0.75rem;
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
            background: transparent;
            color: var(--text-secondary);
            border: 1px solid var(--border-color);
            cursor: pointer;
            user-select: none;
            white-space: nowrap;
        }}
        .btn-copy:hover {{
            color: var(--zhihu-blue);
            border-color: var(--zhihu-blue);
        }}
        .answer-content-wrapper {{
            position: relative;
            overflow: hidden;
            transition: max-height 0.3s ease;
        }}
        .answer-content-wrapper.collapsed {{
            max-height: 240px;
        }}
        .answer-content-wrapper.collapsed .fade-overlay {{
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 90px;
            background: linear-gradient(to bottom, rgba(255, 255, 255, 0), var(--card-bg));
            pointer-events: none;
        }}
        @media (prefers-color-scheme: dark) {{
            .answer-content-wrapper.collapsed .fade-overlay {{
                background: linear-gradient(to bottom, rgba(30, 30, 30, 0), var(--card-bg));
            }}
        }}
        .answer-content-wrapper.expanded {{
            max-height: none;
        }}
        .answer-content-wrapper.expanded .fade-overlay {{
            display: none;
        }}
        .article-content {{
            font-size: 0.98rem;
            color: var(--text-primary);
        }}
        .article-content img {{
            max-width: 100%;
            height: auto;
            border-radius: 4px;
            margin: 1rem 0;
            display: block;
        }}
        .article-content p {{
            margin-bottom: 1rem;
        }}
        .ai-card {{
            margin: 1rem 0;
            border-radius: 6px;
            border-left: 3px solid var(--zhihu-blue);
            background: var(--zhihu-blue-bg);
            padding: 1rem 1.25rem;
        }}
        .ai-card-title {{
            font-weight: 600;
            font-size: 0.92rem;
            margin-bottom: 0.5rem;
            color: var(--zhihu-blue);
        }}
        .ai-card ul, .ai-card ol {{
            margin: 0.35rem 0;
            padding-left: 1.25rem;
        }}
        .ai-card li {{
            margin-bottom: 0.25rem;
            font-size: 0.92rem;
        }}
        .inline-note-term {{
            position: relative;
            border-bottom: 1.5px dotted var(--zhihu-blue);
            cursor: pointer;
            color: var(--text-primary);
        }}
        .note-badge {{
            font-size: 0.65rem;
            padding: 0 0.2rem;
            color: var(--zhihu-blue);
            font-weight: 500;
            vertical-align: super;
        }}
        .note-tooltip {{
            position: absolute;
            bottom: 125%;
            left: 50%;
            transform: translateX(-50%);
            width: max-content;
            max-width: min(280px, 80vw);
            white-space: normal;
            word-break: break-word;
            overflow-wrap: break-word;
            background: var(--text-primary);
            color: var(--card-bg);
            padding: 0.6rem 0.85rem;
            border-radius: 8px;
            font-size: 0.82rem;
            line-height: 1.5;
            box-shadow: 0 4px 16px rgba(0,0,0,0.25);
            z-index: 1000;
            pointer-events: none;
            opacity: 0;
            visibility: hidden;
            transition: opacity 0.2s ease, transform 0.2s ease;
        }}
        .inline-note-term:hover .note-tooltip,
        .inline-note-term.active .note-tooltip {{
            opacity: 1;
            visibility: visible;
        }}
        .ai-background-block {{
            margin: 1rem 0;
            padding: 0.85rem 1.15rem;
            border-left: 3px solid var(--zhihu-blue);
            background: var(--zhihu-blue-bg);
            border-radius: 0 6px 6px 0;
        }}
        .ai-block-header {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
            margin-bottom: 0.4rem;
        }}
        .ai-block-tag {{
            font-size: 0.75rem;
            font-weight: 600;
            color: var(--zhihu-blue);
        }}
        .ai-block-title {{
            font-weight: 600;
            font-size: 0.9rem;
            color: var(--text-primary);
        }}
        .ai-block-body {{
            font-size: 0.88rem;
            line-height: 1.6;
            color: var(--text-secondary);
        }}
        .card-footer-actions {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-top: 1.25rem;
            padding-top: 0.85rem;
            border-top: 1px solid var(--border-color);
        }}
        .btn-toggle-expand {{
            background: var(--zhihu-blue-bg);
            color: var(--zhihu-blue);
            border: 1px solid rgba(5, 109, 232, 0.2);
            padding: 0.4rem 1rem;
            border-radius: 6px;
            font-size: 0.85rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
        }}
        .btn-toggle-expand:hover {{
            background: var(--zhihu-blue);
            color: #ffffff;
        }}
        .btn-zhihu {{
            font-size: 0.85rem;
            color: var(--text-secondary);
            text-decoration: none;
        }}
        .btn-zhihu:hover {{
            color: var(--zhihu-blue);
        }}
        .dash-comments-wrapper {{
            margin-top: 1rem;
            border-top: 1px dashed var(--border-color);
            padding-top: 0.75rem;
        }}
        .dash-comments-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            cursor: pointer;
            user-select: none;
            padding: 0.25rem 0;
        }}
        .dash-comments-title {{
            font-size: 0.88rem;
            font-weight: 600;
            color: var(--text-secondary);
        }}
        .btn-toggle-comments {{
            background: transparent;
            color: var(--zhihu-blue);
            border: 1px solid var(--border-color);
            padding: 0.2rem 0.6rem;
            border-radius: 4px;
            font-size: 0.78rem;
            cursor: pointer;
            transition: all 0.2s ease;
        }}
        .btn-toggle-comments:hover {{
            border-color: var(--zhihu-blue);
            background: var(--zhihu-blue-bg);
        }}
        .dash-comments-list.collapsed {{
            display: none !important;
        }}
        .dash-comments-list.expanded {{
            display: flex;
            flex-direction: column;
            gap: 0.6rem;
            margin-top: 0.75rem;
        }}
        .dash-comment-item {{
            background: var(--bg-color);
            padding: 0.6rem 0.85rem;
            border-radius: 6px;
            font-size: 0.85rem;
        }}
        .dash-comment-header {{
            display: flex;
            align-items: center;
            gap: 0.4rem;
            margin-bottom: 0.3rem;
            font-size: 0.78rem;
        }}
        .dash-comment-avatar {{
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 0.7rem;
            flex-shrink: 0;
        }}
        .dash-comment-author {{
            color: var(--text-primary);
            font-weight: 600;
        }}
        .dash-comment-time {{
            color: var(--text-secondary);
        }}
        .dash-comment-votes {{
            margin-left: auto;
            color: var(--text-secondary);
            font-size: 0.78rem;
            background: var(--card-bg);
            padding: 0.1rem 0.4rem;
            border-radius: 4px;
            border: 1px solid var(--border-color);
        }}
        .dash-comment-body {{
            color: var(--text-primary);
            line-height: 1.5;
            word-break: break-word;
        }}
        .dash-comment-body p {{
            margin: 0;
        }}
        .dash-child-comments-list {{
            margin-top: 0.6rem;
            padding-left: 0.85rem;
            border-left: 2px solid var(--border-color);
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }}
        .dash-child-comment-item {{
            background: transparent;
            padding: 0.35rem 0;
            font-size: 0.83rem;
        }}
        .author-badge {{
            font-size: 0.68rem;
            background: var(--border-color);
            color: var(--text-secondary);
            padding: 0.05rem 0.35rem;
            border-radius: 3px;
            margin-left: 0.15rem;
            font-weight: 500;
        }}
        .reply-symbol {{
            color: var(--text-secondary);
            font-size: 0.75rem;
            margin: 0 0.15rem;
        }}
        .reply-author-name {{
            color: var(--text-secondary);
            font-weight: 500;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="dash-header">
            <div class="dash-header-top">
                <h1>知乎 A 股热议全景</h1>
                {history_selector_html}
            </div>
            <div class="dash-meta">
                <span>日期：{date_formatted}</span>
                <span>精选回答：{total_articles} 篇</span>
                <span>累计赞同：{total_votes} 次</span>
            </div>
        </div>
        <div class="dash-grid">
            {cards_html}
        </div>
    </div>
    <script>
        function switchDashboardDate(targetDate) {{
            if (!targetDate) return;
            var currentPath = window.location.pathname;
            if (new RegExp('\\/\\d{8}\\/').test(currentPath)) {{
                window.location.href = '../' + targetDate + '/index.html';
            }} else {{
                window.location.href = './' + targetDate + '/index.html';
            }}
        }}

        function copyQId(qId) {{
            if (!qId) return;
            navigator.clipboard.writeText(qId).then(() => {{
                alert('已复制问题 ID: ' + qId);
            }});
        }}

        function toggleExpand(idx) {{
            const wrapper = document.getElementById('content-wrapper-' + idx);
            const btn = document.getElementById('btn-toggle-' + idx);
            if (!wrapper || !btn) return;

            if (wrapper.classList.contains('collapsed')) {{
                wrapper.classList.remove('collapsed');
                wrapper.classList.add('expanded');
                btn.querySelector('.btn-text').innerText = '收起回答';
            }} else {{
                wrapper.classList.remove('expanded');
                wrapper.classList.add('collapsed');
                btn.querySelector('.btn-text').innerText = '阅读完整回答与图表解析';
                document.getElementById('card-' + idx).scrollIntoView({{ behavior: 'smooth', block: 'start' }});
            }}
        }}

        function toggleComments(idx) {{
            const list = document.getElementById('comments-list-' + idx);
            const btn = document.getElementById('btn-toggle-comments-' + idx);
            if (!list || !btn) return;

            if (list.classList.contains('collapsed')) {{
                list.classList.remove('collapsed');
                list.classList.add('expanded');
                btn.innerText = '收起评论';
            }} else {{
                list.classList.remove('expanded');
                list.classList.add('collapsed');
                btn.innerText = '展开评论';
            }}
        }}

        function adjustTooltipPosition(term) {{
            const tooltip = term.querySelector('.note-tooltip');
            if (!tooltip) return;

            tooltip.style.left = '50%';
            tooltip.style.transform = 'translateX(-50%)';
            tooltip.style.right = 'auto';

            const rect = tooltip.getBoundingClientRect();
            const padding = 16;
            if (rect.left < padding) {{
                tooltip.style.left = '0';
                tooltip.style.transform = 'translateX(0)';
            }} else if (rect.right > window.innerWidth - padding) {{
                tooltip.style.left = 'auto';
                tooltip.style.right = '0';
                tooltip.style.transform = 'translateX(0)';
            }}
        }}

        document.addEventListener('mouseover', function(e) {{
            const term = e.target.closest('.inline-note-term');
            if (term) adjustTooltipPosition(term);
        }});

        document.addEventListener('click', function(e) {{
            const term = e.target.closest('.inline-note-term');
            document.querySelectorAll('.inline-note-term.active').forEach(t => {{
                if (t !== term) t.classList.remove('active');
            }});
            if (term) {{
                term.classList.toggle('active');
                adjustTooltipPosition(term);
            }}
        }});

        document.addEventListener('DOMContentLoaded', () => {{
            const total = {total_articles};
            for (let i = 1; i <= total; i++) {{
                const body = document.getElementById('body-' + i);
                const wrapper = document.getElementById('content-wrapper-' + i);
                const btn = document.getElementById('btn-toggle-' + i);
                if (body && wrapper && btn) {{
                    if (body.scrollHeight <= 240) {{
                        wrapper.classList.remove('collapsed');
                        wrapper.classList.add('expanded');
                        btn.style.display = 'none';
                    }}
                }}
            }}
        }});
    </script>
</body>
</html>
"""
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(dashboard_html)
    logger.info(f"已生成每日全景单页知乎仪表盘: {index_path}")
    return str(index_path)


def run_pipeline(
    target_date_str: str | None = None,
    raw_cookie: str | None = None,
    base_output_dir: str = "output",
    question_id: str | None = None,
    limit: int = 20,
    sort_by: str = "default",
    enable_file_log: bool = False
):
    """Fetch Zhihu answers and generate initial index.html and Markdown files."""
    if enable_file_log:
        setup_file_logging()

    dt, date_iso, date_compact, date_formatted, search_dt = parse_date(target_date_str)
    
    logger.info(f"--- 开始执行知乎 A 股数据抓取任务 ---")
    logger.info(f"工作目录 (CWD): {WORK_DIR}")
    logger.info(f"目标运行与落盘日期: {date_iso} ({date_compact})")
    if search_dt != dt:
        logger.info(f"周末非交易日策略：搜索知乎关键词将锚定周五开盘日 ({search_dt.strftime('%Y-%m-%d')})，保持落盘目录为 {date_compact} 独占。")
    
    # 获取授权客户端
    client = get_authenticated_client(raw_cookie)
    
    tz_beijing = timezone(timedelta(hours=8))
    # 严格锚定运行当天 dt 的 00:00:00（确保周五、周六、周日每天抓取的都是当天 0 点后发表的最新回答，绝不重复）
    target_date_start = datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=tz_beijing)
    target_start_ts = int(target_date_start.timestamp())
    
    # 将输出目录锚定在当前工作目录下
    out_base_path = Path(base_output_dir) if Path(base_output_dir).is_absolute() else WORK_DIR / base_output_dir
    daily_out_dir = (out_base_path / date_compact).resolve()
    temp_dir = daily_out_dir / "temp"
    images_dir = daily_out_dir / "images"
    comments_dir = temp_dir / "comments"
    
    daily_out_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    comments_dir.mkdir(parents=True, exist_ok=True)
    
    if question_id:
        logger.info(f"指定抓取知乎热门问题 ID: {question_id} (限制 {limit} 条回答)")
        raw_results, error_msg = client.get_question_answers(question_id, limit=limit)
        if error_msg:
            logger.error(f"抓取中断: {error_msg}")
            save_last_error(f"【Step 1 知乎抓取中断】{error_msg}", base_output_dir)
            client.close()
            return str(daily_out_dir), [], f"抓取失败: {error_msg}"
            
        candidate_items = []
        for item in raw_results:
            details = extract_answer_details(item)
            if details["created_time"] >= target_start_ts or not target_start_ts:
                candidate_items.append(item)
        candidate_items.sort(key=lambda x: extract_answer_details(x)["voteup_count"], reverse=True)
        valid_items = candidate_items[:limit]
    else:
        query = f"如何看待{search_dt.year}年{search_dt.month}月{search_dt.day}日A股"
        logger.info(f"搜索关键词发现热门问题池: '{query}' (限制 20 条搜索结果)")
        search_results, error_msg = client.search(query, limit=20, sort_by="default")
        if error_msg:
            logger.error(f"搜索中断: {error_msg}")
            save_last_error(f"【Step 1 知乎搜索中断】{error_msg}", base_output_dir)
            client.close()
            return str(daily_out_dir), [], f"搜索失败: {error_msg}"

        question_id_set = set()
        for item in search_results:
            details = extract_answer_details(item)
            if details["created_time"] >= target_start_ts and details["question_id"]:
                question_id_set.add(details["question_id"])

        logger.info(f"检索到符合日期要求的知乎热门问题 ID 共 {len(question_id_set)} 个: {list(question_id_set)}")

        candidate_items = []
        seen_answer_ids = set()

        def _fetch_q_answers(q_id):
            return client.get_question_answers(q_id, limit=20)

        if question_id_set:
            workers = min(len(question_id_set), 5)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_qid = {executor.submit(_fetch_q_answers, q_id): q_id for q_id in question_id_set}
                for future in as_completed(future_to_qid):
                    q_answers, q_err = future.result()
                    if not q_err and q_answers:
                        for item in q_answers:
                            details = extract_answer_details(item)
                            ans_id = details["id"]
                            if ans_id not in seen_answer_ids and details["created_time"] >= target_start_ts:
                                seen_answer_ids.add(ans_id)
                                candidate_items.append(item)

        candidate_items.sort(key=lambda x: extract_answer_details(x)["voteup_count"], reverse=True)
        valid_items = candidate_items[:limit]
            
    logger.info(f"筛选并汇总有效候选回答共 {len(candidate_items)} 条，最终按点赞数降序选出 Top {len(valid_items)} 条。")
    
    if not valid_items:
        msg = f"未检索到相关知乎回答（目标日期：{date_formatted}）。"
        logger.warning(msg)
        client.close()
        return str(daily_out_dir), [], msg

    # 并发预拉取 Top N 回答的热门评论及子评论
    def _fetch_comments_task(item):
        details = extract_answer_details(item)
        ans_id = details["id"]
        comments, _ = client.get_answer_comments(ans_id, limit=20)
        if comments:
            comment_file = comments_dir / f"comments_{ans_id}.json"
            try:
                with open(comment_file, "w", encoding="utf-8") as cf:
                    json.dump(comments, cf, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"保存回答 {ans_id} 热评失败: {e}")

    if valid_items:
        workers = min(len(valid_items), 5)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_fetch_comments_task, item) for item in valid_items]
            for future in as_completed(futures):
                pass

    saved_files = []
    articles_meta = []

    for idx, item in enumerate(valid_items, start=1):
        details = extract_answer_details(item)
        answer_id = details["id"]
        logger.info(f"正在处理第 {idx} 条回答 (ID: {answer_id}) - 作者: {details['author_name']}")

        processed_html = process_content_images(client, details["content_html"], idx, str(images_dir))
        md_body = convert_html_to_markdown(processed_html)
        
        created_datetime_str = datetime.fromtimestamp(details["created_time"], tz_beijing).strftime("%Y-%m-%d %H:%M:%S") if details["created_time"] else date_iso

        # 导出同名 Frontmatter + 纯净 Markdown 文件到 temp/ 目录
        md_filename = f"answer_{idx}_{date_compact}.md"
        md_filepath = temp_dir / md_filename

        md_document = f"""---
id: "{details['id']}"
question_id: "{details['question_id']}"
question_title: "{details['question_title']}"
question_url: "{details['question_url']}"
author_name: "{details['author_name']}"
created_time: "{created_datetime_str}"
voteup_count: {details['voteup_count']}
---

# {details['question_title']}

{md_body}
"""
        with open(md_filepath, "w", encoding="utf-8") as f:
            f.write(md_document)

        logger.info(f"已导出纯净 Markdown 批注文件: {md_filepath}")

        # 提取未批注正文元数据
        _, meta = compile_markdown_file(md_filepath)
        articles_meta.append(meta)
        saved_files.append(str(md_filepath))
    client.close()
    logger.info(f"阶段一数据抓取完成，已导出 {len(saved_files)} 个纯净 .md 文件与热评 JSON 数据。目录: {daily_out_dir}")
    return str(daily_out_dir), saved_files, None


def run_compiler_pipeline(target_date_str: str | None = None, base_output_dir: str = "output"):
    """Compile answer_*.md files into index.html dashboard and cleanup md files."""
    dt, date_iso, date_compact, date_formatted, _ = parse_date(target_date_str)

    out_base_path = Path(base_output_dir) if Path(base_output_dir).is_absolute() else WORK_DIR / base_output_dir
    daily_out_dir = (out_base_path / date_compact).resolve()

    if not daily_out_dir.exists():
        recent_dirs = sorted([d for d in out_base_path.glob("20*") if d.is_dir()], key=lambda x: x.name, reverse=True)
        if recent_dirs:
            daily_out_dir = recent_dirs[0]
            date_compact = daily_out_dir.name
            date_formatted = f"{date_compact[:4]}年{date_compact[4:6]}月{date_compact[6:]}日"
            logger.info(f"未直接找到 {out_base_path / date_compact}，自动平滑定位到最新输出目录: {daily_out_dir}")
        else:
            return str(daily_out_dir), [], f"找不到目录: {daily_out_dir}"

    temp_dir = daily_out_dir / "temp"
    md_files = sorted(list(temp_dir.glob("answer_*.md")), key=lambda p: [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', p.name)])
    if not md_files:
        md_files = sorted(list(daily_out_dir.glob("answer_*.md")), key=lambda p: [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', p.name)])

    if not md_files:
        index_file = daily_out_dir / "index.html"
        if index_file.exists():
            logger.info(f"目录 {daily_out_dir} 下无待编译的 .md 批注文件，但检测到已合并且编译完成的 index.html 仪表盘。优雅返回已有产物。")
            return str(daily_out_dir), [str(index_file)], None
        return str(daily_out_dir), [], f"在目录 {daily_out_dir} 中未找到任何 answer_*.md 批注文件，且无可用 index.html。"

    logger.info(f"--- 开始编译 Markdown 批注文件为 HTML 网页 ---")
    logger.info(f"目标目录: {daily_out_dir} (共找到 {len(md_files)} 个 .md 文件)")

    articles_meta = []
    for md_file in md_files:
        _, meta = compile_markdown_file(md_file)
        articles_meta.append(meta)

    index_path_str = generate_index_dashboard(daily_out_dir, articles_meta, date_formatted, date_compact)

    # 同步复制最新的 index.html 与 images/ 到 output 根目录，并生成 .nojekyll
    try:
        import shutil
        root_index = out_base_path / "index.html"
        root_images = out_base_path / "images"
        nojekyll_file = out_base_path / ".nojekyll"

        shutil.copy2(index_path_str, root_index)
        if (daily_out_dir / "images").exists():
            if root_images.exists():
                shutil.rmtree(root_images, ignore_errors=True)
            shutil.copytree(daily_out_dir / "images", root_images)
        with open(nojekyll_file, "w", encoding="utf-8") as f:
            f.write("")
        logger.info(f"已自动更新根目录直达仪表盘: {root_index} 及 .nojekyll 文件。")
    except Exception as e:
        logger.warning(f"复制根目录最新仪表盘异常: {e}")

    # 自动清理 temp/ 中间过程态目录
    if temp_dir.exists():
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

    cleaned_count = len(md_files)
    for md_file in md_files:
        if md_file.exists():
            try:
                md_file.unlink()
            except Exception:
                pass

    logger.info(f"单页仪表盘 index.html 编译落盘成功，已自动清理中间 md 批注与过程态文件。")
    return str(daily_out_dir), [index_path_str], None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="抓取知乎 A 股问答导出 Markdown 批注文件，并提供确定性 HTML 编译器")

    parser.add_argument("--date", type=str, help="目标日期，格式为 YYYY-MM-DD（默认今天）")
    parser.add_argument("--question-id", type=str, help="指定知乎问题 ID (例如 654321)，直接抓取该问题下的高赞回答")
    parser.add_argument("--sort", type=str, choices=["default", "created_time", "upvoted_count"], default="default", help="搜索结果排序规则")
    parser.add_argument("--limit", type=int, default=20, help="限制抓取的回答数量 (默认 20)")
    parser.add_argument("--compile", action="store_true", help="编译器模式：将指定日期的 answer_*.md 批注文件统一重新编译为 HTML 网页及 index.html 仪表盘")
    parser.add_argument("--cookie", type=str, help="知乎 Cookie 文本字符串 (包含 z_c0)")
    parser.add_argument("--output-dir", type=str, default="output", help="输出根目录 (默认相对于 CWD 的 output/)")
    parser.add_argument("-q", "--quiet", action="store_true", help="静默模式：关闭标准控制台 INFO 日志输出，仅向 stdout 返回最终 JSON 结果，减少 Agent 上下文 Token")
    parser.add_argument("--enable-file-log", action="store_true", help="显式开启本地文件日志保存 (保存至 logs/fetch.log)")
    args = parser.parse_args()

    if args.quiet:
        logger.setLevel(logging.WARNING)

    try:
        if args.compile:
            out_dir, files, err = run_compiler_pipeline(
                target_date_str=args.date,
                base_output_dir=args.output_dir
            )
        else:
            out_dir, files, err = run_pipeline(
                target_date_str=args.date,
                raw_cookie=args.cookie,
                base_output_dir=args.output_dir,
                question_id=args.question_id,
                limit=args.limit,
                sort_by=args.sort,
                enable_file_log=args.enable_file_log
            )

        if err:
            print(json.dumps({"status": "error", "message": err, "output_dir": out_dir, "files": []}, ensure_ascii=False, indent=2))
            sys.exit(1)
        else:
            print(json.dumps({"status": "success", "output_dir": out_dir, "files": files}, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.critical(f"未捕获的全局异常: {e}", exc_info=True)
        print(json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False, indent=2))
        sys.exit(1)

