"""
evidence/model.py
-----------------
Immutable, serialisable records for retrieved sources and passages.

Each EvidenceRecord represents a single source retrieval:
  - Stable, deterministic evidence_id (first 32 hex chars of SHA-256 of source_url).
  - Explicit requested source_url and optional final_url (preserving redirect provenance).
  - The exact text captured from the tool (or empty string for FAILED status).
  - Optional snippet_text preserving search snippet provenance if upgraded to full-text.
  - Optional snippet_observations preserving all observed search snippets across providers.
  - Optional provider tracking provenance (e.g. "exa", "tavily", "pdf_extractor").
  - A content_hash (full SHA-256 hex of captured_text) for consistency verification.
  - A retrieval_status distinguishing genuine content from failures.

IMPORTANT GUARANTEES AND SCOPE BOUNDARIES
------------------------------------------
A matching content_hash establishes consistency with the stored text at capture time.
It does NOT prove origin-server authenticity, network provenance, or the factual truth
of any claim. Semantic claim verification is outside the scope of Phase 1A / Phase 1B.

Source vs. Passage Distinction
------------------------------
- EvidenceRecord: Source-level entity identified by source_url SHA-256 digest (evidence_id).
- PassageRecord:  Passage-level entity identified by compound hash of parent evidence_id
                  and passage text (passage_id).

Retrieval-status semantics
--------------------------
  FULL_TEXT    – tool fetched and returned full page or PDF text.
  SNIPPET_ONLY – search-result snippet only; full document fetch was not performed.
  FAILED       – fetch raised an exception or tool reported failure. captured_text is
                 always forced to empty string ("") so error strings are never stored
                 as source evidence.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import re
from typing import TYPE_CHECKING, Optional, Tuple
from urllib.parse import urlparse, urlunparse

if TYPE_CHECKING:
    from research_crew.tools.tool_result import ToolFetchResult


class RetrievalStatus(str, Enum):
    """Describes the completeness of a source retrieval."""

    FULL_TEXT = "full_text"
    """Full page or PDF content was fetched successfully."""

    SNIPPET_ONLY = "snippet_only"
    """Only a short search-result snippet is available; no full fetch occurred."""

    FAILED = "failed"
    """The fetch failed (network error, HTTP error, or tool error)."""


# ---------------------------------------------------------------------------
# Hashing helpers (pure functions, no side-effects)
# ---------------------------------------------------------------------------

def make_evidence_id(url: str) -> str:
    """Return a stable, deterministic 32-hex-char ID for a source URL.

    The ID is the first 128 bits (32 hex chars) of SHA-256(url.strip()).
    Identifies the source document entity.
    """
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:32]


def make_passage_id(evidence_id: str, passage_text: str) -> str:
    """Return a deterministic 32-hex-char ID for an individual passage within a source.

    Compound digest: SHA-256(f"{evidence_id}::{passage_text}")[:32].
    Distinct from source evidence_id to enforce source vs. passage separation.
    """
    composite = f"{evidence_id}::{passage_text.strip()}".encode("utf-8")
    return hashlib.sha256(composite).hexdigest()[:32]


def make_content_hash(text: str) -> str:
    """Return the full SHA-256 hex digest (64 chars) of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Canonical URL Matching Helpers
# ---------------------------------------------------------------------------

