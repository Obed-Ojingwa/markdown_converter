#!/usr/bin/env python3
"""
pdf_to_markdown.py
===================

Convert a PDF or plain-text file into a single, readable Markdown document.

What it handles
----------------
- Body text, reconstructed into paragraphs (not one line per PDF line).
- Headings, inferred from relative font size / boldness of each text block.
- Tables, extracted with their grid structure intact and rendered as
  GitHub-flavored Markdown pipe tables.
- Images: logos, diagrams, charts, screenshots. Every raster image on a page
  is extracted to an `images/` folder next to the output and embedded with
  `![alt](path)`. Small images near the top of a page are labeled as a
  probable logo; everything else is labeled as a figure/diagram, and each
  gets a stable, page-ordered filename.
- Reading order: text, tables and images are interleaved in the order they
  actually appear on the page (top-to-bottom), not text-then-tables-then-images.
- Plain .txt/.md input: reflowed into paragraphs with basic heading and
  bullet-list detection (headings, ALL-CAPS lines, numbered/lettered
  sections, "- "/"* " bullets).

Design notes
------------
- Text extraction and heading detection use PyMuPDF (fitz), which exposes
  per-span font size/flags -- this is what makes heading inference possible.
- Table extraction uses pdfplumber, which has a purpose-built table-detection
  algorithm (line/whitespace based) that is far more reliable than trying to
  reconstruct tables from raw text coordinates.
- Because both libraries look at the same PDF independently, we de-duplicate:
  any text block whose bounding box sits inside a detected table's bounding
  box is dropped from the text stream, so table content is not emitted twice.

Requirements
------------
    pip install pymupdf pdfplumber pillow

Usage
-----
    python pdf_to_markdown.py input.pdf -o output.md
    python pdf_to_markdown.py notes.txt -o notes.md
    python pdf_to_markdown.py input.pdf -o output.md --image-dir images --no-logos

Exit codes: 0 success, 1 bad input/arguments, 2 conversion error.
"""

from __future__ import annotations

import utils.transport
import utils.session
import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import warnings
warnings.filterwarnings("ignore", message="The `fitz` API is deprecated")

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Block:
    """One layout element on a page, positioned so we can sort by reading order."""
    kind: str          # "heading" | "paragraph" | "table" | "image" | "bullet_list"
    y0: float           # top coordinate, for ordering
    markdown: str        # rendered markdown for this block
    level: int = 0       # heading level (1-6), unused otherwise


@dataclass
class PageResult:
    page_number: int
    blocks: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# PDF -> Markdown
# --------------------------------------------------------------------------- #

