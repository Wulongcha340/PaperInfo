#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenAlex 论文自动匹配脚本（增强版）

功能：
1. 支持通过命令行直接指定搜索关键词和期刊
2. 支持从 JSON 配置文件读取关键词和期刊（向后兼容）
3. 支持期刊名称自动映射到 OpenAlex Source ID
4. 自动下载开源论文 PDF
5. 生成论文信息报告

作者：基于 search_papers.py 增强
"""

import argparse
import json
import logging
import os
import re
import requests
from pyalex import config
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

# ============================================================================
# 第一部分：配置和常量
# ============================================================================

# 配置 OpenAlex API
# OpenAlex 要求提供邮箱以提升 API 速率限制
config.email = os.getenv("OPENALEX_EMAIL", "wulongcha340@outlook.com")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # 用于向 GitHub 提交 Issue（可选）
REPO_NAME = os.getenv("GITHUB_REPOSITORY")  # GitHub 仓库名（可选）

# 状态文件路径：记录已处理过的论文，避免重复
STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

# 默认搜索时间窗口：14天
DEFAULT_WINDOW_DAYS = 14

# 报告展示窗口：始终显示最近7天（含结束日）
REPORT_WINDOW_DAYS = 7

# state.json 中最多保留的论文数量，超出时删除最早的，以提升匹配速度
MAX_SEEN_KEYS = 500

# OpenAlex 单页条数与分页上限（防止意外全量拉取过大）
OPENALEX_PER_PAGE = 200
MAX_WORKS_PER_KEYWORD = 2000

# 下载行为控制
MAX_URL_ATTEMPTS = 3
DOWNLOAD_WORKERS = 3
MIN_PDF_BYTES = 10 * 1024

logger = logging.getLogger("search_papers_enhanced")


# ============================================================================
# 第二部分：工具函数 - 文件操作和状态管理
# ============================================================================

def _ensure_dir(path: str) -> None:
    """
    确保目录存在，如果不存在则创建
    
    参数：
        path: 目录路径
    """
    os.makedirs(path, exist_ok=True)


def setup_logging(level: str = "INFO") -> None:
    """初始化日志。"""
    level_name = (level or "INFO").upper()
    log_level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _load_state():
    """
    加载 state.json 文件，读取已处理过的论文记录
    
    返回：
        包含 seen_keys（已处理论文列表）和 last_run_date（最后运行日期）的字典
        如果文件不存在或格式错误，返回空状态
    
    说明：
        state.json 用于记录已进入过周报的论文，避免在长时间窗口下重复出现
        论文的唯一标识优先使用 DOI，如果没有 DOI 则使用 OpenAlex Work ID
    """
    if not os.path.exists(STATE_PATH):
        return {"seen_keys": [], "seen_items": [], "last_run_date": None}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("seen_keys"), list):
            data.setdefault("seen_items", [])
            return data
    except Exception:
        pass
    return {"seen_keys": [], "seen_items": [], "last_run_date": None}


def _save_state(state):
    """
    保存状态到 state.json 文件
    
    参数：
        state: 包含 seen_keys 和 last_run_date 的字典
    """
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("state.json 写入失败: %s", e)


def _resolve_path(path: str, default_name: str) -> str:
    """
    解析文件路径：如果是相对路径，则相对于脚本所在目录
    
    参数：
        path: 用户提供的路径（可能是相对路径、绝对路径或 None）
        default_name: 如果 path 为空，使用的默认文件名
    
    返回：
        绝对路径
    """
    if not path:
        path = os.path.join(os.path.dirname(__file__), default_name)
    elif not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), path)
    return os.path.abspath(path)


def load_config(file_name):
    """
    加载 JSON 配置文件（关键词或期刊配置）
    
    参数：
        file_name: JSON 文件路径
    
    返回：
        解析后的 JSON 数据（字典）
    """
    with open(file_name, 'r', encoding='utf-8') as f:
        return json.load(f)


def _validate_journals_config(journals_data: dict) -> None:
    """校验 journals.json 结构。"""
    if not isinstance(journals_data, dict):
        raise ValueError("journals.json 必须是对象，格式为 {source_id: display_name}")
    if not journals_data:
        raise ValueError("journals.json 不能为空")
    for source_id, display_name in journals_data.items():
        if not isinstance(source_id, str) or not re.match(r"^S\d+$", source_id):
            raise ValueError(f"无效 Source ID: {source_id}")
        if not isinstance(display_name, str):
            raise ValueError(f"期刊名称必须是字符串: {source_id}")


def _validate_keywords_config(keywords_data: dict) -> None:
    """校验 keywords.json 结构。"""
    if not isinstance(keywords_data, dict):
        raise ValueError("keywords.json 必须是对象")
    for name in ("broad_keywords", "refine_keywords", "active_keywords"):
        values = keywords_data.get(name)
        if values is None:
            continue
        if not isinstance(values, list):
            raise ValueError(f"{name} 必须是字符串数组")
        for item in values:
            if not isinstance(item, str):
                raise ValueError(f"{name} 包含非字符串元素: {item}")


def _extract_seen_list(state: dict):
    """
    统一解析 state 里的历史键顺序。
    优先使用 seen_items（带时间戳），否则回退 seen_keys。
    """
    seen_items = state.get("seen_items")
    if isinstance(seen_items, list) and seen_items:
        out = []
        for item in seen_items:
            if isinstance(item, dict):
                key = item.get("key")
                if isinstance(key, str):
                    out.append(key)
        if out:
            return out
    return [k for k in state.get("seen_keys", []) if isinstance(k, str)]


# ============================================================================
# 第三部分：期刊名称映射功能（新增）
# ============================================================================

def _resolve_journal_id(journal_input: str, journals_data: dict) -> str:
    """
    将用户输入的期刊标识解析为 OpenAlex Source ID
    
    功能：
        1. 如果输入已经是 Source ID（如 S36624081），直接返回
        2. 如果输入是期刊名称（如 "GRL"、"Nature"），从 journals_data 中查找匹配的 Source ID
        3. 支持精确匹配和部分匹配
    
    参数：
        journal_input: 用户输入的期刊标识（可能是 Source ID 或期刊名称）
        journals_data: 从 journals.json 加载的字典，格式为 {source_id: display_name}
    
    返回：
        OpenAlex Source ID（如 "S36624081"），如果找不到则返回 None
    
    示例：
        _resolve_journal_id("GRL", {"S36624081": "GRL"}) -> "S36624081"
        _resolve_journal_id("S36624081", {...}) -> "S36624081"
        _resolve_journal_id("Invalid", {...}) -> None
    """
    # 步骤1：检查是否已经是完整的 Source ID
    # OpenAlex Source ID 格式：S + 数字（如 S36624081）
    if journal_input.startswith("S") and journal_input[1:].isdigit():
        return journal_input
    
    # 步骤2：构建反向映射：期刊显示名称 -> Source ID
    # journals_data 格式：{"S36624081": "GRL", "S3880285": "Science", ...}
    # 我们需要构建：{"GRL": "S36624081", "SCIENCE": "S3880285", ...}
    name_to_id = {}
    for source_id, display_name in journals_data.items():
        if display_name:
            # 将期刊名称转为大写，便于匹配
            name_upper = display_name.upper()
            name_to_id[name_upper] = source_id
    
    # 步骤3：尝试精确匹配
    journal_upper = journal_input.upper()
    if journal_upper in name_to_id:
        return name_to_id[journal_upper]
    
    # 步骤4：尝试部分匹配（模糊匹配）
    # 例如：用户输入 "Nature Geoscience"，可以匹配到 "Nature Geoscience"
    # 或者用户输入 "GRL"，可以匹配到 "GRL"
    for name, sid in name_to_id.items():
        # 检查用户输入是否包含在期刊名称中，或期刊名称是否包含用户输入
        if journal_upper in name or name in journal_upper:
            return sid
    
    # 步骤5：如果都找不到，返回 None（调用者会处理并给出警告）
    return None


# ============================================================================
# 第四部分：工具函数 - 文件名和 URL 处理
# ============================================================================

def _safe_filename(title: str, suffix: str, max_len: int = 180) -> str:
    """
    将论文标题转换为安全的文件名
    
    功能：
        1. 移除特殊字符，只保留字母、数字、中文、空格、下划线、连字符
        2. 限制文件名长度
        3. 添加后缀（通常是 DOI 的一部分）以区分同名论文
    
    参数：
        title: 论文标题
        suffix: 文件名后缀（通常是 DOI 的一部分）
        max_len: 最大文件名长度（不含扩展名）
    
    返回：
        安全的文件名（带 .pdf 扩展名）
    """
    title = title or "untitled"
    # 将多个连续空格合并为单个空格
    title = re.sub(r"\s+", " ", title).strip()
    # 只保留字母、数字、中文、空格、下划线、连字符
    title = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff _-]+", "", title)
    title = title.strip(" .-_")
    if not title:
        title = "untitled"
    
    # 处理后缀
    suffix = (suffix or "").strip()
    suffix = re.sub(r"[^0-9A-Za-z._-]+", "_", suffix)
    if suffix:
        name = f"{title}__{suffix}"
    else:
        name = title
    
    # 限制长度
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .-_")
    return f"{name}.pdf"


def _normalize_doi(doi: str) -> str:
    """
    标准化 DOI 格式，移除 URL 前缀
    
    参数：
        doi: 可能是完整 URL（如 https://doi.org/10.1029/...）或纯 DOI（如 10.1029/...）
    
    返回：
        标准化的 DOI（如 10.1029/...）
    """
    if not doi or not isinstance(doi, str):
        return ""
    d = doi.strip()
    # 移除 https://doi.org/ 或 http://dx.doi.org/ 前缀
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.IGNORECASE)
    return d


def _doi_to_pdf_urls(doi: str):
    """
    根据 DOI 生成可能的 PDF 直链 URL
    
    功能：
        不同出版商有不同的 PDF URL 格式，这里根据 DOI 前缀识别出版商，
        并生成对应的 PDF URL 列表
    
    参数：
        doi: 论文的 DOI
    
    返回：
        PDF URL 列表（按优先级排序）
    
    说明：
        这些 URL 不是保证可用的，但能显著提高部分站点的成功率
        例如：PNAS、Wiley（AGU 期刊）等
    """
    d = _normalize_doi(doi)
    if not d:
        return []
    
    urls = []
    d_lower = d.lower()
    d_upper = d.upper()
    
    # PNAS 期刊：通常可以直接通过 /doi/pdf/ 访问
    if d_lower.startswith("10.1073/pnas."):
        urls.append(f"https://www.pnas.org/doi/pdf/{d}")
        urls.append(f"https://www.pnas.org/doi/epdf/{d}")
    
    # Wiley / AGU 期刊（GRL、JGR、AGU Advances 等）
    # 这些期刊的 DOI 通常以 10.1029/ 开头
    if d_lower.startswith("10.1029/"):
        urls.append(f"https://agupubs.onlinelibrary.wiley.com/doi/pdf/{d_upper}")
        urls.append(f"https://agupubs.onlinelibrary.wiley.com/doi/epdf/{d_upper}")
        urls.append(f"https://agupubs.onlinelibrary.wiley.com/doi/pdfdirect/{d_upper}")
        urls.append(f"https://onlinelibrary.wiley.com/doi/pdf/{d_upper}")
        urls.append(f"https://onlinelibrary.wiley.com/doi/epdf/{d_upper}")
        urls.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{d_upper}")
    
    return urls


def _candidate_urls(work):
    """
    收集论文所有可能的 PDF 或访问 URL
    
    功能：
        从 OpenAlex 返回的 work 对象中提取所有可能的 PDF URL，
        按优先级排序（best_oa_location > open_access > primary_location > locations）
    
    参数：
        work: OpenAlex API 返回的论文对象
    
    返回：
        URL 列表（已去重，保持优先级顺序）
    
    说明：
        其中有些 URL 可能是落地页（HTML），会在下载时被识别并跳过
    """
    urls = []
    
    def add(u):
        """辅助函数：添加 URL 到列表（如果 URL 有效且未重复）"""
        if u and isinstance(u, str) and u not in urls:
            urls.append(u)
    
    # 优先级1：最佳开放获取位置（OpenAlex 认为最可靠的 OA 链接）
    best = work.get("best_oa_location") or {}
    add(best.get("pdf_url"))
    add(best.get("url"))
    
    # 优先级2：开放获取信息中的 URL
    oa = work.get("open_access") or {}
    add(oa.get("oa_url"))
    
    # 优先级3：主要位置（primary location）
    primary = work.get("primary_location") or {}
    add(primary.get("pdf_url"))
    add(primary.get("landing_page_url"))
    
    # 优先级4：所有位置（locations 列表）
    for loc in work.get("locations") or []:
        if not loc:
            continue
        add(loc.get("pdf_url"))
        add(loc.get("landing_page_url"))
    
    # 优先级5：根据 DOI 生成的 PDF URL
    doi = work.get("doi")
    for u in _doi_to_pdf_urls(doi):
        add(u)
    add(doi)  # DOI 本身也可以作为 URL
    
    # 优先级6：OpenAlex Work ID（作为备用）
    wid = work.get("id")
    add(wid)
    
    return urls


# ============================================================================
# 第五部分：PDF 下载功能
# ============================================================================

def download_pdf(work, filename, output_dir):
    """
    尝试下载论文的 PDF 文件
    
    功能：
        1. 收集所有可能的 PDF URL
        2. 按优先级逐个尝试下载
        3. 验证下载的内容是否为 PDF（检查文件头或 Content-Type）
        4. 保存到指定目录
    
    参数：
        work: OpenAlex API 返回的论文对象
        filename: 保存的文件名
        output_dir: 输出目录
    
    返回：
        True 如果成功下载，False 如果失败
    
    说明：
        某些站点可能需要 Cookie 或登录状态，这种情况下会失败
        但脚本会尝试多个 URL，提高成功率
    """
    # 步骤1：收集所有候选 URL
    urls = _candidate_urls(work)[:MAX_URL_ATTEMPTS]
    if not urls:
        logger.info("[跳过] %s: 未找到有效的开源链接", filename)
        return False
    
    try:
        # 步骤2：确保输出目录存在
        _ensure_dir(output_dir)
        filepath = os.path.join(output_dir, filename)
        
        # 步骤3：设置 HTTP 请求头，模拟浏览器访问
        base_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
            "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        }
        
        # 步骤4：创建会话，逐个尝试 URL
        session = requests.Session()
        for u in urls:
            headers = dict(base_headers)
            # 某些站点会检查 Referer，设置它为论文的落地页
            headers["Referer"] = (work.get("primary_location") or {}).get("landing_page_url") or work.get("doi") or u
            
            # 步骤5：发送 HTTP GET 请求（流式下载，避免内存占用过大）
            response = session.get(u, headers=headers, stream=True, timeout=60, allow_redirects=True)
            
            # 步骤6：检查 HTTP 状态码
            if response.status_code != 200:
                # 常见错误：403（需要登录/Cookie）、404（不存在）、429（请求过多）
                if response.status_code in (403, 404, 429):
                    logger.info("[跳过] %s: %s 返回 HTTP %s", filename, u, response.status_code)
                    continue
                logger.info("[跳过] %s: %s 下载失败 HTTP %s", filename, u, response.status_code)
                continue
            
            # 步骤7：验证内容是否为 PDF
            # 方法1：检查 Content-Type 响应头
            content_type = (response.headers.get("Content-Type") or "").lower()
            # 方法2：检查文件头（PDF 文件以 %PDF 开头）
            it = response.iter_content(chunk_size=8192)
            first = next(it, b"")
            # 两者满足其一即可（因为有些站点 Content-Type 不准确）
            if (b"%PDF" not in first[:16]) and ("pdf" not in content_type):
                logger.info(
                    "[跳过] %s: %s 疑似非 PDF (Content-Type=%s)",
                    filename,
                    u,
                    content_type or "N/A",
                )
                continue
            
            # 步骤8：保存文件
            size = 0
            with open(filepath, 'wb') as f:
                if first:
                    f.write(first)  # 写入第一个块
                    size += len(first)
                for chunk in it:
                    if chunk:
                        f.write(chunk)  # 写入剩余块
                        size += len(chunk)

            # 增加最小文件大小校验，避免把错误页写成 PDF
            if size < MIN_PDF_BYTES:
                logger.info("[跳过] %s: 下载文件过小(%s bytes)，可能不是有效 PDF", filename, size)
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                continue
            
            logger.info("[成功] 已下载: %s", filename)
            return True
            
    except Exception as e:
        logger.warning("[错误] 下载 %s 时异常: %s", filename, e)
    
    return False


def _extract_authors(work, max_authors: int = 5) -> str:
    """提取作者名称；当作者数 > 6 时显示前5位 + ... + 最后1位。"""
    names = []
    for authorship in work.get("authorships") or []:
        author = (authorship or {}).get("author") or {}
        name = author.get("display_name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    if not names:
        return "N/A"
    if len(names) > 6:
        return ", ".join(names[:max_authors] + ["..."] + [names[-1]])
    return ", ".join(names)


def _doi_to_url(doi: str) -> str:
    """将 DOI 统一为 https://doi.org/... 链接。"""
    norm = _normalize_doi(doi)
    if not norm:
        return ""
    return f"https://doi.org/{norm}"


