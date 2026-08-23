#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
微信通知与 Cookie 过期告警脚本 (notify_wechat.py)
支持 Server酱 (ServerChan) 与 WxPusher 微信推送通道。
构建成功时推送 HTML 仪表盘 Pages URL 及概要；
抓取失败/Cookie 过期时自动推送微信告警提醒更新 GitHub Secrets。
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

logger = logging.getLogger("notify_wechat")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)


def send_serverchan(sendkey: str, title: str, content: str) -> bool:
    """使用 Server酱 推送微信消息"""
    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    data = {"title": title, "desp": content}
    try:
        resp = requests.post(url, data=data, timeout=10)
        if resp.status_code == 200:
            res_json = resp.json()
            if res_json.get("code") == 0 or res_json.get("data", {}).get("errno") == 0:
                logger.info("Server酱 微信消息推送成功！")
                return True
            else:
                logger.warning(f"Server酱 推送返回错误: {res_json}")
        else:
            logger.warning(f"Server酱 请求失败 HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"Server酱 推送异常: {e}")
    return False


def send_wxpusher(app_token: str, topic_ids: list[int], uids: list[str], title: str, content: str, url: str = "") -> bool:
    """使用 WxPusher 推送微信消息"""
    endpoint = "https://wxpusher.zjiecode.com/api/send/message"
    payload = {
        "appToken": app_token,
        "content": f"<h2>{title}</h2><br/>{content.replace(chr(10), '<br/>')}",
        "contentType": 2,  # HTML
        "topicIds": topic_ids,
        "uids": uids,
        "url": url
    }
    try:
        resp = requests.post(endpoint, json=payload, timeout=10)
        if resp.status_code == 200:
            res_json = resp.json()
            if res_json.get("code") == 1000:
                logger.info("WxPusher 微信消息推送成功！")
                return True
            else:
                logger.warning(f"WxPusher 推送返回错误: {res_json}")
        else:
            logger.warning(f"WxPusher 请求失败 HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"WxPusher 推送异常: {e}")
    return False


def push_notification(title: str, content: str, target_url: str = "") -> bool:
    """自动判断环境变量并调用已配置的推送服务"""
    serverchan_key = os.environ.get("SERVERCHAN_SENDKEY") or os.environ.get("SCT_KEY")
    wxpusher_token = os.environ.get("WXPUSHER_APP_TOKEN")
    wxpusher_uids = [u.strip() for u in os.environ.get("WXPUSHER_UIDS", "").split(",") if u.strip()]
    wxpusher_topic_ids = [int(t.strip()) for t in os.environ.get("WXPUSHER_TOPICS", "").split(",") if t.strip().isdigit()]

    pushed = False

    if serverchan_key:
        msg_content = content
        if target_url:
            msg_content += f"\n\n[👉 点击在浏览器中直接打开仪表盘]({target_url})"
        if send_serverchan(serverchan_key, title, msg_content):
            pushed = True

    if wxpusher_token and (wxpusher_uids or wxpusher_topic_ids):
        if send_wxpusher(wxpusher_token, wxpusher_topic_ids, wxpusher_uids, title, content, url=target_url):
            pushed = True

    if not pushed:
        logger.warning("未检测到有效微信推送 Secrets (SERVERCHAN_SENDKEY 或 WXPUSHER_APP_TOKEN)。跳过微信推送。")

    return pushed


def main():
    parser = argparse.ArgumentParser(description="知乎 A 股 GitHub Actions 微信推送与 Cookie 告警脚本")
    parser.add_argument("--status", choices=["success", "error"], required=True, help="构建状态 (success/error)")
    parser.add_argument("--pages-url", type=str, default="", help="GitHub Pages 仪表盘在线 URL")
    parser.add_argument("--date", type=str, help="日期 YYYY-MM-DD")
    parser.add_argument("--error-message", type=str, default="", help="失败原因或异常信息")
    args = parser.parse_args()

    tz_beijing = timezone(timedelta(hours=8))
    date_str = args.date or datetime.now(tz_beijing).strftime("%Y-%m-%d")

    if args.status == "error":
        title = f"⚠️ [告警] 知乎 A 股每日分析构建失败 ({date_str})"
        content = (
            f"**构建时间**：{datetime.now(tz_beijing).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"**异常信息**：{args.error_message or '可能知乎 Cookie (z_c0) 已过期或遭遇接口限流。'}\n\n"
            f"**请处理**：请登录知乎重新获取 `z_c0` Cookie，并前往 GitHub 仓库 -> `Settings` -> `Secrets and variables` -> `Actions` 更新 `ZHIHU_COOKIE`。"
        )
        push_notification(title, content)
    else:
        title = f"📈 今日 A 股知乎热议全景解析已完成 ({date_str})"
        content = (
            f"**完成时间**：{datetime.now(tz_beijing).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"已自动按点赞数为您筛选全网 Top 15 知乎高赞回答，并完成 Gemini AI 图文因果拆解与避坑指南批注。\n\n"
        )
        if args.pages_url:
            content += f"👉 **点击在线仪表盘查看**：[点击打开]({args.pages_url})"

        push_notification(title, content, target_url=args.pages_url)


if __name__ == "__main__":
    main()
