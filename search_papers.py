import json
import os
import requests
from pyalex import Works, config
from datetime import datetime, timedelta

# 配置 OpenAlex
config.email = "wulongcha340@outlook.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

def download_pdf(url, filename):
    """
    下载 PDF 文件到指定的本地路径
    """
    try:
        # 创建 downloads 文件夹（如果不存在）
        if not os.path.exists('downloads'):
            os.makedirs('downloads')
        
        filepath = os.path.join('downloads', filename)
        response = requests.get(url, stream=True, timeout=30)
        
        if response.status_code == 200:
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            print(f"  - 成功下载: {filename}")
            return True
        else:
            print(f"  - 下载失败，状态码: {response.status_code}")
            return False
    except Exception as e:
        print(f"  - 下载出现异常: {e}")
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