def _title_with_doi_link(title: str, doi: str) -> str:
    """在标题中嵌入 DOI HTML 链接（新标签页打开）。"""
    safe_title = title or "Untitled"
    doi_url = _doi_to_url(doi)
    if doi_url:
        return (
            f'<a href="{doi_url}" target="_blank" rel="noopener noreferrer">'
            f"{safe_title}</a>"
        )
    return safe_title


# ============================================================================
# 第六部分：工具函数 - 论文信息提取
# ============================================================================

def _get_source_id_and_name(work):
    """
    从 work 对象中提取期刊的 Source ID 和显示名称
    
    参数：
        work: OpenAlex API 返回的论文对象
    
    返回：
        (source_id, source_display_name) 元组
    
    说明：
        优先从 primary_location 获取，如果没有则从 locations 列表的第一个获取
    """
    # 方法1：从 primary_location 获取
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    sid = source.get("id") or ""
    sname = source.get("display_name") or ""
    
    # 方法2：如果 primary_location 没有，从 locations 列表获取
    if not sid:
        locations = work.get("locations") or []
        if locations:
            src = (locations[0] or {}).get("source") or {}
            sid = src.get("id") or ""
            sname = src.get("display_name") or sname
    
    return sid, sname


def _abstract_from_inverted_index(inv):
    """
    将 OpenAlex 的倒排索引格式摘要还原为普通文本
    
    功能：
        OpenAlex 的摘要字段通常是 abstract_inverted_index 格式：
        {"word1": [0, 5, 10], "word2": [1, 6], ...}
        表示 word1 出现在位置 0、5、10，word2 出现在位置 1、6
        我们需要将其还原为："word1 word2 word1 ..."
    
    参数：
        inv: 倒排索引字典
    
    返回：
        还原后的摘要文本
    """
    if not inv or not isinstance(inv, dict):
        return ""
    try:
        # 步骤1：找到最大位置索引
        max_pos = -1
        for positions in inv.values():
            if positions:
                max_pos = max(max_pos, max(positions))
        if max_pos < 0:
            return ""
        
        # 步骤2：创建位置数组，将单词填入对应位置
        words = [""] * (max_pos + 1)
        for w, positions in inv.items():
            for p in positions:
                if 0 <= p <= max_pos:
                    words[p] = w
        
        # 步骤3：连接所有单词
        return " ".join([w for w in words if w])
    except Exception:
        return ""


