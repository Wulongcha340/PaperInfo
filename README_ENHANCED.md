# search_papers_enhanced_v2.py 使用说明

## 概述

`search_papers_enhanced_v2.py` 是论文自动检索与周报生成脚本，支持：

1. 命令行关键词与期刊参数（可覆盖 JSON 配置）
2. 两层关键词机制（OpenAlex 全文检索 + 本地二层筛选）
3. 历史去重与状态持久化
4. OA 论文并发下载（3 worker）
5. 单篇最多尝试 3 个下载链接
6. 下载内容校验（PDF 头 + 最小文件大小）
7. JSON 配置校验（启动即校验）
8. 结构化日志（`logging` + `--log-level`）
9. 报告包含作者信息
10. `state.json` 支持时间戳（`seen_items`）
11. 每篇论文自动编号（格式 `WYYYYMMDD-001`）

---

## 快速开始

### 1) 推荐基础运行

```bash
python search_papers_enhanced_v2.py \
  --days 30 \
  --log-level INFO
```

### 2) 使用命令行关键词和期刊

```bash
python search_papers_enhanced_v2.py \
  -k earthquake -k fault \
  -r initiation -r rupture \
  -j GRL -j "Nature Geoscience" \
  --days 30
```

### 3) 禁用二层 refine（新增）

```bash
python search_papers_enhanced_v2.py \
  -k earthquake -k fault \
  --no-refine \
  --days 30
```

### 4) 忽略历史记录完整重跑

```bash
python search_papers_enhanced_v2.py \
  --days 30 \
  --ignore_history \
  --log-level INFO
```

### 5) 仅检查配置（不请求 API、不写文件）

```bash
python search_papers_enhanced_v2.py --dry-run
```

---

## 参数说明

| 参数 | 简写 | 说明 |
|---|---|---|
| `--keyword` | `-k` | 宽泛关键词，可重复 |
| `--refine-keyword` | `-r` | 二层筛选关键词，可重复 |
| `--no-refine` | - | 禁用二层筛选，覆盖 `-r` 和 JSON 的 `refine_keywords` |
| `--journal` | `-j` | 期刊（Source ID 或名称），可重复 |
| `--days` | - | 搜索最近 N 天 |
| `--keywords` | - | 指定关键词配置文件路径 |
| `--journals` | - | 指定期刊配置文件路径 |
| `--ignore_history` | - | 忽略 `state.json`（不读不写） |
| `--output` | `-o` | 输出目录 |
| `--no-download` | - | 不下载 PDF，仅输出报告 |
| `--post` | - | 发 GitHub Issue（默认不发） |
| `--dry-run` | - | 仅打印配置，不执行 |
| `--log-level` | - | `DEBUG/INFO/WARNING/ERROR` |

---

## 两层关键词机制

### 第一层（broad）

使用 OpenAlex 的 `search` 参数检索，范围由 OpenAlex 定义，通常包含：

1. 标题（`display_name`）
2. 摘要（`abstract_inverted_index`）
3. 已索引的全文 n-gram（若可用）

### 第二层（refine）

脚本本地筛选：将“标题 + 还原摘要”拼接后做关键词包含匹配。

1. 开启：默认（有 `refine_keywords` 时）
2. 关闭：`--no-refine`

---

## 下载策略

1. 只处理 OA 论文
2. 每篇论文最多尝试 3 个候选 URL
3. 全局并发下载 worker 数为 3
4. 校验规则：
   1. `Content-Type` 含 `pdf` 或文件头包含 `%PDF`
   2. 文件大小至少 `10KB`

---

## 配置文件校验

启动时会校验：

1. `journals.json` 必须为 `{ "Sxxxx": "Journal Name" }` 结构
2. `keywords.json` 的 `broad_keywords/refine_keywords/active_keywords`（若存在）必须是字符串数组

校验失败会直接退出，并输出错误日志。

---

## 状态文件说明

`state.json` 关键字段：

1. `seen_keys`：历史论文 key 列表（兼容字段）
2. `seen_items`：带时间戳的新结构，元素形如：

```json
{"key": "https://doi.org/...", "seen_at": "2026-02-11T15:30:01"}
```

3. `last_run_date`：最后运行日期

---

## 输出说明

默认输出目录：`downloads/YYYY-MM-DD`

主要产物：

1. `weekly_paper_info.md`
2. 下载成功的 PDF（若未使用 `--no-download`）

报告中包含：标题、期刊、作者、DOI、下载状态。
每条论文会附带编号，便于后续重命名与追踪。

---

## 常见问题

### 1) 想禁用 refine，但不改 JSON

使用：`--no-refine`

### 2) 想完全忽略历史去重

使用：`--ignore_history`

### 3) 期刊名称无法识别

优先检查 `journals.json`，或直接传 Source ID（例如 `S36624081`）。

### 4) API 连通失败（DNS）

先在本机检查：

```bash
nslookup api.openalex.org
curl -I https://api.openalex.org/works
```

---

## 相关文件

1. `search_papers_enhanced_v2.py`
2. `keywords.json`
3. `journals.json`
4. `state.json`
