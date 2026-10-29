"""
Convert BRD / To-Be Process / FRS documents (.doc, .docx, .pdf) to Markdown.

Usage:
    python3 -I convert_docs.py --out docs/spec/00-sources <file-or-folder> [...]

For each input file this writes:
    <out>/<stem>.md          Markdown text (headings, lists, tables preserved)
    <out>/<stem>_media/      Embedded images from .docx (process diagrams, UI mockups)
    <out>/<stem>_figures.md  Checklist of every figure / PDF page with a figure, by section

and finally <out>/INDEX.md listing every converted file, its word count, figure
count and any warnings.

Conversion strategy:
- .docx  -> parsed directly with the standard library (no python-docx needed)
- .doc   -> converted to .docx with LibreOffice (soffice --headless), then parsed
- .pdf   -> pdftotext -layout, running headers/footers removed; pages holding
            diagrams are found with pdfimages and flagged for visual reading.
            Without poppler-utils, a stub tells the agent to read the PDF directly.

Word specifics (formats vary between clients, so nothing assumes one template):
- headings come from heading styles, custom styles based on them, or outline levels;
  without any, they are inferred from numbered lines ("3.2 Title") and short bold lines
- nested list levels are indented and numbered lists keep their real numbers
  (documents say "see point 7"); bold labels stay **bold**; line breaks inside
  paragraphs and table cells are kept; merged table cells keep columns aligned
- figures: pasted images (EMF/WMF/TIFF converted to PNG via LibreOffice), flowcharts
  drawn with shapes / text boxes and SmartArt (their labels are written into the text),
  embedded files (copied out); 1x1 spacer images are dropped
- reviewer comments are appended; unaccepted tracked changes are flagged
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SUPPORTED = (".doc", ".docx", ".pdf")


R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
DECORATIVE_PX = 32          # images this small are spacers/placeholders, not content
VIEWABLE = (".png", ".jpg", ".jpeg", ".gif", ".webp")   # formats the Read tool can show


def run_text(r):
    parts = []
    for node in r:
        if node.tag == W + "t" and node.text:
            parts.append(node.text)
        elif node.tag == W + "tab":
            parts.append(" ")
        elif node.tag in (W + "br", W + "cr"):
            parts.append("\n")
    return "".join(parts)


def is_bold(r):
    b = r.find(f"{W}rPr/{W}b")
    return b is not None and b.get(W + "val", "true") not in ("0", "false")


def walk(el, skip=(W + "txbxContent", MC_FALLBACK)):
    """Iterate descendants, not entering text boxes or mc:Fallback (a duplicate of mc:Choice)."""
    for child in el:
        if child.tag in skip:
            continue
        yield child
        yield from walk(child, skip)


def own_runs(p):
    return [n for n in walk(p) if n.tag == W + "r"]


def text_of(p, markup=True):
    """Visible text of a paragraph. Bold runs become **bold**, line breaks become \\n.

    Text inside text boxes / shapes is excluded here and reported separately by shape_text().
    """
    out = []
    for r in own_runs(p):
        t = run_text(r)
        if markup and t.strip() and is_bold(r):
            lead, core, trail = re.match(r"^(\s*)(.*?)(\s*)$", t, re.S).groups()
            t = f"{lead}**{core}**{trail}"
        out.append(t)
    text = "".join(out).replace("****", "")
    lines = [re.sub(r"[ \t\u00a0]+", " ", l).strip() for l in text.split("\n")]
    return "\n".join(l for l in lines if l)


class Styles:
    """Resolves heading levels and list numbering through styles.xml (custom styles,
    basedOn inheritance, outline levels) so non-standard templates still give structure."""

    def __init__(self, xml):
        self.styles = {}
        if xml is None:
            return
        for st in ET.fromstring(xml).findall(W + "style"):
            name = st.find(W + "name")
            based = st.find(W + "basedOn")
            ppr = st.find(W + "pPr")
            outline = ppr.find(W + "outlineLvl") if ppr is not None else None
            numpr = ppr.find(W + "numPr") if ppr is not None else None
            self.styles[st.get(W + "styleId")] = {
                "name": name.get(W + "val", "") if name is not None else "",
                "based": based.get(W + "val") if based is not None else None,
                "outline": int(outline.get(W + "val")) if outline is not None else None,
                "num": numpr,
            }

    def chain(self, style_id):
        seen = set()
        while style_id and style_id not in seen:
            seen.add(style_id)
            yield style_id, self.styles.get(style_id, {})
            style_id = self.styles.get(style_id, {}).get("based")

    def heading_level(self, style_id, own_outline=None):
        if own_outline is not None:
            return own_outline + 1 if own_outline < 6 else 0
        for sid, st in self.chain(style_id):
            for label in (st.get("name", ""), sid):
                m = re.match(r"(?i)^(heading|titre|überschrift|título|titolo)\s*([1-6])$", label.replace("-", ""))
                if m:
                    return int(m.group(2))
                if label.lower() == "title":
                    return 1
            if st.get("outline") is not None:
                return st["outline"] + 1 if st["outline"] < 6 else 0
        return 0

    def style_num(self, style_id):
        for _, st in self.chain(style_id):
            if st.get("num") is not None:
                return st["num"]
        return None

    def is_list_style(self, style_id):
        return any("list" in (st.get("name", "") + sid).lower() for sid, st in self.chain(style_id))


def numpr_tuple(numpr):
    if numpr is None:
        return None
    num_id = numpr.find(W + "numId")
    ilvl = numpr.find(W + "ilvl")
    if num_id is None or num_id.get(W + "val") == "0":
        return None
    return num_id.get(W + "val"), int(ilvl.get(W + "val")) if ilvl is not None else 0


def paragraph_info(p, styles):
    """Return (heading_level, list_numbering, is_list_style) for a paragraph."""
    ppr = p.find(W + "pPr")
    style_id, own_outline, numpr = "", None, None
    if ppr is not None:
        style = ppr.find(W + "pStyle")
        style_id = style.get(W + "val", "") if style is not None else ""
        outline = ppr.find(W + "outlineLvl")
        own_outline = int(outline.get(W + "val")) if outline is not None else None
        numpr = ppr.find(W + "numPr")
    num = numpr_tuple(numpr) or numpr_tuple(styles.style_num(style_id))
    return styles.heading_level(style_id, own_outline), num, styles.is_list_style(style_id)


MANUAL_HEADING = re.compile(r"^(?:(?:chapter|section)\s+)?(\d+(?:\.\d+){0,4})\.?\s+\S", re.I)


def inferred_heading_level(p, text):
    """For documents without heading styles: '3.2 Purchase Approval' or a short all-bold line."""
    plain = text.replace("**", "")
    words = len(plain.split())
    if words == 0 or words > 12 or plain.endswith((".", ";", ",")):
        return 0
    m = MANUAL_HEADING.match(plain)
    if m:
        return min(m.group(1).count(".") + 1, 6)
    runs = [r for r in own_runs(p) if run_text(r).strip()]
    if runs and all(is_bold(r) for r in runs) and words <= 8 and not plain.endswith(":"):
        return 3
    return 0


class Numbering:
    """Renders Word list numbers (1., a), i.) so references like "see point 7" still resolve."""

    def __init__(self, xml):
        self.levels, self.num_to_abs, self.counters = {}, {}, {}
        if xml is None:
            return
        root = ET.fromstring(xml)
        for a in root.findall(W + "abstractNum"):
            aid = a.get(W + "abstractNumId")
            for lvl in a.findall(W + "lvl"):
                fmt = lvl.find(W + "numFmt")
                text = lvl.find(W + "lvlText")
                start = lvl.find(W + "start")
                self.levels[(aid, int(lvl.get(W + "ilvl")))] = (
                    fmt.get(W + "val") if fmt is not None else "bullet",
                    text.get(W + "val") if text is not None else "",
                    int(start.get(W + "val")) if start is not None else 1,
                )
        for n in root.findall(W + "num"):
            ref = n.find(W + "abstractNumId")
            if ref is not None:
                self.num_to_abs[n.get(W + "numId")] = ref.get(W + "val")

    @staticmethod
    def fmt(n, kind):
        if kind == "lowerLetter":
            return chr(96 + (n - 1) % 26 + 1)
        if kind == "upperLetter":
            return chr(64 + (n - 1) % 26 + 1)
        if kind in ("lowerRoman", "upperRoman"):
            vals = [(10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
            out = ""
            for v, sym in vals:
                while n >= v:
                    out, n = out + sym, n - v
            return out if kind == "lowerRoman" else out.upper()
        return str(n)

    def marker(self, num_id, ilvl):
        aid = self.num_to_abs.get(num_id)
        kind, text, start = self.levels.get((aid, ilvl), ("bullet", "", 1))
        if kind in ("bullet", "none"):
            return "-"
        counts = self.counters.setdefault(aid, {})
        counts[ilvl] = counts.get(ilvl, start - 1) + 1
        for deeper in [k for k in counts if k > ilvl]:
            del counts[deeper]
        label = text or f"%{ilvl + 1}."
        for lvl in range(ilvl + 1):
            lk, _, ls = self.levels.get((aid, lvl), ("decimal", "", 1))
            label = label.replace(f"%{lvl + 1}", self.fmt(counts.get(lvl, ls), lk))
        label = label.strip()
        return label if label.endswith((".", ")")) else label + "."


def image_targets(el, rels):
    """Media paths referenced by drawings inside an element, in document order."""
    found = []
    for node in walk(el, skip=(MC_FALLBACK,)):
        if node.tag.endswith("}blip") or node.tag.endswith("}imagedata"):
            rid = node.get(R + "embed") or node.get(R + "id")
            if rid in rels:
                found.append(rels[rid])
    return found


def shape_text(el, diagrams):
    """Labels of flowcharts drawn with Word shapes/text boxes, and SmartArt text."""
    labels = []
    for node in walk(el, skip=(MC_FALLBACK,)):
        if node.tag == W + "txbxContent":
            text = " ".join(text_of(p, markup=False).replace("\n", " ") for p in node.iter(W + "p"))
            if text.strip():
                labels.append(text.strip())
        elif node.tag.endswith("}relIds"):
            dm = node.get(R + "dm")
            labels += diagrams.get(dm, [])
    return labels


def cell_md(tc):
    text = "<br>".join(text_of(p).replace("\n", "<br>") for p in tc.iter(W + "p") if text_of(p))
    return text.replace("|", "\\|")


def table_md(tbl):
    rows = []
    for tr in tbl.findall(W + "tr"):
        row = []
        for tc in tr.findall(W + "tc"):
            span = tc.find(f"{W}tcPr/{W}gridSpan")
            row.append(cell_md(tc))
            row += [""] * (int(span.get(W + "val", "1")) - 1 if span is not None else 0)
        rows.append(row)
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def to_png(path):
    """EMF/WMF/TIFF/BMP (typical for Visio pastes) -> PNG so the Read tool can show it."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return path
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run([soffice, f"-env:UserInstallation=file://{tmp}/lo", "--headless",
                            "--convert-to", "png", "--outdir", os.path.dirname(path), path],
                           check=True, capture_output=True, timeout=120)
        except (subprocess.SubprocessError, OSError):
            return path
    png = os.path.splitext(path)[0] + ".png"
    return png if os.path.exists(png) else path