# ============================================================================
# 第七部分：核心搜索功能（增强版 - 支持命令行参数）
# ============================================================================

def _prepare_search_inputs(opts):
    """加载配置、解析参数并返回统一上下文。"""
    journals_path = _resolve_path(opts.get("journals") or "journals.json", "journals.json")
    keywords_path = _resolve_path(opts.get("keywords") or "keywords.json", "keywords.json")

    journals_data = load_config(journals_path)
    keywords_data = load_config(keywords_path)
    _validate_journals_config(journals_data)
    _validate_keywords_config(keywords_data)

    ignore_history = opts.get("ignore_history", False)
    if ignore_history:
        state = {"seen_keys": [], "seen_items": [], "last_run_date": None}
        seen_list = []
        logger.info("已忽略 state.json（--ignore-history）")
    else:
        state = _load_state()
        seen_list = _extract_seen_list(state)
    seen_set = set(seen_list)

    if opts.get("days") is not None:
        window_days = int(opts["days"])
    else:
        window_days = int(os.getenv("WINDOW_DAYS", str(DEFAULT_WINDOW_DAYS)))
        if not seen_list:
            window_days = 7
            logger.info("state.json 无历史论文，搜索窗口缩短为 %s 天", window_days)

    if opts.get("end_date"):
        end_dt = datetime.strptime(str(opts["end_date"]), "%Y-%m-%d")
    else:
        end_dt = datetime.now()

    start_date = (end_dt - timedelta(days=window_days)).strftime("%Y-%m-%d")
    today = end_dt.strftime("%Y-%m-%d")
    report_start_date = (end_dt - timedelta(days=REPORT_WINDOW_DAYS - 1)).strftime("%Y-%m-%d")

    broad_keywords_json = keywords_data.get("broad_keywords") or []
    refine_keywords_json = keywords_data.get("refine_keywords") or []
    legacy_active = keywords_data.get("active_keywords")
    if (not broad_keywords_json) and legacy_active:
        broad_keywords_json = legacy_active
        refine_keywords_json = []

    broad_keywords_cli = opts.get("broad_keywords_cli") or []
    refine_keywords_cli = opts.get("refine_keywords_cli") or []

    if broad_keywords_cli:
        broad_keywords = broad_keywords_cli
        logger.info("[提示] 使用命令行宽泛关键词: %s", broad_keywords)
    else:
        broad_keywords = broad_keywords_json
        logger.info("[提示] 使用 JSON 宽泛关键词: %s", broad_keywords)

    if refine_keywords_cli:
        refine_keywords = refine_keywords_cli
        logger.info("[提示] 使用命令行精炼关键词: %s", refine_keywords)
    else:
        refine_keywords = refine_keywords_json
        logger.info("[提示] 使用 JSON 精炼关键词: %s", refine_keywords)

    if opts.get("no_refine"):
        refine_keywords = []
        logger.info("[提示] 已启用 --no-refine：跳过二层关键词筛选")

    broad_keywords = [k for k in broad_keywords if isinstance(k, str) and k.strip()]
    refine_keywords = [k for k in refine_keywords if isinstance(k, str) and k.strip()]

    journal_ids_json = list(journals_data.keys())
    journals_cli = opts.get("journals_cli") or []
    if journals_cli:
        resolved_ids = []
        for j_input in journals_cli:
            j_id = _resolve_journal_id(j_input, journals_data)
            if j_id:
                resolved_ids.append(j_id)
                journal_display_name = journals_data.get(j_id, j_input)
                logger.info("[提示] 期刊 '%s' → Source ID: %s (%s)", j_input, j_id, journal_display_name)
            else:
                logger.warning("[警告] 无法识别期刊 '%s'，已跳过（请检查 journals.json 或使用 Source ID）", j_input)
        if resolved_ids:
            journal_ids = resolved_ids
            logger.info("[提示] 使用命令行期刊: %s", journal_ids)
        else:
            journal_ids = journal_ids_json
            logger.warning("[警告] 命令行期刊解析失败，回退到 JSON 期刊配置")
    else:
        journal_ids = journal_ids_json
        logger.info("[提示] 使用 JSON 期刊: %s", journal_ids)

    if opts.get("output"):
        week_dir = os.path.abspath(opts["output"])
    else:
        downloads_root = os.path.join(os.path.dirname(__file__), "downloads")
        week_dir = os.path.join(downloads_root, today)

    return {
        "journals_path": journals_path,
        "keywords_path": keywords_path,
        "journals_data": journals_data,
        "state": state,
        "seen_list": seen_list,
        "seen_set": seen_set,
        "ignore_history": ignore_history,
        "window_days": window_days,
        "start_date": start_date,
        "report_start_date": report_start_date,
        "today": today,
        "broad_keywords": broad_keywords,
        "refine_keywords": refine_keywords,
        "journal_ids": journal_ids,
        "journal_filter": "|".join(journal_ids),
        "week_dir": week_dir,
    }


