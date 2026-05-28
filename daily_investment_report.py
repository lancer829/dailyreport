"""
每日投资日报 — 从RSS抓取新闻，DeepSeek AI翻译分析，邮件发送（优化版）
"""

import feedparser
from openai import OpenAI
import smtplib
import os
import re
import json
import sys
import threading
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# 加载当前目录下的 .env，覆盖系统环境变量
env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path, override=True)

# ── 配置 ──────────────────────────────────────────────
def _parse_rss_sources(raw: str) -> list[tuple[str, str, int, bool]]:
    """解析 'Name|URL|count[|deep];...' 格式，返回 [(name, url, count, is_deep), ...]"""
    from urllib.parse import urlparse
    result = []
    for item in raw.replace("；", ";").split(";"):
        item = item.strip()
        if not item:
            continue
        parts = item.split("|")
        if len(parts) >= 3:
            name, url = parts[0].strip(), parts[1].strip()
            try:
                count = int(parts[2].strip())
            except ValueError:
                count = 10
        elif len(parts) == 2:
            name, url, count = parts[0].strip(), parts[1].strip(), 10
        else:
            url = parts[0].strip()
            name = urlparse(url).netloc or url
            count = 10
        is_deep = len(parts) >= 4 and parts[3].strip().lower() == "deep"
        result.append((name, url, count, is_deep))
    return result

RSS_SOURCES = _parse_rss_sources(os.getenv("RSS_URLS", "https://www.fool.com/money/feed"))
EMAIL_FROM       = os.getenv("EMAIL_FROM", "lancer829@163.com")
EMAIL_TO         = os.getenv("EMAIL_TO",   "lancer829@163.com")
EMAIL_PASSWORD   = os.getenv("EMAIL_PASSWORD")
SMTP_SERVER      = os.getenv("SMTP_SERVER", "smtp.163.com")
SMTP_PORT        = int(os.getenv("SMTP_PORT", "465"))
DEEPSEEK_API_KEY = os.getenv("api_key")
DEEPSEEK_MODEL   = os.getenv("model", "deepseek-chat")
MAX_HISTORY      = int(os.getenv("MAX_HISTORY", "10"))
# ─────────────────────────────────────────────────────


def fetch_articles(rss_url: str, max_count: int = 10, source_name: str = "",
                   keep_html: bool = False) -> tuple[list[dict], list[dict]]:
    """从单个 RSS 源抓取最多 max_count 条，返回 (今日列表, 历史列表)。
    keep_html=True 时保留完整 HTML 正文供深度分析使用。
    """
    feed = feedparser.parse(rss_url)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS解析失败: {rss_url}")

    from urllib.parse import urlparse
    source_name = source_name or feed.feed.get("title", "") or urlparse(rss_url).netloc

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    today, history = [], []

    for entry in feed.entries[:max_count]:
        published = None
        if getattr(entry, "published_parsed", None):
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

        is_today = published is not None and published > cutoff

        if keep_html:
            if hasattr(entry, "content") and entry.content:
                raw = entry.content[0].value
            else:
                raw = getattr(entry, "summary", "") or ""
            article_summary = raw  # 保留完整 HTML
        else:
            raw_summary = getattr(entry, "summary", "") or ""
            article_summary = re.sub(r"<[^>]+>", "", raw_summary).strip()[:600]

        article = {
            "title":     entry.get("title", "No Title"),
            "summary":   article_summary,
            "url":       entry.get("link", ""),
            "published": published.strftime("%Y-%m-%d %H:%M UTC") if published else "Unknown",
            "source":    source_name,
        }

        if is_today:
            today.append(article)
        else:
            history.append(article)

    return today, history


def fetch_all_articles(sources) -> tuple[list[dict], list[dict], list[dict]]:
    """[并发优化版] 从多个 RSS 源并发抓取并合并，返回 (深度报告列表, 普通列表, 历史列表)。"""
    deep_today, regular_today, all_history = [], [], []

    def fetch_single(source):
        name, url, count = source[0], source[1], source[2]
        is_deep = len(source) > 3 and bool(source[3])
        try:
            t, h = fetch_articles(url, count, source_name=name, keep_html=is_deep)
            return is_deep, t, h
        except Exception as e:
            print(f"[警告] 抓取失败 {name} ({url}): {e}", flush=True)
            return is_deep, [], []

    import concurrent.futures
    # 并发执行网络抓取，最大并发度为数据源个数或 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(sources) or 4, 8)) as executor:
        results = list(executor.map(fetch_single, sources))

    for is_deep, t, h in results:
        if is_deep:
            deep_today.extend(t)
        else:
            regular_today.extend(t)
        all_history.extend(h)

    return deep_today, regular_today, all_history