def png_size(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def docx_to_md(path, media_dir):
    """Return (markdown, figures, warnings) for a .docx file.

    figures: list of dicts {no, file, section, size} for content images, in document order.
    """
    warnings = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        doc_xml = z.read("word/document.xml")
        body = ET.fromstring(doc_xml).find(W + "body")
        numbering = Numbering(z.read("word/numbering.xml") if "word/numbering.xml" in names else None)
        styles = Styles(z.read("word/styles.xml") if "word/styles.xml" in names else None)
        rels, diagrams = {}, {}
        if "word/_rels/document.xml.rels" in names:
            for rel in ET.fromstring(z.read("word/_rels/document.xml.rels")).findall(PKG_REL + "Relationship"):
                if rel.get("TargetMode") == "External":
                    continue
                target = "word/" + rel.get("Target").lstrip("/").replace("word/", "", 1)
                kind = rel.get("Type", "")
                if kind.endswith("/image"):
                    rels[rel.get("Id")] = target
                elif kind.endswith("/diagramData") and target in names:
                    diagrams[rel.get("Id")] = [t.text.strip() for t in ET.fromstring(z.read(target)).iter(A + "t")
                                               if t.text and t.text.strip()]
        media = {}
        for target in set(rels.values()):
            if target in names:
                data = z.read(target)
                media[target] = (data, png_size(data))
        comments = []
        if "word/comments.xml" in names:
            for c in ET.fromstring(z.read("word/comments.xml")).findall(W + "comment"):
                text = " ".join(text_of(p, markup=False) for p in c.iter(W + "p")).strip()
                if text:
                    comments.append(text)
        embedded = [n for n in names if n.startswith("word/embeddings/")]
        if embedded:
            target_dir = os.path.join(media_dir, "embedded")
            os.makedirs(target_dir, exist_ok=True)
            for n in embedded:
                with open(os.path.join(target_dir, os.path.basename(n)), "wb") as f:
                    f.write(z.read(n))
            warnings.append(f"{len(embedded)} embedded file(s) (Excel/Visio/other) copied to "
                            f"{os.path.basename(media_dir)}/embedded/ - check them for requirements")
        if re.search(rb"<w:(ins|del) ", doc_xml):
            warnings.append("document has unaccepted tracked changes - text is shown with insertions kept "
                            "and deletions removed; confirm the final wording with the client")
        use_style_headings = any(paragraph_info(p, styles)[0] for p in body.iter(W + "p"))
        if not use_style_headings:
            warnings.append("no heading styles - headings inferred from numbering ('3.2 Title') and short bold lines; "
                            "check the section structure")

    lines, figures, written = [], [], {}
    section, section_words, empty_sections = "(before first heading)", {}, []
    heading_levels = []  # (name, level) in order, to tell empty sections from parent headings

    def add_figures(el):
        for target in image_targets(el, rels):
            data, size = media.get(target, (b"", None))
            if len(data) < 200 or (size and max(size) <= DECORATIVE_PX):
                continue  # 1x1 spacers and missing targets carry no content
            if target not in written:
                os.makedirs(media_dir, exist_ok=True)
                out = os.path.join(media_dir, os.path.basename(target))
                with open(out, "wb") as f:
                    f.write(data)
                written[target] = out
            if not written[target].lower().endswith(VIEWABLE):
                written[target] = to_png(written[target])
            rel_path = f"{os.path.basename(media_dir)}/{os.path.basename(written[target])}"
            no = len(figures) + 1
            figures.append({"no": no, "file": rel_path, "section": section,
                            "size": f"{size[0]}x{size[1]}" if size else "?"})
            lines.append(f"![Figure {no}]({rel_path})")

    def add_shapes(el):
        labels = shape_text(el, diagrams)
        if labels:
            no = len(figures) + 1
            figures.append({"no": no, "file": "(drawn with Word shapes - labels in the text)",
                            "section": section, "size": f"{len(labels)} labels"})
            lines.append(f"> **Figure {no} - diagram drawn with shapes** (label order may not match the flow): "
                         + " · ".join(labels) + "\n")

    for el in body:
        if el.tag == W + "p":
            level, num, list_style = paragraph_info(el, styles)
            add_figures(el)
            add_shapes(el)
            text = text_of(el)
            if not text:
                continue
            if not use_style_headings and not num:
                level = inferred_heading_level(el, text)
            if level:
                plain = text_of(el, markup=False).replace("\n", " ")
                section = plain
                section_words.setdefault(section, 0)
                heading_levels.append((plain, level))
                lines.append("\n" + "#" * level + " " + plain + "\n")
                continue
            section_words[section] = section_words.get(section, 0) + len(text.split())
            if num or list_style:
                num_id, ilvl = num or ("", 0)
                marker = numbering.marker(num_id, ilvl) if num else "-"
                indent = "  " * ilvl
                body_text = text.replace("\n", "\n" + indent + "  ")
                lines.append(f"{indent}{marker} {body_text}")
            else:
                lines.append(text.replace("\n", "  \n") + "\n")
        elif el.tag == W + "tbl":
            add_figures(el)
            add_shapes(el)
            section_words[section] = section_words.get(section, 0) + 1
            lines.append("\n" + table_md(el) + "\n")

    if comments:
        lines.append("\n## Reviewer comments in the document\n")
        lines += [f"- [C{i}] {c}" for i, c in enumerate(comments, 1)]
        warnings.append(f"{len(comments)} reviewer comment(s) appended at the end - they often hold open questions")

    figure_sections = {f["section"] for f in figures}
    for i, (name, level) in enumerate(heading_levels):
        if section_words.get(name):
            continue
        is_parent = i + 1 < len(heading_levels) and heading_levels[i + 1][1] > level
        if name in figure_sections:
            warnings.append(f"section '{name}' is described only by figures - its requirements are in the images")
        elif not is_parent and level > 1:
            empty_sections.append(name)
    if empty_sections:
        warnings.append("empty sections (heading with no content): " + "; ".join(f"'{s}'" for s in empty_sections))
    if figures:
        warnings.append(f"{len(figures)} content figure(s) - view each one with the Read tool (see the figures list)")
    if not heading_levels:
        warnings.append("no headings found at all - section numbers must be inferred from text")
    return "\n".join(lines).strip() + "\n", figures, warnings


def doc_to_docx(path, workdir):
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("legacy .doc needs LibreOffice (soffice) - install it or save the file as .docx")
    # A private profile avoids clashing with a LibreOffice window the user already has open
    profile = "file://" + os.path.join(workdir, "lo-profile")
    subprocess.run(
        [soffice, f"-env:UserInstallation={profile}", "--headless",
         "--convert-to", "docx", "--outdir", workdir, path],
        check=True, capture_output=True, timeout=180,
    )
    converted = os.path.join(workdir, os.path.splitext(os.path.basename(path))[0] + ".docx")
    if not os.path.exists(converted):
        raise RuntimeError("LibreOffice did not produce a .docx")
    return converted


def pdf_image_pages(path):
    """Pages carrying a content image (diagram, screenshot), via poppler's pdfimages."""
    if not shutil.which("pdfimages"):
        return None
    result = subprocess.run(["pdfimages", "-list", path], capture_output=True, text=True, timeout=180)
    pages = {}
    for line in result.stdout.splitlines()[2:]:
        cols = line.split()
        if len(cols) > 4 and cols[0].isdigit() and cols[3].isdigit() and cols[4].isdigit():
            w, h = int(cols[3]), int(cols[4])
            if max(w, h) > 200:  # skip icons, bullets and logos
                pages.setdefault(int(cols[0]), f"{w}x{h}")
    return pages


def strip_running_lines(pages):
    """Drop header/footer lines repeated on most pages (e.g. 'Company | BRD | 12')."""
    norm = lambda l: re.sub(r"\d+", "#", l.strip())
    counts = {}
    for p in pages:
        for key in {norm(l) for l in p.splitlines() if l.strip()}:
            counts[key] = counts.get(key, 0) + 1
    running = {k for k, c in counts.items() if c >= max(3, len(pages) // 2)}
    return ["\n".join(l for l in p.splitlines() if norm(l) not in running) for p in pages]


def pdf_to_md(path):
    """Return (markdown, figures, warnings). Figures point at PDF pages to view."""
    if not shutil.which("pdftotext"):
        return None, [], ["pdftotext not installed - read the PDF directly with the Read tool"]
    result = subprocess.run(["pdftotext", "-layout", path, "-"],
                            capture_output=True, text=True, timeout=180)
    text = result.stdout
    if len(text.strip()) < 200:
        return None, [], ["almost no extractable text - probably a scanned PDF; read it directly with the Read tool"]
    pages = strip_running_lines(text.split("\f"))
    image_pages = pdf_image_pages(path)
    figures, chunks = [], []
    for i, page in enumerate(pages, 1):
        if not page.strip() and i not in (image_pages or {}):
            continue
        marker = f"<!-- page {i} -->"
        if image_pages and i in image_pages:
            figures.append({"no": len(figures) + 1, "file": f"{os.path.basename(path)} page {i}",
                            "section": next((l.strip() for l in page.splitlines() if l.strip()), ""),
                            "size": image_pages[i]})
            marker = f"<!-- page {i} - contains a figure: read this page with the Read tool -->"
        chunks.append(f"{marker}\n{page.rstrip()}")
    warnings = []
    if image_pages is None:
        warnings.append("pdfimages not installed - cannot tell which pages hold diagrams; read the PDF visually")
    elif figures:
        warnings.append(f"{len(figures)} page(s) with figures - read them with the Read tool (pages: "
                        + ", ".join(str(f["file"].rsplit(" ", 1)[1]) for f in figures) + ")")
    return "\n\n".join(chunks) + "\n", figures, warnings


def collect(inputs):
    files = []
    for item in inputs:
        if os.path.isdir(item):
            for root, _, names in os.walk(item):
                files += [os.path.join(root, n) for n in sorted(names)
                          if n.lower().endswith(SUPPORTED) and not n.startswith("~$")]
        elif item.lower().endswith(SUPPORTED):
            files.append(item)
        else:
            print(f"SKIP (unsupported type): {item}", file=sys.stderr)
    return files


def main():
    parser = argparse.ArgumentParser(description="Convert requirement documents to Markdown")
    parser.add_argument("--out", required=True, help="Output folder, e.g. docs/spec/00-sources")
    parser.add_argument("inputs", nargs="+", help="Files or folders (.doc, .docx, .pdf)")
    args = parser.parse_args()

    files = collect(args.inputs)
    if not files:
        print("No .doc/.docx/.pdf files found", file=sys.stderr)
        sys.exit(1)
    os.makedirs(args.out, exist_ok=True)

    index = ["# Source Documents\n",
             "| Source file | Markdown | Size | Figures | Warnings |",
             "|---|---|---|---|---|"]
    failures = 0
    for path in files:
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.splitext(os.path.basename(path))[0])
        md_path = os.path.join(args.out, stem + ".md")
        media_dir = os.path.join(args.out, stem + "_media")
        figures_path = os.path.join(args.out, stem + "_figures.md")
        figures, warnings = [], []
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext == ".pdf":
                md, figures, warnings = pdf_to_md(path)
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    src = doc_to_docx(os.path.abspath(path), tmp) if ext == ".doc" else path
                    md, figures, warnings = docx_to_md(src, media_dir)
            if md is None:
                md = f"# {os.path.basename(path)}\n\nText extraction failed. Read the original directly: `{path}`\n"
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(f"<!-- source: {path} -->\n\n" + md)
            if figures:
                # A checklist the analyst fills in, so no diagram or mockup is skipped
                rows = ["# Figures - " + os.path.basename(path) + "\n",
                        "Open each figure with the Read tool and record what it adds.\n",
                        "| Figure | File / page | Section | Size | Kind | Requirements taken from it |",
                        "|---|---|---|---|---|---|"]
                rows += [f"| {f['no']} | `{f['file']}` | {f['section'].replace('|', '/')} | {f['size']} | | |"
                         for f in figures]
                with open(figures_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(rows) + "\n")
            size = f"{len(md.split())} words"
            print(f"OK   {path} -> {md_path} ({size}, {len(figures)} figures)")
        except Exception as e:  # report and continue with the remaining documents
            failures += 1
            size, warnings = "-", [f"FAILED: {e}"]
            print(f"FAIL {path}: {e}", file=sys.stderr)
        fig_cell = f"{len(figures)} (`{os.path.basename(figures_path)}`)" if figures else "0"
        index.append(f"| `{path}` | `{os.path.basename(md_path)}` | {size} | "
                     f"{fig_cell} | {'<br>'.join(warnings) or '-'} |")

    with open(os.path.join(args.out, "INDEX.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(index) + "\n")
    print(f"\nIndex written to {os.path.join(args.out, 'INDEX.md')}")
    sys.exit(1 if failures == len(files) else 0)


if __name__ == "__main__":
    main()