def _print_dry_run(ctx):
    """输出 dry-run 配置。"""
    logger.info("[dry-run] 将要使用的配置与日期范围：")
    logger.info("  --keywords   %s", ctx["keywords_path"])
    logger.info("  --journals   %s", ctx["journals_path"])
    logger.info("  --days       %s（search_start=%s, end_date=%s）", ctx["window_days"], ctx["start_date"], ctx["today"])
    logger.info("  report_range = %s 至 %s（固定近%s天）", ctx["report_start_date"], ctx["today"], REPORT_WINDOW_DAYS)
    logger.info("  --output     %s", ctx["week_dir"])
    logger.info("  ignore_history = %s", ctx["ignore_history"])
    logger.info("  broad_keywords  = %s", ctx["broad_keywords"])
    logger.info("  refine_keywords = %s", ctx["refine_keywords"])
    logger.info("  journal_ids     = %s", ctx["journal_ids"])
    logger.info("[dry-run] 未请求 API，未写入任何文件。")


def _fetch_works_for_keyword(keyword, start_date, end_date, journal_filter):
    """按关键词分页拉取 OpenAlex works。"""
    url = "https://api.openalex.org/works"
    params = {
        "filter": (
            f"from_publication_date:{start_date},"
            f"to_publication_date:{end_date},"
            f"locations.source.id:{journal_filter}"
        ),
        "search": keyword,
        "per-page": OPENALEX_PER_PAGE,
        "cursor": "*",
        "mailto": config.email,
    }

    works = []
    while True:
        resp = requests.get(url, params=params, timeout=60)
        if resp.status_code != 200:
            logger.warning("  - 检索 '%s' 失败: HTTP %s", keyword, resp.status_code)
            break

        payload = resp.json()
        page_results = payload.get("results") or []
        if not page_results:
            break

        works.extend(page_results)
        if len(works) >= MAX_WORKS_PER_KEYWORD:
            works = works[:MAX_WORKS_PER_KEYWORD]
            logger.info("  - 检索 '%s' 达到上限 %s，提前停止分页", keyword, MAX_WORKS_PER_KEYWORD)
            break

        next_cursor = (payload.get("meta") or {}).get("next_cursor")
        if not next_cursor:
            break
        params["cursor"] = next_cursor

    return works