def parse_arxiv_id(url: str) -> Optional[str]:
    """Extract canonical arXiv paper identifier if the URL points to arXiv.

    Supports:
    - Standard modern IDs: YYMM.NNNNN or YYMM.NNNNNvV (e.g., '2404.03715', '2404.03715v2')
    - Pre-2007 IDs: archive/YYMMNNN or archive/YYMMNNNvV (e.g., 'hep-th/9901001', 'math/0309136v1')
    - Host variants: arxiv.org, www.arxiv.org, export.arxiv.org, ar5iv.labs.arxiv.org, ar5iv.org
    - Endpoint variants: /abs/, /html/, /pdf/, /format/
    - Preserves explicit version suffixes (e.g. 'v1' vs 'v2' are kept distinct).
    - Returns lowercase identifier or None if not an arXiv paper URL.
    """
    if not url or not isinstance(url, str):
        return None
    cand = url.strip()
    if not cand:
        return None

    if "://" not in cand:
        cand_lower = cand.lower()
        if (
            cand_lower.startswith("arxiv.org")
            or cand_lower.startswith("www.arxiv.org")
            or cand_lower.startswith("ar5iv.org")
            or cand_lower.startswith("ar5iv.labs.arxiv.org")
            or cand_lower.startswith("arxiv.")
        ):
            cand = "https://" + cand

    try:
        parsed = urlparse(cand)
    except Exception:
        return None

    netloc = parsed.netloc.lower()
    if not (
        netloc == "arxiv.org"
        or netloc.endswith(".arxiv.org")
        or netloc.startswith("arxiv.")
        or netloc in ("ar5iv.labs.arxiv.org", "ar5iv.org")
    ):
        return None

    path = parsed.path.strip("/")
    m = re.match(
        r"^(?:abs|html|pdf|format)/((?:[a-zA-Z\-]+/)?\d{4}\.\d{4,5}(?:v\d+)?|(?:[a-zA-Z\-]+/)?\d{7}(?:v\d+)?|[a-zA-Z0-9_\.\-]+)(?:\.pdf)?/?$",
        path,
        re.IGNORECASE,
    )
    if m:
        id_part = m.group(1).lower()
        if id_part not in ("index", "help", "about", "contact"):
            return id_part
    return None


