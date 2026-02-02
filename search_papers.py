import argparse
import json
import os
import re
import requests
from pyalex import Works, config
from datetime import datetime, timedelta

# 配置 OpenAlex
config.email = os.getenv("OPENALEX_EMAIL", "wulongcha340@outlook.com")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")
DEFAULT_WINDOW_DAYS = 30
MAX_SEEN_KEYS = 100  # state 中最多保留篇数，超出时删除最早进入的，以提升匹配速度

def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def _load_state():
    """
    记录已进入过周报的论文 key（优先 DOI，其次 OpenAlex Work ID），避免“长窗口”带来的重复。
    state.json 示例：
    {
      "seen_keys": ["https://doi.org/10....", "https://openalex.org/W...."],
      "last_run_date": "2026-01-29"
    }
    """
    if not os.path.exists(STATE_PATH):
        return {"seen_keys": [], "last_run_date": None}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("seen_keys"), list):
            return data
    except Exception:
        pass
    return {"seen_keys": [], "last_run_date": None}

def _save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[警告] state.json 写入失败: {e}")

def _safe_filename(title: str, suffix: str, max_len: int = 180) -> str:
    title = title or "untitled"
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff _-]+", "", title)
    title = title.strip(" .-_")
    if not title:
        title = "untitled"

    suffix = (suffix or "").strip()
    suffix = re.sub(r"[^0-9A-Za-z._-]+", "_", suffix)
    if suffix:
        name = f"{title}__{suffix}"
    else:
        name = title

    if len(name) > max_len:
        name = name[:max_len].rstrip(" .-_")
    return f"{name}.pdf"

def _normalize_doi(doi: str) -> str:
    if not doi or not isinstance(doi, str):
        return ""
    d = doi.strip()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.IGNORECASE)
    return d