def _fetch_unique_works(broad_keywords, start_date, end_date, journal_filter):
    """第一层关键词检索并按 DOI/Work ID 去重。"""
    unique_works = {}
    logger.info("开始搜索（时间窗口：%s 至 %s）...", start_date, end_date)
    for kw in broad_keywords:
        logger.info("正在检索宽泛关键词: '%s'...", kw)
        try:
            query = _fetch_works_for_keyword(
                kw,
                start_date=start_date,
                end_date=end_date,
                journal_filter=journal_filter,
            )
            logger.info("  - 找到 %s 篇", len(query))
            for work in query:
                key = work.get("doi") or work.get("id")
                if key:
                    unique_works[key] = work
        except Exception as e:
            logger.warning("  - 检索 '%s' 时出错: %s", kw, e)
    logger.info("第一层搜索完成，共找到 %s 篇唯一论文", len(unique_works))
    return unique_works


def _apply_refine_filter(unique_works, refine_keywords):
    """第二层关键词筛选：标题+摘要匹配。"""
    if not refine_keywords or not unique_works:
        return unique_works
    refine_terms = [t.lower() for t in refine_keywords]
    filtered = {}
    for k, work in unique_works.items():
        title = work.get("display_name") or ""
        abstract = _abstract_from_inverted_index(work.get("abstract_inverted_index"))
        blob = (title + " " + abstract).lower()
        if any(term in blob for term in refine_terms):
            filtered[k] = work
    logger.info("二层筛选完成：%s → %s 篇", len(unique_works), len(filtered))
    return filtered


