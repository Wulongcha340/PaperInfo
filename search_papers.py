import os
import requests
from pyalex import Works, config
from datetime import datetime, timedelta

# 1. 配置
config.email = "wulongcha340@outlook.com" 
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") # 从 GitHub Actions 获取
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

def search_papers():
    # 设定搜寻过去 7 天的论文 (当前日期: 2026-01-19)
    last_week = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    
    keywords = ["earthquake initiation", "rupture mechanism", "rock experiment", "laboratory earthquakes"]
    all_results = []
    
    for kw in keywords:
        query = Works().filter(from_publication_date=last_week).search(kw)
        results = query.get()
        all_results.extend(results)
    
    unique_results = {work['doi']: work for work in all_results if work.get('doi')}.values()
    
    if not unique_results:
        return "本周暂无相关关键词的论文更新。"

    report = f"## 📅 地震学论文周报 ({last_week} 至今)\n\n"
    for work in unique_results:
        title = work.get('display_name', 'No Title')
        doi = work.get('doi', 'No DOI')
        url = work.get('landing_page_url', '#')
        
        # --- 稳健的数据提取开始 ---
        primary_loc = work.get('primary_location') or {}
        source = primary_loc.get('source') or {}
        journal = source.get('display_name', '未知期刊/预印本')
        # --- 稳健的数据提取结束 ---

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
