#!/usr/bin/env python3
"""Prepare a specification and an assignment for the assignment-marker reviewers.

Converts the inputs (docx, pdf, pptx, ipynb, md/txt/tex, html/rtf/odt via pandoc,
source code files or a whole project folder, and images) into two numbered plain-text
files so every reviewer cites the same locations:

    WORK/spec.md        spec paragraphs numbered [R#]
    WORK/assignment.md  assignment numbered [P#]/[T#] (prose), [L#] (maths),
                        [S#]/[S#.#] (slides), [C#]/[C#:L#] (notebooks), path:line (code)

Prints a JSON summary to stdout: paths, detected type, word counts, spec fingerprint,
earlier marking reports for the same spec, extracted media and warnings.

Usage:
    prepare.py --spec SPEC [SPEC ...] --assignment PATH [PATH ...] --work DIR
               [--cwd DIR] [--type essay|report|lab|code|maths|presentation]

Optional dependencies: python-docx, python-pptx, pypdf; pdftotext and pandoc if present.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

CODE_EXT = {
    ".py", ".java", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".cs", ".js", ".jsx",
    ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".kts",
    ".scala", ".hs", ".lhs", ".ml", ".mli", ".fs", ".clj", ".ex", ".exs", ".erl", ".lua",
    ".pl", ".r", ".jl", ".m", ".sql", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".asm", ".s",
    ".v", ".sv", ".vhd", ".vhdl", ".dart", ".groovy", ".pas", ".f90", ".f", ".for",
    ".pro", ".prolog", ".rkt", ".scm", ".lisp", ".el", ".css", ".scss", ".html", ".htm",
    ".vue", ".svelte", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".json",
    ".gradle", ".cmake", ".dockerfile",
}
CODE_NAMES = {"makefile", "dockerfile", "cmakelists.txt", "requirements.txt", "gemfile",
              "rakefile", "procfile", "pom.xml", "build.gradle", "cargo.toml", "go.mod",
              "package.json", "pyproject.toml", "setup.py", "setup.cfg"}
TEXT_EXT = {".md", ".markdown", ".txt", ".text", ".rst", ".tex", ".latex"}
PANDOC_EXT = {".rtf", ".odt", ".epub"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
             ".env", "build", "dist", "target", "out", "bin", "obj", ".idea", ".vscode",
             ".mypy_cache", ".pytest_cache", ".gradle", ".next", ".nuxt", "coverage",
             ".ipynb_checkpoints", ".tox", "site-packages", ".dart_tool"}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
              "cargo.lock", "composer.lock", "gemfile.lock", ".ds_store"}
MAX_CODE_LINES = 8000
MAX_FILE_BYTES = 300_000

WORD_RE = re.compile(r"[A-Za-z0-9\u00C0-\u024F]+(?:['\u2019\-][A-Za-z0-9\u00C0-\u024F]+)*")
REF_RE = re.compile(r"^(\d+(\.\d+)*\.?\s*)?(references?|reference list|bibliography|works cited|"
                    r"literature cited|sources|list of references)\s*:?$", re.I)
APPX_RE = re.compile(r"^(\d+(\.\d+)*\.?\s*)?appendi(x|ces)\b", re.I)
MATH_TOKEN_RE = re.compile(r"(=|\\[a-zA-Z]+|[∫∑∏√≤≥≠≈→⇒⇔∀∃∈∂∞±×÷]|\^|\b(proof|lemma|theorem|"
                           r"corollary|hence|therefore|q\.e\.d|let)\b)", re.I)

warnings = []
media = []


def words(text):
    return len(WORD_RE.findall(text))


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


# --------------------------------------------------------------------------- readers
# Prose readers return a list of blocks: {"kind": heading|para|table|page, "text"/"rows"}.
# "para" text may contain "\n" (kept for maths line numbering, joined for prose).

def split_paragraphs(text, markdown=False):
    blocks = []
    for chunk in re.split(r"\n\s*\n", text):
        lines = [ln.rstrip() for ln in chunk.strip("\n").split("\n")]
        lines = [ln for ln in lines if ln.strip()]
        if not lines:
            continue
        if markdown:
            buf = []
            for ln in lines:
                if re.match(r"^\s{0,3}#{1,6}\s", ln):
                    if buf:
                        blocks.append({"kind": "para", "text": "\n".join(buf)})
                        buf = []
                    blocks.append({"kind": "heading", "text": re.sub(r"^\s*#+\s*", "", ln)})
                else:
                    buf.append(ln)
            if buf:
                blocks.append({"kind": "para", "text": "\n".join(buf)})
        else:
            blocks.append({"kind": "para", "text": "\n".join(lines)})
    return blocks


def read_text(path):
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in {".tex", ".latex"}:
        m = re.search(r"\\begin\{document\}(.*?)(\\end\{document\}|$)", raw, re.S)
        if m:
            raw = m.group(1)
        raw = "\n".join(ln for ln in raw.split("\n") if not ln.lstrip().startswith("%"))
        blocks = []
        sec = re.compile(r"^\s*\\(?:sub)*(?:section|paragraph|chapter)\*?\{(.+)\}\s*$")
        for b in split_paragraphs(raw):
            buf = []
            for ln in b["text"].split("\n"):
                h = sec.match(ln)
                if h:
                    if buf:
                        blocks.append({"kind": "para", "text": "\n".join(buf)})
                        buf = []
                    blocks.append({"kind": "heading", "text": h.group(1)})
                else:
                    buf.append(ln)
            if buf:
                blocks.append({"kind": "para", "text": "\n".join(buf)})
        return blocks
    return split_paragraphs(raw, markdown=path.suffix.lower() in {".md", ".markdown"})


def read_docx(path, work):
    try:
        import docx  # python-docx
        from docx.oxml.ns import qn
    except ImportError:
        out = run(["pandoc", str(path), "-t", "plain", "--wrap=none"])
        if out is None:
            sys.exit("python-docx is not installed and pandoc failed. Run: pip install python-docx")
        warnings.append(f"{path.name}: read with pandoc (python-docx missing); headings not detected.")
        return split_paragraphs(out)

    d = docx.Document(str(path))
    W_T, M_T, W_TAB, W_BR = qn("w:t"), qn("m:t"), qn("w:tab"), qn("w:br")
    W_DRAW, W_PICT = qn("w:drawing"), qn("w:pict")
    has_math = False

    def para_text(el):
        nonlocal has_math
        parts = []
        for node in el.iter():
            if node.tag == W_T and node.text:
                parts.append(node.text)
            elif node.tag == M_T and node.text:
                has_math = True
                parts.append(node.text)
            elif node.tag == W_TAB:
                parts.append("\t")
            elif node.tag == W_BR:
                parts.append("\n")
        n_img = sum(1 for _ in el.iter(W_DRAW)) + sum(1 for _ in el.iter(W_PICT))
        text = "".join(parts).strip()
        if n_img:
            text = (text + " " if text else "") + " ".join("[image]" for _ in range(n_img))
        return text

    blocks = []
    styles = {}
    for p in d.paragraphs:
        styles[p._p] = (p.style.name if p.style is not None else "") or ""
    for child in d.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            text = para_text(child)
            if not text:
                continue
            style = styles.get(child, "").lower()
            if style.startswith("heading") or style == "title":
                blocks.append({"kind": "heading", "text": text})
            else:
                blocks.append({"kind": "para", "text": text})
        elif tag == "tbl":
            rows = []
            for tr in child.iter(qn("w:tr")):
                cells = [para_text(tc).replace("\n", " ") for tc in tr.iter(qn("w:tc"))]
                rows.append(cells)
            if rows:
                blocks.append({"kind": "table", "rows": rows})
    if has_math:
        warnings.append(f"{path.name}: contains Word equations, extracted as linear text. "
                        "If notation matters, view the original and correct assignment.md.")
    # extract embedded media for the main agent to view if figures matter
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.startswith("word/media/")]
            if names:
                outdir = Path(work) / "media"
                outdir.mkdir(exist_ok=True)
                for n in names:
                    target = outdir / f"{path.stem}_{Path(n).name}"
                    target.write_bytes(z.read(n))
                    media.append(str(target))
    except zipfile.BadZipFile:
        warnings.append(f"{path.name}: not a valid .docx archive.")
    return blocks


PAGE_NUM_RE = re.compile(r"\s*(page\s*)?\d{1,3}(\s*(of|/)\s*\d{1,3})?\s*", re.I)


def _pdf_layout_pages(path):
    """Return pages as lists of layout blocks: [{"lines": [str], "size": float}], or None.

    Uses PyMuPDF if installed (font sizes), else poppler's `pdftotext -bbox-layout`
    (line heights). Both keep real paragraph boundaries, which plain extraction loses.
    """
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):  # keep stdout clean for the JSON
            try:
                import pymupdf as fitz
            except ImportError:
                import fitz  # older PyMuPDF
        pages = []
        with contextlib.redirect_stdout(io.StringIO()), fitz.open(str(path)) as doc:
            for page in doc:
                blocks = []
                for b in page.get_text("dict").get("blocks", []):
                    if b.get("type") != 0:
                        continue
                    cur, cur_size, prev_bottom = [], 0, None
                    for ln in b.get("lines", []):
                        spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
                        t = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                        if not t:
                            continue
                        size = max(s.get("size", 0) for s in spans)
                        y0, y1 = ln["bbox"][1], ln["bbox"][3]
                        h = max(y1 - y0, 1)
                        new_para = cur and (abs(size - cur_size) > 0.1 * max(cur_size, 1)
                                            or (prev_bottom is not None and y0 - prev_bottom > 0.6 * h))
                        if new_para:
                            blocks.append({"lines": cur, "size": cur_size})
                            cur, cur_size = [], 0
                        cur.append(t)
                        cur_size = max(cur_size, size)
                        prev_bottom = y1
                    if cur:
                        blocks.append({"lines": cur, "size": cur_size})
                pages.append(blocks)
        return pages
    except ImportError:
        pass
    except Exception:
        return None
    xml = run(["pdftotext", "-enc", "UTF-8", "-bbox-layout", str(path), "-"])
    if xml is None:
        return None
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", xml))
    except ET.ParseError:
        return None
    ns = "{http://www.w3.org/1999/xhtml}"
    pages = []
    for page in root.iter(ns + "page"):
        blocks = []
        for block in page.iter(ns + "block"):
            cur, cur_h, prev_bottom = [], [], None
            for line in block.iter(ns + "line"):
                t = " ".join((w.text or "") for w in line.iter(ns + "word")).strip()
                if not t:
                    continue
                y0, y1 = float(line.get("yMin")), float(line.get("yMax"))
                h = y1 - y0
                # a large vertical gap inside one block means a new paragraph
                if prev_bottom is not None and cur and y0 - prev_bottom > 0.6 * h:
                    blocks.append({"lines": cur, "size": max(cur_h)})
                    cur, cur_h = [], []
                cur.append(t)
                cur_h.append(h)
                prev_bottom = y1
            if cur:
                blocks.append({"lines": cur, "size": max(cur_h)})
        pages.append(blocks)
    return pages


def read_pdf(path):
    pages = _pdf_layout_pages(path)
    if pages is None:
        text = run(["pdftotext", "-enc", "UTF-8", str(path), "-"])
        if text is None:
            try:
                from pypdf import PdfReader
            except ImportError:
                sys.exit("No PDF reader available. Run: pip install pypdf (or install poppler)")
            text = "\f".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
            warnings.append(f"{path.name}: read with pypdf; paragraph boundaries and headings are "
                            "approximate. Installing PyMuPDF (pip install pymupdf) or poppler improves this.")
        raw_pages = text.split("\f")
        if raw_pages and not raw_pages[-1].strip():
            raw_pages = raw_pages[:-1]
        pages = [[{"lines": b["text"].split("\n"), "size": 0} for b in split_paragraphs(p)] for p in raw_pages]

    chars = sum(len(" ".join(b["lines"])) for pg in pages for b in pg)
    if not pages or chars / max(len(pages), 1) < 200:
        warnings.append(f"{path.name}: very little extractable text ({chars} chars over "
                        f"{len(pages)} pages). It is probably scanned or handwritten: view the "
                        "pages and transcribe them into assignment.md.")

    # body text size = the size carrying the most text
    size_weight = {}
    for pg in pages:
        for b in pg:
            if b["size"]:
                key = round(b["size"], 1)
                size_weight[key] = size_weight.get(key, 0) + len(" ".join(b["lines"]))
    body = max(size_weight, key=size_weight.get) if size_weight else 0

    blocks = []
    for i, pg in enumerate(pages, 1):
        blocks.append({"kind": "page", "text": str(i)})
        for b in pg:
            lines = [ln for ln in b["lines"] if ln.strip()]
            if not lines:
                continue
            first = lines[0].strip()
            if len(lines) == 1 and PAGE_NUM_RE.fullmatch(first):
                continue
            short = len(lines) <= 2 and words(" ".join(lines)) <= 12 and not lines[-1].rstrip().endswith((".", ",", ";", ":"))
            bigger = body and b["size"] >= body * 1.15
            patterned = REF_RE.match(first) or APPX_RE.match(first) or re.match(r"^\d{1,2}(\.\d{1,2})*\.?\s+[A-Z][a-z]", first)
            if short and (bigger or (len(lines) == 1 and patterned)):
                blocks.append({"kind": "heading", "text": " ".join(lines)})
                continue
            prev = [x for x in blocks[-3:] if x["kind"] != "page"]
            continues = (blocks and blocks[-1]["kind"] == "page" and prev and prev[-1]["kind"] == "para"
                         and not re.search(r"[.!?:\"\u201d)\]]\s*$", prev[-1]["text"])
                         and re.match(r"^[a-z(,;]", first))
            if continues:
                prev[-1]["text"] += "\n" + "\n".join(lines)
                prev[-1]["continues_on_page"] = int(blocks[-1]["text"])
            else:
                blocks.append({"kind": "para", "text": "\n".join(lines)})
    return blocks


def read_pandoc(path):
    out = run(["pandoc", str(path), "-t", "markdown", "--wrap=none"])
    if out is None:
        warnings.append(f"{path.name}: could not convert (install pandoc). Skipped.")
        return []
    return split_paragraphs(out, markdown=True)


def read_html(path):
    out = run(["pandoc", str(path), "-t", "markdown", "--wrap=none"])
    if out is not None:
        return split_paragraphs(out, markdown=True)
    raw = path.read_text(encoding="utf-8", errors="replace")
    raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>|</div>", "\n\n", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    return split_paragraphs(raw)


# --------------------------------------------------------------------------- rendering

class Out:
    def __init__(self):
        self.lines = []
        self.p = 0
        self.t = 0
        self.l = 0
        self.s = 0
        self.c = 0
        self.prose_words = 0
        self.prose_words_ex_refs = 0
        self.slide_words = 0
        self.note_words = 0
        self.code_lines = 0
        self.code_files = 0
        self.images = 0
        self.headings = []
        self.prose_lines_seen = 0
        self.math_lines_seen = 0

    def add(self, s=""):
        self.lines.append(s)


def render_prose(out, blocks, line_mode):
    in_refs = False
    for b in blocks:
        kind = b["kind"]
        if kind == "page":
            out.add(f"<!-- page {b['text']} -->")
            continue
        if kind == "table":
            out.t += 1
            out.add(f"[T{out.t}]")
            for row in b["rows"]:
                out.add("| " + " | ".join(c.strip() for c in row) + " |")
                w = sum(words(c) for c in row)
                out.prose_words += w
                if not in_refs:
                    out.prose_words_ex_refs += w
            out.add()
            continue
        text = b["text"]
        out.images += text.count("[image]")
        first = text.strip().split("\n")[0].strip()
        is_heading = kind == "heading" or (len(text) < 60 and REF_RE.match(first))
        if is_heading and REF_RE.match(first):
            in_refs = True
        elif is_heading and APPX_RE.match(first):
            in_refs = False
        w = words(re.sub(r"(\w)-\n(\w)", r"\1-\2", text).replace("[image]", ""))
        out.prose_words += w
        if not in_refs:
            out.prose_words_ex_refs += w
        if is_heading:
            out.headings.append(first)
        for ln in text.split("\n"):
            if ln.strip():
                out.prose_lines_seen += 1
                if MATH_TOKEN_RE.search(ln):
                    out.math_lines_seen += 1
        if line_mode:
            for ln in text.split("\n"):
                if not ln.strip():
                    continue
                out.l += 1
                out.add(f"[L{out.l}] " + ("## " if is_heading else "") + ln.strip())
        else:
            joined = re.sub(r"(\w)-\n(\w)", r"\1-\2", text)
            joined = re.sub(r"\s*\n\s*", " ", joined).strip()
            out.p += 1
            out.add(f"[P{out.p}] " + ("## " if is_heading else "") + joined)
        if not line_mode:
            out.add()


def render_pptx(out, path):
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except ImportError:
        sys.exit("python-pptx is not installed. Run: pip install python-pptx")
    prs = Presentation(str(path))

    def shape_items(shapes):
        for sh in shapes:
            if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from shape_items(sh.shapes)
                continue
            if sh.shape_type == MSO_SHAPE_TYPE.PICTURE:
                yield ("image", None)
                continue
            if getattr(sh, "has_table", False) and sh.has_table:
                rows = [[c.text.strip().replace("\n", " ") for c in r.cells] for r in sh.table.rows]
                yield ("table", rows)
                continue
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                for para in sh.text_frame.paragraphs:
                    t = "".join(r.text for r in para.runs).strip()
                    if t:
                        yield ("text", (t, sh))

    for slide in prs.slides:
        out.s += 1
        n = out.s
        title_shape = slide.shapes.title
        title = title_shape.text.strip() if title_shape is not None and title_shape.has_text_frame else ""
        out.add(f"[S{n}] ## {title or '(no title)'}")
        out.slide_words += words(title)
        k = 0
        imgs = 0
        for kind, val in shape_items(slide.shapes):
            if kind == "image":
                imgs += 1
            elif kind == "table":
                k += 1
                out.add(f"[S{n}.{k}] table:")
                for row in val:
                    out.add("| " + " | ".join(row) + " |")
                    out.slide_words += sum(words(c) for c in row)
            else:
                t, sh = val
                if title_shape is not None and sh.shape_id == title_shape.shape_id:
                    continue
                k += 1
                out.add(f"[S{n}.{k}] {t}")
                out.slide_words += words(t)
        if imgs:
            out.images += imgs
            out.add(f"(slide {n}: {imgs} image(s), not visible to reviewers)")
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip() if slide.notes_slide.notes_text_frame else ""
            if notes:
                out.add(f"[S{n} notes] " + re.sub(r"\s*\n\s*", " ", notes))
                out.note_words += words(notes)
        out.add()


def render_code_file(out, path, rel):
    try:
        data = path.read_bytes()
    except OSError as e:
        warnings.append(f"{rel}: could not read ({e}).")
        return
    if b"\x00" in data[:4096]:
        return
    if len(data) > MAX_FILE_BYTES:
        warnings.append(f"{rel}: skipped, larger than {MAX_FILE_BYTES // 1000} KB.")
        return
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if out.code_lines + len(lines) > MAX_CODE_LINES:
        warnings.append(f"CODE_CAP: {rel} skipped, total code exceeds {MAX_CODE_LINES} lines. "
                        "Ask the user which files matter.")
        return
    width = len(str(len(lines)))
    out.code_files += 1
    out.add(f"### FILE: {rel} ({len(lines)} lines) — cite as {rel}:<line>")
    out.add("```")
    for i, ln in enumerate(lines, 1):
        out.add(f"{str(i).rjust(width)}| {ln}")
    out.add("```")
    out.add()
    out.code_lines += sum(1 for ln in lines if ln.strip())


def render_ipynb(out, path):
    nb = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    for cell in nb.get("cells", []):
        src = cell.get("source", "")
        src = "".join(src) if isinstance(src, list) else src
        if not src.strip() and not cell.get("outputs"):
            continue
        out.c += 1
        n = out.c
        if cell.get("cell_type") == "code":
            lines = src.split("\n")
            out.add(f"[C{n}] code cell:")
            for i, ln in enumerate(lines, 1):
                out.add(f"[C{n}:L{i}] {ln}")
            out.code_lines += sum(1 for ln in lines if ln.strip())
            outs = []
            for o in cell.get("outputs", []):
                if "text" in o:
                    t = o["text"]
                    outs.append("".join(t) if isinstance(t, list) else t)
                elif "data" in o:
                    d = o["data"]
                    if "text/plain" in d:
                        t = d["text/plain"]
                        outs.append("".join(t) if isinstance(t, list) else t)
                    if any(k.startswith("image/") for k in d):
                        outs.append("(image output)")
                        out.images += 1
                elif o.get("output_type") == "error":
                    outs.append(f"ERROR {o.get('ename')}: {o.get('evalue')}")
            if outs:
                olines = "\n".join(outs).strip().split("\n")
                out.add(f"[C{n} output] (saved in the notebook{', first 20 lines' if len(olines) > 20 else ''})")
                for ln in olines[:20]:
                    out.add("    " + ln)
        else:
            text = re.sub(r"\s*\n\s*", " ", src).strip()
            out.add(f"[C{n}] {text}")
            out.prose_words += words(text)
            out.prose_words_ex_refs += words(text)
        out.add()


# --------------------------------------------------------------------------- main helpers

def collect_files(paths):
    files = []
    for p in paths:
        p = Path(p).expanduser()
        if not p.exists():
            sys.exit(f"Not found: {p}")
        if p.is_dir():
            for root, dirs, fnames in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith("."))
                for f in sorted(fnames):
                    if f.lower() in SKIP_FILES or f.startswith("."):
                        continue
                    fp = Path(root) / f
                    files.append((fp, str(fp.relative_to(p.parent))))
        else:
            files.append((p, p.name))
    return files


def file_kind(fp):
    ext = fp.suffix.lower()
    name = fp.name.lower()
    if ext == ".docx":
        return "docx"
    if ext == ".pdf":
        return "pdf"
    if ext == ".pptx":
        return "pptx"
    if ext == ".ipynb":
        return "ipynb"
    if ext in IMG_EXT:
        return "image"
    if ext in PANDOC_EXT:
        return "pandoc"
    if ext in TEXT_EXT:
        return "text"
    if ext in CODE_EXT or name in CODE_NAMES:
        return "code"
    if ext in {".doc", ".ppt", ".pages", ".key", ".numbers", ".xls", ".xlsx"}:
        return "unsupported"
    return "other"


def prose_blocks_for(fp, kind, work):
    if kind == "docx":
        return read_docx(fp, work)
    if kind == "pdf":
        return read_pdf(fp)
    if kind == "pandoc":
        return read_pandoc(fp)
    if kind == "text":
        return read_text(fp)
    return []


def guess_type(spec_text, kinds, prose_words, code_lines, math_ratio, headings, has_tex):
    s = spec_text.lower()
    hs = " ".join(h.lower() for h in headings)
    if "pptx" in kinds or re.search(r"\b(slide deck|presentation slides|powerpoint)\b", s):
        if "pptx" in kinds:
            return "presentation"
    if "ipynb" in kinds:
        return "code"
    if code_lines and (prose_words == 0 or code_lines * 5 > prose_words):
        return "code"
    if math_ratio > 0.25 or (has_tex and math_ratio > 0.15):
        return "maths"
    if re.search(r"\b(lab(oratory)? report|practical report|prac report)\b", s) or (
            "method" in hs and "result" in hs and "discussion" in hs):
        return "lab"
    if re.search(r"\bessay\b", s):
        return "essay"
    if re.search(r"\breport\b", s) or len(headings) >= 4:
        return "report"
    return "essay"


def previous_reports(cwd, fingerprint):
    found = []
    for fp in Path(cwd).resolve().glob("marking-report-*.md"):
        meta = {}
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.match(r"^---\n(.*?)\n---", text, re.S)
        if m:
            for line in m.group(1).split("\n"):
                kv = re.match(r"^(\w+):\s*(.*)$", line)
                if kv:
                    meta[kv.group(1)] = kv.group(2).strip()
        found.append({
            "path": str(fp),
            "date": meta.get("date", ""),
            "mark": meta.get("mark", ""),
            "matches_spec": meta.get("spec_fingerprint", "") == fingerprint,
            "mtime": fp.stat().st_mtime,
        })
    found.sort(key=lambda r: r["mtime"], reverse=True)
    for r in found:
        r.pop("mtime")
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", nargs="+", required=True)
    ap.add_argument("--assignment", nargs="+", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--type", choices=["essay", "report", "lab", "code", "maths", "presentation"])
    args = ap.parse_args()

    work = Path(args.work).expanduser()
    work.mkdir(parents=True, exist_ok=True)

    # ---- spec
    spec_out = Out()
    spec_raw_parts = []
    for fp, rel in collect_files(args.spec):
        kind = file_kind(fp)
        if kind == "image":
            warnings.append(f"Spec {rel} is an image: view it and add its text to spec.md.")
            continue
        if kind in ("code", "other", "text") and fp.suffix.lower() not in {".tex", ".latex"}:
            # pasted / plain-text specs: one requirement per line
            blocks = []
            for ln in fp.read_text(encoding="utf-8", errors="replace").split("\n"):
                if not ln.strip():
                    continue
                if re.match(r"^\s{0,3}#{1,6}\s", ln):
                    blocks.append({"kind": "heading", "text": re.sub(r"^\s*#+\s*", "", ln)})
                else:
                    blocks.append({"kind": "para", "text": ln.strip()})
        elif kind == "pptx":
            tmp = Out()
            render_pptx(tmp, fp)
            blocks = split_paragraphs("\n".join(tmp.lines))
        elif kind == "unsupported":
            warnings.append(f"Spec {rel}: format not supported; export it to PDF or .docx.")
            continue
        else:
            blocks = prose_blocks_for(fp, kind, work)
        spec_out.add(f"<!-- source: {rel} -->")
        for b in blocks:
            if b["kind"] == "page":
                continue
            if b["kind"] == "table":
                spec_out.p += 1
                spec_out.add(f"[R{spec_out.p}] table:")
                for row in b["rows"]:
                    spec_out.add("| " + " | ".join(c.strip() for c in row) + " |")
                    spec_raw_parts.append(" ".join(row))
                spec_out.add()
                continue
            text = re.sub(r"\s*\n\s*", " ", b["text"]).strip()
            spec_out.p += 1
            spec_out.add(f"[R{spec_out.p}] " + ("## " if b["kind"] == "heading" else "") + text)
            spec_out.add()
            spec_raw_parts.append(text)
    spec_text = " ".join(spec_raw_parts)
    if words(spec_text) < 8:
        warnings.append("Specification is very short or empty. Check spec.md before marking.")
    fingerprint = hashlib.sha256(re.sub(r"\s+", " ", spec_text.lower()).strip().encode()).hexdigest()[:12]
    spec_path = work / "spec.md"
    spec_path.write_text("# Specification (numbered for reference)\n\n" + "\n".join(spec_out.lines), encoding="utf-8")

    # ---- assignment: first pass, read everything
    files = collect_files(args.assignment)
    items = []
    kinds = set()
    has_tex = False
    for fp, rel in files:
        kind = file_kind(fp)
        kinds.add(kind if kind not in ("pptx", "ipynb") else kind)
        if kind in ("docx", "pdf", "pandoc", "text"):
            if fp.suffix.lower() in {".tex", ".latex"}:
                has_tex = True
            # README-style text inside a code project is shown as a numbered file
            if kind == "text" and len(files) > 1 and any(file_kind(f) == "code" for f, _ in files):
                items.append(("code", fp, rel, None))
                continue
            items.append(("prose", fp, rel, prose_blocks_for(fp, kind, work)))
        elif kind == "pptx":
            items.append(("pptx", fp, rel, None))
        elif kind == "ipynb":
            items.append(("ipynb", fp, rel, None))
        elif kind == "code":
            items.append(("code", fp, rel, None))
        elif kind == "image":
            target = work / "media" / rel.replace(os.sep, "_")
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(fp, target)
            media.append(str(target))
            items.append(("image", fp, rel, None))
            warnings.append(f"{rel}: image file. View it and transcribe its content into assignment.md "
                            "at the marked placeholder.")
        elif kind == "unsupported":
            warnings.append(f"{rel}: format not supported ({fp.suffix}). Ask the user to export it "
                            "(e.g. .doc→.docx, Pages/Keynote→PDF, spreadsheet→CSV).")
        else:
            if len(files) == 1:
                items.append(("prose", fp, rel, split_paragraphs(fp.read_text(encoding="utf-8", errors="replace"))))

    # quick stats for type guess
    probe = Out()
    for typ, fp, rel, blocks in items:
        if typ == "prose":
            render_prose(probe, blocks, line_mode=False)
    code_files = [i for i in items if i[0] == "code"]
    approx_code_lines = 0
    for _, fp, _, _ in code_files:
        try:
            approx_code_lines += sum(1 for ln in fp.read_text(encoding="utf-8", errors="replace").split("\n") if ln.strip())
        except OSError:
            pass
    if any(i[0] == "ipynb" for i in items):
        kinds.add("ipynb")
    math_ratio = probe.math_lines_seen / probe.prose_lines_seen if probe.prose_lines_seen else 0.0
    detected = guess_type(spec_text, kinds, probe.prose_words, approx_code_lines, math_ratio,
                          probe.headings, has_tex)
    atype = args.type or detected
    line_mode = atype == "maths"

    # ---- second pass, render
    out = Out()
    for typ, fp, rel, blocks in items:
        if len(items) > 1 and typ != "code":
            out.add(f"### FILE: {rel}")
            out.add()
        if typ == "prose":
            render_prose(out, blocks, line_mode)
        elif typ == "pptx":
            render_pptx(out, fp)
        elif typ == "ipynb":
            render_ipynb(out, fp)
        elif typ == "code":
            render_code_file(out, fp, rel)
        elif typ == "image":
            out.add(f"[TRANSCRIBE: {rel} — replace this line with the transcription, "
                    f"numbered {'[L#]' if line_mode else '[P#]'}]")
            out.add()

    legend = []
    if out.p:
        legend.append("[P#] paragraph or heading (## marks headings)")
    if out.l:
        legend.append("[L#] line of working (## marks headings)")
    if out.t:
        legend.append("[T#] table")
    if out.s:
        legend.append("[S#] slide, [S#.#] item on slide, [S# notes] speaker notes")
    if out.c:
        legend.append("[C#] notebook cell, [C#:L#] line in code cell")
    if out.code_files:
        legend.append("code: cite as path:line")
    if any(i[0] == "image" for i in items):
        legend.append("transcribed content uses the same scheme")

    header = ["# Assignment (prepared for review)", ""]
    header.append("Files: " + ", ".join(rel for _, _, rel, _ in items))
    header.append("Numbering: " + "; ".join(legend))
    wc = {}
    if out.prose_words:
        wc["total_words"] = out.prose_words
        wc["words_excluding_references"] = out.prose_words_ex_refs
        refs_note = ("" if out.prose_words == out.prose_words_ex_refs
                     else f" · {out.prose_words_ex_refs:,} excluding the reference list")
        header.append(f"Word count: {out.prose_words:,} total{refs_note} "
                      "(includes headings, tables and in-text citations)")
    if out.s:
        wc["slide_words"] = out.slide_words
        wc["speaker_note_words"] = out.note_words
        header.append(f"Slides: {out.s} · {out.slide_words:,} words on slides · "
                      f"{out.note_words:,} words in speaker notes")
    if out.code_lines:
        wc["non_blank_code_lines"] = out.code_lines
        header.append(f"Code: {out.code_lines:,} non-blank lines")
    if out.images:
        header.append(f"Images/figures: {out.images} (not visible to reviewers unless described)")
    header += ["", "---", ""]

    assign_path = work / "assignment.md"
    assign_path.write_text("\n".join(header + out.lines).rstrip() + "\n", encoding="utf-8")

    if not out.lines:
        warnings.append("No assignment content could be extracted.")

    first = Path(args.assignment[0]).expanduser()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", first.stem if first.is_file() else first.name).strip("-") or "assignment"

    result = {
        "work": str(work),
        "spec_path": str(spec_path),
        "assignment_path": str(assign_path),
        "detected_type": detected,
        "type_used_for_numbering": atype,
        "math_line_ratio": round(math_ratio, 2),
        "word_count": wc,
        "spec_fingerprint": fingerprint,
        "report_stem": stem,
        "timestamp": datetime.datetime.now().strftime("%Y%m%d-%H%M"),
        "previous_reports": previous_reports(args.cwd, fingerprint),
        "media": media,
        "warnings": warnings,
    }
    if args.type is None and detected == "maths":
        result["note"] = "Maths detected: assignment numbered by line [L#]."
    elif args.type is None:
        result["note"] = ("If the real type is maths, rerun with --type maths so working is "
                          "numbered line by line.")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