def _exclude_seen(unique_works, seen_set):
    """过滤已处理论文。"""
    if not unique_works:
        return unique_works
    before = len(unique_works)
    filtered = {k: w for k, w in unique_works.items() if k not in seen_set}
    logger.info("历史去重完成：%s → %s 篇（仅保留本次新增）", before, len(filtered))
    return filtered


def _build_empty_report(report_start_date, today):
    """生成无新增结果的报告文本。"""
    return (
        f"## 📅 本周论文信息 ({report_start_date} 至 {today})\n\n"
        f"在指定期刊中未发现**新增**匹配论文（可能是本期无新增，或都已在历史中出现过）。"
    )


def _work_sort_key(work, journal_index, journal_order):
    """排序键：期刊顺序 -> 发表日期倒序 -> 引用数倒序。"""
    source_id, _ = _get_source_id_and_name(work)
    raw_id = (source_id or "").split("/")[-1].upper()
    idx = journal_index.get(raw_id, len(journal_order))
    pub = (work.get("publication_date") or "")[:10]
    try:
        parts = pub.split("-")
        date_ord = -(int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])) if len(parts) >= 3 else 0
    except (ValueError, IndexError):
        date_ord = 0
    return (idx, date_ord, -(work.get("cited_by_count") or 0))


def _render_paper_block(paper_id, title_md, journal_name, authors, abstract_text):
    """统一渲染周报中的单篇论文条目。"""
    abstract_full = (abstract_text or "").strip() or "N/A"
    return (
        f"#### [{paper_id}] {title_md}\n"
        f"- **编号**: {paper_id}\n"
        f"- **期刊**: {journal_name}\n"
        f"- **作者**: {authors}\n"
        f"- **摘要**: {abstract_full}\n\n---\n"
    )


def _safe_name_component(value: str, max_len: int = 64) -> str:
    """清理文件名组件，仅保留常见安全字符。"""
    v = (value or "").strip()
    v = re.sub(r"\s+", "_", v)
    v = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", v)
    v = v.strip("._-") or "NA"
    return v[:max_len]


def _first_author_lastname_from_work(work) -> str:
    """从 work 提取第一作者姓氏。"""
    for authorship in work.get("authorships") or []:
        author = (authorship or {}).get("author") or {}
        name = (author.get("display_name") or "").strip()
        if not name:
            continue
        parts = name.split()
        return parts[-1] if parts else name
    return "UnknownAuthor"


def _download_and_render_report(works, journals_data, week_dir, report_start_date, today, broad_keywords, refine_keywords, no_download):
    """下载与报告生成阶段。"""
    report = f"## 📅 本周论文信息 ({report_start_date} 至 {today})\n\n"
    if broad_keywords:
        report += f"- **宽泛关键词**: {', '.join(broad_keywords)}\n"
    if refine_keywords:
        report += f"- **二层关键词**: {', '.join(refine_keywords)}\n"
    downloaded_list = "\n### 📥 已自动获取的 PDF\n"
    manual_list = "\n### 🔗 需手动下载 / 自动下载失败的论文\n"
    downloaded_count = 0
    processed_keys = []

    journal_order = list(journals_data.keys())
    journal_index = {k: i for i, k in enumerate(journal_order)}
    sorted_works = sorted(works.values(), key=lambda x: _work_sort_key(x, journal_index, journal_order))
    weekly_tag = today.replace("-", "")

    download_results = {}
    if not no_download:
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
            future_to_work = {}
            for idx, work in enumerate(sorted_works, start=1):
                oa_info = work.get("open_access", {})
                is_oa = oa_info.get("is_oa", False)
                if not is_oa:
                    continue
                paper_id = f"W{weekly_tag}-{idx:03d}"
                source_id, source_display_name = _get_source_id_and_name(work)
                raw_id = (source_id or "").split("/")[-1].upper()
                journal_name = journals_data.get(raw_id, "Core Journal")
                if journal_name == "Core Journal" and source_display_name:
                    journal_name = source_display_name
                first_author_lastname = _first_author_lastname_from_work(work)
                filename = (
                    f"{_safe_name_component(paper_id, max_len=24)}_"
                    f"{_safe_name_component(journal_name, max_len=48)}_"
                    f"{_safe_name_component(first_author_lastname, max_len=40)}.pdf"
                )
                future = executor.submit(download_pdf, work, filename, week_dir)
                future_to_work[future] = (work.get("doi") or work.get("id"))
            for future in as_completed(future_to_work):
                work_key = future_to_work[future]
                try:
                    download_results[work_key] = bool(future.result())
                except Exception as e:
                    logger.warning("下载任务异常: %s", e)
                    download_results[work_key] = False

    for idx, work in enumerate(sorted_works, start=1):
        paper_id = f"W{weekly_tag}-{idx:03d}"
        title = work.get("display_name") or "Untitled"
        doi = work.get("doi")
        title_md = _title_with_doi_link(title, doi)
        authors = _extract_authors(work)
        abstract_text = _abstract_from_inverted_index(work.get("abstract_inverted_index"))
        source_id, source_display_name = _get_source_id_and_name(work)
        raw_id = (source_id or "").split("/")[-1].upper()
        journal_name = journals_data.get(raw_id, "Core Journal")
        if journal_name == "Core Journal" and source_display_name:
            journal_name = source_display_name

        oa_info = work.get("open_access", {})
        is_oa = oa_info.get("is_oa", False)
        key = work.get("doi") or work.get("id")

        if is_oa and not no_download:
            success = bool(download_results.get(key))
            if success:
                downloaded_count += 1
                downloaded_list += _render_paper_block(
                    paper_id=paper_id,
                    title_md=title_md,
                    journal_name=journal_name,
                    authors=authors,
                    abstract_text=abstract_text,
                )
            else:
                manual_list += _render_paper_block(
                    paper_id=paper_id,
                    title_md=title_md,
                    journal_name=journal_name,
                    authors=authors,
                    abstract_text=abstract_text,
                )
        elif is_oa and no_download:
            manual_list += _render_paper_block(
                paper_id=paper_id,
                title_md=title_md,
                journal_name=journal_name,
                authors=authors,
                abstract_text=abstract_text,
            )
        else:
            manual_list += _render_paper_block(
                paper_id=paper_id,
                title_md=title_md,
                journal_name=journal_name,
                authors=authors,
                abstract_text=abstract_text,
            )

        if key:
            processed_keys.append(key)

    if downloaded_count == 0:
        downloaded_list += "- (本周无开源论文自动下载)\n"

    return report + downloaded_list + manual_list, processed_keys


