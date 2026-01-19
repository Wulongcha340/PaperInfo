from pyalex import Works, config
from datetime import datetime, timedelta

# 1. 配置 - 加入邮箱可以进入 OpenAlex 的 "Polite Pool"，请求速度更快且更稳定
config.email = "your_email@example.com"  # 请替换为你的邮箱

def search_earthquake_papers():
    # 2. 设定时间范围：过去 7 天
    last_week = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    print(f"正在搜寻 {last_week} 至今发表的论文...\n")

    # 3. 定义目标期刊的 OpenAlex ID (Source ID)
    # 你可以根据需要添加更多期刊 ID
    target_journals = {
        "Nature": "S137773608",
        "JGR: Solid Earth": "S148149864",
        "Seismological Research Letters": "S65330870",
        "Nature Geoscience": "S160132380",
        "Earth and Planetary Science Letters": "S137119741"
    }

    # 4. 构建关键词搜索
    # 使用 | 表示“或”，空格在 search 中默认通常视为关键词组合
    keywords = "earthquake initiation | rupture mechanism | rock experiment | laboratory earthquakes"

    # 5. 执行查询
    # 我们通过 filter 限制日期，通过 search 进行关键词匹配
    query = (
        Works()
        .filter(from_publication_date=last_week)
        .search(keywords)
    )

    # 获取结果
    results = query.get()

    if not results:
        print("本周暂无符合条件的论文更新。")
        return

    print(f"共发现 {len(results)} 篇相关论文：\n" + "="*50)

    for work in results:
        # 提取标题
        title = work.get('display_name', 'No Title')
        
        # 提取 DOI 和 链接
        doi = work.get('doi', 'N/A')
        url = work.get('landing_page_url', 'N/A')
        
        # 提取发表日期
        pub_date = work.get('publication_date', 'N/A')
        
        # 提取期刊名称
        source = work.get('primary_location', {}).get('source', {})
        journal_name = source.get('display_name', 'Unknown Journal')

        # 打印输出
        print(f"标题: {title}")
        print(f"期刊: {journal_name}")
        print(f"日期: {pub_date}")
        print(f"DOI: {doi}")
        print(f"链接: {url}")
        print("-" * 30)

if __name__ == "__main__":
    search_earthquake_papers()
