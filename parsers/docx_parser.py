"""
DOCX parser — used in Track A2 (template structure) and Track B3 (source content).

Design principles (from skill guidance):
- A .docx is a ZIP archive; open with zipfile, parse word/document.xml with lxml.
- Merged cells share the same underlying XML <w:tc> element — deduplicate by
  element identity (Python id()), not by grid position.
- Every extracted unit gets an immutable locator string.

Locator format:
  table_cells:   "table:{t_idx}/row:{r_idx}/col:{c_idx}"   (logical, merge-resolved)
  paragraphs:    "para:{p_idx}"
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

# Word namespace map
_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
}

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _tag(local: str) -> str:
    return f"{{{_W}}}{local}"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RawSpan:
    source_document: str
    location: str
    raw_text: str
    span_type: str  # "prose" | "table_cell"


@dataclass
class FieldSkeleton:
    """Structural field extracted from a template (Track A2)."""
    field_name: str
    location: str           # same locator format as RawSpan
    data_type_hint: str     # "text" | "number" | "list"
    context: str = ""       # surrounding header text for LLM enrichment
    instructions: str = ""  # verbatim guidance text from template for this section


@dataclass
class TableStructure:
    """Parsed table with merge-resolved cells."""
    table_index: int
    headers: list[str]       # text of first row (assumed header)
    rows: list[list[str]]    # [row_idx][col_idx] text, merge-resolved
    cell_locations: list[list[str]]  # same shape as rows, locator strings
    raw_xml_cells: list[list[Any]]   # same shape, lxml element refs (for writing)


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

class DocxParser:
    def __init__(self, docx_bytes: bytes, source_name: str = "template.docx"):
        self.source_name = source_name
        self._docx_bytes = docx_bytes
        self._doc_xml: etree._Element = self._load_document_xml()

    def _load_document_xml(self) -> etree._Element:
        with zipfile.ZipFile(io.BytesIO(self._docx_bytes)) as zf:
            xml_bytes = zf.read("word/document.xml")
        return etree.fromstring(xml_bytes)

    # ------------------------------------------------------------------
    # Track A2 — template structural parse
    # ------------------------------------------------------------------

    def extract_field_skeletons(self) -> list[FieldSkeleton]:
        """
        Parse the template's tables and paragraphs into FieldSkeleton objects.

        For narrative templates (CSR-style), section headings are paired with
        the guidance text that immediately follows them.  The guidance is stored
        in FieldSkeleton.instructions so Track A can forward it to the LLM
        enrichment step and Track C can use it during prose generation.

        A paragraph is treated as a *section heading* if it looks like a
        numbered section (e.g. "8.1. Primary Objective(s)") or is short and
        title-cased.  The paragraphs that follow until the next heading are
        collected as instructions.
        """
        import re as _re
        skeletons: list[FieldSkeleton] = []
        body = self._doc_xml.find(f".//{_tag('body')}")
        if body is None:
            body = self._doc_xml

        # Collect all body children with their texts first
        children_info: list[tuple[str, int, str]] = []  # (local_tag, index, text)
        para_idx = 0
        tbl_idx = 0
        for child in body:
            local = etree.QName(child.tag).localname if child.tag != etree.Comment else None
            if local == "p":
                text = _para_text(child).strip()
                children_info.append(("p", para_idx, text))
                para_idx += 1
            elif local == "tbl":
                children_info.append(("tbl", tbl_idx, ""))
                tbl_idx += 1

        _numbered = _re.compile(r"^\d+(\.\d+)*\.?\s+\S")
        _placeholder = _re.compile(r"[«»«»]|\[.*?\]|<<.*?>>")

        def _is_heading(text: str) -> bool:
            if _numbered.match(text):
                return True
            # Short lines that look like labels (not full sentences)
            if len(text) < 80 and not text.endswith(".") and text[0].isupper():
                return True
            return False

        i = 0
        while i < len(children_info):
            local, idx, text = children_info[i]

            if local == "tbl":
                tbl_skeletons = self._parse_table_skeletons_by_index(idx)
                skeletons.extend(tbl_skeletons)
                i += 1
                continue

            if not text:
                i += 1
                continue

            # Placeholder cells (e.g. «Study Title») → direct field
            if _placeholder.search(text):
                skeletons.append(FieldSkeleton(
                    field_name=text[:120],
                    location=f"para:{idx}",
                    data_type_hint=_guess_type(text),
                    context="",
                    instructions="",
                ))
                i += 1
                continue

            if _is_heading(text):
                # Collect following non-heading paragraphs as instructions
                guidance_parts: list[str] = []
                j = i + 1
                while j < len(children_info):
                    n_local, n_idx, n_text = children_info[j]
                    if n_local == "tbl":
                        break
                    if not n_text:
                        j += 1
                        continue
                    if _is_heading(n_text):
                        break
                    guidance_parts.append(n_text)
                    j += 1

                instructions = " ".join(guidance_parts)[:1000]
                skeletons.append(FieldSkeleton(
                    field_name=text[:120],
                    location=f"para:{idx}",
                    data_type_hint=_guess_type(text),
                    context="",
                    instructions=instructions,
                ))
                i += 1
                continue

            # Plain non-heading prose — skip (it's guidance consumed by a heading above)
            i += 1

        return skeletons

    def _parse_table_skeletons_by_index(self, tbl_idx: int) -> list[FieldSkeleton]:
        """Locate the tbl_idx-th table element and extract its skeletons."""
        body = self._doc_xml.find(f".//{_tag('body')}")
        if body is None:
            body = self._doc_xml
        tables = [c for c in body if etree.QName(c.tag).localname == "tbl"]
        if tbl_idx >= len(tables):
            return []
        return self._parse_table_skeletons(tables[tbl_idx], tbl_idx)

    def _parse_table_skeletons(self, tbl_el: Any, tbl_idx: int) -> list[FieldSkeleton]:
        """Extract field skeletons from a single table element."""
        skeletons: list[FieldSkeleton] = []
        rows = tbl_el.findall(f"{_tag('tr')}")
        seen_cell_ids: set[int] = set()

        # Header text from row 0 for context
        header_texts: list[str] = []
        if rows:
            for tc in rows[0].findall(f".//{_tag('tc')}"):
                if id(tc) not in seen_cell_ids:
                    seen_cell_ids.add(id(tc))
                    header_texts.append(_cell_text(tc).strip())

        seen_cell_ids.clear()

        for r_idx, row_el in enumerate(rows):
            cells = row_el.findall(f".//{_tag('tc')}")
            col_idx = 0
            for tc in cells:
                if id(tc) in seen_cell_ids:
                    col_idx += 1
                    continue
                seen_cell_ids.add(id(tc))

                text = _cell_text(tc).strip()
                if text:
                    ctx = header_texts[col_idx] if col_idx < len(header_texts) else ""
                    skeletons.append(FieldSkeleton(
                        field_name=text[:120],
                        location=f"table:{tbl_idx}/row:{r_idx}/col:{col_idx}",
                        data_type_hint=_guess_type(text),
                        context=ctx,
                    ))
                col_idx += 1

        return skeletons

    def get_table_structures(self) -> list[TableStructure]:
        """
        Return fully parsed TableStructure objects for all tables.
        Used by Track C to map field locations back to XML elements.
        """
        body = self._doc_xml.find(f".//{_tag('body')}")
        if body is None:
            body = self._doc_xml

        tables: list[TableStructure] = []
        tbl_idx = 0
        for child in body:
            local = etree.QName(child.tag).localname if child.tag != etree.Comment else None
            if local == "tbl":
                tables.append(self._build_table_structure(child, tbl_idx))
                tbl_idx += 1
        return tables

    def _build_table_structure(self, tbl_el: Any, tbl_idx: int) -> TableStructure:
        rows_el = tbl_el.findall(f"{_tag('tr')}")
        all_rows_text: list[list[str]] = []
        all_rows_loc: list[list[str]] = []
        all_rows_xml: list[list[Any]] = []
        seen: set[int] = set()

        for r_idx, row_el in enumerate(rows_el):
            cells = row_el.findall(f".//{_tag('tc')}")
            row_texts, row_locs, row_xml = [], [], []
            col_idx = 0
            for tc in cells:
                if id(tc) not in seen:
                    seen.add(id(tc))
                    row_texts.append(_cell_text(tc).strip())
                    row_locs.append(f"table:{tbl_idx}/row:{r_idx}/col:{col_idx}")
                    row_xml.append(tc)
                col_idx += 1
            all_rows_text.append(row_texts)
            all_rows_loc.append(row_locs)
            all_rows_xml.append(row_xml)

        headers = all_rows_text[0] if all_rows_text else []
        return TableStructure(
            table_index=tbl_idx,
            headers=headers,
            rows=all_rows_text,
            cell_locations=all_rows_loc,
            raw_xml_cells=all_rows_xml,
        )

    # ------------------------------------------------------------------
    # Track B3 — source document content extraction
    # ------------------------------------------------------------------

    def extract_spans(self) -> list[RawSpan]:
        """
        Extract all paragraphs and table cells as RawSpan objects with locators.
        One span per paragraph, one per (deduplicated) table cell.
        """
        spans: list[RawSpan] = []
        body = self._doc_xml.find(f".//{_tag('body')}")
        if body is None:
            body = self._doc_xml

        para_idx = 0
        tbl_idx = 0

        for child in body:
            local = etree.QName(child.tag).localname if child.tag != etree.Comment else None
            if local == "p":
                text = _para_text(child).strip()
                if text:
                    spans.append(RawSpan(
                        source_document=self.source_name,
                        location=f"para:{para_idx}",
                        raw_text=text,
                        span_type="prose",
                    ))
                para_idx += 1
            elif local == "tbl":
                spans.extend(self._extract_table_spans(child, tbl_idx))
                tbl_idx += 1

        return spans

    def _extract_table_spans(self, tbl_el: Any, tbl_idx: int) -> list[RawSpan]:
        spans: list[RawSpan] = []
        seen: set[int] = set()
        for r_idx, row_el in enumerate(tbl_el.findall(f"{_tag('tr')}")):
            col_idx = 0
            for tc in row_el.findall(f".//{_tag('tc')}"):
                if id(tc) not in seen:
                    seen.add(id(tc))
                    text = _cell_text(tc).strip()
                    if text:
                        spans.append(RawSpan(
                            source_document=self.source_name,
                            location=f"table:{tbl_idx}/row:{r_idx}/col:{col_idx}",
                            raw_text=text,
                            span_type="table_cell",
                        ))
                col_idx += 1
        return spans


# ---------------------------------------------------------------------------
# XML cell writer — used by Track C
# ---------------------------------------------------------------------------

class DocxCellWriter:
    """
    Opens a .docx in-memory, edits specific cells by location string,
    and produces the modified bytes without altering anything else.

    Procedure per the skill:
      1. Unzip in memory.
      2. Parse word/document.xml with lxml.
      3. Find target cell by locator, replace only its <w:t> text nodes.
      4. Reserialize and rezip — all other files stay byte-for-byte identical.
    """

    def __init__(self, docx_bytes: bytes):
        self._original_bytes = docx_bytes
        # Parse document.xml into a mutable tree
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as zf:
            self._doc_xml_bytes = zf.read("word/document.xml")
            # Keep a manifest of all other files for repacking
            self._other_files: dict[str, bytes] = {}
            for name in zf.namelist():
                if name != "word/document.xml":
                    self._other_files[name] = zf.read(name)

        self._doc_root: etree._Element = etree.fromstring(self._doc_xml_bytes)
        # Pre-index all cells for fast lookup
        self._cell_index: dict[str, Any] = {}  # locator → <w:tc> element
        self._build_cell_index()

    def _build_cell_index(self) -> None:
        body = self._doc_root.find(f".//{_tag('body')}")
        if body is None:
            body = self._doc_root
        tbl_idx = 0
        for child in body:
            local = etree.QName(child.tag).localname if child.tag != etree.Comment else None
            if local == "tbl":
                seen: set[int] = set()
                for r_idx, row_el in enumerate(child.findall(f"{_tag('tr')}")):
                    col_idx = 0
                    for tc in row_el.findall(f".//{_tag('tc')}"):
                        if id(tc) not in seen:
                            seen.add(id(tc))
                            loc = f"table:{tbl_idx}/row:{r_idx}/col:{col_idx}"
                            self._cell_index[loc] = tc
                        col_idx += 1
                tbl_idx += 1

    def write_cell(self, location: str, value: str) -> bool:
        """
        Set the text of the cell at `location` to `value`.
        Returns True if the cell was found and written, False otherwise.
        Preserves all formatting (<w:rPr>) — only replaces <w:t> content.
        """
        tc = self._cell_index.get(location)
        if tc is None:
            # Try paragraph location
            return self._write_para(location, value)
        _set_cell_text(tc, value)
        return True

    def _write_para(self, location: str, value: str) -> bool:
        if not location.startswith("para:"):
            return False
        try:
            para_idx = int(location.split(":")[1])
        except (IndexError, ValueError):
            return False
        body = self._doc_root.find(f".//{_tag('body')}")
        if body is None:
            body = self._doc_root
        paras = [c for c in body if etree.QName(c.tag).localname == "p"]
        if para_idx >= len(paras):
            return False
        _set_para_text(paras[para_idx], value)
        return True

    def get_bytes(self) -> bytes:
        """Serialize the modified document back to .docx bytes."""
        new_doc_xml = etree.tostring(self._doc_root, xml_declaration=True, encoding="UTF-8", standalone=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Other files byte-for-byte
            for name, data in self._other_files.items():
                zf.writestr(name, data)
            # Modified document.xml
            zf.writestr("word/document.xml", new_doc_xml)
        return buf.getvalue()

    def verify_non_target_unchanged(self, original_bytes: bytes, written_locations: set[str]) -> bool:
        """
        C9 format integrity check: confirm the repacked file is still valid
        and that we haven't accidentally touched non-target sections.
        (Simple check: all non-target cells still contain same text as original.)
        """
        try:
            orig_parser = DocxParser(original_bytes, "original")
            orig_spans = {s.location: s.raw_text for s in orig_parser.extract_spans()}
            new_spans = {s.location: s.raw_text for s in DocxParser(self.get_bytes(), "new").extract_spans()}
            for loc, orig_text in orig_spans.items():
                if loc not in written_locations and new_spans.get(loc) != orig_text:
                    return False
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _para_text(para_el: Any) -> str:
    """Concatenate all <w:t> text nodes inside a paragraph."""
    return "".join(t.text or "" for t in para_el.iter(_tag("t")))


def _cell_text(tc_el: Any) -> str:
    """Concatenate all text inside a table cell (may span multiple paragraphs)."""
    parts = []
    for para in tc_el.iter(_tag("p")):
        t = _para_text(para)
        if t.strip():
            parts.append(t.strip())
    return " ".join(parts)


def _set_cell_text(tc_el: Any, value: str) -> None:
    """
    Replace cell text while preserving run properties (<w:rPr>).
    Strategy: find the first <w:r> with <w:t>, set its text, remove others.
    If no run exists, create a minimal one.
    """
    # Collect all paragraphs in the cell
    paras = list(tc_el.iter(_tag("p")))
    if not paras:
        return

    # Use the first paragraph, clear remaining paragraphs' content
    first_para = paras[0]
    for extra_para in paras[1:]:
        for run in list(extra_para.findall(_tag("r"))):
            extra_para.remove(run)

    # Find runs in first paragraph
    runs = first_para.findall(_tag("r"))

    if runs:
        # Keep first run's rPr, set its text, remove other runs
        first_run = runs[0]
        # Get or create <w:t>
        t_el = first_run.find(_tag("t"))
        if t_el is None:
            t_el = etree.SubElement(first_run, _tag("t"))
        t_el.text = value
        if " " in value or value.startswith(" ") or value.endswith(" "):
            t_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        # Remove remaining runs
        for extra_run in runs[1:]:
            first_para.remove(extra_run)
    else:
        # No existing runs — create a minimal run
        run_el = etree.SubElement(first_para, _tag("r"))
        t_el = etree.SubElement(run_el, _tag("t"))
        t_el.text = value


def _set_para_text(para_el: Any, value: str) -> None:
    """Replace paragraph text preserving the first run's formatting."""
    runs = para_el.findall(_tag("r"))
    if runs:
        first_run = runs[0]
        t_el = first_run.find(_tag("t"))
        if t_el is None:
            t_el = etree.SubElement(first_run, _tag("t"))
        t_el.text = value
        for extra_run in runs[1:]:
            para_el.remove(extra_run)
    else:
        run_el = etree.SubElement(para_el, _tag("r"))
        t_el = etree.SubElement(run_el, _tag("t"))
        t_el.text = value


def _guess_type(text: str) -> str:
    lower = text.lower()
    if any(k in lower for k in ("number", "count", "n ", "#", "dose", "mg", "kg")):
        return "number"
    if any(k in lower for k in ("list", "findings", "observations", "summary")):
        return "list"
    return "text"
