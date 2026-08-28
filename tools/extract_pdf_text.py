#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extract text from all PDFs in the repository into a mirrored _全文文本 tree.

Primary extractor: Poppler pdftotext.
Outputs:
  _全文文本/<original pdf path>.txt
  _全文文本/提取状态.csv
  _全文文本/提取状态.md

The script is designed for academic PDFs (including CNKI downloads) that already
contain a text layer. It does not run OCR. Files with very little extracted text
are flagged for manual/OCR follow-up instead of silently treated as readable.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


OUTPUT_ROOT_NAME = "_全文文本"
EXCLUDED_DIRS = {".git", ".github", OUTPUT_ROOT_NAME, "node_modules", ".venv", "venv"}


@dataclass
class Result:
    pdf: str
    txt: str
    status: str
    pages: int | None
    chars: int
    non_ws_chars: int
    han_chars: int
    replacement_chars: int
    note: str


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, encoding="utf-8", errors="replace")


def pdf_page_count(pdf: Path) -> int | None:
    proc = run(["pdfinfo", str(pdf)])
    if proc.returncode != 0:
        return None
    m = re.search(r"^Pages:\s+(\d+)\s*$", proc.stdout, flags=re.MULTILINE)
    return int(m.group(1)) if m else None


def clean_text(raw: str) -> str:
    # Keep page structure explicit so later reading can cite/locate context.
    raw = raw.replace("\x00", "")
    pages = raw.split("\f")
    cleaned_pages: list[str] = []
    for i, page in enumerate(pages, start=1):
        # Trim line-end whitespace but retain line layout.
        lines = [line.rstrip() for line in page.splitlines()]
        page_text = "\n".join(lines).strip()
        if not page_text and i == len(pages):
            continue
        cleaned_pages.append(f"===== PAGE {i} =====\n{page_text}".rstrip())
    text = "\n\n".join(cleaned_pages).strip() + "\n"
    # Remove rare control chars except newline/tab.
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return text


def assess(text: str, pages: int | None, returncode: int) -> tuple[str, str, int, int, int, int]:
    chars = len(text)
    non_ws = len(re.sub(r"\s", "", text))
    han = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    repl = text.count("�")

    if returncode != 0 and non_ws == 0:
        return "extract_error", "pdftotext failed and no readable text was produced", chars, non_ws, han, repl

    # Page markers contribute a little text, so use a fairly conservative floor.
    page_floor = max(300, (pages or 1) * 80)
    if non_ws < page_floor:
        return "low_text", "very little text extracted; likely scan/image PDF or broken text layer", chars, non_ws, han, repl

    if repl > max(20, non_ws * 0.01):
        return "encoding_warning", "many Unicode replacement characters; inspect extraction quality", chars, non_ws, han, repl

    # For Chinese-titled PDFs, a low Han ratio can be meaningful, but many papers are bilingual.
    return "ok", "", chars, non_ws, han, repl


def extract_one(repo: Path, pdf: Path, out_root: Path) -> Result:
    rel = pdf.relative_to(repo)
    out = out_root / rel.with_suffix(rel.suffix + ".txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".raw")

    pages = pdf_page_count(pdf)
    proc = run(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), str(tmp)])

    raw = ""
    if tmp.exists():
        raw = tmp.read_text(encoding="utf-8", errors="replace")
        tmp.unlink(missing_ok=True)

    text = clean_text(raw) if raw else ""
    status, note, chars, non_ws, han, repl = assess(text, pages, proc.returncode)

    header = (
        f"SOURCE_PDF: {rel.as_posix()}\n"
        f"EXTRACTION_STATUS: {status}\n"
        f"PAGES: {pages if pages is not None else 'unknown'}\n"
        f"NOTE: {note or 'none'}\n"
        "EXTRACTOR: poppler pdftotext -layout -enc UTF-8\n"
        "---\n\n"
    )
    out.write_text(header + text, encoding="utf-8")

    return Result(
        pdf=rel.as_posix(),
        txt=out.relative_to(repo).as_posix(),
        status=status,
        pages=pages,
        chars=chars,
        non_ws_chars=non_ws,
        han_chars=han,
        replacement_chars=repl,
        note=note,
    )


def discover_pdfs(repo: Path) -> list[Path]:
    pdfs: list[Path] = []
    for p in repo.rglob("*.pdf"):
        rel_parts = set(p.relative_to(repo).parts[:-1])
        if rel_parts & EXCLUDED_DIRS:
            continue
        pdfs.append(p)
    return sorted(pdfs, key=lambda p: p.as_posix().lower())


def write_status(repo: Path, out_root: Path, results: list[Result]) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    csv_path = out_root / "提取状态.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["PDF路径", "TXT路径", "状态", "页数", "字符数", "非空白字符", "汉字数", "替换字符数", "说明"])
        for r in results:
            w.writerow([r.pdf, r.txt, r.status, r.pages or "", r.chars, r.non_ws_chars, r.han_chars, r.replacement_chars, r.note])

    md = [
        "# PDF全文提取状态",
        "",
        "> 由 `tools/extract_pdf_text.py` 自动生成。`ok` 表示已提取出足量文本；`low_text` 多见于扫描版/无文字层PDF；`encoding_warning` 表示需人工检查编码质量。",
        "",
        "|PDF|状态|页数|非空白字符|汉字数|说明|",
        "|---|---:|---:|---:|---:|---|",
    ]
    for r in results:
        note = r.note.replace("|", "\\|")
        pdf = r.pdf.replace("|", "\\|")
        md.append(f"|`{pdf}`|{r.status}|{r.pages or ''}|{r.non_ws_chars}|{r.han_chars}|{note}|")
    md.extend([
        "",
        f"共处理 **{len(results)}** 个PDF。",
        "",
    ])
    (out_root / "提取状态.md").write_text("\n".join(md), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--clean", action="store_true", help="delete and rebuild _全文文本")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    out_root = repo / OUTPUT_ROOT_NAME

    for exe in ("pdftotext", "pdfinfo"):
        if shutil.which(exe) is None:
            print(f"ERROR: required executable not found: {exe}", file=sys.stderr)
            return 2

    if args.clean and out_root.exists():
        shutil.rmtree(out_root)

    pdfs = discover_pdfs(repo)
    print(f"Discovered {len(pdfs)} PDF files")

    results: list[Result] = []
    for i, pdf in enumerate(pdfs, start=1):
        print(f"[{i}/{len(pdfs)}] {pdf.relative_to(repo)}")
        try:
            result = extract_one(repo, pdf, out_root)
        except Exception as exc:  # keep processing the corpus
            rel = pdf.relative_to(repo).as_posix()
            out = out_root / pdf.relative_to(repo).with_suffix(pdf.suffix + ".txt")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                f"SOURCE_PDF: {rel}\nEXTRACTION_STATUS: exception\nNOTE: {exc}\n---\n",
                encoding="utf-8",
            )
            result = Result(rel, out.relative_to(repo).as_posix(), "exception", None, 0, 0, 0, 0, str(exc))
        results.append(result)

    write_status(repo, out_root, results)

    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("Status summary:", counts)

    # Extraction warnings should not fail the whole workflow; the status report is the QA signal.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