class PdfToMarkdownConverter:
    """Converts a single PDF into Markdown, extracting text, tables and images."""

    MIN_IMAGE_DIM = 20          # px below this in both dims -> treat as decorative, skip
    LOGO_MAX_AREA_RATIO = 0.06  # image area / page area below this...
    LOGO_TOP_BAND_RATIO = 0.18  # ...and top within this fraction of page height -> "logo"

    def __init__(self, pdf_path: str, image_dir: Path, image_url_prefix: str = "",
                 extract_logos: bool = True):
        if fitz is None or pdfplumber is None:
            raise RuntimeError(
                "Missing dependencies. Install with: pip install pymupdf pdfplumber pillow"
            )
        self.pdf_path = Path(pdf_path)
        self.image_dir = Path(image_dir)
        self.image_url_prefix = image_url_prefix
        self.extract_logos = extract_logos
        self._image_dir_created = False

    # -- public API ---------------------------------------------------------

    def convert(self) -> str:
        doc = fitz.open(self.pdf_path)
        plumber_doc = pdfplumber.open(self.pdf_path)

        try:
            body_font_size = self._estimate_body_font_size(doc)
            pages_md = []
            for page_index in range(len(doc)):
                fitz_page = doc[page_index]
                plumber_page = plumber_doc.pages[page_index]
                result = self._convert_page(
                    fitz_page, plumber_page, page_index + 1, body_font_size
                )
                pages_md.append(self._render_page(result))
            return "\n\n".join(p for p in pages_md if p.strip()) + "\n"
        finally:
            doc.close()
            plumber_doc.close()

    # -- font-size baseline ---------------------------------------------------

    def _estimate_body_font_size(self, doc) -> float:
        """Find the most common font size across the doc; that's "body text"."""
        sizes: dict = {}
        for page in doc:
            text_dict = page.get_text("dict")
            for block in text_dict.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        size = round(span["size"])
                        text_len = len(span["text"].strip())
                        if text_len == 0:
                            continue
                        sizes[size] = sizes.get(size, 0) + text_len
        if not sizes:
            return 11.0
        return float(max(sizes, key=sizes.get))

    # -- per-page conversion --------------------------------------------------

    def _convert_page(self, fitz_page, plumber_page, page_number, body_font_size) -> PageResult:
        page_rect = fitz_page.rect
        result = PageResult(page_number=page_number)

        # 1. Tables first, so we know their bounding boxes for de-duplication.
        table_bboxes = []
        try:
            found_tables = plumber_page.find_tables()
        except Exception:
            found_tables = []
        for t_index, table in enumerate(found_tables):
            data = table.extract()
            if not data or not any(any(cell for cell in row) for row in data):
                continue
            md_table = self._render_table(data)
            if md_table:
                x0, top, x1, bottom = table.bbox
                table_bboxes.append((x0, top, x1, bottom))
                result.blocks.append(Block(kind="table", y0=top, markdown=md_table))

        # 2. Text blocks, skipping anything inside a table bbox.
        text_dict = fitz_page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:  # 0 = text block, 1 = image block
                continue
            bbox = block.get("bbox", (0, 0, 0, 0))
            if self._bbox_in_any(bbox, table_bboxes):
                continue
            rendered = self._render_text_block(block, body_font_size)
            if rendered is not None:
                level, kind, md = rendered
                result.blocks.append(Block(kind=kind, y0=bbox[1], markdown=md, level=level))

        # 3. Images (logos, diagrams, photos, charts).
        result.blocks.extend(
            self._extract_images(fitz_page, page_rect, page_number)
        )

        return result

    @staticmethod
    def _bbox_in_any(bbox, boxes, tolerance: float = 2.0) -> bool:
        x0, y0, x1, y1 = bbox
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for bx0, by0, bx1, by1 in boxes:
            if (bx0 - tolerance) <= cx <= (bx1 + tolerance) and (by0 - tolerance) <= cy <= (by1 + tolerance):
                return True
        return False

    # -- text block rendering --------------------------------------------------

    def _render_text_block(self, block, body_font_size):
        """Turn one PyMuPDF text block into a heading / paragraph / bullet list."""
        lines_out = []
        max_size = 0.0
        any_bold = False
        for line in block.get("lines", []):
            spans_text = []
            for span in line.get("spans", []):
                txt = span["text"]
                if not txt.strip():
                    continue
                spans_text.append(txt)
                max_size = max(max_size, span["size"])
                if span["flags"] & 2**4:  # bold flag bit
                    any_bold = True
            line_text = "".join(spans_text).strip()
            if line_text:
                lines_out.append(line_text)

        if not lines_out:
            return None

        full_text = self._join_wrapped_lines(lines_out)

        # Headings take priority: a short, large/bold single line like "1. Introduction"
        # would otherwise also match the numbered-list pattern below.
        heading_level = self._heading_level(max_size, any_bold, body_font_size, full_text)
        if heading_level:
            return heading_level, "heading", f"{'#' * heading_level} {full_text}"

        # Detect bullet / numbered list lines and keep them as a list block.
        # Require an explicit bullet glyph, or a numeric/lettered marker on a
        # block with more than one line (a genuine list rather than a single
        # numbered heading that just happened to not qualify as a heading above).
        dash_bullet = re.compile(r"^[•\-\*\u2022\u25CF\u25AA]\s+")
        numbered_item = re.compile(r"^(\d+[\.\)]|[a-zA-Z][\.\)])\s+")
        is_dash_list = all(dash_bullet.match(l) for l in lines_out)
        is_numbered_list = len(lines_out) > 1 and all(numbered_item.match(l) for l in lines_out)
        if is_dash_list or is_numbered_list:
            pattern = dash_bullet if is_dash_list else numbered_item
            items = [f"- {pattern.sub('', l).strip()}" for l in lines_out]
            return 0, "bullet_list", "\n".join(items)

        return 0, "paragraph", full_text

    @staticmethod
    def _join_wrapped_lines(lines: list) -> str:
        """Join PDF line-wraps into flowing paragraph text without losing real breaks."""
        # Short blocks (<=2 lines) are usually headings/labels: keep as-is, space-joined.
        if len(lines) <= 1:
            return lines[0] if lines else ""
        return " ".join(l.rstrip("-") if l.endswith("-") else l for l in lines)

    @staticmethod
    def _heading_level(size: float, bold: bool, body_size: float, text: str) -> Optional[int]:
        if len(text) > 120:
            return None  # long text is never a heading, regardless of font size
        ratio = size / body_size if body_size else 1.0
        if ratio >= 1.6:
            return 1
        if ratio >= 1.35:
            return 2
        if ratio >= 1.15:
            return 3
        if bold and ratio >= 1.0 and len(text.split()) <= 12 and not text.endswith((".", ",", ";")):
            return 4
        return None

    # -- table rendering ---------------------------------------------------

    @staticmethod
    def _render_table(data: list) -> str:
        def clean(cell) -> str:
            if cell is None:
                return ""
            return re.sub(r"\s+", " ", str(cell)).strip().replace("|", "\\|")

        rows = [[clean(c) for c in row] for row in data]
        rows = [r for r in rows if any(c for c in r)]
        if not rows:
            return ""
        col_count = max(len(r) for r in rows)
        rows = [r + [""] * (col_count - len(r)) for r in rows]

        header, *body = rows
        out = ["| " + " | ".join(header) + " |"]
        out.append("| " + " | ".join(["---"] * col_count) + " |")
        for row in body:
            out.append("| " + " | ".join(row) + " |")
        return "\n".join(out)

    # -- image extraction ---------------------------------------------------

    def _extract_images(self, fitz_page, page_rect, page_number) -> list:
        blocks = []
        page_area = max(page_rect.width * page_rect.height, 1.0)
        seen_xrefs = set()

        for img_index, img in enumerate(fitz_page.get_images(full=True)):
            xref = img[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)

            rects = fitz_page.get_image_rects(xref)
            rect = rects[0] if rects else page_rect
            width, height = rect.width, rect.height
            if width < self.MIN_IMAGE_DIM or height < self.MIN_IMAGE_DIM:
                continue  # skip tiny decorative artifacts (bullets, rules, etc.)

            area_ratio = (width * height) / page_area
            is_logo = (
                area_ratio <= self.LOGO_MAX_AREA_RATIO
                and rect.y0 <= page_rect.height * self.LOGO_TOP_BAND_RATIO
            )
            if is_logo and not self.extract_logos:
                continue

            label = "logo" if is_logo else "figure"
            filename = f"page{page_number:03d}_{label}{img_index + 1}.png"
            saved_path = self._save_image(fitz_page.parent, xref, filename)
            if saved_path is None:
                continue

            url = f"{self.image_url_prefix}{filename}" if self.image_url_prefix else str(saved_path)
            alt = "Logo" if is_logo else f"Diagram/figure on page {page_number}"
            blocks.append(
                Block(kind="image", y0=rect.y0, markdown=f"![{alt}]({url})")
            )
        return blocks

    def _save_image(self, doc, xref: int, filename: str) -> Optional[Path]:
        try:
            if not self._image_dir_created:
                self.image_dir.mkdir(parents=True, exist_ok=True)
                self._image_dir_created = True
            pix = fitz.Pixmap(doc, xref)
            if pix.n - pix.alpha >= 4:  # CMYK -> convert to RGB first
                pix = fitz.Pixmap(fitz.csRGB, pix)
            out_path = self.image_dir / filename
            pix.save(str(out_path))
            pix = None
            return out_path
        except Exception:
            return None

    # -- page rendering ---------------------------------------------------

    @staticmethod
    def _render_page(result: PageResult) -> str:
        blocks = sorted(result.blocks, key=lambda b: b.y0)
        pieces = []
        for b in blocks:
            pieces.append(b.markdown)
        return "\n\n".join(pieces)