def _robust_json_loads(raw: str) -> dict | list:
    """[鲁棒性优化版] 容错解析大模型输出的 JSON"""
    raw = raw.strip()

    # 1. 尝试直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2. 如果包含 Markdown 代码块，提取内容
    if "```" in raw:
        blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
        for block in blocks:
            try:
                return json.loads(block.strip())
            except json.JSONDecodeError:
                pass

    # 3. 寻找第一个 [ 且最后一个 ] 或者是第一个 { 且最后一个 }
    array_match = re.search(r"(\[[\s\S]*\])", raw)
    if array_match:
        try:
            return json.loads(array_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    object_match = re.search(r"(\{[\s\S]*\})", raw)
    if object_match:
        try:
            return json.loads(object_match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 4. 使用 ast.literal_eval 兜底解析（适合包含尾部逗号、单引号等语法的伪 JSON）
    try:
        import ast
        parsed = ast.literal_eval(raw)
        return json.loads(json.dumps(parsed))
    except Exception:
        pass

    raise json.JSONDecodeError("无法通过任何鲁棒手段解析 JSON", raw, 0)


def _create_chat_completion(client, model, max_tokens, messages, require_json=True, timeout=120):
    """包装 API 调用，支持 JSON 模式并能安全降级"""
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "timeout": timeout,
    }
    if require_json:
        try:
            # 尝试启用 JSON Mode
            response = client.chat.completions.create(
                response_format={"type": "json_object"},
                **kwargs
            )
            return response
        except Exception as e:
            print(f"[警告] JSON Mode 不可用，降级为普通模式: {e}", flush=True)
    # 降级方案
    return client.chat.completions.create(**kwargs)


BATCH_SIZE = 10  # 每批处理条数，避免输出截断


def _call_api(client, articles: list[dict]) -> list[dict]:
    """单批次 API 调用。"""
    articles_text = "\n".join(
        f"文章{i}:\n标题: {a['title']}\n摘要: {a['summary']}\nURL: {a['url']}\n发布: {a['published']}\n---"
        for i, a in enumerate(articles, 1)
    )
    prompt = (
        "你是一位专业的投资分析师，精通中英文金融市场。\n"
        "请对以下投资新闻逐条处理（中文文章直接保留标题，英文文章翻译为中文）：\n"
        "1. 输出准确流畅的中文标题\n"
        "2. 用中文概括2-3句核心要点\n"
        "3. 给出1-2句专业投资建议或风险提示\n"
        "4. 给出一个简短的中文分类标签（如：宏观经济、科技股、大宗商品、个人理财、政策监管、市场动态 等，3-6字）\n\n"
        "以 JSON 数组返回，每个元素字段：\n"
        "- title_cn   : 中文标题\n"
        "- category   : 分类标签（中文，3-6字）\n"
        "- key_points : 核心要点（中文，2-3句）\n"
        "- advice     : 投资建议/风险提示（中文，1-2句）\n"
        "- url        : 原始URL（原样保留）\n"
        "- published  : 发布时间（原样保留）\n\n"
        "只返回 JSON 数组，不要任何其他内容。\n\n---\n"
        + articles_text
    )

    response = _create_chat_completion(
        client,
        DEEPSEEK_MODEL,
        8192,
        [{"role": "user", "content": prompt}],
        require_json=True,
        timeout=120,
    )
    msg = response.choices[0].message
    raw = (msg.content or getattr(msg, "reasoning_content", "") or "").strip()

    return _robust_json_loads(raw)


def _call_api_deep(client, article: dict) -> dict:
    """深度分析单篇文章：提取全部新闻条目，按板块分组，逐条给出摘要和投资分析。"""
    html = article.get("summary", "")
    # 清理 HTML：去掉脚本、样式、base64 图片，保留文字结构
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'src="data:[^"]*"', "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()[:12000]

    prompt = (
        "你是专业投资分析师。以下财经简报正文，请提取所有新闻条目，按板块分组。\n\n"
        "每条新闻输出：\n"
        "- source: 来源机构（简短）\n"
        "- tags: 1-3个关键数据标签（如'增长19.5%'）\n"
        "- summary: 1-2句核心事实，关键数字用**...**包裹\n"
        "- analysis: 1句投资分析\n"
        "股指板块额外输出indices:[{name,value,change,up}]\n\n"
        "只返回JSON，格式：\n"
        '{"sections":[{"name":"板块名","indices":[...],"items":[{"source":"","tags":[],"summary":"","analysis":""}]}]}\n\n'
        f"标题：{article.get('title', '')}\n正文：\n{text}"
    )

    response = _create_chat_completion(
        client,
        DEEPSEEK_MODEL,
        32768,
        [{"role": "user", "content": prompt}],
        require_json=True,
        timeout=180,
    )
    msg = response.choices[0].message
    raw = msg.content or ""
    # 推理模型实际输出可能在 reasoning_content
    if not raw.strip():
        raw = getattr(msg, "reasoning_content", "") or ""
    raw = raw.strip()

    if not raw:
        raise ValueError("API 返回内容为空。")

    result = _robust_json_loads(raw)

    return {
        "title_cn":  article.get("title", "（无标题）"),
        "url":       article.get("url", ""),
        "published": article.get("published", ""),
        "source":    article.get("source", ""),
        "sections":  result.get("sections", []),
    }


def _progress_bar(done: int, total: int, width: int = 20) -> str:
    filled = int(width * done / total) if total else width
    return f"{'█' * filled}{'░' * (width - filled)} {int(100 * done / total) if total else 100}%"


_PROGRESS_PREFIX = "[PROGRESS]"
_SPINNERS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def translate_and_summarize(articles: list[dict]) -> list[dict]:
    """[并发优化版] 并发处理多批次 API 调用，并提供平滑的进度展示。"""
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    total = len(articles)
    total_batches = (total - 1) // BATCH_SIZE + 1

    # 拆分 Batch
    batches = [articles[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
    results = [None] * len(batches)

    lock = threading.Lock()
    completed_batches = 0

    # 打印初始状态
    bar = _progress_bar(0, total_batches, width=20)
    print(f"{_PROGRESS_PREFIX}  翻译分析 {bar} 批次 0/{total_batches} ⠋ 启动中...", flush=True)

    def process_batch(idx, batch):
        nonlocal completed_batches
        try:
            batch_result = _call_api(client, batch)
            results[idx] = batch_result
        except Exception as e:
            print(f"[警告] 批次 {idx+1} 分析失败: {e}", flush=True)
            results[idx] = []

        with lock:
            completed_batches += 1
            bar_str = _progress_bar(completed_batches, total_batches, width=20)
            print(f"{_PROGRESS_PREFIX}  翻译分析 {bar_str} 批次 {completed_batches}/{total_batches} ✅ 阶段推进", flush=True)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(batches) or 2, 4)) as executor:
        executor.map(lambda pair: process_batch(pair[0], pair[1]), enumerate(batches))

    print(f"{_PROGRESS_PREFIX}  翻译分析 {_progress_bar(total_batches, total_batches)} 批次 {total_batches}/{total_batches} ✅ 完成", flush=True)
    print("", flush=True)

    # 合并结果
    flattened = []
    for r in results:
        if not r:
            continue
        # 防止 AI 返回 JSON 对象而非数组导致 extend 遍历 dict 的 key
        if isinstance(r, dict):
            flattened.append(r)
        elif isinstance(r, list):
            flattened.extend(r)
        else:
            print(f"[警告] 批次返回了非预期的类型: {type(r).__name__}", flush=True)
    return flattened


def translate_and_summarize_deep(articles: list[dict]) -> list[dict]:
    """[并发性能优化版] 多篇微信文章并发调用 API，线程安全进度推送"""
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    results = [None] * len(articles)
    total = len(articles)

    lock = threading.Lock()
    completed = 0

    # 初始状态
    print(f"{_PROGRESS_PREFIX}  深度分析 {_progress_bar(0, total)} 第 0/{total} 篇 ⠋ 启动并发...", flush=True)

    def process_one(idx, article):
        nonlocal completed
        try:
            result = _call_api_deep(client, article)
            results[idx] = result
        except Exception as e:
            err = str(e)[:300]
            print(f"[警告] 深度分析失败 {article.get('title', '')}: {err}", flush=True)
            results[idx] = {
                "title_cn": article.get("title", "（无标题）"),
                "url":      article.get("url", ""),
                "published": article.get("published", ""),
                "source":   article.get("source", ""),
                "sections": [{
                    "name": "⚠️ 分析出错",
                    "items": [{
                        "source": "系统提示",
                        "tags":   ["分析失败"],
                        "summary": f"深度分析调用失败，错误信息：**{err}**",
                        "analysis": "请检查 API 配置或网络连接，然后重试。"
                    }]
                }],
            }

        with lock:
            completed += 1
            bar = _progress_bar(completed, total, width=20)
            print(f"{_PROGRESS_PREFIX}  深度分析 {bar} 第 {completed}/{total} 篇 ✅ 完成", flush=True)

    import concurrent.futures
    # 限制并发度为 5，防止 API 触发高频限流或消耗过多瞬时连接
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(total, 5)) as executor:
        executor.map(lambda pair: process_one(pair[0], pair[1]), enumerate(articles))

    print(f"{_PROGRESS_PREFIX}  深度分析 {_progress_bar(total, total)} 全部 {total} 篇 ✅ 并发完成", flush=True)
    print("", flush=True)
    return results