def _persist_state(state, today, existing_seen_list, processed_keys, ignore_history):
    """按出现顺序写回 seen_keys，避免 set 打乱顺序。"""
    now_iso = datetime.now().isoformat(timespec="seconds")
    seen_list = list(existing_seen_list)
    seen_set = set(seen_list)
    seen_items = [{"key": k, "seen_at": now_iso} for k in seen_list]
    seen_items_index = {k: i for i, k in enumerate(seen_list)}

    for key in processed_keys:
        if key not in seen_set:
            seen_list.append(key)
            seen_set.add(key)
            seen_items.append({"key": key, "seen_at": now_iso})
            seen_items_index[key] = len(seen_items) - 1
        else:
            idx = seen_items_index.get(key)
            if idx is not None:
                seen_items[idx]["seen_at"] = now_iso

    if len(seen_list) > MAX_SEEN_KEYS:
        removed = len(seen_list) - MAX_SEEN_KEYS
        seen_list = seen_list[-MAX_SEEN_KEYS:]
        allowed = set(seen_list)
        seen_items = [item for item in seen_items if item.get("key") in allowed]
        logger.info("[state] 已删除最早进入的 %s 条，仅保留最近 %s 条", removed, MAX_SEEN_KEYS)
    state["seen_keys"] = seen_list
    state["seen_items"] = seen_items
    state["last_run_date"] = today
    if not ignore_history:
        _save_state(state)


def search_papers(options=None):
    """核心搜索函数：检索、筛选、下载并输出报告。"""
    opts = options or {}
    ctx = _prepare_search_inputs(opts)

    if not ctx["broad_keywords"]:
        logger.error("[错误] 未找到任何宽泛关键词，请通过命令行参数或 JSON 文件提供")
        return ""
    if not ctx["journal_ids"]:
        logger.error("[错误] 未找到任何期刊，请通过命令行参数或 JSON 文件提供")
        return ""

    if opts.get("dry_run"):
        _print_dry_run(ctx)
        return ""

    _ensure_dir(ctx["week_dir"])
    unique_works = _fetch_unique_works(
        broad_keywords=ctx["broad_keywords"],
        start_date=ctx["start_date"],
        end_date=ctx["today"],
        journal_filter=ctx["journal_filter"],
    )
    unique_works = _apply_refine_filter(unique_works, ctx["refine_keywords"])
    unique_works = _exclude_seen(unique_works, ctx["seen_set"])

    report_path = os.path.join(ctx["week_dir"], "weekly_paper_info.md")
    if not unique_works:
        content = _build_empty_report(ctx["report_start_date"], ctx["today"])
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(content + "\n")
        logger.info("[已生成] %s", report_path)
        _persist_state(
            state=ctx["state"],
            today=ctx["today"],
            existing_seen_list=ctx["seen_list"],
            processed_keys=[],
            ignore_history=ctx["ignore_history"],
        )
        return content

    content, processed_keys = _download_and_render_report(
        works=unique_works,
        journals_data=ctx["journals_data"],
        week_dir=ctx["week_dir"],
        report_start_date=ctx["report_start_date"],
        today=ctx["today"],
        broad_keywords=ctx["broad_keywords"],
        refine_keywords=ctx["refine_keywords"],
        no_download=opts.get("no_download", False),
    )

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content + "\n")
    logger.info("[已生成] %s", report_path)

    _persist_state(
        state=ctx["state"],
        today=ctx["today"],
        existing_seen_list=ctx["seen_list"],
        processed_keys=processed_keys,
        ignore_history=ctx["ignore_history"],
    )
    return content


# ============================================================================
# 第八部分：GitHub Issue 发布功能（可选）
# ============================================================================

def post_issue(content):
    """
    将报告内容发布到 GitHub Issue（可选功能）
    
    参数：
        content: 报告内容（Markdown 格式）
    
    说明：
        需要设置 GITHUB_TOKEN 和 GITHUB_REPOSITORY 环境变量
    """
    if not GITHUB_TOKEN:
        logger.info("未检测到 GITHUB_TOKEN，输出结果到控制台：")
        logger.info(content)
        return
    url = f"https://api.github.com/repos/{REPO_NAME}/issues"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    data = {"title": f"学术周报：{datetime.now().strftime('%Y-%m-%d')}", "body": content}
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 201:
        logger.info("Issue 发布成功！")
    else:
        logger.warning("Issue 发布失败，HTTP %s", response.status_code)


# ============================================================================
# 第九部分：命令行参数解析（增强版）
# ============================================================================

