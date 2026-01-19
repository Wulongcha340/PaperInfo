import os
import requests
from pyalex import Works, config
from datetime import datetime, timedelta

# 1. 配置
config.email = "wulongcha340@outlook.com" 
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") # 从 GitHub Actions 获取
REPO_NAME = os.getenv("GITHUB_REPOSITORY")

def search_papers():
    # 设定 50 天
    start_date = (datetime.now() - timedelta(days=50)).strftime('%Y-%m-%d')
    
    journal_ids = [
        "S137773608", "S190130009", "S160132380", "S200557434", 
        "S196041119", "S148149864", "S45189098", "S183021966", 
        "S137119741", "S117621406", "S65330870", "S121960251"
    ]
    journal_filter = "|".join(journal_ids)
    
    # 修改关键词：使用更核心的单次组合，增加命中率
    # 注意：在 search 中，多个词默认是 AND 关系
    keywords = [
        "earthquake initiation", 
        "rupture mechanism", 
        "rock experiment", 
        "fault mechanics",
        "dynamic rupture",
        "laboratory earthquake"
    ]
    
    all_results = []
    
    for kw in keywords:
        print(f"正在检索关键词: {kw}...")
        # 使用扁平化过滤器字符串，这是最稳妥的写法
        query = (
            Works()
            .filter(
                from_publication_date=start_date,
                primary_location={"source": {"id": journal_filter}}
            )
            .search(kw)
        )
        results = query.get()
        print(f"  - 发现 {len(results)} 篇")
        all_results.extend(results)
    
    unique_results = {work['doi']: work for work in all_results if work.get('doi')}.values()
    
    if not unique_results:
        # 如果还是没结果，做一次“保底测试”：不搜关键词，只搜该时段内这些期刊的所有论文
        print("警告：关键词检索无结果。正在尝试无关键词保底检索...")
        test_query = Works().filter(
            from_publication_date=start_date,
            primary_location={"source": {"id": journal_filter}}
        ).sample(5).get() # 随机取5篇
        if test_query:
            print(f"保底检索成功，说明期刊 ID 无误。前两篇标题为：{test_query[0]['display_name']}")
        return "未能匹配到包含指定关键词的论文。"

    # 后续生成报告的代码保持不变...
    report = f"## 📅 地震学核心期刊周报 (过去 50 天)\n\n"
    for work in unique_results:
        # ... 提取信息的代码 ...
        report += f"### {work.get('display_name')}\n- **期刊**: {work.get('primary_location', {}).get('source', {}).get('display_name')}\n\n"
    
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
