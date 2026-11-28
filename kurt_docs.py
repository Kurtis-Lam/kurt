"""Turn uploaded files into text Kurt can read. Standard library only; optional tools are used when present."""
import io
import json
import re
import shutil
import subprocess
import zipfile
from xml.etree import ElementTree as ET

EXTRACTABLE = {
    ".docx", ".dotx", ".xlsx", ".xlsm", ".pptx", ".odt", ".ods", ".odp", ".epub",
    ".pdf", ".rtf", ".ipynb", ".mxl", ".mscz", ".html", ".htm",
}
MAX_CHARS = 400_000


def decode_text(raw):
    for enc in ("utf-8-sig", "utf-16", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeError, ValueError):
            continue
    return raw.decode("utf-8", "replace")


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _xml_text(data, para_tags=("p",), text_tags=("t",)):
    root = ET.fromstring(data)
    out = []
    for el in root.iter():
        name = _local(el.tag)
        if name in text_tags and el.text:
            out.append(el.text)
        elif name in para_tags:
            out.append("\n")
        elif name in ("tab",):
            out.append("\t")
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _docx(z):
    names = [n for n in z.namelist() if re.match(r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$", n)]
    parts = []
    for n in sorted(names, key=lambda x: (x != "word/document.xml", x)):
        text = _xml_text(z.read(n))
        if text:
            parts.append(("" if n == "word/document.xml" else "[" + n.split("/")[-1] + "]\n") + text)
    return "\n\n".join(parts)


def _pptx(z):
    slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                    key=lambda n: int(re.findall(r"\d+", n)[-1]))
    parts = ["## Slide %d\n%s" % (i, _xml_text(z.read(n), text_tags=("t",))) for i, n in enumerate(slides, 1)]
    notes = sorted(n for n in z.namelist() if re.match(r"ppt/notesSlides/notesSlide\d+\.xml$", n))
    parts += ["[notes %s]\n%s" % (n.split("/")[-1], _xml_text(z.read(n))) for n in notes]
    return "\n\n".join(parts)


def _xlsx(z):
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root:
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    sheet_names = {}
    if "xl/workbook.xml" in z.namelist():
        for i, sh in enumerate([e for e in ET.fromstring(z.read("xl/workbook.xml")).iter() if _local(e.tag) == "sheet"], 1):
            sheet_names[i] = sh.get("name")
    out = []
    sheets = sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                    key=lambda n: int(re.findall(r"\d+", n)[-1]))
    for idx, n in enumerate(sheets, 1):
        rows = []
        for row in (e for e in ET.fromstring(z.read(n)).iter() if _local(e.tag) == "row"):
            cells = []
            for c in row:
                ref, kind, val = c.get("r", ""), c.get("t"), ""
                v = next((x for x in c if _local(x.tag) == "v"), None)
                f = next((x for x in c if _local(x.tag) == "f"), None)
                if kind == "s" and v is not None and v.text and v.text.isdigit() and int(v.text) < len(shared):
                    val = shared[int(v.text)]
                elif kind == "inlineStr":
                    val = "".join(t.text or "" for t in c.iter() if _local(t.tag) == "t")
                elif v is not None:
                    val = v.text or ""
                if f is not None and f.text:
                    val = "%s  [=%s]" % (val, f.text)
                if val != "":
                    cells.append("%s=%s" % (ref, val))
            if cells:
                rows.append("\t".join(cells))
        out.append("## Sheet: %s\n%s" % (sheet_names.get(idx, "Sheet%d" % idx), "\n".join(rows)))
    return "\n\n".join(out)


def _odf_text(z):
    root = ET.fromstring(z.read("content.xml"))
    out = []

    def walk(el):
        if el.text and _local(el.tag) in ("p", "h", "span", "a"):
            out.append(el.text)
        for child in el:
            walk(child)
            if child.tail and _local(el.tag) in ("p", "h", "span", "a"):
                out.append(child.tail)
        if _local(el.tag) in ("p", "h", "table-row"):
            out.append("\n")
        elif _local(el.tag) == "table-cell":
            out.append("\t")

    walk(root)
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


def _epub(z):
    pages = sorted(n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm")))
    return "\n\n".join(_html(z.read(n).decode("utf-8", "replace")) for n in pages)


def _html(text):
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    import html as _h
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", _h.unescape(text))).strip()


def _rtf(raw):
    text = decode_text(raw)
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"), text)
    text = re.sub(r"\\par[d]?", "\n", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]", "", text)
    return text.strip()


def _ipynb(raw):
    nb = json.loads(decode_text(raw))
    out = []
    for cell in nb.get("cells", []):
        src = "".join(cell.get("source", []))
        out.append(("```python\n%s\n```" % src) if cell.get("cell_type") == "code" else src)
    return "\n\n".join(out)


def _pdf(path, raw):
    exe = shutil.which("pdftotext")
    if exe:
        try:
            res = subprocess.run([exe, "-layout", str(path), "-"], capture_output=True, timeout=60)
            if res.stdout.strip():
                return res.stdout.decode("utf-8", "replace")
        except Exception:
            pass
    for mod in ("pypdf", "PyPDF2"):
        try:
            lib = __import__(mod)
            reader = lib.PdfReader(io.BytesIO(raw))
            text = "\n\n".join((p.extract_text() or "") for p in reader.pages)
            if text.strip():
                return text
        except Exception:
            continue
    return "[PDF has no extractable text layer here (scanned, or no PDF reader installed). Install pypdf or poppler to read it.]"


def extract_text(path, raw=None):
    """Return readable text for a file, or '' if it cannot be read as text."""
    suffix = path.suffix.lower()
    raw = raw if raw is not None else path.read_bytes()
    try:
        if suffix == ".pdf":
            text = _pdf(path, raw)
        elif suffix in (".rtf",):
            text = _rtf(raw)
        elif suffix == ".ipynb":
            text = _ipynb(raw)
        elif suffix in (".html", ".htm"):
            return decode_text(raw)[:MAX_CHARS]
        elif zipfile.is_zipfile(io.BytesIO(raw)):
            z = zipfile.ZipFile(io.BytesIO(raw))
            if suffix in (".docx", ".dotx"):
                text = _docx(z)
            elif suffix in (".xlsx", ".xlsm"):
                text = _xlsx(z)
            elif suffix == ".pptx":
                text = _pptx(z)
            elif suffix in (".odt", ".ods", ".odp"):
                text = _odf_text(z)
            elif suffix == ".epub":
                text = _epub(z)
            elif suffix == ".mxl":
                inner = next((n for n in z.namelist() if n.endswith(".xml") and not n.startswith("META-INF")), None)
                text = decode_text(z.read(inner)) if inner else ""
            else:
                text = ""
        else:
            text = decode_text(raw) if b"\x00" not in raw[:4096] else ""
    except Exception as exc:  # corrupt or unsupported file: say so instead of crashing the chat
        return "[Could not read %s: %s]" % (path.name, exc)
    return (text or "")[:MAX_CHARS]