_BORDER_COLORS   = ["#c62828", "#1565c0", "#e65100", "#6a1b9a", "#00695c", "#f57f17"]
_SECTION_COLORS  = ["#1565c0", "#00695c", "#6a1b9a", "#e65100", "#c62828", "#f57f17"]
_UP_WORDS   = ("增长", "上涨", "新高", "扩大", "增加", "突破", "创历史", "涨", "增", "+")
_DOWN_WORDS = ("下降", "下跌", "风险", "警惕", "收缩", "减少", "跌", "亏损", "下滑", "-")


def _md_bold(text: str) -> str:
    """将 **...** 转为红色加粗 HTML。"""
    return re.sub(r"\*\*([^*]+)\*\*", r'<strong style="color:#c62828">\1</strong>', text)


def _tag_style(tag: str) -> tuple[str, str]:
    """根据 tag 内容自动选配色，返回 (background, color)。"""
    if any(w in tag for w in _UP_WORDS):
        return "#e8f5e9", "#2e7d32"
    if any(w in tag for w in _DOWN_WORDS):
        return "#fce4ec", "#c62828"
    if any(w in tag for w in ("注意", "警告", "关注", "谨慎")):
        return "#fff3e0", "#e65100"
    return "#e8eaf6", "#3949ab"


def build_html(deep_articles: list[dict], regular_articles: list[dict], date_str: str) -> str:
    """[邮件客户端排版优化版] 生成今日资讯 HTML 日报，使用高兼容性的内联布局"""

    def card(idx: int, a: dict) -> str:
        border = _BORDER_COLORS[(idx - 1) % len(_BORDER_COLORS)]
        cat    = a.get("category", "")
        pub    = a.get("published", "")
        cat_badge = (
            f'<span style="background:#f0f0f0;color:#666;padding:1px 8px;'
            f'border-radius:10px;font-size:11px;margin-left:8px;display:inline-block;">{cat}</span>'
        ) if cat else ""
        return (
            f'<div style="padding:20px 22px 16px;border-left:3px solid {border};'
            f'background:#fff;border-radius:6px;margin-bottom:10px;'
            f'box-shadow:0 1px 3px rgba(0,0,0,.04);">'
            f'<div style="font-size:11px;color:#bbb;margin-bottom:8px;">'
            f'{pub}{cat_badge}</div>'
            f'<h3 style="margin:0 0 10px;font-size:17px;font-weight:700;'
            f'color:#1a1a2e;line-height:1.45;">'
            f'{a.get("title_cn","（无标题）")}</h3>'
            f'<p style="margin:0 0 12px;color:#444;line-height:1.75;font-size:14px;">'
            f'{a.get("key_points","")}</p>'
            f'<div style="padding:10px 14px;background:#fffde7;border-radius:4px;'
            f'margin-bottom:12px;border-left:3px solid #f9a825;">'
            f'<span style="font-size:13px;color:#5d4037;line-height:1.7;">'
            f'💡 {a.get("advice","")}</span></div>'
            f'<div style="font-size:12px;color:#bbb;">'
            f'<span style="background:#e8eaf6;color:#3949ab;padding:1px 8px;'
            f'border-radius:10px;font-size:11px;display:inline-block;">{a.get("source","")}</span>'
            f'&nbsp;&nbsp;<a href="{a.get("url","#")}" '
            f'style="color:#bbb;text-decoration:none;">🔗 阅读原文</a></div>'
            f'</div>'
        )

    def deep_card(a: dict) -> str:
        """深度报告卡片：排版优化，全面兼容 Outlook / Gmail / 手机自带邮箱"""
        sections_html = ""
        for si, sec in enumerate(a.get("sections", [])):
            color = _SECTION_COLORS[si % len(_SECTION_COLORS)]

            # 股指盒子：采用 display: inline-block + 百分比宽度，完美解决 Flexbox 在邮件中的脱水崩塌
            indices_html = ""
            for idx in sec.get("indices", []):
                up       = idx.get("up", True)
                chg_clr  = "#c62828" if up else "#2e7d32"
                arrow    = "▲" if up else "▼"
                indices_html += (
                    f'<div style="display:inline-block;vertical-align:top;width:22%;min-width:110px;'
                    f'background:#f5f5f5;border-radius:8px;padding:10px 14px;text-align:center;'
                    f'margin:4px 1%;box-sizing:border-box;">'
                    f'<div style="font-size:11px;color:#888;margin-bottom:4px;">'
                    f'{idx.get("name","")}</div>'
                    f'<div style="font-size:16px;font-weight:700;color:#1a1a2e;">'
                    f'{idx.get("value","")}</div>'
                    f'<div style="font-size:12px;color:{chg_clr};margin-top:2px;">'
                    f'{arrow} {idx.get("change","")}</div>'
                    f'</div>'
                )
            indices_row = (
                f'<div style="padding:6px 0 10px;text-align:left;">'
                f'{indices_html}</div>'
            ) if indices_html else ""

            # 新闻条目
            items_html = ""
            for item in sec.get("items", []):
                src = item.get("source", "")
                src_line = (
                    f'<div style="font-size:11px;font-weight:700;color:#999;'
                    f'letter-spacing:.5px;margin-bottom:5px;">{src}</div>'
                ) if src else ""

                tags_html = "".join(
                    f'<span style="display:inline-block;font-size:11px;padding:1px 8px;'
                    f'border-radius:10px;margin:0 4px 6px 0;font-weight:600;'
                    f'background:{_tag_style(t)[0]};color:{_tag_style(t)[1]};">{t}</span>'
                    for t in item.get("tags", [])
                )
                tags_row = f'<div style="margin-bottom:4px;">{tags_html}</div>' if tags_html else ""

                items_html += (
                    f'<div style="border-left:3px solid {color};padding:13px 15px 11px;'
                    f'margin:8px 0;border-radius:0 6px 6px 0;background:#fafafa;">'
                    f'{src_line}{tags_row}'
                    f'<div style="font-size:14px;color:#222;line-height:1.75;margin-bottom:8px;">'
                    f'{_md_bold(item.get("summary",""))}</div>'
                    f'<div style="font-size:13px;color:#5d4037;background:#fffde7;'
                    f'border-left:3px solid #f9a825;border-radius:0 4px 4px 0;'
                    f'padding:8px 12px;line-height:1.65;">'
                    f'<strong>💡 分析：</strong>{item.get("analysis","")}</div>'
                    f'</div>'
                )

            sections_html += (
                f'<div style="margin-bottom:4px;">'
                f'<div style="font-size:13px;font-weight:700;color:{color};'
                f'letter-spacing:1px;padding:16px 0 8px;'
                f'border-bottom:2px solid #f0f0f0;margin-bottom:2px;">'
                f'{sec.get("name","")}</div>'
                f'{indices_row}{items_html}</div>'
            )

        pub = a.get("published", "")
        pub_line = (
            f'<div style="font-size:11px;color:#bbb;margin-bottom:10px;">{pub}</div>'
        ) if pub else ""
        return (
            f'<div style="background:#fff;padding:16px 16px 14px;margin-bottom:2px;border-bottom:1px solid #f0f0f0;">'
            f'{pub_line}{sections_html}'
            f'<div style="font-size:12px;color:#bbb;margin-top:8px;">'
            f'<span style="background:#fff3e0;color:#e65100;padding:1px 8px;'
            f'border-radius:10px;font-size:11px;display:inline-block;">{a.get("source","")}</span>'
            f'&nbsp;&nbsp;<a href="{a.get("url","#")}" '
            f'style="color:#bbb;text-decoration:none;">🔗 阅读原文</a></div>'
            f'</div>'
        )

    def section_header(icon: str, title: str, count: int, header_css: str) -> str:
        return (
            f'<div style="{header_css}padding:10px 18px;border-radius:6px;'
            f'margin-bottom:12px;font-weight:600;font-size:14px;">'
            f'{icon} {title}'
            f'<span style="font-size:12px;font-weight:400;opacity:.7;margin-left:8px;">'
            f'共 {count} 条</span></div>'
        )

    body = ""

    if deep_articles:
        hdr = section_header(
            "📰", "深度报告", len(deep_articles),
            "background:linear-gradient(90deg,#bf360c,#e64a19);color:#fff;"
        )
        cards = "".join(deep_card(a) for a in deep_articles)
        body += (
            f'<div style="padding:16px 8px 0;">{hdr}</div>'
            f'<div style="margin:0 8px 12px;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.04);">'
            f'{cards}</div>'
        )

    if regular_articles:
        hdr = section_header(
            "📋", "扩展阅读", len(regular_articles),
            "background:#eceff1;color:#546e7a;border-left:3px solid #90a4ae;"
        )
        cards = "".join(card(i + 1, a) for i, a in enumerate(regular_articles))
        body += f'<div style="padding:16px 8px 4px;">{hdr}{cards}</div>'

    if not body:
        body = '<div style="padding:32px 16px;color:#aaa;text-align:center;">今日暂无新资讯</div>'

    total = len(deep_articles) + len(regular_articles)
    subtitle = f'{date_str} &nbsp;·&nbsp; 共 {total} 条资讯'

    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<style>'
        'body{padding:16px 8px!important}'
        '@media(max-width:600px){'
        'body{padding:4px 0!important;background:#f0f2f5!important}'
        '.outer{border-radius:0!important;box-shadow:none!important}'
        '}'
        '</style>'
        '</head>'
        '<body style="font-family:\'PingFang SC\',\'Microsoft YaHei\',Arial,sans-serif;'
        'max-width:700px;margin:0 auto;padding:16px 8px;color:#333;background:#e9ecf2;">'
        '<div class="outer" style="border-radius:10px;overflow:hidden;'
        'box-shadow:0 2px 12px rgba(0,0,0,.08);background:#f0f2f5;">'
        '<div style="background:linear-gradient(135deg,#0d47a1,#1565c0);color:#fff;'
        'padding:24px 28px;">'
        '<div style="font-size:22px;margin-bottom:6px;">📈</div>'
        '<h1 style="margin:0 0 4px;font-size:21px;font-weight:700;letter-spacing:.5px;">'
        '每日投资日报</h1>'
        f'<p style="margin:0;font-size:12px;opacity:.7;">{subtitle}</p>'
        '</div>'
        + body
        + '</div>'
        + f'<div style="margin-top:20px;text-align:center;font-size:11px;color:#aaa;line-height:1.8;">'
        f'由 DeepSeek AI 自动生成 · {date_str}<br>'
        f'内容仅供参考，不构成投资建议，投资有风险</div>'
        '</body></html>'
    )


