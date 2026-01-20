import json
import os
import requests
from pyalex import Works, config
from datetime import datetime, timedelta

# 配置 OpenAlex
config.email = "your_email@example.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

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
    start_date = (datetime.now() - timedelta(days=50)).strftime('%Y-%m-%d')
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
    report = f"## 📅 地震学核心期刊周报 ({start_date} 至今)\n\n"
    report += f"> 监测关键词: {', '.join(search_keywords)}\n\n---\n"

    # 按引用次数排序（让更重要的论文排在前面）
    sorted_works = sorted(unique_works.values(), key=lambda x: x.get('cited_by_count', 0), reverse=True)

    for work in sorted_works:
        title = work.get('display_name')
        # 获取美化后的期刊名
        raw_id = work.get('primary_location', {}).get('source', {}).get('id', '').split('/')[-1].upper()
        journal_name = journals_data.get(raw_id, "Core Journal")
        
        doi = work.get('doi')
        url = work.get('landing_page_url', '#')
        
        report += f"### {title}\n"
        report += f"- **期刊**: {journal_name}\n"
        report += f"- **DOI**: {doi}\n"
        report += f"- **链接**: [点击访问原文]({url})\n\n"
    
    return report

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
