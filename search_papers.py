import os
import requests
from pyalex import Works, config
from datetime import datetime, timedelta

# 1. 配置
config.email = "wulongcha340@outlook.com" 
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") # 从 GitHub Actions 获取
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

def search_papers():
    # 设定搜寻过去 7 天的论文
    last_week = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    
    # 1. 定义你指定的期刊 ID（这里包含了 Nature, JGR, GRL, SRL 等）
    # 如果你需要添加更多，可以参考下方的“如何获取 ID”部分
    journal_ids = [
        "S137773608", # Nature
        "S190130009", # Science
        "S160132380", # Nature Geoscience
        "S148149864", # Journal of Geophysical Research: Solid Earth
        "S45189098",  # Geophysical Research Letters
        "S65330870",  # Seismological Research Letters
        "S137119741", # Earth and Planetary Science Letters
    ]
    
    # 将 ID 列表转换成 OpenAlex 要求的过滤字符串 (用 | 分隔表示 "或")
    journal_filter = "|".join(journal_ids)
    
    keywords = ["earthquake initiation", "rupture mechanism", "rock experiment", "laboratory earthquakes"]
    all_results = []
    
    for kw in keywords:
        # 2. 在 filter 中加入 primary_location.source.id
        query = (
            Works()
            .filter(
                from_publication_date=last_week,
                primary_location={"source": {"id": journal_filter}} # 锁定期刊
            )
            .search(kw)
        )
        results = query.get()
        all_results.extend(results)
    
    # 根据 DOI 去重
    unique_results = {work['doi']: work for work in all_results if work.get('doi')}.values()
    
    if not unique_results:
        return f"本周在指定期刊中暂无相关论文更新。"

    report = f"## 📅 地震学精选周报 ({last_week} 至今)\n\n"
    report += "> 检索范围：Nature, Science, JGR, GRL, SRL, EPSL 等顶级期刊\n\n"
    
    for work in unique_results:
        title = work.get('display_name', 'No Title')
        doi = work.get('doi', 'No DOI')
        url = work.get('landing_page_url', '#')
        
        primary_loc = work.get('primary_location') or {}
        source = primary_loc.get('source') or {}
        journal = source.get('display_name', '未知期刊')

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
