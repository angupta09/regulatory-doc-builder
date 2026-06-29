"""
BM25 retrieval index over raw_spans.

BM25 (Best Match 25) is highly effective for domain-specific regulatory text:
  - No model downloads required.
  - Handles numeric values, abbreviations, and rare terms well.
  - Fast for the document sizes typical in CTD/CSR submissions.

Usage:
    index = BM25Index(spans)           # spans: list of raw_span dicts from DB
    hits = index.search(query, k=15)   # returns top-k spans as dicts
"""

from __future__ import annotations

import re
from typing import Any

from rank_bm25 import BM25Okapi


_STOP = frozenset({
    "the", "a", "an", "and", "or", "of", "in", "to", "for", "with",
    "was", "were", "is", "are", "be", "been", "has", "have", "had",
    "at", "by", "from", "on", "as", "this", "that", "these", "those",
})


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, remove stopwords and empties."""
    tokens = re.split(r"[^a-zA-Z0-9/\.\-]", text.lower())
    return [t for t in tokens if t and t not in _STOP and len(t) > 1]


class BM25Index:
    def __init__(self, spans: list[dict[str, Any]]):
        """
        `spans` is a list of raw_span dicts, each with at least:
          span_id, location, raw_text, span_type, source_document
        """
        self._spans = spans
        corpus = [_tokenize(s["raw_text"]) for s in spans]
        self._bm25 = BM25Okapi(corpus) if corpus else None

    def search(
        self,
        query: str,
        k: int = 15,
        span_type_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return top-k spans most relevant to `query`.
        Optional `span_type_filter`: "prose" | "table_cell" | None (both).
        """
        if self._bm25 is None or not self._spans:
            return []

        tokens = _tokenize(query)
        if not tokens:
            return []

        scores = self._bm25.get_scores(tokens)
        ranked = sorted(
            range(len(self._spans)), key=lambda i: scores[i], reverse=True
        )

        results = []
        for idx in ranked:
            if len(results) >= k:
                break
            span = self._spans[idx]
            if span_type_filter and span.get("span_type") != span_type_filter:
                continue
            results.append({**span, "_score": float(scores[idx])})

        return results

    def search_by_header_match(
        self, headers: list[str], k: int = 10
    ) -> list[dict[str, Any]]:
        """
        M2 direct header-string matching: find table_cell spans whose text
        closely matches any of the given header strings.
        Used when the schema field corresponds to a structured table.
        """
        query = " ".join(headers)
        return self.search(query, k=k, span_type_filter="table_cell")

    def all_table_cell_spans(self) -> list[dict[str, Any]]:
        return [s for s in self._spans if s.get("span_type") == "table_cell"]

    def __len__(self) -> int:
        return len(self._spans)