# --------------------------------------------------------------------------- #
# Plain text -> Markdown
# --------------------------------------------------------------------------- #

class TextToMarkdownConverter:
    """Reflow a .txt file into Markdown: paragraphs, headings, bullet lists."""

    # "1", "1.", "1.2", "1.2.3." etc. followed by a title -- a numbered *heading*.
    HEADING_NUMBERED = re.compile(r"^(\d+(\.\d+)*)\.?\s+\S")
    DASH_BULLET = re.compile(r"^\s*([-*\u2022])\s+\S")
    NUMBERED_ITEM = re.compile(r"^\s*\d+[\.\)]\s+\S")

    def convert(self, text: str) -> str:
        raw_lines = text.splitlines()
        out_lines = []
        paragraph_buf = []
        in_list = False

        def flush_paragraph():
            if paragraph_buf:
                out_lines.append(" ".join(paragraph_buf).strip())
                out_lines.append("")
                paragraph_buf.clear()

        def end_list_if_needed():
            nonlocal in_list
            if in_list:
                out_lines.append("")
                in_list = False

        i = 0
        while i < len(raw_lines):
            line = raw_lines[i].rstrip()
            stripped = line.strip()

            if not stripped:
                flush_paragraph()
                end_list_if_needed()
                i += 1
                continue

            if self.DASH_BULLET.match(line):
                flush_paragraph()
                out_lines.append(re.sub(r"^\s*[-*\u2022]\s+", "- ", line))
                in_list = True
                i += 1
                continue

            # A numbered line is a *list item* only when it's part of a run of
            # consecutive numbered lines; an isolated one ("1. Introduction"
            # followed by prose) is treated as a numbered heading instead.
            if self.NUMBERED_ITEM.match(line) and self._is_part_of_numbered_run(raw_lines, i):
                flush_paragraph()
                out_lines.append(re.sub(r"^\s*\d+[\.\)]\s+", "- ", line))
                in_list = True
                i += 1
                continue

            if self._looks_like_heading(stripped, raw_lines, i):
                flush_paragraph()
                end_list_if_needed()
                level = self._heading_level_for(stripped)
                out_lines.append(f"{'#' * level} {stripped.rstrip(':')}")
                out_lines.append("")
                i += 1
                continue

            end_list_if_needed()
            paragraph_buf.append(stripped)
            i += 1

        flush_paragraph()
        end_list_if_needed()
        cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines))
        return cleaned.strip() + "\n"

    def _is_part_of_numbered_run(self, all_lines: list, idx: int) -> bool:
        """True if the numbered line at idx is one of >=2 consecutive numbered lines."""
        def is_numbered(j):
            return 0 <= j < len(all_lines) and bool(self.NUMBERED_ITEM.match(all_lines[j].rstrip()))

        # Look at the nearest non-blank neighbor in each direction.
        j = idx - 1
        while j >= 0 and not all_lines[j].strip():
            j -= 1
        prev_is_numbered = is_numbered(j)

        j = idx + 1
        while j < len(all_lines) and not all_lines[j].strip():
            j += 1
        next_is_numbered = is_numbered(j)

        return prev_is_numbered or next_is_numbered

    def _looks_like_heading(self, line: str, all_lines: list, idx: int) -> bool:
        if len(line) > 80:
            return False
        if self.HEADING_NUMBERED.match(line) and len(line.split()) <= 12:
            return True
        if line.isupper() and len(line.split()) <= 10:
            return True
        # Underlined with === or --- on next line (setext-style heading)
        if idx + 1 < len(all_lines):
            nxt = all_lines[idx + 1].strip()
            if nxt and set(nxt) <= {"="} and len(nxt) >= 3:
                return True
            if nxt and set(nxt) <= {"-"} and len(nxt) >= 3:
                return True
        if line.endswith(":") and len(line.split()) <= 8:
            return True
        return False

    def _heading_level_for(self, line: str) -> int:
        m = self.HEADING_NUMBERED.match(line)
        if m:
            depth = m.group(1).count(".") + 1
            return min(depth + 1, 6)
        return 2


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def convert_file(input_path: str, output_path: str, image_dir: Optional[str] = None,
                  image_url_prefix: str = "", extract_logos: bool = True) -> None:
    src = Path(input_path)
    if not src.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    suffix = src.suffix.lower()
    if suffix == ".pdf":
        img_dir = Path(image_dir) if image_dir else out.parent / f"{out.stem}_images"
        converter = PdfToMarkdownConverter(
            str(src), img_dir, image_url_prefix=image_url_prefix, extract_logos=extract_logos
        )
        markdown = converter.convert()
    elif suffix in (".txt", ".md", ".text"):
        raw = src.read_text(encoding="utf-8", errors="replace")
        markdown = TextToMarkdownConverter().convert(raw)
    else:
        raise ValueError(f"Unsupported input type '{suffix}'. Use .pdf, .txt or .md.")

    out.write_text(markdown, encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a PDF or text file into Markdown (text, tables, images, headings)."
    )
    parser.add_argument("input", help="Path to the input .pdf or .txt/.md file")
    parser.add_argument("-o", "--output", required=True, help="Path to the output .md file")
    parser.add_argument(
        "--image-dir", default=None,
        help="Directory to save extracted images into (default: <output_stem>_images/)"
    )
    parser.add_argument(
        "--image-url-prefix", default="",
        help="Prefix to use for image links in the markdown, e.g. 'images/' "
             "(default: relative filesystem path to the saved image)"
    )
    parser.add_argument(
        "--no-logos", action="store_true",
        help="Skip extracting small top-of-page images (logos); keep only larger figures"
    )
    args = parser.parse_args(argv)

    try:
        convert_file(
            args.input, args.output,
            image_dir=args.image_dir,
            image_url_prefix=args.image_url_prefix,
            extract_logos=not args.no_logos,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Conversion failed: {e}", file=sys.stderr)
        return 2

    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
