import os
import requests
from pyalex import Works, config
from datetime import datetime, timedelta

# 1. 配置
config.email = "wulongcha340@outlook.com" 
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") # 从 GitHub Actions 获取
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

def search_papers():
    # 设定搜寻过去 10 天的论文，防止某些期刊更新稍慢
    start_date = (datetime.now() - timedelta(days=50)).strftime('%Y-%m-%d')
    
    # 扩充后的核心期刊 ID 列表
    journal_ids = [
        "S137773608", # Nature
        "S190130009", # Science
        "S160132380", # Nature Geoscience
        "S200557434", # Science Advances
        "S196041119", # Nature Communications
        "S148149864", # JGR: Solid Earth
        "S45189098",  # GRL
        "S183021966", # GJI (Geophysical Journal International)
        "S137119741", # EPSL (Earth and Planetary Science Letters)
        "S117621406", # BSSA (Bulletin of the Seismological Society of America)
        "S65330870",  # SRL (Seismological Research Letters)
        "S121960251", # Tectonophysics
        "S46648057",  # Rock Mechanics and Rock Engineering
        "S4210168496" # Communications Earth & Environment
    ]
    
    # 组合期刊过滤器
    journal_filter = "|".join(journal_ids)
    
    # 精准的关键词列表
    keywords = [
        "earthquake initiation", 
        "rupture mechanism", 
        "rock experiment", 
        "fault mechanics",
        "laboratory earthquake",
        "dynamic rupture",
        "frictional sliding"
    ]
    
    all_results = []
    
    for kw in keywords:
        query = (
            Works()
            .filter(
                from_publication_date=start_date,
                primary_location={"source": {"id": journal_filter}}
            )
            .search(kw)
        )
        results = query.get()
        all_results.extend(results)
    
    # 根据 DOI 去重
    unique_results = {work['doi']: work for work in all_results if work.get('doi')}.values()
    
    if not unique_results:
        return f"过去 10 天内在指定核心期刊中未发现相关论文。"

    # 按引用次数排序（通常新出的论文引用为0，这里主要起稳定排序作用）
    sorted_results = sorted(unique_results, key=lambda x: x.get('cited_by_count', 0), reverse=True)

    report = f"## 📅 地震学核心期刊周报 ({start_date} 至今)\n\n"
    report += "> 监测期刊范围：Nature, Science, JGR, GRL, GJI, BSSA, SRL, Tectonophysics 等\n\n"
    
    for work in sorted_results:
        title = work.get('display_name', 'No Title')
        doi = work.get('doi', 'No DOI')
        url = work.get('landing_page_url', '#')
        
        # 提取期刊名
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