def _doi_to_pdf_urls(doi: str):
    """
    用 DOI 生成一些“更可能是直链 PDF”的 URL（按经验覆盖常见平台）。
    这些 URL 不是保证可用，但能显著提高部分站点的成功率（如 PNAS / Wiley）。
    """
    d = _normalize_doi(doi)
    if not d:
        return []

    urls = []
    d_lower = d.lower()
    d_upper = d.upper()

    # PNAS: 通常可直接 /doi/pdf/
    if d_lower.startswith("10.1073/pnas."):
        urls.append(f"https://www.pnas.org/doi/pdf/{d}")
        urls.append(f"https://www.pnas.org/doi/epdf/{d}")

    # Wiley / AGU（GRL、JGR、AGU Advances 等大量 DOI 在 10.1029/）
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
    收集可能的 OA/PDF 链接（去重、保持优先级顺序）。
    注意：其中有些可能是落地页（HTML），会在下载时被识别并跳过。
    """
    urls = []

    def add(u):
        if u and isinstance(u, str) and u not in urls:
            urls.append(u)

    best = work.get("best_oa_location") or {}
    add(best.get("pdf_url"))
    add(best.get("url"))

    oa = work.get("open_access") or {}
    add(oa.get("oa_url"))

    primary = work.get("primary_location") or {}
    add(primary.get("pdf_url"))
    add(primary.get("landing_page_url"))

    for loc in work.get("locations") or []:
        if not loc:
            continue
        add(loc.get("pdf_url"))
        add(loc.get("landing_page_url"))

    doi = work.get("doi")
    for u in _doi_to_pdf_urls(doi):
        add(u)
    add(doi)

    wid = work.get("id")
    add(wid)

    return urls

def download_pdf(work, filename, output_dir):
    """
    接收完整的 work 对象，尝试多种路径下载 PDF
    """
    urls = _candidate_urls(work)
    if not urls:
        print(f"  - [跳过] {filename}: 未找到有效的开源链接")
        return False

    try:
        _ensure_dir(output_dir)
        filepath = os.path.join(output_dir, filename)
        base_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
            "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        }

        session = requests.Session()
        for u in urls:
            headers = dict(base_headers)
            # 某些站点会检查 Referer
            headers["Referer"] = (work.get("primary_location") or {}).get("landing_page_url") or work.get("doi") or u

            response = session.get(u, headers=headers, stream=True, timeout=60, allow_redirects=True)

            if response.status_code != 200:
                # 常见原因：403（反爬/需要 Cookie）、404、429 等
                if response.status_code in (403, 404, 429):
                    print(f"  - [跳过] {filename}: {u} 返回 HTTP {response.status_code}")
                    continue
                print(f"  - [跳过] {filename}: {u} 下载失败 HTTP {response.status_code}")
                continue

            content_type = (response.headers.get("Content-Type") or "").lower()
            it = response.iter_content(chunk_size=8192)
            first = next(it, b"")
            # 简单校验是否为 PDF（有些站点 Content-Type 不准，所以两者满足其一即可）
            if (b"%PDF" not in first[:16]) and ("pdf" not in content_type):
                print(f"  - [跳过] {filename}: {u} 疑似非 PDF (Content-Type={content_type or 'N/A'})")
                continue

            with open(filepath, 'wb') as f:
                if first:
                    f.write(first)
                for chunk in it:
                    if chunk:
                        f.write(chunk)
            print(f"  - [成功] 已下载: {filename}")
            return True
    except Exception as e:
        print(f"  - [错误] 下载 {filename} 时异常: {e}")
    return False


def load_config(file_name):
    with open(file_name, 'r', encoding='utf-8') as f:
        return json.load(f)

def _get_source_id_and_name(work):
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    sid = source.get("id") or ""
    sname = source.get("display_name") or ""
    if not sid:
        # 兜底：从 locations 里取第一个 source
        locations = work.get("locations") or []
        if locations:
            src = (locations[0] or {}).get("source") or {}
            sid = src.get("id") or ""
            sname = src.get("display_name") or sname
    return sid, sname

def _abstract_from_inverted_index(inv):
    """
    OpenAlex 的摘要字段通常是 abstract_inverted_index:
    {"word": [pos1, pos2, ...], ...}
    这里将其还原成可检索的文本（近似）。
    """
    if not inv or not isinstance(inv, dict):
        return ""
    try:
        max_pos = -1
        for positions in inv.values():
            if positions:
                max_pos = max(max_pos, max(positions))
        if max_pos < 0:
            return ""
        words = [""] * (max_pos + 1)
        for w, positions in inv.items():
            for p in positions:
                if 0 <= p <= max_pos:
                    words[p] = w
        return " ".join([w for w in words if w])
    except Exception:
        return ""

def _resolve_path(path: str, default_name: str) -> str:
    """解析路径：若为相对路径则相对于脚本所在目录。"""
    if not path:
        path = os.path.join(os.path.dirname(__file__), default_name)
    elif not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), path)
    return os.path.abspath(path)


def search_papers(options=None):
    opts = options or {}
    # 1. 加载配置
    journals_path = opts.get("journals") or "journals.json"
    keywords_path = opts.get("keywords") or "keywords.json"
    journals_path = _resolve_path(journals_path, "journals.json")
    keywords_path = _resolve_path(keywords_path, "keywords.json")

    journals_data = load_config(journals_path)
    keywords_data = load_config(keywords_path)

    ignore_history = opts.get("ignore_history", False)
    if ignore_history:
        state = {"seen_keys": [], "last_run_date": None}
        seen = set()
        print("已忽略 state.json（--ignore_history）")
    else:
        state = _load_state()
        seen = set([k for k in state.get("seen_keys", []) if isinstance(k, str)])

    # 2. 设置时间窗口：命令行 --days 优先，否则环境变量，无历史时缩短为 7 天
    if opts.get("days") is not None:
        window_days = int(opts["days"])
    else:
        window_days = int(os.getenv("WINDOW_DAYS", str(DEFAULT_WINDOW_DAYS)))
        if not seen:
            window_days = 7
            print(f"state.json 无历史论文，搜索窗口缩短为 {window_days} 天")

    journal_ids = list(journals_data.keys())
    # 两层关键词：先 broad 搜索候选，再用 refine 在标题/摘要里做二次筛选
    broad_keywords = keywords_data.get("broad_keywords")
    refine_keywords = keywords_data.get("refine_keywords")
    # 兼容旧格式 active_keywords（如果用户还没更新 keywords.json）
    legacy_active = keywords_data.get("active_keywords")
    if (not broad_keywords) and legacy_active:
        broad_keywords = legacy_active
        refine_keywords = []
    broad_keywords = [k for k in (broad_keywords or []) if isinstance(k, str) and k.strip()]
    refine_keywords = [k for k in (refine_keywords or []) if isinstance(k, str) and k.strip()]
    start_date = (datetime.now() - timedelta(days=window_days)).strftime('%Y-%m-%d')
    today = datetime.now().strftime('%Y-%m-%d')
    journal_filter = "|".join(journal_ids)

    # 输出目录：--output 指定或默认 downloads/YYYY-MM-DD/
    if opts.get("output"):
        week_dir = os.path.abspath(opts["output"])
    else:
        downloads_root = os.path.join(os.path.dirname(__file__), "downloads")
        week_dir = os.path.join(downloads_root, today)

    if opts.get("dry_run"):
        print("[dry-run] 将要使用的配置与日期范围：")
        print(f"  --keywords   {keywords_path}")
        print(f"  --journals  {journals_path}")
        print(f"  --days      {window_days}（start_date={start_date}, today={today}）")
        print(f"  --output    {week_dir}")
        print(f"  ignore_history = {ignore_history}")
        print(f"  broad_keywords  = {broad_keywords}")
        print(f"  refine_keywords = {refine_keywords}")
        print(f"  journal_ids     = {list(journals_data.keys())}")
        print("[dry-run] 未请求 API，未写入任何文件。")
        return ""

    _ensure_dir(week_dir)

    unique_works = {}

    # 3. 核心搜索循环
    for kw in broad_keywords:
        print(f"正在检索宽泛关键词: {kw}...")
        try:
            # 用 locations.source.id 做期刊过滤（比 primary_location 更全）
            query = (
                Works()
                .filter(
                    from_publication_date=start_date,
                    **{"locations.source.id": journal_filter}
                )
                .search(kw)
                # 显式增大每页数量，避免默认只拿到很少结果
                .get(per_page=200)
            )
            print(f"  - 找到 {len(query)} 篇")
            for work in query:
                # DOI 可能为空；用 OpenAlex work id 兜底去重
                key = work.get("doi") or work.get("id")
                if key:
                    unique_works[key] = work
        except Exception as e:
            print(f"  - 检索 {kw} 时出错: {e}")

    # 3.5 二层筛选：在候选结果中匹配 refine 关键词（标题 + 摘要）
    if refine_keywords and unique_works:
        refine_terms = [t.lower() for t in refine_keywords]
        filtered = {}
        for k, work in unique_works.items():
            title = (work.get("display_name") or "")
            abstract = _abstract_from_inverted_index(work.get("abstract_inverted_index"))
            blob = (title + " " + abstract).lower()
            if any(term in blob for term in refine_terms):
                filtered[k] = work
        print(f"二层筛选完成：{len(unique_works)} → {len(filtered)} 篇")
        unique_works = filtered

    # 3.6 去重：只保留“本次新增”（未进入过历史周报的论文）
    if unique_works:
        before = len(unique_works)
        unique_works = {k: w for k, w in unique_works.items() if k not in seen}
        print(f"历史去重完成：{before} → {len(unique_works)} 篇（仅保留本次新增）")

    if not unique_works:
        content = (
            f"## 📅 本周论文信息 ({start_date} 至 {today})\n\n"
            f"本次使用滑动窗口 {window_days} 天。\n\n"
            f"在指定期刊中未发现**新增**匹配论文（可能是本期无新增，或都已在历史中出现过）。"
        )
        report_path = os.path.join(week_dir, "weekly_paper_info.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(content + "\n")
        print(f"[已生成] {report_path}")
        state["last_run_date"] = today
        seen_list = list(state.get("seen_keys", []))
        if len(seen_list) > MAX_SEEN_KEYS:
            seen_list = seen_list[-MAX_SEEN_KEYS:]
            state["seen_keys"] = seen_list
            print(f"[state] 已裁剪为最近 {MAX_SEEN_KEYS} 条")
        if not ignore_history:
            _save_state(state)
        return content

    # 4. 构造本周论文信息（weekly_paper_info.md，与周报正文区分）
    report = f"## 📅 本周论文信息 ({start_date} 至 {today})\n\n"
    report += f"- **滑动窗口**: {window_days} 天\n"
    if broad_keywords:
        report += f"- **宽泛关键词**: {', '.join(broad_keywords)}\n"
    if refine_keywords:
        report += f"- **二层关键词**: {', '.join(refine_keywords)}\n"
    report += "\n"
    report += "### 📥 已自动获取的 PDF\n"
    
    manual_list = "\n### 🔗 需手动下载 / 自动下载失败的论文\n"
    downloaded_count = 0

    # 按 journals.json 中期刊出现顺序排列，同刊内按发表日期（新在前）、引用数（高在前）
    journal_order = list(journals_data.keys())
    journal_index = {k: i for i, k in enumerate(journal_order)}

    def _work_sort_key(x):
        source_id, _ = _get_source_id_and_name(x)
        raw_id = (source_id or "").split("/")[-1].upper()
        idx = journal_index.get(raw_id, len(journal_order))
        pub = (x.get("publication_date") or "")[:10]
        try:
            parts = pub.split("-")
            date_ord = -(int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])) if len(parts) >= 3 else 0
        except (ValueError, IndexError):
            date_ord = 0
        return (idx, date_ord, -(x.get("cited_by_count") or 0))

    sorted_works = sorted(unique_works.values(), key=_work_sort_key)

    for work in sorted_works:
        title = work.get('display_name')
        doi = work.get('doi')

        # 提取期刊名逻辑
        source_id, source_display_name = _get_source_id_and_name(work)
        raw_id = (source_id or "").split('/')[-1].upper()
        journal_name = journals_data.get(raw_id, "Core Journal")
        if journal_name == "Core Journal" and source_display_name:
            journal_name = source_display_name

        # --- 开源检查与下载逻辑 ---
        oa_info = work.get('open_access', {})
        is_oa = oa_info.get('is_oa', False)
        no_download = opts.get("no_download", False)
        if is_oa and not no_download:
            suffix = (doi or (work.get("id") or "")).split("/")[-1]
            filename = _safe_filename(title, suffix=suffix)

            success = download_pdf(work, filename, output_dir=week_dir)
            if success:
                downloaded_count += 1
                report += f"- ✅ **[已下载]** {title} ({journal_name})\n"
            else:
                manual_list += f"#### {title}\n- **期刊**: {journal_name}\n- **DOI**: {doi}\n\n---\n"
        elif is_oa and no_download:
            report += f"- 📄 **[未下载]** {title} ({journal_name})\n"
        else:
            manual_list += f"#### {title}\n- **期刊**: {journal_name}\n- **DOI**: {doi}\n\n---\n"

        # 本次周报纳入的论文，一律写入 state（无论是否成功下载），避免下次重复出现
        key = work.get("doi") or work.get("id")
        if key:
            seen.add(key)

    # 合并报告
    if downloaded_count == 0:
        report += "- (本周无开源论文自动下载)\n"
    
    content = report + manual_list
    report_path = os.path.join(week_dir, "weekly_paper_info.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content + "\n")
    print(f"[已生成] {report_path}")

    # 维护 seen_keys：保持进入顺序（新加入的追加在末尾），最多保留 MAX_SEEN_KEYS 条，超出则删除最早进入的
    seen_list = list(state.get("seen_keys", []))
    for key in seen:
        if key not in seen_list:
            seen_list.append(key)
    if len(seen_list) > MAX_SEEN_KEYS:
        removed = len(seen_list) - MAX_SEEN_KEYS
        seen_list = seen_list[-MAX_SEEN_KEYS:]
        print(f"[state] 已删除最早进入的 {removed} 条，仅保留最近 {MAX_SEEN_KEYS} 条")
    state["seen_keys"] = seen_list
    state["last_run_date"] = today
    if not ignore_history:
        _save_state(state)
    return content

def post_issue(content):
    if not GITHUB_TOKEN:
        print("未检测到 GITHUB_TOKEN，输出结果到控制台：")
        print(content)
        return
    url = f"https://api.github.com/repos/{REPO_NAME}/issues"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    data = {"title": f"学术周报：{datetime.now().strftime('%Y-%m-%d')}", "body": content}
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 201:
        print("Issue 发布成功！")

def parse_args():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_journals = os.path.join(script_dir, "journals.json")
    default_keywords = os.path.join(script_dir, "keywords.json")

    p = argparse.ArgumentParser(
        description="按期刊与关键词检索论文，生成 weekly_paper_info.md，可选自动下载 OA PDF。"
    )
    p.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="搜索最近 N 天的论文（默认：有历史用 WINDOW_DAYS 或 30，无历史用 7）",
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
        "--ignore_history",
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
        "--no-post",
        action="store_true",
        dest="no_post",
        help="不向 GitHub 提交 Issue，仅本地输出",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="仅打印将要使用的配置与日期范围，不请求 API、不写文件",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    options = {
        "days": args.days,
        "keywords": args.keywords,
        "journals": args.journals,
        "ignore_history": args.ignore_history,
        "output": args.output,
        "no_download": args.no_download,
        "no_post": args.no_post,
        "dry_run": args.dry_run,
    }
    report_content = search_papers(options)
    if not args.no_post and not args.dry_run:
        post_issue(report_content)