def canonicalize_url(url: str) -> str:
    """Return a safe canonical form of *url* for matching equivalent references.

    Normalization guarantees:
    - Trims leading/trailing whitespace.
    - Resolves equivalent arXiv URLs (/abs/, /html/, /pdf/, mirror domains) to a
      standard canonical endpoint ('https://arxiv.org/abs/<id>'), while strictly
      preserving paper IDs and explicit version suffixes (e.g. 'v1' vs 'v2').
    - For general web URLs: lowercases scheme and hostname, strips default ports,
      and normalizes trailing slashes on path segments (without stripping query params).
    - Returns unchanged string if parsing fails or input is empty.
    """
    if not url or not isinstance(url, str):
        return ""
    cand = url.strip()
    if not cand:
        return ""

    arxiv_id = parse_arxiv_id(cand)
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"

    try:
        parsed = urlparse(cand)
        if not parsed.scheme and not parsed.netloc:
            return cand
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()
        if (scheme == "http" and netloc.endswith(":80")) or (scheme == "https" and netloc.endswith(":443")):
            netloc = netloc.rsplit(":", 1)[0]
        path = parsed.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        return urlunparse((scheme, netloc, path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        return cand


_TITLE_FILLER_WORDS = frozenset({
    "and", "the", "for", "with", "from", "via", "using", "based", "into",
    "onto", "over", "under", "between", "through", "toward", "towards",
    "of", "in", "on", "to", "at", "by", "or", "is", "are", "as", "a", "an",
})

_TITLE_AUTHOR_LINE_RE = re.compile(
    r"^(?:by\s+|authors?\b|affiliations?\b|abstract\b|doi\b|https?://|www\.|@)",
    re.IGNORECASE,
)

_TITLE_ARXIV_HEADER_RE = re.compile(r"^arxiv[:\s]", re.IGNORECASE)

_TITLE_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def _normalize_title_text(text: str) -> str:
    """Collapse whitespace and strip markdown heading/emphasis artifacts from a title candidate."""
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", text.strip())
    cleaned = cleaned.strip().strip("*_").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.rstrip("#").strip()


def _title_token_set(text: str) -> set:
    """Significant lowercase tokens (>=3 chars, non-filler) used for title comparison."""
    tokens = re.findall(r"[A-Za-z0-9]+", text.lower())
    return {t for t in tokens if len(t) >= 3 and t not in _TITLE_FILLER_WORDS}


def _first_significant_title_token(text: str) -> Optional[str]:
    """First significant lowercase token of a title, or None when only filler/stopwords are present."""
    for token in re.findall(r"[A-Za-z0-9]+", text.lower()):
        if len(token) >= 3 and token not in _TITLE_FILLER_WORDS:
            return token
    return None


def _is_title_like_line(line: str) -> bool:
    """Heuristic for a plausible document title line (not a sentence, author line, or metadata)."""
    candidate = line.strip()
    if not candidate or len(candidate) < 6 or len(candidate) > 300:
        return False
    if candidate.endswith((".", "!", "?", ":", ";", ",")):
        return False
    if ". " in candidate:
        return False
    if candidate[0].islower() or candidate[0].isdigit():
        return False
    if _TITLE_AUTHOR_LINE_RE.match(candidate) or _TITLE_ARXIV_HEADER_RE.match(candidate):
        return False
    return True


def derive_title_from_content(captured_text: str, max_scan_lines: int = 30) -> Optional[str]:
    """Derive a trusted title from captured content: first markdown heading, else leading title-like line."""
    if not captured_text:
        return None
    lines = captured_text.splitlines()
    for line in lines[:max_scan_lines]:
        match = _TITLE_HEADING_RE.match(line)
        if match:
            title = _normalize_title_text(match.group(1))
            if title:
                return title
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _is_title_like_line(stripped):
            return _normalize_title_text(stripped) or None
        return None
    return None


def _titles_conflict(provider_title: str, content_title: str) -> bool:
    """True when provider and content titles are led by different primary names."""
    provider_first = _first_significant_title_token(provider_title)
    content_first = _first_significant_title_token(content_title)
    if not provider_first or not content_first:
        return False
    provider_tokens = _title_token_set(provider_title)
    content_tokens = _title_token_set(content_title)
    if provider_first in content_tokens or content_first in provider_tokens:
        return False
    return True


def resolve_trusted_title(provider_title: Optional[str], captured_text: str) -> Optional[str]:
    """Prefer a content-derived title over a conflicting provider title; otherwise keep provider title."""
    if not provider_title:
        return provider_title
    content_title = derive_title_from_content(captured_text)
    if content_title and _titles_conflict(provider_title, content_title):
        return content_title
    return provider_title


# ---------------------------------------------------------------------------
# PassageRecord (Passage-level entity)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PassageRecord:
    """Immutable record of an individual text passage derived from a source.

    Attributes
    ----------
    passage_id:
        Deterministic passage-level identifier.
    evidence_id:
        Parent source-level evidence_id.
    text:
        Exact passage text.
    char_offset:
        Character offset where passage begins in parent captured_text, if known.
    """

    passage_id: str
    evidence_id: str
    text: str
    char_offset: Optional[int] = None


# ---------------------------------------------------------------------------
# EvidenceRecord (Source-level entity)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceRecord:
    """Immutable record of a single retrieved source.

    Attributes
    ----------
    evidence_id:
        32-hex-char deterministic ID derived from source_url via SHA-256.
    source_url:
        The exact URL requested by the tool/agent.
    retrieval_status:
        FULL_TEXT, SNIPPET_ONLY, or FAILED.
    captured_text:
        The exact string returned by the tool. Always empty ("") for FAILED.
    content_hash:
        SHA-256 hex digest of captured_text for consistency checking.
    final_url:
        Optional final response URL after any redirects. Defaults to source_url.
    title:
        Optional document title.
    snippet_text:
        Optional snippet text preserved if upgraded to FULL_TEXT from search results.
    provider:
        Optional search/tool provider identifier (e.g. "exa", "tavily", "pdf_extractor").
    snippet_observations:
        Tuple of raw snippet strings observed across multiple search providers.
    """

    evidence_id: str
    source_url: str
    retrieval_status: RetrievalStatus
    captured_text: str
    content_hash: str
    final_url: Optional[str] = None
    title: Optional[str] = None
    snippet_text: Optional[str] = None
    provider: Optional[str] = None
    snippet_observations: Tuple[str, ...] = ()

    def matches_url(self, candidate_url: str) -> bool:
        """Check if candidate_url matches requested source_url or final_url.

        Safe matching strategy:
        1. Exact string match (after whitespace trim) against source_url or final_url.
        2. Canonical URL match: normalizes trailing slashes and equivalent arXiv
           representations (/abs/, /html/, /pdf/, mirrors) without conflating
           distinct paper IDs or different explicit paper versions.
        """
        if not candidate_url or not isinstance(candidate_url, str):
            return False
        cand = candidate_url.strip()
        if cand == self.source_url.strip():
            return True
        if self.final_url and cand == self.final_url.strip():
            return True

        cand_canon = canonicalize_url(cand)
        if not cand_canon:
            return False

        if cand_canon == canonicalize_url(self.source_url):
            return True
        if self.final_url and cand_canon == canonicalize_url(self.final_url):
            return True

        return False

    def extract_passage(self, passage_text: str) -> PassageRecord:
        """Extract and return a typed PassageRecord from this source record.

        Raises ValueError if passage_text does not occur in captured_text.
        """
        if not passage_text or not passage_text.strip():
            raise ValueError("Passage text cannot be empty or whitespace-only")
        if passage_text not in self.captured_text:
            raise ValueError(
                f"Passage does not occur in captured_text for evidence_id={self.evidence_id}"
            )
        offset = self.captured_text.index(passage_text)
        p_id = make_passage_id(self.evidence_id, passage_text)
        return PassageRecord(
            passage_id=p_id,
            evidence_id=self.evidence_id,
            text=passage_text,
            char_offset=offset,
        )

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_fetch(
        cls,
        url: str,
        captured_text: str,
        retrieval_status: RetrievalStatus,
        final_url: Optional[str] = None,
        title: Optional[str] = None,
        snippet_text: Optional[str] = None,
        provider: Optional[str] = None,
        snippet_observations: Tuple[str, ...] = (),
    ) -> "EvidenceRecord":
        """Create a correctly hashed record from a fetch result.

        Enforces that FAILED status always sets captured_text to empty string.
        """
        clean_url = url.strip()
        clean_final = final_url.strip() if final_url else clean_url

        if retrieval_status == RetrievalStatus.FAILED:
            stored_text = ""
        else:
            stored_text = captured_text

        resolved_title = resolve_trusted_title(title, stored_text)

        # If observations were not explicitly provided but this is a snippet, initialize it
        obs = snippet_observations
        if not obs and retrieval_status == RetrievalStatus.SNIPPET_ONLY and stored_text:
            obs = (stored_text,)

        return cls(
            evidence_id=make_evidence_id(clean_url),
            source_url=clean_url,
            retrieval_status=retrieval_status,
            captured_text=stored_text,
            content_hash=make_content_hash(stored_text),
            final_url=clean_final,
            title=resolved_title,
            snippet_text=snippet_text,
            provider=provider,
            snippet_observations=obs,
        )

    @classmethod
    def from_tool_result(
        cls,
        result: "ToolFetchResult",
        provider: Optional[str] = None,
    ) -> "EvidenceRecord":
        """Create an EvidenceRecord from an explicit typed ToolFetchResult."""
        snippet = (
            result.raw_content
            if result.status == RetrievalStatus.SNIPPET_ONLY
            else None
        )
        obs = (result.raw_content,) if snippet else ()
        prov = provider or getattr(result, "provider", None)
        return cls.from_fetch(
            url=result.url,
            captured_text=result.raw_content,
            retrieval_status=result.status,
            final_url=result.final_url,
            title=result.title,
            snippet_text=snippet,
            provider=prov,
            snippet_observations=obs,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceRecord":
        """Reconstruct a record from a deserialized dict.

        Validates all required fields and enum values.
        """
        required = ["evidence_id", "source_url", "retrieval_status", "captured_text", "content_hash"]
        for field in required:
            if field not in data:
                raise ValueError(f"Record data missing required field: {field!r}")

        try:
            status = RetrievalStatus(data["retrieval_status"])
        except ValueError as exc:
            raise ValueError(f"Invalid retrieval_status: {data['retrieval_status']!r}") from exc

        obs = tuple(data.get("snippet_observations") or ())

        return cls(
            evidence_id=data["evidence_id"],
            source_url=data["source_url"],
            retrieval_status=status,
            captured_text=data["captured_text"],
            content_hash=data["content_hash"],
            final_url=data.get("final_url"),
            title=data.get("title"),
            snippet_text=data.get("snippet_text"),
            provider=data.get("provider"),
            snippet_observations=obs,
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize to a plain dict suitable for JSON encoding."""
        return {
            "evidence_id": self.evidence_id,
            "source_url": self.source_url,
            "retrieval_status": self.retrieval_status.value,
            "captured_text": self.captured_text,
            "content_hash": self.content_hash,
            "final_url": self.final_url,
            "title": self.title,
            "snippet_text": self.snippet_text,
            "provider": self.provider,
            "snippet_observations": list(self.snippet_observations),
        }
