#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gemini API 自动图文标注脚本 (annotate_gemini.py)
在 GitHub Actions / 无 Agent 模式下，自动读取 fetch_zhihu.py 生成的 answer_*.md 及图片，
调用 Google Gemini API (gemini-2.5-flash) 进行深度因果拆解，生成结构化 JSON 批注，
并通过确定性 DOM 匹配算法写回 .md 文件。
"""

import os
import re
import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# 修复 Windows 终端 UTF-8 输出
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

WORK_DIR = Path.cwd().resolve()

logger = logging.getLogger("annotate_gemini")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)


# ===== 结构化 Output Schema (Pydantic) =====
class InlineNote(BaseModel):
    term: str = Field(description="需要白话拆解的硬专业术语、黑话短语或博弈现象（如：尾盘拉升、互换便利、量价背离）")
    note: str = Field(description="【本质逻辑】资金博弈与内在机制...【新手提示】防踩雷提示...")


class ChartCard(BaseModel):
    img_name: str = Field(description="正文中对应的图片文件名，如 img_1_1.jpg")
    analysis: str = Field(description="基于多空力量博弈与图形心理暗示的新手向视觉解读 Bullet points")


class AnswerAnnotation(BaseModel):
    ai_summary: str = Field(description="拆解核心观点的内在因果传导逻辑（为什么作者是对的/市场规律背后的原理）")
    inline_notes: List[InlineNote] = Field(default_factory=list, description="黑话与博弈现象拆解列表")
    chart_cards: List[ChartCard] = Field(default_factory=list, description="图表视觉解读列表")
    ai_final_summary: str = Field(description="全文逻辑总结与新手避坑指南（含核心逻辑总结与新手防踩雷法则）")


SYSTEM_INSTRUCTION = """你是知乎 A 股理财新手学习助手的 AI 导读专家。
你的任务是为理财新手深度拆解知乎 A 股分析回答中的“内在因果逻辑”与“资金博弈机制”。

