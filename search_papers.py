import os
import requests
from pyalex import Works, config
from datetime import datetime, timedelta

# 1. 配置
config.email = "wulongcha340@outlook.com" 
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") # 从 GitHub Actions 获取
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

def search_papers():
    # 1. 稍微拉长时间窗口到 10 天，防止某些期刊延迟收录
    lookback_days = 10
    start_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')
    
    # 2. 地震学 Concept ID
    SEISMOLOGY_CONCEPT_ID = "C127313418"
    
    # 3. 核心关键词
    keywords = [
        "earthquake initiation", 
        "rupture mechanism", 
        "rock experiment", 
        "laboratory earthquake",
        "fault mechanics"
    ]
    
    all_results = []
    
    for kw in keywords:
        # 使用学科过滤 + 关键词搜索
        query = (
            Works()
            .filter(
                from_publication_date=start_date,
                concepts={"id": SEISMOLOGY_CONCEPT_ID}
            )
            .search(kw)
        )
        results = query.get()
        all_results.extend(results)
    
    # 去重
    unique_results = {work['doi']: work for work in all_results if work.get('doi')}.values()
    
    if not unique_results:
        return f"过去 {lookback_days} 天内未发现相关论文。"

    # 按引用次数或相关度排序（可选）
    sorted_results = sorted(unique_results, key=lambda x: x.get('cited_by_count', 0), reverse=True)

    report = f"## 📅 地震学动态周报 ({start_date} 至今)\n\n"
    for work in sorted_results:
        title = work.get('display_name', 'No Title')
        doi = work.get('doi', 'No DOI')
        url = work.get('landing_page_url', '#')
        
        primary_loc = work.get('primary_location') or {}
        source = primary_loc.get('source') or {}
        journal = source.get('display_name', '未知期刊/预印本')
        
        report += f"### {title}\n- **期刊**: {journal}\n- **DOI**: {doi}\n- **链接**: [点击阅读]({url})\n\n---\n"
    
    return report

def post_issue(content):
    if not GITHUB_TOKEN or not REPO_NAME:
        print("未检测到 GitHub Token，仅在控制台打印结果：")
        print(content)
        return

    url = f"https://api.github.com/repos/{REPO_NAME}/issues"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    data = {
        "title": f"学术周报：{datetime.now().strftime('%Y-%m-%d')}",
        "body": content
    }
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 201:
        print("已成功发布 GitHub Issue！")
    else:
        print(f"发布失败: {response.text}")

if __name__ == "__main__":
    report_content = search_papers()
    post_issue(report_content)
