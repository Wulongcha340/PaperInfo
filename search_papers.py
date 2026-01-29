import json
import os
import requests
from pyalex import Works, config
from datetime import datetime, timedelta

# 配置 OpenAlex
config.email = "wulongcha340@outlook.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

def download_pdf(work, filename):
    """
    使用更精准的路径尝试下载 PDF
    """
    # 1. 优先获取最可能的 PDF 直连地址
    best_location = work.get('best_oa_location') or {}
    pdf_url = best_location.get('pdf_url')
    
    # 如果 pdf_url 为空，再尝试 oa_url
    if not pdf_url:
        pdf_url = work.get('open_access', {}).get('oa_url')

    if not pdf_url:
        return False

    try:
        if not os.path.exists('downloads'):
            os.makedirs('downloads')
        
        filepath = os.path.join('downloads', filename)
        
        # 2. 增加浏览器伪装头
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Accept": "application/pdf,application/octet-stream"
        }

        response = requests.get(pdf_url, headers=headers, stream=True, timeout=60, allow_redirects=True)
        
        # 3. 校验返回内容是否为 PDF
        content_type = response.headers.get('Content-Type', '').lower()
        
        if response.status_code == 200 and ('pdf' in content_type or response.content[:4] == b'%PDF'):
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f"  - [成功] 已下载: {filename}")
            return True
        else:
            print(f"  - [跳过] 链接非 PDF 字节流 (Type: {content_type})")
            return False
            
    except Exception as e:
        print(f"  - [错误] 下载过程中断: {e}")
        return False


def load_config(file_name):
    with open(file_name, 'r', encoding='utf-8') as f:
        return json.load(f)

def search_papers():
    # 1. 加载配置
    journals_data = load_config('journals.json')
    keywords_data = load_config('keywords.json')
    
    journal_ids = list(journals_data.keys())
    search_keywords = keywords_data['active_keywords']
    
    # 2. 设置时间窗口（建议 50 天做初次测试，稳定后改回 7-14 天）
    start_date = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
    journal_filter = "|".join(journal_ids)
    
    unique_works = {}

    # 3. 核心搜索循环
    for kw in search_keywords:
        print(f"正在检索关键词: {kw}...")
        try:
            # 直接搜索，不加额外的 title.search 限制，让 API 匹配标题和摘要
            query = (
                Works()
                .filter(
                    from_publication_date=start_date,
                    primary_location={"source": {"id": journal_filter}}
                )
                .search(kw)
                .get()
            )
            print(f"  - 找到 {len(query)} 篇")
            for work in query:
                if work.get('doi'):
                    unique_works[work['doi']] = work
        except Exception as e:
            print(f"  - 检索 {kw} 时出错: {e}")

    if not unique_works:
        return f"## 📅 地震学核心期刊周报\n\n自 {start_date} 以来，在指定期刊中未发现匹配关键词的论文。"

    # 4. 构造 Markdown 报告
    report = f"## 📅 学术前沿文献周报 ({start_date} 至今)\n\n"
    report += "### 📥 本周已自动获取的 PDF 列表\n"
    
    manual_list = "\n### 🔗 需手动下载的论文 (非开源)\n"
    
    downloaded_count = 0
    
    sorted_works = sorted(unique_works.values(), key=lambda x: x.get('cited_by_count', 0), reverse=True)
    
    for work in sorted_works:
        title = work.get('display_name')
        doi = work.get('doi')
        url = work.get('landing_page_url', '#')
        
        # 提取期刊名逻辑
        raw_id = work.get('primary_location', {}).get('source', {}).get('id', '').split('/')[-1].upper()
        journal_name = journals_data.get(raw_id, "Core Journal")

        # --- 增加开源检查与下载逻辑 ---
        oa_info = work.get('open_access', {})
        is_oa = oa_info.get('is_oa', False)
        pdf_url = oa_info.get('oa_url') # 或者使用 work.get('best_oa_location', {}).get('pdf_url')

        if is_oa and pdf_url:
            # 清理标题中的特殊字符作为文件名
            safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"{safe_title}.pdf"
            
            success = download_pdf(pdf_url, filename)
            if success:
                downloaded_count += 1
                report += f"- ✅ **[已下载]** {title} ({journal_name})\n"
                continue # 已下载的论文不再放入手动列表

        # 如果不开源或下载失败，放入手动列表
        manual_list += f"#### {title}\n- **期刊**: {journal_name}\n- **DOI**: {doi}\n- **下载入口**: [点击跳转]({url})\n\n---\n"

    # 合并报告
    if downloaded_count == 0:
        report += "- (本周无开源论文自动下载)\n"
    
    return report + manual_list

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

if __name__ == "__main__":
    report_content = search_papers()
    post_issue(report_content)
