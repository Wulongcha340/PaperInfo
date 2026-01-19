import os
import requests
from pyalex import Works, config
from datetime import datetime, timedelta

# 1. 配置
config.email = "wulongcha340@outlook.com" 
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") # 从 GitHub Actions 获取
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

def search_papers():
    # 设定 50 天 (当前日期: 2026-01-19)
    start_date = (datetime.now() - timedelta(days=50)).strftime('%Y-%m-%d')
    
    journal_ids = [
        "S137773608", "S190130009", "S160132380", "S200557434", 
        "S196041119", "S148149864", "S45189098", "S183021966", 
        "S137119741", "S117621406", "S65330870", "S121960251"
    ]
    journal_filter = "|".join(journal_ids)
    
    # 1. 锚点词：只要包含这些核心词之一，就先抓回来
    anchor_keywords = ["earthquake", "seismic", "rupture", "fault", "rock"]
    
    # 2. 筛选词：在抓回来的结果中，标题或摘要包含以下词汇才保留
    target_concepts = ["initiation", "mechanism", "experiment", "nucleation", "laboratory", "fracture", "propagation"]
    
    all_results = []
    
    for kw in anchor_keywords:
        print(f"正在抓取包含 '{kw}' 的论文...")
        # 使用最简单的 search 逻辑
        query = (
            Works()
            .filter(
                from_publication_date=start_date,
                primary_location={"source": {"id": journal_filter}}
            )
            .search(kw)
        )
        results = query.get()
        print(f"  - 找到 {len(results)} 篇初步候选文章")
        all_results.extend(results)
    
    # 根据 DOI 去重
    unique_works = {work['doi']: work for work in all_results if work.get('doi')}
    
    filtered_report_list = []
    
    # 3. Python 二次筛选（模糊匹配）
    for doi, work in unique_works.items():
        title = (work.get('display_name') or "").lower()
        # 尝试获取摘要（OpenAlex 摘要通常是倒排索引，这里做简单处理）
        abstract = str(work.get('abstract_inverted_index') or "").lower()
        
        # 只要标题或摘要里包含任何一个“目标概念词”，就采纳
        if any(concept in title or concept in abstract for concept in target_concepts):
            filtered_report_list.append(work)

    print(f"筛选完成，最终保留 {len(filtered_report_list)} 篇高度相关的论文。")

    if not filtered_report_list:
        return "在候选文章中未发现匹配‘地震起始、破裂机理或岩石实验’特征的论文。"

    # 生成报告内容
    report = f"## 📅 地震学模糊搜索周报 (过去 50 天)\n\n"
    for work in filtered_report_list:
        title = work.get('display_name', 'No Title')
        journal = work.get('primary_location', {}).get('source', {}).get('display_name', '未知期刊')
        url = work.get('landing_page_url', '#')
        report += f"### {title}\n- **期刊**: {journal}\n- **链接**: [点击阅读]({url})\n\n---\n"
    
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