【终极规训】：
1. 严禁做字典式的机械名词解释！批注的核心是帮新手搞懂“为什么会有这个结果/市场规律背后的原理”。
2. 观点拆解：拒绝机械转述“作者看好XX板块”，必须解释逻辑传导链（例如：为什么央行互换便利能刺激股市？因为机构抵押低流动性资产换国债->低成本变现->直接向股市注入抄底活水）。
3. 行内词汇：不仅标注硬专业词汇，更要对“尾盘拉升”、“缩量微涨”、“利好落地”等暗含博弈机制的短语进行拆解。note 必须包含【本质逻辑】和【新手提示】。
4. 图表解读：若有图片，结合正文解读多空力量对比与心理博弈，绝不能只汇报最高最低点。
5. 必须返回标准的 JSON 数据，严格匹配 schema 要求。
"""


def parse_date_compact(date_str: str | None = None) -> str:
    tz_beijing = timezone(timedelta(hours=8))
    if date_str:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz_beijing)
        except ValueError:
            dt = datetime.now(tz_beijing)
    else:
        dt = datetime.now(tz_beijing)
    return dt.strftime("%Y%m%d")


def annotate_single_markdown(
    client: genai.Client,
    md_filepath: Path,
    model_name: str = "gemini-3.7-flash",
    market_context: str = ""
) -> bool:
    """使用 Gemini API 为单个 answer_X.md 进行标注并覆写"""
    try:
        with open(md_filepath, "r", encoding="utf-8") as f:
            full_content = f.read()

        # 提取 Frontmatter
        frontmatter_str = ""
        body_text = full_content
        if full_content.startswith("---"):
            parts = full_content.split("---", 2)
            if len(parts) >= 3:
                frontmatter_str = f"---{parts[1]}---\n\n"
                body_text = parts[2].strip()

        # 查找图片路径
        images_dir = md_filepath.parent / "images"
        img_matches = re.findall(r'!\[.*?\]\(\./images/([^\)]+)\)', body_text)

        contents = []
        prompt_text = f"以下是待批注的知乎 A 股回答正文：\n\n{body_text}\n"
        if market_context:
            prompt_text = f"【今日 A 股全局背景】：{market_context}\n\n" + prompt_text

        contents.append(prompt_text)

        # 加载关联图片 Binary
        loaded_img_names = []
        for img_name in img_matches:
            img_path = images_dir / img_name
            if img_path.exists():
                try:
                    with open(img_path, "rb") as img_file:
                        img_bytes = img_file.read()
                    mime_type = "image/jpeg"
                    if img_name.endswith(".png"):
                        mime_type = "image/png"
                    elif img_name.endswith(".webp"):
                        mime_type = "image/webp"

                    contents.append(types.Part.from_bytes(data=img_bytes, mime_type=mime_type))
                    contents.append(f"【关联图片标识】: {img_name}")
                    loaded_img_names.append(img_name)
                    logger.info(f"已加载关联图片: {img_name}")
                except Exception as e:
                    logger.warning(f"读取图片失败 {img_name}: {e}")

        logger.info(f"发送 Gemini 请求分析 {md_filepath.name} (含 {len(loaded_img_names)} 张图片)...")

        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=AnswerAnnotation,
                temperature=0.3,
            ),
        )

        if not response.text:
            logger.error(f"Gemini 返回空内容: {md_filepath.name}")
            return False

        annotation_data = json.loads(response.text)
        annotation = AnswerAnnotation(**annotation_data)

        # ===== 确定性 DOM / Markdown 插入 =====
        new_body = body_text

        # 1. 行内黑话 [词汇]{注:note} 替换
        if annotation.inline_notes:
            for note_item in annotation.inline_notes:
                term = note_item.term.strip()
                note_str = note_item.note.strip()
                if term and term in new_body and f"[{term}]" not in new_body:
                    # 原位替换第一次出现的词汇
                    replacement = f"[{term}]{{注:{note_str}}}"
                    new_body = new_body.replace(term, replacement, 1)

        # 2. 图片下方插入 [!AI-CHART-CARD]
        if annotation.chart_cards:
            for chart_item in annotation.chart_cards:
                img_name = chart_item.img_name.strip()
                analysis = chart_item.analysis.strip()
                if not analysis:
                    continue

                # 匹配对应的图片 Markdown 行
                img_pattern = re.compile(rf'(!\[.*?\]\(\./images/{re.escape(img_name)}\))')
                card_block = (
                    f"\n> [!AI-CHART-CARD]\n" +
                    "\n".join([f"> {line}" for line in analysis.splitlines()]) +
                    "\n"
                )
                if img_pattern.search(new_body):
                    new_body = img_pattern.sub(r'\1\n' + card_block, new_body, count=1)

        # 3. 头部插入 [!AI-SUMMARY]
        if annotation.ai_summary:
            summary_block = (
                f"> [!AI-SUMMARY]\n" +
                "\n".join([f"> {line}" for line in annotation.ai_summary.strip().splitlines()]) +
                "\n\n"
            )
            # 在主标题 `# Title` 之后插入
            title_match = re.search(r'^(#\s+.*?\n)', new_body)
            if title_match:
                title_end = title_match.end()
                new_body = new_body[:title_end] + "\n" + summary_block + new_body[title_end:]
            else:
                new_body = summary_block + new_body

        # 4. 尾部追加 [!AI-FINAL-SUMMARY]
        if annotation.ai_final_summary:
            final_block = (
                f"\n\n> [!AI-FINAL-SUMMARY]\n" +
                "\n".join([f"> {line}" for line in annotation.ai_final_summary.strip().splitlines()]) +
                "\n"
            )
            new_body = new_body.strip() + final_block

        # 组合完整 Frontmatter + 新正文写回
        final_file_content = frontmatter_str + new_body
        with open(md_filepath, "w", encoding="utf-8") as f:
            f.write(final_file_content)

        logger.info(f"成功为 {md_filepath.name} 注入 Gemini 因果批注！")
        return True

    except Exception as e:
        logger.error(f"处理 {md_filepath.name} 出现异常: {e}", exc_info=True)
        return False


def run_annotation_pipeline(
    api_key: str,
    target_date_str: str | None = None,
    output_dir: str = "output",
    model_name: str = "gemini-3.7-flash"
):
    date_compact = parse_date_compact(target_date_str)
    out_base_path = Path(output_dir) if Path(output_dir).is_absolute() else WORK_DIR / output_dir
    daily_out_dir = (out_base_path / date_compact).resolve()

    if not daily_out_dir.exists():
        logger.error(f"找不到输出目录: {daily_out_dir}")
        sys.exit(1)

    md_files = sorted(list(daily_out_dir.glob("answer_*.md")))
    if not md_files:
        logger.warning(f"在 {daily_out_dir} 下未找到待批注的 answer_*.md 文件。")
        return

    logger.info(f"找到 {len(md_files)} 个 Markdown 文件准备进行 Gemini 批注...")

    # 提取全局大盘微背景
    titles = []
    for mf in md_files:
        try:
            with open(mf, "r", encoding="utf-8") as f:
                txt = f.read(500)
                m = re.search(r'question_title:\s*"(.*?)"', txt)
                if m:
                    titles.append(m.group(1))
        except Exception:
            pass
    market_context = f"今日热门热议话题包括: {' | '.join(titles[:5])}"

    client = genai.Client(api_key=api_key)

    success_count = 0
    for md_file in md_files:
        ok = annotate_single_markdown(client, md_file, model_name=model_name, market_context=market_context)
        if ok:
            success_count += 1

    logger.info(f"Gemini API 批注完成: {success_count}/{len(md_files)} 个文件成功覆写。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="使用 Gemini API 自动批注 fetch_zhihu 导出的 Markdown 文件")
    parser.add_argument("--api-key", type=str, help="Google Gemini API Key (也可通过环境变量 GEMINI_API_KEY 配置)")
    parser.add_argument("--date", type=str, help="目标日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--output-dir", type=str, default="output", help="输出根目录 (默认 output/)")
    parser.add_argument("--model", type=str, default="gemini-3.7-flash", help="Gemini 模型名称")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.error("未检测到 GEMINI_API_KEY。请在命令行传入 --api-key 或配置 GEMINI_API_KEY 环境变量。")
        sys.exit(1)

    run_annotation_pipeline(
        api_key=api_key,
        target_date_str=args.date,
        output_dir=args.output_dir,
        model_name=args.model
    )
