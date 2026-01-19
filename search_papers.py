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
    start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    
    # 核心期刊 ID 列表
    journal_ids = [
        "S137773608",  # Nature
        "S3880285",    # Science
        "S4210228715", # JGR Solid Earth
        "S183957208",  # SRL
        "S36624081",   # GRL
        "S64187185",   # Nature Comm.
        "S2737427234", # Science Advances
        "S119230507",  # EPSL
        "S59098468",   # BSSA
        "S48977010",   # Nature Geoscience
        "S4210240585", # AGU Advances
        "S14326618",   # G3
        "S125754415",  # PNAS
        "S195250905",  # Tectophysics
        "S108821158",  # GJI
    ]
    journal_filter = "|".join(journal_ids)
    
    # 模糊搜索词：涵盖地震、断层、岩石力学、破裂
    # 改用 title.search，这在 OpenAlex 中比全局 search 更稳定
    keywords = ["earthquake", "seismic", "rupture", "fault", "rock", "fracture"]
    
    all_results = []
    
    for kw in keywords:
        print(f"正在检索关键词: {kw}...")
        # 注意：这里改用了最稳健的字符串键名过滤
        try:
            query = (
                Works()
                .filter(
                    from_publication_date=start_date,
                    # 使用更广泛的 locations.source.id 而不是 primary_location
                    # 这样只要论文在这些期刊发表，无论是否是“首要链接”都能搜到
                    **{"locations.source.id": journal_filter} 
                )
                # 使用标题搜索，这通常是 API 最核心的支持项
                .filter(title={"search": kw})
            )
            results = query.get()
            print(f"  - 发现 {len(results)} 篇")
            all_results.extend(results)
        except Exception as e:
            print(f"  - 检索 {kw} 时出错: {e}")
    
    # 根据 DOI 去重
    unique_works = {work['doi']: work for work in all_results if work.get('doi')}
    results_list = list(unique_works.values())
    
    print(f"初筛完成，共获得 {len(results_list)} 篇论文。正在进行专业度清洗...")

    # 二次手动筛选：确保论文确实是关于地震机制、破裂或实验的
    final_list = []
    expert_terms = ["initiation", "mechanism", "experiment", "nucleation", "laboratory", "dynamic", "friction", "constitutive"]
    
    for work in results_list:
        content = (work.get('display_name') or "").lower()
        # 如果标题包含任意一个专家词汇，则保留
        if any(term in content for term in expert_terms):
            final_list.append(work)

    if not final_list:
        return "在指定期刊中未匹配到符合‘地震机理/岩石实验’特征的高相关性论文。"

    # 构造报告
    report = f"## 📅 地震学精选周报 ({start_date} 至今)\n\n"
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
