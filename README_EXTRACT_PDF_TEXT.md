# extract_pdf_text.py 使用说明（升级版）

## 概述

`extract_pdf_text.py` 现在是一个面向大模型总结流程的“混合提取管线”：

1. 多引擎数字文本提取（`pymupdf` -> `pdfplumber` -> `pypdf`）
2. 低质量结果自动 OCR 回退（默认开启，依赖 `ocrmypdf`）
3. 文本清洗与分块
4. 同时输出 `txt`、每篇 `chunks.jsonl`、全局 `extract_chunks.jsonl` 和 `extract_manifest.json`
5. 可基于 `weekly_paper_info.md` 自动匹配并重命名 PDF（支持 dry-run/apply/rollback）

默认输出目录：`<input_dir>/extracted_text`

---

## 默认行为

不传 `--input-dir` 时，脚本会自动选择：

1. `downloads` 根目录（若其中直接有 PDF）
2. 否则选择 `downloads` 下“最近修改且包含 PDF”的子目录

不传 `--output-dir` 时，输出写到：

`<input_dir>/extracted_text`

---

## 参数

| 参数 | 说明 |
|---|---|
| `--input-dir PATH` | 输入 PDF 目录 |
| `--output-dir PATH` | 输出目录（默认 `<input_dir>/extracted_text`） |
| `--file NAME_OR_PATH` | 只处理指定 PDF（可重复） |
| `--max-chars N` | 单个 PDF 最大提取字符数（默认 `120000`） |
| `--min-chars-warning N` | 提取字符低于阈值告警（默认 `500`） |
| `--min-chars-per-page-warning N` | 每页字符低于阈值告警（默认 `80`） |
| `--engine-order LIST` | 提取引擎顺序，逗号分隔（默认 `pymupdf,pdfplumber,pypdf`） |
| `--disable-ocr-fallback` | 禁用低质量时 OCR 回退 |
| `--ocr-timeout N` | OCR 超时秒数（默认 `600`） |
| `--chunk-chars N` | 分块目标长度（默认 `2500`） |
| `--log-level LEVEL` | `DEBUG/INFO/WARNING/ERROR` |
| `--dry-run` | 只打印计划，不写文件 |
| `--rename-from-weekly PATH` | 从 `weekly_paper_info.md` 读取论文信息并触发重命名流程 |
| `--rename-dry-run` | 仅生成重命名映射，不改名 |
| `--rename-apply` | 实际执行重命名 |
| `--rename-manifest PATH` | 重命名映射输出路径（默认 `<output_dir>/rename_manifest.json`） |
| `--rename-rollback PATH` | 根据既有 rename manifest 回滚重命名并退出 |

---

## 输出文件

输出目录中包含：

1. `extracted_<stem>_<hash>.txt`
   - 清洗后的全文文本
2. `extracted_<stem>_<hash>.chunks.jsonl`
   - 单篇分块结果
3. `extract_chunks.jsonl`
   - 全部论文的合并分块（最适合直接喂给 LLM）
4. `extract_manifest.json`
   - 运行配置、每篇状态、引擎尝试详情、质量指标、错误信息

---

## 提取策略

### 1) 数字文本提取

按 `--engine-order` 逐个尝试，成功后优先选择结果；默认引擎顺序：

1. `pymupdf`
2. `pdfplumber`
3. `pypdf`

### 2) 质量评估

若任一条件满足，判定低质量：

1. `chars < min_chars_warning`
2. `chars/pages < min_chars-per-page-warning`

### 3) OCR 回退

当低质量且未禁用 OCR 时：

1. 调用 `ocrmypdf --skip-text --force-ocr`
2. 对 OCR 后 PDF 再跑同一引擎链
3. 若 OCR 结果字符数更好或不差，替换原结果

---

## 分块与清洗（面向 LLM）

脚本会做轻量清洗：

1. 统一换行
2. 修复断词连字符（`word-\nword`）
3. 合并明显换行断句

然后按段落分块（默认每块 ~2500 字符），并尽量识别小节标题，输出到 JSONL。

---

## 示例命令

### 基础运行

```bash
python extract_pdf_text.py
```

### 指定目录并保留 OCR 回退

```bash
python extract_pdf_text.py \
  --input-dir downloads/2026-02-11 \
  --log-level INFO
```

### 只处理一个文件，关闭 OCR

```bash
python extract_pdf_text.py \
  --input-dir downloads/2026-02-11 \
  --file "paper.pdf" \
  --disable-ocr-fallback
```

### 自定义引擎顺序

```bash
python extract_pdf_text.py \
  --input-dir downloads/2026-02-11 \
  --engine-order pypdf
```

### 仅预览

```bash
python extract_pdf_text.py --dry-run
```

### 基于 weekly 文件做重命名（只预览，不改名）

```bash
python extract_pdf_text.py \
  --input-dir downloads/2026-02-11 \
  --rename-from-weekly downloads/2026-02-11/weekly_paper_info.md \
  --rename-dry-run \
  --log-level INFO
```

### 基于 weekly 文件实际改名

```bash
python extract_pdf_text.py \
  --input-dir downloads/2026-02-11 \
  --rename-from-weekly downloads/2026-02-11/weekly_paper_info.md \
  --rename-apply \
  --log-level INFO
```

### 回滚重命名

```bash
python extract_pdf_text.py \
  --rename-rollback downloads/2026-02-11/extracted_text/rename_manifest.json \
  --log-level INFO
```

---

## 退出码

1. `0`：成功（或无 PDF）
2. `1`：存在失败文件（missing/error）

重命名回滚模式同样遵循：成功 `0`，失败 `1`。

---

## 依赖建议

最小依赖：

1. `pypdf`

推荐增强：

1. `PyMuPDF`（`fitz`）
2. `pdfplumber`
3. `ocrmypdf`（系统命令，用于 OCR 回退）

未安装增强依赖时，脚本会在 manifest 里记录对应引擎失败原因，不会中断整个流程。

---

## 重命名规则说明

重命名目标格式：

`周报编号_期刊_第一作者姓氏_标题关键词.pdf`

示例：`W20260211-003_GRL_Smith_fault_rupture_dynamics.pdf`

匹配策略（按优先级）：

1. DOI 精确匹配
2. 标题文本相似度 + 关键词重叠（结合提取文本和原文件名）
3. 辅助特征：期刊、第一作者姓氏

安全策略：

1. 默认建议先 `--rename-dry-run`
2. 匹配不确定时不会强制改名（会写入 `rename_manifest.json`）
3. 若目标重名自动加后缀（`_2`、`_3`...）