def parse_args():
    """
    解析命令行参数
    
    新增参数：
        --keyword / -k: 宽泛搜索关键词（可多次使用）
        --refine-keyword / -r: 精炼筛选关键词（可多次使用）
        --journal / -j: 期刊（可多次使用，支持 Source ID 或期刊名称）
    
    保留原有参数：
        --days: 搜索时间窗口
        --end-date: 搜索结束日期（YYYY-MM-DD，默认今天）
        --keywords: 关键词配置文件路径
        --journals: 期刊配置文件路径
        --ignore-history: 忽略历史记录
        --output: 输出目录
        --no-download: 不下载 PDF
        --post: 发布到 GitHub（默认不发布）
        --dry-run: 仅打印配置
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_journals = os.path.join(script_dir, "journals.json")
    default_keywords = os.path.join(script_dir, "keywords.json")
    
    p = argparse.ArgumentParser(
        description="按期刊与关键词检索论文，生成 weekly_paper_info.md，可选自动下载 OA PDF。\n"
                    "支持通过命令行直接指定关键词和期刊，也支持从 JSON 配置文件读取。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例：
  # 完全使用命令行参数
  python search_papers_enhanced.py -k earthquake -k fault -r initiation -j GRL -j Nature --days 30
  
  # 混合使用（命令行关键词 + JSON 期刊）
  python search_papers_enhanced.py -k earthquake -r rupture
  
  # 完全使用 JSON 配置（向后兼容）
  python search_papers_enhanced.py --days 30
  
  # 使用期刊名称而非 Source ID
  python search_papers_enhanced.py -k seismic -j "Nature Geoscience" -j "Science Advances"
        """
    )
    
    # ========================================================================
    # 新增：关键词参数
    # ========================================================================
    p.add_argument(
        "--keyword", "-k",
        action="append",
        dest="broad_keywords_cli",
        metavar="KEYWORD",
        help="宽泛搜索关键词（可多次使用，如: -k earthquake -k fault）。"
             "如果提供此参数，将覆盖 keywords.json 中的 broad_keywords。",
    )
    p.add_argument(
        "--refine-keyword", "-r",
        action="append",
        dest="refine_keywords_cli",
        metavar="KEYWORD",
        help="精炼筛选关键词（可多次使用，如: -r initiation -r rupture）。"
             "如果提供此参数，将覆盖 keywords.json 中的 refine_keywords。",
    )
    p.add_argument(
        "--no-refine",
        action="store_true",
        dest="no_refine",
        help="禁用二层精炼关键词筛选（忽略 --refine-keyword 与 keywords.json 的 refine_keywords）。",
    )
    
    # ========================================================================
    # 新增：期刊参数
    # ========================================================================
    p.add_argument(
        "--journal", "-j",
        action="append",
        dest="journals_cli",
        metavar="JOURNAL",
        help="期刊（OpenAlex Source ID 或期刊名称，可多次使用，如: -j GRL -j Nature）。"
             "如果提供此参数，将覆盖 journals.json 中的配置。"
             "支持期刊名称自动映射（如 'GRL' 会自动映射到对应的 Source ID）。",
    )
    
    # ========================================================================
    # 保留：原有参数
    # ========================================================================
    p.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="搜索最近 N 天的论文（默认：有历史用 WINDOW_DAYS 或 14，无历史用 7）",
    )
    p.add_argument(
        "--end-date",
        type=str,
        default=None,
        metavar="YYYY-MM-DD",
        help="将搜索结束日期固定为 YYYY-MM-DD（默认：今天）。配合 --days 可回填历史 state.json。",
    )
    p.add_argument(
        "--keywords",
        type=str,
        default=None,
        metavar="PATH",
        help=f"关键词配置文件路径（默认: {default_keywords}）",
    )
    p.add_argument(
        "--journals",
        type=str,
        default=None,
        metavar="PATH",
        help=f"期刊配置文件路径（默认: {default_journals}）",
    )
    p.add_argument(
        "--ignore-history",
        action="store_true",
        help="忽略 state.json：不读取、不写入，本次结果不参与去重",
    )
    p.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        metavar="DIR",
        help="报告与 PDF 输出目录（默认: downloads/YYYY-MM-DD）",
    )
    p.add_argument(
        "--no-download",
        action="store_true",
        dest="no_download",
        help="不下载 PDF，只生成 weekly_paper_info.md",
    )
    p.add_argument(
        "--post",
        action="store_true",
        dest="post",
        help="向 GitHub 提交 Issue（默认不提交）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="仅打印将要使用的配置与日期范围，不请求 API、不写文件",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    
    return p.parse_args()


# ============================================================================
# 第十部分：主程序入口
# ============================================================================

if __name__ == "__main__":
    # 步骤1：解析命令行参数
    args = parse_args()
    setup_logging(args.log_level)
    
    # 步骤2：构建选项字典，传递给 search_papers 函数
    options = {
        "days": args.days,
        "end_date": args.end_date,
        "keywords": args.keywords,
        "journals": args.journals,
        "ignore_history": args.ignore_history,
        "output": args.output,
        "no_download": args.no_download,
        "no_post": (not args.post),
        "dry_run": args.dry_run,
        # 新增：传递命令行关键词和期刊
        "broad_keywords_cli": args.broad_keywords_cli,
        "refine_keywords_cli": args.refine_keywords_cli,
        "journals_cli": args.journals_cli,
        "no_refine": args.no_refine,
    }
    
    # 步骤3：执行搜索并生成报告
    try:
        report_content = search_papers(options)
    except ValueError as e:
        logger.error("配置校验失败: %s", e)
        raise SystemExit(1)
    
    # 步骤4：可选：发布到 GitHub Issue
    if args.post and not args.dry_run:
        post_issue(report_content)
