#!/usr/bin/env python3
"""Hybrid PDF text extraction for LLM summarization pipelines."""

import argparse
import csv
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import pypdf

LOGGER = logging.getLogger("extract_pdf_text")


def setup_logging(level: str) -> None:
    level_name = (level or "INFO").upper()
    log_level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Extract text from PDFs with multi-engine extraction + OCR fallback. "
            "Default output dir: <input_dir>/extracted_text."
        )
    )
    parser.add_argument("--input-dir", type=str, default=None, help="PDF directory.")
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory (default: <input_dir>/extracted_text).",
    )
    parser.add_argument(
        "--file",
        action="append",
        dest="files",
        default=None,
        help="Specific PDF path or filename under input-dir. Can be repeated.",
    )
    parser.add_argument("--max-chars", type=int, default=120000, help="Max chars per PDF.")
    parser.add_argument(
        "--min-chars-warning",
        type=int,
        default=500,
        help="Warn when extracted chars are below this threshold.",
    )
    parser.add_argument(
        "--min-chars-per-page-warning",
        type=int,
        default=80,
        help="Warn when chars/page are below this threshold.",
    )
    parser.add_argument(
        "--engine-order",
        type=str,
        default="pymupdf,pdfplumber,pypdf",
        help="Comma-separated extraction engine order.",
    )
    parser.add_argument(
        "--disable-ocr-fallback",
        action="store_true",
        help="Disable OCR fallback on low-quality extraction.",
    )
    parser.add_argument(
        "--ocr-timeout",
        type=int,
        default=600,
        help="OCR timeout in seconds.",
    )
    parser.add_argument(
        "--chunk-chars",
        type=int,
        default=2500,
        help="Target max chars per chunk for JSONL output.",
    )
    parser.add_argument(
        "--extract-images",
        action="store_true",
        help="Extract figure images and align with nearby text.",
    )
    parser.add_argument(
        "--extract-tables",
        action="store_true",
        help="Extract tables and align with nearby text.",
    )
    parser.add_argument(
        "--image-min-width",
        type=int,
        default=120,
        help="Minimum image width in pixels.",
    )
    parser.add_argument(
        "--image-min-height",
        type=int,
        default=120,
        help="Minimum image height in pixels.",
    )
    parser.add_argument(
        "--image-min-bytes",
        type=int,
        default=2048,
        help="Minimum raw image bytes.",
    )
    parser.add_argument(
        "--table-min-cells",
        type=int,
        default=6,
        help="Minimum cell count for a detected table.",
    )
    parser.add_argument(
        "--alignment-context-chars",
        type=int,
        default=900,
        help="Max chars for alignment context text.",
    )
    parser.add_argument(
        "--alignment-neighbor-pages",
        type=int,
        default=1,
        help="How many nearby pages to search for captions when current page fails.",
    )
    parser.add_argument(
        "--save-intermediate",
        action="store_true",
        help="Write alignment debug artifacts for manual reliability checks.",
    )
    parser.add_argument(
        "--image-dir-name",
        type=str,
        default="images",
        help="Subdirectory name for extracted images.",
    )
    parser.add_argument(
        "--table-dir-name",
        type=str,
        default="tables",
        help="Subdirectory name for extracted tables.",
    )
    parser.add_argument(
        "--intermediate-dir-name",
        type=str,
        default="_intermediate",
        help="Subdirectory name for intermediate alignment artifacts.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    parser.add_argument("--dry-run", action="store_true", help="No writes, just planning.")
    parser.add_argument(
        "--rename-from-weekly",
        type=str,
        default=None,
        help="Path to weekly_paper_info.md used for PDF renaming.",
    )
    parser.add_argument(
        "--rename-dry-run",
        action="store_true",
        help="Show rename mapping only, do not rename files.",
    )
    parser.add_argument(
        "--rename-apply",
        action="store_true",
        help="Apply PDF renaming based on weekly file matching.",
    )
    parser.add_argument(
        "--rename-manifest",
        type=str,
        default=None,
        help="Path to write rename manifest (default: <output_dir>/rename_manifest.json).",
    )
    parser.add_argument(
        "--rename-rollback",
        type=str,
        default=None,
        help="Rollback renaming using a previous rename manifest JSON, then exit.",
    )
    return parser.parse_args()


def detect_default_input_dir(script_dir: Path) -> Path:
    downloads = script_dir / "downloads"
    if not downloads.exists():
        return downloads
    if any(downloads.glob("*.pdf")):
        return downloads
    candidates = []
    for child in downloads.iterdir():
        if child.is_dir():
            count = len(list(child.glob("*.pdf")))
            if count:
                candidates.append((child.stat().st_mtime, child))
    if not candidates:
        return downloads
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def resolve_input_dir(script_dir: Path, input_dir: str | None) -> Path:
    if input_dir:
        p = Path(input_dir).expanduser()
        if not p.is_absolute():
            p = (script_dir / p).resolve()
        return p
    return detect_default_input_dir(script_dir).resolve()


def resolve_pdf_list(input_dir: Path, files):
    if files:
        out = []
        for item in files:
            p = Path(item).expanduser()
            if not p.is_absolute():
                p = input_dir / item
            out.append(p.resolve())
        return out
    return sorted(input_dir.glob("*.pdf"))


def safe_stem(pdf_path: Path, max_len: int = 70) -> str:
    stem = re.sub(r"\s+", "_", pdf_path.stem).strip("_")
    stem = re.sub(r"[^0-9A-Za-z._-]", "_", stem)
    stem = stem[:max_len] or "untitled"
    digest = hashlib.sha1(str(pdf_path).encode("utf-8")).hexdigest()[:8]
    return f"{stem}_{digest}"


def is_heading(paragraph: str) -> bool:
    p = paragraph.strip()
    if not p:
        return False
    if len(p) > 120:
        return False
    if re.match(r"^(\d+(\.\d+)*|[IVXLC]+)\s+", p):
        return True
    if p.isupper() and len(p.split()) <= 14:
        return True
    if p.endswith(":") and len(p.split()) <= 12:
        return True
    return False


def clean_text(text: str) -> str:
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)
    # Join likely wrapped lines (lowercase-ending line + lowercase-start line).
    t = re.sub(r"([a-z0-9,;])\n([a-z])", r"\1 \2", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def chunk_text(text: str, chunk_chars: int):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    current = []
    current_chars = 0
    current_section = "Unknown"

    def flush():
        nonlocal current, current_chars
        if not current:
            return
        joined = "\n\n".join(current).strip()
        if joined:
            chunks.append({"section": current_section, "text": joined, "chars": len(joined)})
        current = []
        current_chars = 0

    for para in paragraphs:
        if is_heading(para):
            flush()
            current_section = para
            continue
        para_chars = len(para) + (2 if current else 0)
        if current and current_chars + para_chars > chunk_chars:
            flush()
        current.append(para)
        current_chars += para_chars
    flush()
    for i, chunk in enumerate(chunks, start=1):
        chunk["chunk_id"] = i
    return chunks


FIGURE_CAPTION_RE = re.compile(r"^\s*(figure|fig\.?|图)\s*([0-9ivxlcdm]+)?[\s\.\-:：]", flags=re.IGNORECASE)
TABLE_CAPTION_RE = re.compile(r"^\s*(table|tab\.?|表)\s*([0-9ivxlcdm]+)?[\s\.\-:：]", flags=re.IGNORECASE)


def block_to_dict(block):
    x0, y0, x1, y1, text, bno, btype = block
    return {
        "x0": float(x0),
        "y0": float(y0),
        "x1": float(x1),
        "y1": float(y1),
        "text": (text or "").strip(),
        "block_no": int(bno),
        "block_type": int(btype),
    }


def page_blocks_to_text(page_blocks):
    lines = []
    for b in page_blocks:
        t = (b.get("text") or "").strip()
        if not t:
            continue
        lines.append(t)
    return clean_text("\n".join(lines))


def detect_caption(text: str, kind: str):
    t = (text or "").strip()
    if not t:
        return {"is_caption": False, "label": "", "number": "", "pattern": ""}
    if kind == "image":
        m = FIGURE_CAPTION_RE.search(t)
        if m:
            return {"is_caption": True, "label": m.group(1), "number": (m.group(2) or ""), "pattern": "figure"}
    if kind == "table":
        m = TABLE_CAPTION_RE.search(t)
        if m:
            return {"is_caption": True, "label": m.group(1), "number": (m.group(2) or ""), "pattern": "table"}
    return {"is_caption": False, "label": "", "number": "", "pattern": ""}


def bbox_distance(bbox_a, bbox_b):
    ax0, ay0, ax1, ay1 = bbox_a
    bx0, by0, bx1, by1 = bbox_b
    if ax1 < bx0:
        dx = bx0 - ax1
    elif bx1 < ax0:
        dx = ax0 - bx1
    else:
        dx = 0.0

    if ay1 < by0:
        dy = by0 - ay1
    elif by1 < ay0:
        dy = ay0 - by1
    else:
        dy = 0.0
    return (dx, dy)


def overlap_ratio_1d(a0, a1, b0, b1):
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    denom = max(1e-6, min(a1 - a0, b1 - b0))
    return inter / denom


def score_caption_candidate(target_bbox, block, page_gap: int, kind: str):
    block_bbox = (block["x0"], block["y0"], block["x1"], block["y1"])
    dx, dy = bbox_distance(target_bbox, block_bbox)
    x_overlap = overlap_ratio_1d(target_bbox[0], target_bbox[2], block["x0"], block["x1"])
    y_center_target = (target_bbox[1] + target_bbox[3]) / 2
    y_center_block = (block["y0"] + block["y1"]) / 2
    vertical_after = 1.0 if y_center_block >= y_center_target else 0.0

    caption_hit = detect_caption(block.get("text", ""), kind=kind)
    caption_bonus = 0.4 if caption_hit["is_caption"] else 0.0
    score = 0.0
    score += max(0.0, 0.45 - min(0.45, dy / 260.0))
    score += max(0.0, 0.2 - min(0.2, dx / 200.0))
    score += min(0.2, x_overlap * 0.2)
    score += vertical_after * 0.1
    score += caption_bonus
    score -= min(0.25, page_gap * 0.25)
    score = max(0.0, min(1.0, score))
    return {
        "score": round(score, 4),
        "x_overlap": round(x_overlap, 4),
        "dx": round(dx, 2),
        "dy": round(dy, 2),
        "page_gap": page_gap,
        "caption_hit": caption_hit,
        "vertical_after": vertical_after,
    }


def sort_blocks_for_reading(blocks):
    return sorted(blocks, key=lambda b: (round(b["y0"], 1), round(b["x0"], 1)))


def build_context_text(page_layouts, page_index: int, anchor_bbox, max_chars: int):
    blocks = sort_blocks_for_reading(page_layouts[page_index]["blocks"])
    scored = []
    for b in blocks:
        bx = (b["x0"], b["y0"], b["x1"], b["y1"])
        dx, dy = bbox_distance(anchor_bbox, bx)
        dist = (dx * dx + dy * dy) ** 0.5
        if not b.get("text"):
            continue
        scored.append((dist, b["text"]))
    scored.sort(key=lambda x: x[0])
    out = []
    total = 0
    for _, txt in scored:
        if total >= max_chars:
            break
        remain = max_chars - total
        picked = txt[:remain]
        out.append(picked)
        total += len(picked) + 1
    return clean_text("\n".join(out))[:max_chars]


def align_visual_with_text(page_layouts, target_page_idx: int, target_bbox, kind: str, neighbor_pages: int, context_chars: int):
    page_count = len(page_layouts)
    candidates = []
    for gap in range(0, max(0, neighbor_pages) + 1):
        candidate_pages = [target_page_idx]
        if gap > 0:
            candidate_pages = []
            if target_page_idx - gap >= 0:
                candidate_pages.append(target_page_idx - gap)
            if target_page_idx + gap < page_count:
                candidate_pages.append(target_page_idx + gap)
        for pidx in candidate_pages:
            for block in page_layouts[pidx]["blocks"]:
                if block.get("block_type", 0) != 0:
                    continue
                if not block.get("text"):
                    continue
                s = score_caption_candidate(target_bbox, block, page_gap=abs(pidx - target_page_idx), kind=kind)
                if s["score"] <= 0:
                    continue
                candidates.append(
                    {
                        "page_index": pidx,
                        "block": block,
                        "score": s["score"],
                        "metrics": s,
                    }
                )
    candidates.sort(key=lambda x: x["score"], reverse=True)
    if not candidates:
        return {
            "caption_guess": "",
            "caption_page": target_page_idx + 1,
            "figure_or_table_id": "",
            "alignment_confidence": 0.0,
            "alignment_method": "none",
            "context_text": "",
            "candidate_count": 0,
            "top_candidates": [],
        }

    best = candidates[0]
    best_caption = best["block"]["text"]
    cap = detect_caption(best_caption, kind=kind)
    target_page = best["page_index"]
    context = build_context_text(
        page_layouts=page_layouts,
        page_index=target_page,
        anchor_bbox=(best["block"]["x0"], best["block"]["y0"], best["block"]["x1"], best["block"]["y1"]),
        max_chars=context_chars,
    )
    method = "bbox+caption" if cap["is_caption"] else "bbox"
    conf = best["score"]
    if cap["is_caption"] and best["metrics"]["page_gap"] == 0:
        conf = min(1.0, conf + 0.08)
    if best["metrics"]["page_gap"] > 0:
        method = "cross-page-" + method
        conf = max(0.0, conf - 0.08 * best["metrics"]["page_gap"])

    top_candidates = []
    for c in candidates[:5]:
        top_candidates.append(
            {
                "page": c["page_index"] + 1,
                "score": c["score"],
                "text": c["block"]["text"][:220],
                "metrics": c["metrics"],
            }
        )
    return {
        "caption_guess": best_caption,
        "caption_page": target_page + 1,
        "figure_or_table_id": cap["number"] or "",
        "alignment_confidence": round(max(0.0, min(1.0, conf)), 4),
        "alignment_method": method,
        "context_text": context,
        "candidate_count": len(candidates),
        "top_candidates": top_candidates,
    }


def load_page_layouts(pdf_path: Path):
    import fitz

    layouts = []
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            blocks = [block_to_dict(b) for b in page.get_text("blocks")]
            layouts.append(
                {
                    "page_number": page.number + 1,
                    "width": float(page.rect.width),
                    "height": float(page.rect.height),
                    "blocks": blocks,
                    "page_text": page_blocks_to_text(blocks),
                }
            )
    return layouts


def extract_images_with_alignment(
    pdf_path: Path,
    output_dir: Path,
    base_stem: str,
    page_layouts,
    min_width: int,
    min_height: int,
    min_bytes: int,
    neighbor_pages: int,
    context_chars: int,
):
    import fitz

    image_root = output_dir / base_stem
    image_root.mkdir(parents=True, exist_ok=True)
    records = []
    seen_sha = {}
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            images = page.get_images(full=True)
            if not images:
                continue
            page_index = page.number
            for image_idx, meta in enumerate(images, start=1):
                xref = meta[0]
                try:
                    ext = "png"
                    raw = doc.extract_image(xref)
                    data = raw.get("image", b"")
                    width = int(raw.get("width", 0))
                    height = int(raw.get("height", 0))
                    ext = raw.get("ext", ext) or ext
                except Exception as exc:
                    LOGGER.warning("Image extract failed (page=%s, xref=%s): %s", page_index + 1, xref, exc)
                    continue
                if width < min_width or height < min_height or len(data) < min_bytes:
                    continue
                sha = hashlib.sha1(data).hexdigest()
                if sha in seen_sha:
                    continue
                seen_sha[sha] = True
                rects = page.get_image_rects(xref)
                if rects:
                    r = rects[0]
                    bbox = (float(r.x0), float(r.y0), float(r.x1), float(r.y1))
                else:
                    bbox = (0.0, 0.0, float(page.rect.width), float(page.rect.height))
                align = align_visual_with_text(
                    page_layouts=page_layouts,
                    target_page_idx=page_index,
                    target_bbox=bbox,
                    kind="image",
                    neighbor_pages=neighbor_pages,
                    context_chars=context_chars,
                )
                filename = f"p{page_index + 1:03d}_img{image_idx:02d}_{sha[:10]}.{ext}"
                out_path = image_root / filename
                out_path.write_bytes(data)
                record = {
                    "source_pdf": str(pdf_path),
                    "source_pdf_name": pdf_path.name,
                    "page": page_index + 1,
                    "xref": xref,
                    "image_id": f"p{page_index + 1}_img{image_idx}",
                    "path": str(out_path),
                    "ext": ext,
                    "width": width,
                    "height": height,
                    "bytes": len(data),
                    "sha1": sha,
                    "bbox": [round(x, 2) for x in bbox],
                    "caption_guess": align["caption_guess"],
                    "caption_page": align["caption_page"],
                    "figure_id": align["figure_or_table_id"],
                    "context_text": align["context_text"],
                    "alignment_confidence": align["alignment_confidence"],
                    "alignment_method": align["alignment_method"],
                    "alignment_candidate_count": align["candidate_count"],
                    "alignment_top_candidates": align["top_candidates"],
                    "needs_review": align["alignment_confidence"] < 0.5,
                }
                records.append(record)
                LOGGER.info(
                    "Image aligned: %s p%s conf=%.2f caption=%s",
                    pdf_path.name,
                    page_index + 1,
                    record["alignment_confidence"],
                    (record["caption_guess"] or "")[:80],
                )
    return records


def write_table_csv(path: Path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        for row in rows:
            if isinstance(row, list):
                writer.writerow([("" if v is None else str(v)) for v in row])
            else:
                writer.writerow([str(row)])


def extract_tables_with_alignment(
    pdf_path: Path,
    output_dir: Path,
    base_stem: str,
    page_layouts,
    min_cells: int,
    neighbor_pages: int,
    context_chars: int,
):
    import pdfplumber

    table_root = output_dir / base_stem
    table_root.mkdir(parents=True, exist_ok=True)
    records = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_index, page in enumerate(pdf.pages):
            try:
                tables = page.find_tables()
            except Exception as exc:
                LOGGER.warning("Table detect failed (page=%s): %s", page_index + 1, exc)
                continue
            for table_idx, table in enumerate(tables, start=1):
                rows = table.extract() or []
                if not rows:
                    continue
                cells = sum(1 for r in rows for c in (r or []) if c not in (None, ""))
                if cells < min_cells:
                    continue
                bbox = table.bbox or (0, 0, page.width, page.height)
                x0, top, x1, bottom = [float(v) for v in bbox]
                align = align_visual_with_text(
                    page_layouts=page_layouts,
                    target_page_idx=page_index,
                    target_bbox=(x0, top, x1, bottom),
                    kind="table",
                    neighbor_pages=neighbor_pages,
                    context_chars=context_chars,
                )
                csv_path = table_root / f"p{page_index + 1:03d}_table{table_idx:02d}.csv"
                write_table_csv(csv_path, rows)
                preview_rows = rows[:8]
                record = {
                    "source_pdf": str(pdf_path),
                    "source_pdf_name": pdf_path.name,
                    "page": page_index + 1,
                    "table_id": f"p{page_index + 1}_table{table_idx}",
                    "path_csv": str(csv_path),
                    "bbox": [round(x0, 2), round(top, 2), round(x1, 2), round(bottom, 2)],
                    "rows": len(rows),
                    "cols": max((len(r) for r in rows), default=0),
                    "non_empty_cells": cells,
                    "caption_guess": align["caption_guess"],
                    "caption_page": align["caption_page"],
                    "table_number": align["figure_or_table_id"],
                    "context_text": align["context_text"],
                    "alignment_confidence": align["alignment_confidence"],
                    "alignment_method": align["alignment_method"],
                    "alignment_candidate_count": align["candidate_count"],
                    "alignment_top_candidates": align["top_candidates"],
                    "preview_rows": preview_rows,
                    "needs_review": align["alignment_confidence"] < 0.5,
                }
                records.append(record)
                LOGGER.info(
                    "Table aligned: %s p%s conf=%.2f caption=%s",
                    pdf_path.name,
                    page_index + 1,
                    record["alignment_confidence"],
                    (record["caption_guess"] or "")[:80],
                )
    return records


def write_intermediate_alignment(path: Path, page_layouts, images, tables):
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "page_layouts": [
            {
                "page_number": p["page_number"],
                "width": p["width"],
                "height": p["height"],
                "block_count": len(p["blocks"]),
                "blocks": p["blocks"],
            }
            for p in page_layouts
        ],
        "images": images,
        "tables": tables,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_with_pypdf(pdf_path: Path, max_chars: int):
    reader = pypdf.PdfReader(str(pdf_path))
    parts = []
    total = 0
    per_page_chars = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if total + len(text) > max_chars:
            text = text[: max_chars - total]
        parts.append(text)
        page_chars = len(text)
        per_page_chars.append(page_chars)
        total += page_chars
        if total >= max_chars:
            break
    return {
        "text": "\n".join(parts),
        "chars": total,
        "pages": len(reader.pages),
        "per_page_chars": per_page_chars,
    }


def extract_with_pdfplumber(pdf_path: Path, max_chars: int):
    import pdfplumber

    parts = []
    total = 0
    per_page_chars = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = pdf.pages
        for page in pages:
            text = page.extract_text() or ""
            if total + len(text) > max_chars:
                text = text[: max_chars - total]
            parts.append(text)
            page_chars = len(text)
            per_page_chars.append(page_chars)
            total += page_chars
            if total >= max_chars:
                break
        page_count = len(pages)
    return {
        "text": "\n".join(parts),
        "chars": total,
        "pages": page_count,
        "per_page_chars": per_page_chars,
    }


def extract_with_pymupdf(pdf_path: Path, max_chars: int):
    import fitz

    parts = []
    total = 0
    per_page_chars = []
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            text = page.get_text("text") or ""
            if total + len(text) > max_chars:
                text = text[: max_chars - total]
            parts.append(text)
            page_chars = len(text)
            per_page_chars.append(page_chars)
            total += page_chars
            if total >= max_chars:
                break
        page_count = len(doc)
    return {
        "text": "\n".join(parts),
        "chars": total,
        "pages": page_count,
        "per_page_chars": per_page_chars,
    }


ENGINE_EXTRACTORS = {
    "pypdf": extract_with_pypdf,
    "pdfplumber": extract_with_pdfplumber,
    "pymupdf": extract_with_pymupdf,
}


def extract_with_engine_chain(pdf_path: Path, engine_order, max_chars: int):
    attempts = []
    best = None
    for engine in engine_order:
        extractor = ENGINE_EXTRACTORS.get(engine)
        if extractor is None:
            attempts.append({"engine": engine, "ok": False, "error": "unknown_engine"})
            continue
        started = time.perf_counter()
        try:
            out = extractor(pdf_path, max_chars=max_chars)
            elapsed = time.perf_counter() - started
            text = clean_text(out["text"])
            result = {
                "ok": True,
                "engine": engine,
                "text": text,
                "chars": len(text),
                "pages": out["pages"],
                "per_page_chars": out["per_page_chars"],
                "elapsed_sec": round(elapsed, 3),
                "error": None,
            }
            attempts.append({"engine": engine, "ok": True, "chars": result["chars"], "error": None})
            if best is None or result["chars"] > best["chars"]:
                best = result
            if result["chars"] > 0:
                break
        except Exception as exc:
            elapsed = time.perf_counter() - started
            attempts.append(
                {
                    "engine": engine,
                    "ok": False,
                    "error": str(exc),
                    "elapsed_sec": round(elapsed, 3),
                }
            )
    if best is None:
        return {
            "ok": False,
            "engine": None,
            "text": "",
            "chars": 0,
            "pages": 0,
            "per_page_chars": [],
            "elapsed_sec": 0,
            "error": "all_engines_failed",
            "attempts": attempts,
        }
    best["attempts"] = attempts
    return best


def is_low_quality(result, min_chars_warning: int, min_cpp_warning: int):
    chars = result.get("chars", 0)
    pages = max(1, result.get("pages", 0))
    cpp = chars / pages
    return chars < min_chars_warning or cpp < min_cpp_warning


def run_ocrmypdf(src_pdf: Path, timeout_sec: int):
    if shutil.which("ocrmypdf") is None:
        return {"ok": False, "output_pdf": None, "error": "ocrmypdf_not_found"}
    with tempfile.TemporaryDirectory(prefix="ocr_pdf_") as tmpdir:
        out_pdf = Path(tmpdir) / f"{src_pdf.stem}.ocr.pdf"
        cmd = [
            "ocrmypdf",
            "--skip-text",
            "--force-ocr",
            "--optimize",
            "0",
            str(src_pdf),
            str(out_pdf),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            return {"ok": False, "output_pdf": None, "error": "ocr_timeout"}
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            return {"ok": False, "output_pdf": None, "error": f"ocr_failed: {stderr[:300]}"}

        # Copy OCR result to a durable temp path because TemporaryDirectory will be removed.
        durable = Path(tempfile.gettempdir()) / f"{src_pdf.stem}.ocr.{os.getpid()}.pdf"
        shutil.copy2(out_pdf, durable)
        return {"ok": True, "output_pdf": durable, "error": None}


def write_manifest(output_dir: Path, rows, run_config: dict) -> Path:
    path = output_dir / "extract_manifest.json"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "run_config": run_config,
        "files": rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def append_jsonl(path: Path, records):
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def normalize_text(s: str) -> str:
    if not s:
        return ""
    t = s.lower()
    t = re.sub(r"https?://doi\.org/", "", t)
    t = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_doi(doi: str) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    return d.rstrip(").,; ")


def extract_doi_from_text(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"(10\.\d{4,9}/[-._;()/:a-z0-9]+)", text.lower())
    return normalize_doi(m.group(1)) if m else ""


def tokenize(s: str):
    stop = {
        "the",
        "and",
        "of",
        "in",
        "on",
        "for",
        "with",
        "a",
        "an",
        "to",
        "by",
        "from",
        "at",
        "using",
        "based",
        "study",
        "analysis",
    }
    toks = normalize_text(s).split()
    return [x for x in toks if x and x not in stop and len(x) > 1]


def first_author_lastname(authors: str) -> str:
    if not authors:
        return "UnknownAuthor"
    first = re.split(r",|;| and ", authors)[0].strip()
    if not first:
        return "UnknownAuthor"
    parts = first.split()
    if len(parts) == 1:
        return parts[0]
    return parts[-1]


def sanitize_component(s: str, max_len: int) -> str:
    out = re.sub(r"\s+", "_", (s or "").strip())
    out = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]", "_", out)
    out = re.sub(r"_+", "_", out).strip("_")
    if not out:
        out = "NA"
    return out[:max_len]


def build_title_keywords(title: str, n: int = 6) -> str:
    toks = tokenize(title)
    if not toks:
        toks = tokenize(title[:80]) or ["paper"]
    return "_".join(toks[:n])


def parse_weekly_paper_info(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    entries = []

    # Section style:
    # #### title
    # - **期刊**: ...
    # - **作者**: ...
    # - **DOI**: ...
    block_pattern = re.compile(
        r"####\s+(?:\[(?P<weekly_id>[^\]]+)\]\s+)?(?P<title>.+?)\n(?:- \*\*编号\*\*: (?P<num_line>.*?)\n)?- \*\*期刊\*\*: (?P<journal>.+?)\n(?:- \*\*作者\*\*: (?P<authors>.+?)\n)?- \*\*DOI\*\*: (?P<doi>.*?)(?:\n|$)",
        flags=re.MULTILINE,
    )
    for m in block_pattern.finditer(text):
        wid = (m.group("weekly_id") or m.group("num_line") or "").strip()
        entries.append(
            {
                "title": m.group("title").strip(),
                "journal": m.group("journal").strip(),
                "authors": (m.group("authors") or "").strip(),
                "doi": normalize_doi((m.group("doi") or "").strip()),
                "weekly_id": wid,
            }
        )

    # Downloaded list style:
    # - ✅ **[已下载]** title (journal)
    #   - 作者: xxx
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(
            r"^- .*?\*\*.*?\*\*\s+(?:\[(?P<weekly_id>[^\]]+)\]\s+)?(?P<title>.+?)\s+\((?P<journal>.+?)\)\s*$",
            line,
        )
        if not m:
            continue
        authors = ""
        if i + 1 < len(lines):
            a = re.match(r"^\s*-\s*作者:\s*(.+?)\s*$", lines[i + 1])
            if a:
                authors = a.group(1).strip()
        entries.append(
            {
                "title": m.group("title").strip(),
                "journal": m.group("journal").strip(),
                "authors": authors,
                "doi": "",
                "weekly_id": (m.group("weekly_id") or "").strip(),
            }
        )

    # deduplicate by normalized title+journal
    uniq = []
    seen = set()
    for e in entries:
        key = f"{e.get('weekly_id','')}|{normalize_text(e['title'])}|{normalize_text(e['journal'])}"
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    return uniq


def make_unique_target(source: Path, target_name: str) -> Path:
    target = source.with_name(target_name)
    if not target.exists() or target == source:
        return target
    stem = target.stem
    suffix = target.suffix
    idx = 2
    while True:
        candidate = source.with_name(f"{stem}_{idx}{suffix}")
        if not candidate.exists() or candidate == source:
            return candidate
        idx += 1


def score_match(entry, candidate):
    entry_title_norm = normalize_text(entry["title"])
    c_text_norm = normalize_text(candidate.get("text_head", ""))
    c_name_norm = normalize_text(candidate["pdf_path"].stem)
    combined = f"{c_name_norm} {c_text_norm}".strip()

    entry_doi = normalize_doi(entry.get("doi", ""))
    candidate_doi = normalize_doi(candidate.get("doi_found", ""))
    doi_match = bool(entry_doi and candidate_doi and entry_doi == candidate_doi)

    title_ratio = difflib.SequenceMatcher(None, entry_title_norm, combined[: max(len(entry_title_norm) * 3, 1)]).ratio()
    entry_tokens = set(tokenize(entry["title"]))
    cand_tokens = set(tokenize(combined))
    overlap = len(entry_tokens & cand_tokens) / max(1, len(entry_tokens))

    score = overlap * 70 + title_ratio * 20

    journal = normalize_text(entry.get("journal", ""))
    if journal and journal in combined:
        score += 10

    lname = normalize_text(first_author_lastname(entry.get("authors", "")))
    if lname and lname != "unknownauthor" and lname in combined:
        score += 8

    if doi_match:
        score += 1000

    return {
        "score": round(score, 3),
        "doi_match": doi_match,
        "title_ratio": round(title_ratio, 3),
        "token_overlap": round(overlap, 3),
    }


def write_rename_manifest(path: Path, rows, config: dict):
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config": config,
        "renames": rows,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def rollback_renames(manifest_path: Path):
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = payload.get("renames") or []
    restored = 0
    missing = 0
    for row in rows:
        if row.get("status") != "renamed":
            continue
        new_path = Path(row.get("new_path", ""))
        old_path = Path(row.get("old_path", ""))
        if not new_path.exists():
            missing += 1
            LOGGER.warning("Rollback skip (missing): %s", new_path)
            continue
        if old_path.exists():
            LOGGER.warning("Rollback skip (target exists): %s", old_path)
            continue
        new_path.rename(old_path)
        restored += 1
        LOGGER.info("Rollback restored: %s -> %s", new_path.name, old_path.name)
    LOGGER.info("Rollback done. restored=%s missing=%s", restored, missing)
    return 0


def run_rename_from_weekly(
    weekly_path: Path,
    manifest_rows,
    input_dir: Path,
    output_dir: Path,
    apply_changes: bool,
    rename_manifest_path: Path,
):
    entries = parse_weekly_paper_info(weekly_path)
    if not entries:
        LOGGER.warning("No paper entries parsed from weekly file: %s", weekly_path)
        return 0

    candidates = []
    for row in manifest_rows:
        if row.get("status") != "ok":
            continue
        pdf_path = Path(row.get("source_pdf", ""))
        txt_path = Path(row.get("output_txt", "")) if row.get("output_txt") else None
        text_head = ""
        if txt_path and txt_path.exists():
            text_head = txt_path.read_text(encoding="utf-8", errors="replace")[:12000]
        doi_found = extract_doi_from_text(f"{text_head}\n{pdf_path.name}")
        candidates.append(
            {
                "pdf_path": pdf_path,
                "text_head": text_head,
                "doi_found": doi_found,
                "assigned": False,
            }
        )

    rename_rows = []
    renamed = 0
    unmatched = 0

    for entry in entries:
        scored = []
        for c in candidates:
            if c["assigned"]:
                continue
            m = score_match(entry, c)
            scored.append((m, c))

        if not scored:
            unmatched += 1
            rename_rows.append(
                {
                    "status": "unmatched",
                    "paper_title": entry["title"],
                    "weekly_id": entry.get("weekly_id", ""),
                    "journal": entry["journal"],
                    "authors": entry.get("authors", ""),
                    "doi": entry.get("doi", ""),
                    "old_path": None,
                    "new_path": None,
                    "match_score": 0,
                    "match_method": "none",
                }
            )
            continue

        doi_hits = [(m, c) for (m, c) in scored if m["doi_match"]]
        chosen = None
        method = "fuzzy"
        if len(doi_hits) == 1:
            chosen = doi_hits[0]
            method = "doi"
        else:
            scored.sort(key=lambda x: x[0]["score"], reverse=True)
            top = scored[0]
            second_score = scored[1][0]["score"] if len(scored) > 1 else -1
            if top[0]["score"] >= 45 and (top[0]["score"] - second_score >= 8):
                chosen = top
                method = "fuzzy"

        if chosen is None:
            unmatched += 1
            rename_rows.append(
                {
                    "status": "ambiguous_or_low_confidence",
                    "paper_title": entry["title"],
                    "weekly_id": entry.get("weekly_id", ""),
                    "journal": entry["journal"],
                    "authors": entry.get("authors", ""),
                    "doi": entry.get("doi", ""),
                    "old_path": None,
                    "new_path": None,
                    "match_score": scored[0][0]["score"],
                    "match_method": "none",
                }
            )
            continue

        match_info, cand = chosen
        old_path = cand["pdf_path"]
        cand["assigned"] = True

        weekly_id = sanitize_component(entry.get("weekly_id", ""), 20)
        journal_comp = sanitize_component(entry.get("journal", ""), 28)
        author_comp = sanitize_component(first_author_lastname(entry.get("authors", "")), 24)
        title_comp = sanitize_component(build_title_keywords(entry.get("title", "")), 72)
        prefix = f"{weekly_id}_" if weekly_id and weekly_id != "NA" else ""
        target_name = f"{prefix}{journal_comp}_{author_comp}_{title_comp}.pdf"
        target_path = make_unique_target(old_path, target_name)

        row = {
            "status": "planned",
            "paper_title": entry["title"],
            "weekly_id": entry.get("weekly_id", ""),
            "journal": entry["journal"],
            "authors": entry.get("authors", ""),
            "doi": entry.get("doi", ""),
            "old_path": str(old_path),
            "new_path": str(target_path),
            "match_score": match_info["score"],
            "match_method": method,
            "title_ratio": match_info["title_ratio"],
            "token_overlap": match_info["token_overlap"],
        }

        if old_path == target_path:
            row["status"] = "unchanged"
        elif apply_changes:
            old_path.rename(target_path)
            row["status"] = "renamed"
            renamed += 1
            LOGGER.info("Renamed: %s -> %s", old_path.name, target_path.name)
        else:
            LOGGER.info("[rename-dry-run] %s -> %s", old_path.name, target_path.name)
        rename_rows.append(row)

    # unassigned pdfs
    for c in candidates:
        if not c["assigned"]:
            rename_rows.append(
                {
                    "status": "pdf_unassigned",
                    "paper_title": None,
                    "journal": None,
                    "authors": None,
                    "doi": None,
                    "old_path": str(c["pdf_path"]),
                    "new_path": None,
                    "match_score": 0,
                    "match_method": "none",
                }
            )

    cfg = {
        "weekly_file": str(weekly_path),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "apply_changes": apply_changes,
    }
    write_rename_manifest(rename_manifest_path, rename_rows, cfg)
    LOGGER.info("Wrote rename manifest: %s", rename_manifest_path)
    LOGGER.info("Rename summary: renamed=%s unmatched=%s", renamed, unmatched)
    return 0


def main() -> int:
    args = parse_args()
    setup_logging(args.log_level)

    if args.rename_rollback:
        manifest_path = Path(args.rename_rollback).expanduser().resolve()
        if not manifest_path.exists():
            LOGGER.error("Rename rollback manifest not found: %s", manifest_path)
            return 1
        return rollback_renames(manifest_path)

    script_dir = Path(__file__).resolve().parent
    input_dir = resolve_input_dir(script_dir, args.input_dir)
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else (input_dir / "extracted_text")
    )

    if not input_dir.exists():
        LOGGER.error("Input directory not found: %s", input_dir)
        return 1
    if not input_dir.is_dir():
        LOGGER.error("Input path is not a directory: %s", input_dir)
        return 1

    pdf_paths = resolve_pdf_list(input_dir, args.files)
    if not pdf_paths:
        LOGGER.warning("No PDFs found in: %s", input_dir)
        return 0

    engine_order = [x.strip().lower() for x in args.engine_order.split(",") if x.strip()]
    enable_ocr_fallback = not args.disable_ocr_fallback

    LOGGER.info("Input directory: %s", input_dir)
    LOGGER.info("Output directory: %s", output_dir)
    LOGGER.info("PDF count: %s", len(pdf_paths))
    LOGGER.info("Engine order: %s", engine_order)
    LOGGER.info("OCR fallback: %s", enable_ocr_fallback)

    if args.dry_run:
        for path in pdf_paths:
            LOGGER.info("[dry-run] would extract: %s", path.name)
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_global_path = output_dir / "extract_chunks.jsonl"
    images_global_path = output_dir / "extract_images.jsonl"
    tables_global_path = output_dir / "extract_tables.jsonl"
    if chunks_global_path.exists():
        chunks_global_path.unlink()
    if images_global_path.exists():
        images_global_path.unlink()
    if tables_global_path.exists():
        tables_global_path.unlink()
    if args.extract_images:
        images_global_path.write_text("", encoding="utf-8")
    if args.extract_tables:
        tables_global_path.write_text("", encoding="utf-8")

    manifest_rows = []
    failures = 0

    for pdf_path in pdf_paths:
        if not pdf_path.exists():
            failures += 1
            LOGGER.warning("Missing PDF: %s", pdf_path)
            manifest_rows.append(
                {
                    "source_pdf": str(pdf_path),
                    "status": "missing",
                    "selected_engine": None,
                    "used_ocr_fallback": False,
                    "output_txt": None,
                    "output_chunks_jsonl": None,
                    "output_images_jsonl": None,
                    "output_tables_jsonl": None,
                    "pages": 0,
                    "chars": 0,
                    "chars_per_page": 0,
                    "images": 0,
                    "tables": 0,
                    "review_items": 0,
                    "quality": "missing",
                    "error": "file_not_found",
                }
            )
            continue

        started = time.perf_counter()
        primary = extract_with_engine_chain(pdf_path, engine_order=engine_order, max_chars=args.max_chars)
        selected = primary
        used_ocr = False
        ocr_error = None

        if primary["ok"] and is_low_quality(
            primary, min_chars_warning=args.min_chars_warning, min_cpp_warning=args.min_chars_per_page_warning
        ):
            LOGGER.warning(
                "Low-quality extraction detected for %s (chars=%s, pages=%s).",
                pdf_path.name,
                primary["chars"],
                primary["pages"],
            )
            if enable_ocr_fallback:
                LOGGER.info("Running OCR fallback for %s", pdf_path.name)
                ocr_res = run_ocrmypdf(pdf_path, timeout_sec=args.ocr_timeout)
                if ocr_res["ok"]:
                    used_ocr = True
                    ocr_pdf = ocr_res["output_pdf"]
                    ocr_extract = extract_with_engine_chain(ocr_pdf, engine_order=engine_order, max_chars=args.max_chars)
                    try:
                        ocr_pdf.unlink(missing_ok=True)
                    except OSError:
                        pass
                    if ocr_extract["ok"] and ocr_extract["chars"] >= primary["chars"]:
                        selected = ocr_extract
                    else:
                        LOGGER.info("OCR output not better than primary extraction for %s", pdf_path.name)
                else:
                    ocr_error = ocr_res["error"]
                    LOGGER.warning("OCR fallback failed for %s: %s", pdf_path.name, ocr_error)

        elapsed = round(time.perf_counter() - started, 3)
        stem = safe_stem(pdf_path)
        txt_path = output_dir / f"extracted_{stem}.txt"
        chunks_path = output_dir / f"extracted_{stem}.chunks.jsonl"
        images_path = output_dir / f"extracted_{stem}.images.jsonl"
        tables_path = output_dir / f"extracted_{stem}.tables.jsonl"
        if args.extract_images:
            images_path.write_text("", encoding="utf-8")
        if args.extract_tables:
            tables_path.write_text("", encoding="utf-8")

        if not selected["ok"]:
            failures += 1
            LOGGER.error("Extraction failed for %s", pdf_path.name)
            manifest_rows.append(
                {
                    "source_pdf": str(pdf_path),
                    "status": "error",
                    "selected_engine": None,
                    "used_ocr_fallback": used_ocr,
                    "output_txt": None,
                    "output_chunks_jsonl": None,
                    "output_images_jsonl": None,
                    "output_tables_jsonl": None,
                    "pages": 0,
                    "chars": 0,
                    "chars_per_page": 0,
                    "images": 0,
                    "tables": 0,
                    "review_items": 0,
                    "quality": "error",
                    "elapsed_sec": elapsed,
                    "engine_attempts": selected.get("attempts", []),
                    "error": selected.get("error"),
                    "ocr_error": ocr_error,
                }
            )
            continue

        cleaned_text = selected["text"]
        txt_path.write_text(cleaned_text, encoding="utf-8", errors="replace")

        chunks = chunk_text(cleaned_text, chunk_chars=args.chunk_chars)
        if chunks_path.exists():
            chunks_path.unlink()
        records = []
        for chunk in chunks:
            record = {
                "source_pdf": str(pdf_path),
                "source_pdf_name": pdf_path.name,
                "section": chunk["section"],
                "chunk_id": chunk["chunk_id"],
                "chars": chunk["chars"],
                "text": chunk["text"],
            }
            records.append(record)
        append_jsonl(chunks_path, records)
        append_jsonl(chunks_global_path, records)

        image_records = []
        table_records = []
        page_layouts = []
        if args.extract_images or args.extract_tables or args.save_intermediate:
            try:
                page_layouts = load_page_layouts(pdf_path)
            except Exception as exc:
                LOGGER.warning("Load page layouts failed for %s: %s", pdf_path.name, exc)
                page_layouts = []

        if args.extract_images:
            if page_layouts:
                image_records = extract_images_with_alignment(
                    pdf_path=pdf_path,
                    output_dir=output_dir / args.image_dir_name,
                    base_stem=stem,
                    page_layouts=page_layouts,
                    min_width=args.image_min_width,
                    min_height=args.image_min_height,
                    min_bytes=args.image_min_bytes,
                    neighbor_pages=args.alignment_neighbor_pages,
                    context_chars=args.alignment_context_chars,
                )
                append_jsonl(images_path, image_records)
                append_jsonl(images_global_path, image_records)
            else:
                LOGGER.warning("Skip image extraction for %s due to missing page layouts.", pdf_path.name)

        if args.extract_tables:
            if page_layouts:
                table_records = extract_tables_with_alignment(
                    pdf_path=pdf_path,
                    output_dir=output_dir / args.table_dir_name,
                    base_stem=stem,
                    page_layouts=page_layouts,
                    min_cells=args.table_min_cells,
                    neighbor_pages=args.alignment_neighbor_pages,
                    context_chars=args.alignment_context_chars,
                )
                append_jsonl(tables_path, table_records)
                append_jsonl(tables_global_path, table_records)
            else:
                LOGGER.warning("Skip table extraction for %s due to missing page layouts.", pdf_path.name)

        review_items = sum(1 for r in image_records if r.get("needs_review")) + sum(
            1 for r in table_records if r.get("needs_review")
        )
        if args.save_intermediate and page_layouts:
            inter_dir = output_dir / args.intermediate_dir_name
            inter_dir.mkdir(parents=True, exist_ok=True)
            inter_path = inter_dir / f"alignment_{stem}.json"
            write_intermediate_alignment(
                path=inter_path,
                page_layouts=page_layouts,
                images=image_records,
                tables=table_records,
            )
            LOGGER.info("Wrote intermediate alignment file: %s", inter_path)

        cpp = round(selected["chars"] / max(1, selected["pages"]), 2)
        quality = "low" if is_low_quality(selected, args.min_chars_warning, args.min_chars_per_page_warning) else "ok"

        LOGGER.info(
            "Extracted %s (engine=%s, chars=%s, pages=%s, chunks=%s, images=%s, tables=%s, review_items=%s, quality=%s, %.3fs)",
            pdf_path.name,
            selected["engine"],
            selected["chars"],
            selected["pages"],
            len(chunks),
            len(image_records),
            len(table_records),
            review_items,
            quality,
            elapsed,
        )

        manifest_rows.append(
            {
                "source_pdf": str(pdf_path),
                "status": "ok",
                "selected_engine": selected["engine"],
                "used_ocr_fallback": used_ocr,
                "output_txt": str(txt_path),
                "output_chunks_jsonl": str(chunks_path),
                "output_images_jsonl": str(images_path) if args.extract_images else None,
                "output_tables_jsonl": str(tables_path) if args.extract_tables else None,
                "pages": selected["pages"],
                "chars": selected["chars"],
                "chars_per_page": cpp,
                "chunks": len(chunks),
                "images": len(image_records),
                "tables": len(table_records),
                "review_items": review_items,
                "quality": quality,
                "elapsed_sec": elapsed,
                "engine_attempts": selected.get("attempts", []),
                "error": None,
                "ocr_error": ocr_error,
            }
        )

    run_config = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "engine_order": engine_order,
        "max_chars": args.max_chars,
        "min_chars_warning": args.min_chars_warning,
        "min_chars_per_page_warning": args.min_chars_per_page_warning,
        "chunk_chars": args.chunk_chars,
        "extract_images": args.extract_images,
        "extract_tables": args.extract_tables,
        "image_min_width": args.image_min_width,
        "image_min_height": args.image_min_height,
        "image_min_bytes": args.image_min_bytes,
        "table_min_cells": args.table_min_cells,
        "alignment_context_chars": args.alignment_context_chars,
        "alignment_neighbor_pages": args.alignment_neighbor_pages,
        "save_intermediate": args.save_intermediate,
        "ocr_fallback": enable_ocr_fallback,
        "ocr_timeout": args.ocr_timeout,
    }
    manifest_path = write_manifest(output_dir, manifest_rows, run_config)
    LOGGER.info("Wrote manifest: %s", manifest_path)
    LOGGER.info("Wrote global chunks jsonl: %s", chunks_global_path)
    if args.extract_images:
        LOGGER.info("Wrote global images jsonl: %s", images_global_path)
    if args.extract_tables:
        LOGGER.info("Wrote global tables jsonl: %s", tables_global_path)
    LOGGER.info("Finished with %s failures.", failures)

    if args.rename_from_weekly:
        weekly_path = Path(args.rename_from_weekly).expanduser()
        if not weekly_path.is_absolute():
            weekly_path = (script_dir / weekly_path).resolve()
        if not weekly_path.exists():
            LOGGER.error("Weekly file not found: %s", weekly_path)
            return 1

        if args.rename_apply:
            apply_changes = True
        elif args.rename_dry_run:
            apply_changes = False
        else:
            apply_changes = False
            LOGGER.info("Rename mode defaults to dry-run. Use --rename-apply to actually rename.")

        rename_manifest_path = (
            Path(args.rename_manifest).expanduser().resolve()
            if args.rename_manifest
            else (output_dir / "rename_manifest.json")
        )
        run_rename_from_weekly(
            weekly_path=weekly_path,
            manifest_rows=manifest_rows,
            input_dir=input_dir,
            output_dir=output_dir,
            apply_changes=apply_changes,
            rename_manifest_path=rename_manifest_path,
        )

    return 1 if failures > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
