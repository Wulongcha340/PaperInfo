# 「地震起止过程与机理」学术周报 - 全流程说明

## 流程概览

```
OpenAlex 检索（近 30 天 + 两级关键词）
    → 与 state.json 对比，得到“新增论文”待下载列表
    → 对 OA 论文尝试自动下载
    → 生成 weekly_paper_info.md（已下载列表 + 需手动下载列表，与周报正文区分）
    → 你手动下载未获取的论文到 downloads/
    → 【手动】调用 LLM 对 PDF 做系统分析，生成符合格式的周报 .md
    → 【可选】md_to_pdf.py 或浏览器打印为 PDF
```

---

## 1. 从 OpenAlex 检索近 30 天、匹配两级关键词的论文

**代码位置**：`search_papers.py` 中 `search_papers()`。

- **时间窗口**：默认 **30 天**（`DEFAULT_WINDOW_DAYS = 30`），可通过环境变量 `WINDOW_DAYS` 覆盖。**若 state.json 中尚无任何论文**（`seen_keys` 为空），则自动缩短为 **7 天**，以加快首次或清空后的检索。
- **期刊范围**：`journals.json` 中的 OpenAlex Source ID（如 GRL、Nature、Science 等）。
- **两级关键词**：
  - **一级（宽泛）**：`keywords.json` → `broad_keywords`（如 earthquake, fault, seismic），在 OpenAlex 中全文检索。
  - **二级（精炼）**：`keywords.json` → `refine_keywords`（如 initiation, rupture, nucleation, mechanism 等），在**标题 + 摘要**中做二次筛选。
- **结果**：每篇论文以 DOI 或 OpenAlex Work ID 为 key 去重，得到 `unique_works`。

**结论**：与“近 30 天 + 两级关键词”一致；默认窗口已改为 30 天。

---

## 2. 对比 state.json，生成“待下载论文”列表（仅新增）

**代码位置**：同上，`state = _load_state()`，`seen = set(state["seen_keys"])`，随后：

```python
unique_works = {k: w for k, w in unique_works.items() if k not in seen}
```

- **state.json**：存 `seen_keys`（已进入过周报的 DOI/Work ID 列表，**按进入顺序，新加入的在后**）和 `last_run_date`。
- **逻辑**：只保留 `key not in seen` 的论文，即**相对 state 的新增**，作为本次“待下载 / 待纳入周报”的列表。
- **数量上限**：`seen_keys` 最多保留 **100 条**（`MAX_SEEN_KEYS = 100`）。超过时**删除最早进入的**，只保留最近 100 条，以提升匹配速度。

**结论**：实现正确；所有本次纳入的论文都会写入 `seen_keys`；保存时自动裁剪到最近 100 条。

---

## 3. 对 Open Access 论文尝试自动下载；汇总为 weekly_paper_info.md

**代码位置**：同上，循环 `sorted_works`，对每篇：

- **OA 判定**：`work["open_access"]["is_oa"]`。
- **OA 且尝试下载**：调用 `download_pdf(work, filename, week_dir)`，支持多 URL（best_oa_location、primary_location、locations、以及 PNAS/Wiley 等 DOI 规则），可选 `cookies.json` 带 Cookie。
- **结果**：自动下载成功的计入“已自动获取的 PDF”；未成功或非 OA 的计入“需手动下载 / 自动下载失败的论文”（带期刊、DOI、下载入口链接）。
- **输出文件**：仅 **`downloads/<YYYY-MM-DD>/weekly_paper_info.md`**（本周论文信息，与周报正文区分）：检索参数 + 已下载列表 + 需手动下载列表。

**结论**：流程正确；只保留一份 weekly_paper_info，避免与周报（地震学学术周报.md）歧义。

---

## 4. 你手动下载论文后，调用 LLM 对 PDF 做系统分析并生成周报

**当前实现**：**未在仓库内实现**；由你在本地/IDE 中完成：

1. 将未自动下载的论文手动保存到 `downloads/`（或当日子目录）。
2. 在 Cursor 等环境中，基于 `weekly_paper_info.md` 与 PDF 列表，调用 LLM（如提供 PDF + 固定提示词），要求按既定格式输出：
   - 科学发现综述
   - 论文深度解析（研究对象与方法、核心物理机制、关键实验/观测数据、科学贡献）
   - 亮点论文推荐等。
3. 将 LLM 输出保存为 `地震学学术周报.md`（或你指定的文件名）。

**建议**（可选）：

- 在 `README` 或本流程说明中写明：周报正文由“人工 + LLM + PDF”生成，且 LLM 步骤在仓库外完成。
- 若希望半自动化：可新增脚本（如 `generate_weekly_report.py`），只做：列出 `downloads/` 下 PDF、生成给 LLM 的提示模板、或调用本地/API 的 LLM；具体调用方式依你使用的模型与 API 而定。

---

## 5. 将周报 md 转为美观 PDF（可选）

**实现**：`md_to_pdf.py` + `report.css`。

- 使用 pandoc 将 `地震学学术周报.md` 转为 HTML，再通过 weasyprint 或浏览器“打印 → 另存为 PDF”得到 PDF。
- 字体、版式、日期、标题等均在 `report.css` 中配置。

**结论**：与“生成符合格式的周报”解耦，流程上正确。

---

## 代码修改小结

| 项目 | 说明 |
|------|------|
| **state 未写入已下载论文** | 已修复：所有本次纳入的论文（含自动下载成功）都会加入 `seen_keys`，避免下次重复。 |
| **默认时间窗口** | 由 45 天改为 **30 天**（`DEFAULT_WINDOW_DAYS = 30`），与“近 30 天”一致。 |
| **只保留 weekly_paper_info** | 原 weekly_report 与 download_summary 功能重叠，现只生成 **weekly_paper_info.md**（检索参数 + 已下载 + 需手动下载），避免与周报正文歧义。 |

---

## 建议检查清单

- [ ] **keywords.json**：`broad_keywords` 与 `refine_keywords` 是否符合当前选题。
- [ ] **journals.json**：OpenAlex Source ID 是否覆盖你关心的期刊。
- [ ] **WINDOW_DAYS**：CI/本地若需 30 天以外窗口，可设环境变量 `WINDOW_DAYS=45` 等。
- [ ] **cookies.json**：机构订阅站点（如 Wiley）若需登录态，按注释配置，且勿提交到仓库。
- [ ] **LLM 步骤**：确认“手动下载 → 用 LLM 分析 PDF → 写周报 .md”的提示词与格式要求已固定，便于复现。

若你希望把“调用 LLM 生成周报”也做成可复现脚本（例如读 `downloads/` 列表 + 调用 OpenAI/本地模型），可以说明当前用的模型与接口，我可以再给出具体脚本草稿。