_WEB_SECTION_COLORS = ["#60a5fa", "#34d399", "#a78bfa", "#fb923c", "#f472b6", "#fbbf24"]


def build_html_web(deep_articles: list[dict], regular_articles: list[dict], date_str: str) -> str:
    """生成现代暗色风格的 Web 日报 HTML，适合浏览器阅读和分享（GitHub Pages）。"""

    def _tag_style_web(tag: str) -> tuple[str, str]:
        if any(w in tag for w in _UP_WORDS):
            return "rgba(52,211,153,0.18)", "#34d399"
        if any(w in tag for w in _DOWN_WORDS):
            return "rgba(244,114,182,0.18)", "#f472b6"
        if any(w in tag for w in ("注意", "警告", "关注", "谨慎")):
            return "rgba(251,146,60,0.18)", "#fb923c"
        return "rgba(129,140,248,0.18)", "#818cf8"

    def card_web(idx: int, a: dict) -> str:
        cat = a.get("category", "")
        pub = a.get("published", "")
        url = a.get("url", "#")
        cat_html = f'<span class="card-cat">{cat}</span>' if cat else ""
        pub_html = f'<span>{pub}</span>' if pub else ""
        advice   = a.get("advice", "")
        return (
            f'<div class="card">'
            f'<div class="card-top">'
            f'<div class="card-num">{idx:02d}</div>'
            f'<div class="card-title"><a href="{url}" target="_blank" rel="noopener">'
            f'{a.get("title_cn", "（无标题）")}</a></div>'
            f'</div>'
            f'<div class="card-body">'
            f'<div class="card-summary">{a.get("key_points", "")}</div>'
            + (f'<div class="card-advice">💡 {advice}</div>' if advice else "")
            + f'<div class="card-meta">'
            f'<span class="card-source">{a.get("source", "")}</span>'
            f'{cat_html}{pub_html}'
            f'<a href="{url}" target="_blank" rel="noopener" class="card-link">🔗 阅读原文</a>'
            f'</div></div></div>'
        )

    def deep_card_web(a: dict) -> str:
        sections_html = ""
        for si, sec in enumerate(a.get("sections", [])):
            color = _WEB_SECTION_COLORS[si % len(_WEB_SECTION_COLORS)]

            indices_html = ""
            for idx in sec.get("indices", []):
                up  = idx.get("up", True)
                cls = "up" if up else "down"
                arrow = "▲" if up else "▼"
                indices_html += (
                    f'<div class="index-box">'
                    f'<div class="index-name">{idx.get("name","")}</div>'
                    f'<div class="index-value">{idx.get("value","")}</div>'
                    f'<div class="index-change {cls}">{arrow} {idx.get("change","")}</div>'
                    f'</div>'
                )
            indices_row = f'<div class="indices">{indices_html}</div>' if indices_html else ""

            items_html = ""
            for item in sec.get("items", []):
                src = item.get("source", "")
                src_line = f'<div class="deep-item-source">{src}</div>' if src else ""
                tags_html = "".join(
                    f'<span class="deep-item-tag" style="background:{_tag_style_web(t)[0]};color:{_tag_style_web(t)[1]};">{t}</span>'
                    for t in item.get("tags", [])
                )
                tags_row = f'<div class="deep-item-tags">{tags_html}</div>' if tags_html else ""
                analysis = item.get("analysis", "")
                items_html += (
                    f'<div class="deep-item" style="border-left:3px solid {color};">'
                    f'{src_line}{tags_row}'
                    f'<div class="deep-item-summary">{_md_bold(item.get("summary",""))}</div>'
                    + (f'<div class="deep-item-analysis"><strong>💡 分析：</strong>{analysis}</div>' if analysis else "")
                    + f'</div>'
                )

            sections_html += (
                f'<div class="deep-section">'
                f'<div class="deep-section-title" style="color:{color};">{sec.get("name","")}</div>'
                f'{indices_row}{items_html}</div>'
            )

        pub = a.get("published", "")
        src = a.get("source", "")
        url = a.get("url", "#")
        meta_parts = [p for p in [pub, src] if p]
        meta_html = f'<div class="deep-meta">{" · ".join(meta_parts)}</div>' if meta_parts else ""
        return (
            f'<div class="deep-card">'
            f'{meta_html}{sections_html}'
            f'<div style="margin-top:8px;font-size:12px;color:var(--text-dim);">'
            f'<a href="{url}" target="_blank" rel="noopener" class="card-link">🔗 阅读原文</a>'
            f'</div></div>'
        )

    body = ""
    if deep_articles:
        cards = "".join(deep_card_web(a) for a in deep_articles)
        body += (
            f'<div class="section">'
            f'<div class="section-header">'
            f'<div class="section-dot" style="background:#fb923c;"></div>'
            f'<div class="section-title">深度报告</div>'
            f'<div class="section-count">{len(deep_articles)} 条</div>'
            f'</div>{cards}</div>'
        )
    if regular_articles:
        cards = "".join(card_web(i + 1, a) for i, a in enumerate(regular_articles))
        body += (
            f'<div class="section">'
            f'<div class="section-header">'
            f'<div class="section-dot" style="background:#818cf8;"></div>'
            f'<div class="section-title">扩展阅读</div>'
            f'<div class="section-count">{len(regular_articles)} 条</div>'
            f'</div>{cards}</div>'
        )
    if not body:
        body = '<div style="padding:60px 0;text-align:center;color:var(--text-dim);">今日暂无新资讯</div>'

    total = len(deep_articles) + len(regular_articles)
    css = (
        ":root{--primary:#6366f1;--primary-light:#818cf8;--primary-dark:#4f46e5;"
        "--bg:#0f1117;--bg-card:#1a1d2e;--bg-card-hover:#222640;"
        "--text:#e2e8f0;--text-muted:#94a3b8;--text-dim:#64748b;"
        "--border:#2d3148;--radius:12px}"
        "*{margin:0;padding:0;box-sizing:border-box}"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Noto Sans SC',sans-serif;"
        "background:var(--bg);color:var(--text);line-height:1.7;min-height:100vh}"
        ".container{max-width:860px;margin:0 auto;padding:40px 24px 60px}"
        ".header{text-align:center;margin-bottom:48px;padding-bottom:36px;border-bottom:1px solid var(--border)}"
        ".header-badge{display:inline-block;background:linear-gradient(135deg,var(--primary),var(--primary-light));"
        "color:#fff;font-size:12px;font-weight:700;letter-spacing:2px;padding:4px 16px;"
        "border-radius:20px;margin-bottom:16px}"
        ".header h1{font-size:30px;font-weight:800;background:linear-gradient(135deg,#fff,var(--primary-light));"
        "-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:8px}"
        ".header .meta{font-size:14px;color:var(--text-muted)}"
        ".section{margin-bottom:40px}"
        ".section-header{display:flex;align-items:center;gap:12px;margin-bottom:16px;"
        "padding-bottom:12px;border-bottom:1px solid var(--border)}"
        ".section-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}"
        ".section-title{font-size:18px;font-weight:700;color:#fff}"
        ".section-count{font-size:12px;color:var(--text-dim);background:var(--bg);"
        "border:1px solid var(--border);padding:2px 10px;border-radius:12px;margin-left:auto}"
        ".card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);"
        "padding:18px 22px;margin-bottom:10px;transition:background .2s,border-color .2s,transform .15s}"
        ".card:hover{background:var(--bg-card-hover);border-color:var(--primary-dark);transform:translateY(-1px)}"
        ".card-top{display:flex;align-items:flex-start;gap:12px;margin-bottom:10px}"
        ".card-num{font-size:13px;font-weight:700;color:var(--primary-light);flex-shrink:0;min-width:24px;padding-top:2px}"
        ".card-title{font-size:16px;font-weight:600;color:#fff;line-height:1.5}"
        ".card-title a{color:#fff;text-decoration:none;border-bottom:1px solid transparent;transition:border-color .2s}"
        ".card-title a:hover{border-bottom-color:var(--primary-light)}"
        ".card-body{margin-left:36px}"
        ".card-summary{font-size:14px;color:var(--text-muted);line-height:1.7;margin-bottom:8px}"
        ".card-advice{font-size:13px;color:#fbbf24;background:rgba(251,191,36,.08);"
        "border-left:3px solid #fbbf24;padding:8px 12px;border-radius:0 6px 6px 0;margin-bottom:10px;line-height:1.65}"
        ".card-meta{display:flex;align-items:center;flex-wrap:wrap;gap:8px;font-size:12px;color:var(--text-dim)}"
        ".card-source{background:rgba(99,102,241,.12);color:var(--primary-light);padding:2px 8px;border-radius:6px;font-weight:500}"
        ".card-cat{background:rgba(148,163,184,.1);color:var(--text-dim);padding:2px 8px;border-radius:6px}"
        ".card-link{color:var(--text-dim);text-decoration:none}"
        ".card-link:hover{color:var(--primary-light)}"
        ".deep-card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--radius);padding:20px 24px;margin-bottom:10px}"
        ".deep-meta{font-size:11px;color:var(--text-dim);margin-bottom:12px}"
        ".deep-section{margin-bottom:18px}"
        ".deep-section-title{font-size:13px;font-weight:700;letter-spacing:.5px;"
        "padding-bottom:8px;border-bottom:1px solid var(--border);margin-bottom:10px}"
        ".indices{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}"
        ".index-box{background:rgba(255,255,255,.03);border:1px solid var(--border);"
        "border-radius:8px;padding:10px 14px;text-align:center;min-width:90px;flex:1}"
        ".index-name{font-size:11px;color:var(--text-dim);margin-bottom:4px}"
        ".index-value{font-size:15px;font-weight:700;color:#fff}"
        ".index-change{font-size:12px;margin-top:2px}"
        ".up{color:#f472b6}.down{color:#34d399}"
        ".deep-item{border-radius:0 8px 8px 0;padding:12px 14px 10px;margin-bottom:8px;background:rgba(255,255,255,.02)}"
        ".deep-item-source{font-size:11px;font-weight:700;color:var(--text-dim);letter-spacing:.5px;margin-bottom:4px}"
        ".deep-item-tags{margin-bottom:6px}"
        ".deep-item-tag{display:inline-block;font-size:11px;padding:1px 8px;border-radius:10px;margin:0 4px 4px 0;font-weight:600}"
        ".deep-item-summary{font-size:14px;color:var(--text);line-height:1.75;margin-bottom:8px}"
        ".deep-item-analysis{font-size:13px;color:#fbbf24;background:rgba(251,191,36,.07);"
        "border-left:3px solid #fbbf24;border-radius:0 4px 4px 0;padding:7px 12px;line-height:1.65}"
        ".footer{text-align:center;margin-top:60px;padding-top:24px;border-top:1px solid var(--border);"
        "font-size:13px;color:var(--text-dim)}"
        "@media(max-width:640px){"
        ".container{padding:20px 14px 40px}"
        ".header h1{font-size:22px}"
        ".card-body{margin-left:0;margin-top:6px}"
        ".card-top{flex-direction:column;gap:2px}"
        ".indices{gap:6px}"
        ".index-box{min-width:80px}}"
    )
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        f'<title>每日投资日报 · {date_str}</title>'
        f'<style>{css}</style>'
        '</head><body><div class="container">'
        '<div class="header">'
        '<div class="header-badge">Daily Investment Report</div>'
        f'<h1>📈 每日投资日报</h1>'
        f'<div class="meta">{date_str} &nbsp;·&nbsp; 共 {total} 条资讯</div>'
        '</div>'
        + body
        + '<div class="footer">'
        f'由 DeepSeek AI 自动生成 · {date_str}<br>'
        '内容仅供参考，不构成投资建议，投资有风险'
        '</div></div></body></html>'
    )


