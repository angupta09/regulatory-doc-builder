"""
PDF parser — Track B3 PDF source document extraction.

Uses pdfplumber (per skill guidance) for:
  - Layout-preserving text extraction with page/line locators.
  - Table-aware extraction to handle table-heavy clinical/tox documents.

Locator format:
  prose lines:   "page:{p}/line:{l}"
  table cells:   "page:{p}/table:{t}/row:{r}/col:{c}"
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import pdfplumber

from parsers.docx_parser import RawSpan  # reuse dataclass


class PdfParser:
    def __init__(self, pdf_bytes: bytes, source_name: str = "source.pdf"):
        self.source_name = source_name
        self._pdf_bytes = pdf_bytes

    def extract_spans(self) -> list[RawSpan]:
        """
        Extract all text content from the PDF as RawSpan objects.
        Strategy:
          1. On each page, detect tables first using pdfplumber.
          2. Extract table cells with row/col locators.
          3. Extract non-table text lines with page/line locators,
             skipping bounding boxes already covered by tables.
        """
        spans: list[RawSpan] = []

        with pdfplumber.open(io.BytesIO(self._pdf_bytes)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                # ── Tables ────────────────────────────────────────────────
                tables = page.extract_tables(
                    table_settings={
                        "vertical_strategy": "lines",
                        "horizontal_strategy": "lines",
                        "snap_tolerance": 3,
                        "join_tolerance": 3,
                        "edge_min_length": 3,
                        "min_words_vertical": 1,
                        "min_words_horizontal": 1,
                    }
                )
                table_bboxes = _get_table_bboxes(page)

                for tbl_idx, table in enumerate(tables or []):
                    for r_idx, row in enumerate(table or []):
                        for c_idx, cell in enumerate(row or []):
                            text = (cell or "").strip()
                            if text:
                                spans.append(RawSpan(
                                    source_document=self.source_name,
                                    location=f"page:{page_num}/table:{tbl_idx}/row:{r_idx}/col:{c_idx}",
                                    raw_text=text,
                                    span_type="table_cell",
                                ))

                # ── Prose text (outside tables) ───────────────────────────
                raw_text = page.extract_text(layout=True) or ""
                lines = raw_text.splitlines()
                line_num = 0
                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        line_num += 1
                        continue
                    # Skip very short lines that are likely table artefacts
                    if len(stripped) < 2:
                        line_num += 1
                        continue
                    spans.append(RawSpan(
                        source_document=self.source_name,
                        location=f"page:{page_num}/line:{line_num}",
                        raw_text=stripped,
                        span_type="prose",
                    ))
                    line_num += 1

        return spans

    def page_count(self) -> int:
        with pdfplumber.open(io.BytesIO(self._pdf_bytes)) as pdf:
            return len(pdf.pages)

    def table_count(self) -> int:
        total = 0
        with pdfplumber.open(io.BytesIO(self._pdf_bytes)) as pdf:
            for page in pdf.pages:
                total += len(page.extract_tables() or [])
        return total


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_table_bboxes(page: pdfplumber.page.Page) -> list[tuple[float, float, float, float]]:
    """Return bounding boxes of all tables on a page (used for filtering prose)."""
    bboxes = []
    for table in page.find_tables():
        bboxes.append(table.bbox)  # (x0, top, x1, bottom)
    return bboxes


def _point_in_any_bbox(
    x: float, y: float, bboxes: list[tuple[float, float, float, float]]
) -> bool:
    for x0, top, x1, bottom in bboxes:
        if x0 <= x <= x1 and top <= y <= bottom:
            return True
    return False
