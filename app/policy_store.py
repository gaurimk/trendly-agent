"""
Loads trendly_policy.md and splits it into addressable sections (e.g. "1.5", "2.3")
so the agent can retrieve and cite the exact clause it relied on, instead of either
(a) stuffing the whole document into every prompt, or (b) answering from memory.

This is intentionally simple (regex section-header split + keyword scoring) because
the source document is short (~1,500 words) and static. A vector index would be
over-engineering for one document; if the policy corpus grows, swap the scoring
function in `search` for an embedding-based retriever without touching callers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

POLICY_PATH = Path(__file__).resolve().parent.parent / "data" / "trendly_policy.md"

# Matches lines like "**1.5 Delayed orders.**" which is how every clause in the
# source doc is authored.
_CLAUSE_RE = re.compile(r"\*\*(?P<num>\d+\.\d+)\s+(?P<title>[^*]+?)\.?\*\*")


@dataclass(frozen=True)
class PolicyClause:
    number: str          # e.g. "1.5"
    title: str           # e.g. "Delayed orders"
    section_title: str   # e.g. "1. Shipping"
    text: str            # full clause body, including the "**1.5 ...**" lead-in


class PolicyStore:
    def __init__(self, path: Path = POLICY_PATH):
        self._raw = path.read_text(encoding="utf-8")
        self.clauses: list[PolicyClause] = self._parse(self._raw)

    @staticmethod
    def _parse(raw: str) -> list[PolicyClause]:
        lines = raw.splitlines()
        current_section_title = ""
        clauses: list[PolicyClause] = []
        buffer: list[str] = []
        current_num, current_title = None, None

        def flush():
            if current_num is not None:
                clauses.append(
                    PolicyClause(
                        number=current_num,
                        title=current_title or "",
                        section_title=current_section_title,
                        text="\n".join(buffer).strip(),
                    )
                )

        for line in lines:
            h2 = re.match(r"^##\s+(.*)", line)
            if h2:
                current_section_title = h2.group(1).strip()
                continue
            m = _CLAUSE_RE.match(line.strip())
            if m:
                flush()
                current_num = m.group("num")
                current_title = m.group("title").strip()
                buffer = [line]
            elif current_num is not None:
                buffer.append(line)
        flush()
        return clauses

    def get(self, number: str) -> Optional[PolicyClause]:
        return next((c for c in self.clauses if c.number == number), None)

    def search(self, query: str, top_k: int = 3) -> list[PolicyClause]:
        """Keyword-overlap scoring across clause title + body. Good enough for a
        single short, well-structured document; see module docstring for the
        scaling note."""
        terms = {t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2}
        scored = []
        for clause in self.clauses:
            haystack = f"{clause.title} {clause.text}".lower()
            score = sum(haystack.count(t) for t in terms)
            if score:
                scored.append((score, clause))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:top_k]]

    def full_text(self) -> str:
        return self._raw


policy_store = PolicyStore()