def publish_to_github(html: str, date_str_file: str) -> str:
    """将 Web 版日报上传到 GitHub Pages，返回可分享的 URL。

    环境变量：
      GITHUB_TOKEN     — Personal Access Token（需 repo / contents:write）
      GITHUB_REPO      — 格式 'username/repo'
      GITHUB_PAGES_URL — 可选，自定义域名；留空则自动推导
    """
    import urllib.request
    import urllib.error
    import base64

    token = os.getenv("GITHUB_TOKEN", "").strip()
    repo  = os.getenv("GITHUB_REPO",  "").strip()
    if not token or not repo:
        raise ValueError("请在配置设置中填写 GITHUB_TOKEN 和 GITHUB_REPO")

    parts = repo.split("/", 1)
    if len(parts) != 2 or not parts[1]:
        raise ValueError(f"GITHUB_REPO 格式应为 'username/repo'，当前值：{repo!r}")
    owner, repo_name = parts

    pages_url = (
        os.getenv("GITHUB_PAGES_URL", "").strip().rstrip("/")
        or f"https://{owner}.github.io/{repo_name}"
    )

    content_b64 = base64.b64encode(html.encode("utf-8")).decode()
    common_headers = {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github.v3+json",
        "Content-Type":  "application/json",
        "User-Agent":    "RSS-DailyReport/1.0",
    }

    def _put(path: str) -> None:
        api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
        # 获取现有文件的 SHA（更新时必须传入）
        sha = ""
        get_req = urllib.request.Request(api_url, headers=common_headers)
        try:
            with urllib.request.urlopen(get_req, timeout=15) as r:
                sha = json.loads(r.read()).get("sha", "")
        except urllib.error.HTTPError:
            pass  # 文件不存在则 sha 留空

        payload: dict = {
            "message": f"Daily report {date_str_file}",
            "content": content_b64,
        }
        if sha:
            payload["sha"] = sha

        put_req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            method="PUT",
            headers=common_headers,
        )
        with urllib.request.urlopen(put_req, timeout=30) as r:
            r.read()

    _put(f"docs/{date_str_file}.html")   # 归档版（按日期）
    _put("docs/index.html")              # 最新版（永久链接）

    return f"{pages_url}/"


