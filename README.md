# PDF to Markdown Converter

Convert PDF or plain-text files into single, readable Markdown documents.

## What it handles

- **Body text** - Reconstructed into paragraphs (not one line per PDF line)
- **Headings** - Inferred from relative font size / boldness of each text block
- **Tables** - Extracted with their grid structure intact and rendered as GitHub-flavored Markdown pipe tables
- **Images** - Logos, diagrams, charts, screenshots. Every raster image on a page is extracted to an `images/` folder next to the output and embedded with `![alt](path)`
  - Small images near the top of a page are labeled as a probable logo
  - Everything else is labeled as a figure/diagram
  - Each image gets a stable, page-ordered filename
- **Reading order** - Text, tables and images are interleaved in the order they actually appear on the page (top-to-bottom), not text-then-tables-then-images
- **Plain .txt/.md input** - Reflowed into paragraphs with basic heading and bullet-list detection (headings, ALL-CAPS lines, numbered/lettered sections, "- "/"* " bullets)

## Design notes

- **Text extraction and heading detection** use PyMuPDF (fitz), which exposes per-span font size/flags -- this is what makes heading inference possible
- **Table extraction** uses pdfplumber, which has a purpose-built table-detection algorithm (line/whitespace based) that is far more reliable than trying to reconstruct tables from raw text coordinates
- **De-duplication** - Because both libraries look at the same PDF independently, we de-duplicate: any text block whose bounding box sits inside a detected table's bounding box is dropped from the text stream, so table content is not emitted twice

## Requirements

```bash
pip install pymupdf pdfplumber pillow
```

## Usage

```bash
python pdf_to_markdown.py input.pdf -o output.md
python pdf_to_markdown.py notes.txt -o notes.md
python pdf_to_markdown.py input.pdf -o output.md --image-dir images --no-logos
```

## Arguments

- `input` (required): Path to the input .pdf or .txt/.md file
- `-o, --output` (required): Path to the output .md file
- `--image-dir`: Directory to save extracted images into (default: `<output_stem>_images/`)
- `--image-url-prefix`: Prefix to use for image links in the markdown, e.g. 'images/' (default: relative filesystem path to the saved image)
- `--no-logos`: Skip extracting small top-of-page images (logos); keep only larger figures

## Exit codes

- `0`: Success
- `1`: Bad input/arguments
- `2`: Conversion error

## How it works

The converter uses two complementary PDF processing libraries:

1. **PyMuPDF (fitz)** for text extraction and heading detection
   - Analyzes font sizes and flags to identify heading levels
   - Extracts images with their positioning information

2. **pdfplumber** for table extraction
   - Uses sophisticated line/whitespace-based table detection
   - Extracts table data while preserving grid structure

The results are combined and deduplicated to prevent duplicate content, then rendered in proper reading order (top-to-bottom) as Markdown.

## Plain text conversion

For .txt/.md files, the converter:
- Reflows text into proper paragraphs
- Detects headings via:
  - Numbered sections (e.g., "1. Introduction")
  - ALL-CAPS lines
  - Setext-style underlines (=== or ---)
  - Lines ending with colons
- Identifies bullet lists (-, *, •) and numbered lists