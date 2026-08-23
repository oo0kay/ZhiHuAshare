#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bark iOS 简易推送与 Cookie 告警脚本 (notify_bark.py)
支持通过 Bark (iOS) 推送 A 股每日仪表盘及 Cookie 过期告警通知。
"""

import os
import sys
import json
import logging
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 修复 Windows 终端 UTF-8 输出
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

WORK_DIR = Path.cwd().resolve()

logger = logging.getLogger("notify_bark")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)


def send_bark_notification(
    bark_key: str,
    title: str,
    body: str,
    target_url: str = "",
    bark_server: str = "https://api.day.app",
    is_error: bool = False
) -> bool:
    """使用 Bark POST 接口发送 iOS 消息通知"""
    server = bark_server.rstrip("/")
    key = bark_key.strip("/")
    endpoint = f"{server}/{key}"

    payload = {
        "title": title,
        "body": body,
        "group": "知乎A股-告警" if is_error else "知乎A股热议",
        "icon": "https://static.zhihu.com/heifetz/favicon.ico",
    }

    if target_url:
        payload["url"] = target_url

    if is_error:
        payload["sound"] = "alarm"
    else:
        payload["sound"] = "calypso"

    try:
        resp = requests.post(endpoint, json=payload, timeout=10)
        if resp.status_code == 200:
            res_json = resp.json()
            if res_json.get("code") == 200 or res_json.get("message") == "success":
                logger.info("Bark iOS 消息推送成功！")
                return True
            else:
                logger.warning(f"Bark 返回响异常: {res_json}")
        else:
            logger.warning(f"Bark 请求失败 HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"Bark 推送发生异常: {e}")

    return False


def main():
    parser = argparse.ArgumentParser(description="知乎 A 股 GitHub Actions Bark iOS 推送与告警脚本")
    parser.add_argument("--status", choices=["success", "error"], required=True, help="构建状态 (success/error)")
    parser.add_argument("--pages-url", type=str, default="", help="GitHub Pages 仪表盘在线 URL")
    parser.add_argument("--date", type=str, help="日期 YYYY-MM-DD")
    parser.add_argument("--error-message", type=str, default="", help="失败原因或异常信息")
    parser.add_argument("--bark-key", type=str, help="Bark Key (也可通过环境变量 BARK_KEY 配置)")
    parser.add_argument("--bark-server", type=str, default="https://api.day.app", help="Bark 服务器基础 URL")
    args = parser.parse_args()

    bark_key = args.bark_key or os.environ.get("BARK_KEY")
    bark_server = args.bark_server or os.environ.get("BARK_SERVER", "https://api.day.app")

    if not bark_key:
        logger.warning("未检测到 BARK_KEY (请在 GitHub Secrets 中配置 BARK_KEY)。跳过 Bark 推送。")
        sys.exit(0)

    tz_beijing = timezone(timedelta(hours=8))
    date_str = args.date or datetime.now(tz_beijing).strftime("%Y-%m-%d")

    if args.status == "error":
        title = f"⚠️ 知乎 A 股构建失败 ({date_str})"
        body = (
            f"构建时间：{datetime.now(tz_beijing).strftime('%H:%M:%S')}\n"
            f"异常信息：{args.error_message or '可能知乎 Cookie (z_c0) 已过期或遭遇接口限流。'}\n"
            f"提示：请登录知乎重新获取 z_c0，并在 GitHub 仓库 Secrets 中更新 ZHIHU_COOKIE。"
        )
        send_bark_notification(bark_key, title, body, bark_server=bark_server, is_error=True)
    else:
        title = f"📈 今日 A 股知乎热议全景解析已完成 ({date_str})"
        body = (
            f"已按点赞数为您筛选全网 Top 15 知乎高赞回答，并完成 Gemini 3.7 AI 图文因果拆解。\n"
            f"点击此通知可直接打开极客仪表盘一屏阅读。"
        )
        send_bark_notification(bark_key, title, body, target_url=args.pages_url, bark_server=bark_server, is_error=False)


if __name__ == "__main__":
    main()