def send_email(html_content: str, date_str: str, today_count: int = 0) -> None:
    today_tag = f" · 今日{today_count}条" if today_count else ""
    recipients = [addr.strip() for addr in EMAIL_TO.replace("，", ",").replace("；", ";").replace(";", ",").split(",") if addr.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📈 每日投资日报 {date_str}{today_tag}"
    msg["From"]    = EMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, recipients, msg.as_string())


def main() -> None:
    date_str = datetime.now().strftime("%Y年%m月%d日")
    log = lambda msg: print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

    missing = [k for k in ("api_key", "EMAIL_PASSWORD") if not os.getenv(k)]
    if missing:
        log(f"缺少环境变量: {', '.join(missing)}，请检查 .env 文件")
        sys.exit(1)

    log(f"正在抓取 {len(RSS_SOURCES)} 个 RSS 源...")
    deep_today, regular_today, _ = fetch_all_articles(RSS_SOURCES)
    total_today = len(deep_today) + len(regular_today)
    log(f"共 {total_today} 条今日资讯（深度 {len(deep_today)}，扩展 {len(regular_today)}）")

    if not total_today:
        log("没有找到文章，退出")
        sys.exit(0)

    deep_proc, regular_proc = [], []

    if deep_today:
        log(f"深度分析 {len(deep_today)} 篇公众号文章（正在进行并发提取）...")
        deep_proc = translate_and_summarize_deep(deep_today)

    if regular_today:
        log(f"调用 DeepSeek API 分析 {len(regular_today)} 条扩展资讯（批次并发处理中）...")
        regular_proc = translate_and_summarize(regular_today)
        url_to_source = {a["url"]: a["source"] for a in regular_today}
        for p in regular_proc:
            p["source"] = url_to_source.get(p.get("url", ""), "")

    publish_email  = os.getenv("PUBLISH_EMAIL",  "true").lower()  == "true"
    publish_github = os.getenv("PUBLISH_GITHUB", "false").lower() == "true"

    if not publish_email and not publish_github:
        log("⚠️ 未启用任何发布方式，请在「配置设置」中至少勾选一种")
        sys.exit(0)

    date_file = datetime.now().strftime("%Y-%m-%d")

    if publish_email:
        log("生成邮件版日报...")
        html = build_html(deep_proc, regular_proc, date_str)
        recipients = [a.strip() for a in EMAIL_TO.replace("，", ",").replace("；", ";").replace(";", ",").split(",") if a.strip()]
        log(f"发送邮件至 {len(recipients)} 位收件人: {', '.join(recipients)}...")
        send_email(html, date_str, len(deep_proc) + len(regular_proc))
        log("✅ 邮件已成功发送！")

    if publish_github:
        log("生成 Web 版日报（暗色样式）并发布到 GitHub Pages...")
        try:
            web_html = build_html_web(deep_proc, regular_proc, date_str)
            url = publish_to_github(web_html, date_file)
            log(f"🌐 已发布到: {url}")
        except Exception as e:
            log(f"[警告] GitHub Pages 发布失败: {e}")


if __name__ == "__main__":
    main()
