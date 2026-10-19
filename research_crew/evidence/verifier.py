"""
evidence/verifier.py
--------------------
Claim-level source verification engine for the research pipeline.

Capabilities:
1. Break draft reports into atomic factual claims with section and citation provenance.
2. Associate each claim with its cited source and locate the exact supporting passage
   from the run's EvidenceStore.
3. Verify each claim against the passage using an LLM (routed via Mesh API) or
   a deterministic pluggable/mocked verifier function, classifying verdicts as:
   - supported
   - contradicted
   - insufficient_evidence
4. Generate structured verification records containing claim ID, claim text, source URL,
   passage ID or text, verdict, rationale, and action taken.
5. Update the report by clearly labeling unsupported claims as unverified/contradicted
   or removing them, ensuring no claim is retained as verified merely because its
   citation URL exists in the store.
"""

from dataclasses import dataclass, field
from enum import Enum
import json
import logging
from pathlib import Path
import re
import string
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from research_crew.evidence.model import (
    EvidenceRecord,
    RetrievalStatus,
    make_passage_id,
    parse_arxiv_id,
)
from research_crew.evidence.store import EvidenceStore
from research_crew.evidence.validator import (
    _STOPWORDS,
    extract_report_source_mapping,
)

logger = logging.getLogger(__name__)


class ClaimVerdict(str, Enum):
    """Classification verdict for a factual claim against retrieved evidence."""

    SUPPORTED = "supported"
    """The supporting passage directly substantiates the claim."""

    CONTRADICTED = "contradicted"
    """The supporting passage refutes or conflicts with the claim."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    """The evidence is absent, snippet-only, off-topic, or insufficient to prove the claim."""


class ClaimAction(str, Enum):
    """Action taken on an individual claim in the final report."""

    RETAINED = "retained"
    """Claim was supported by evidence and retained in the report."""

    REMOVED = "removed"
    """Claim was unsupported/contradicted and excised from the report."""

    LABELED_UNVERIFIED = "labeled_unverified"
    """Claim lacked supporting evidence and was explicitly labeled as unverified."""

    LABELED_CONTRADICTED = "labeled_contradicted"
    """Claim was contradicted by source evidence and explicitly labeled as contradicted."""


@dataclass
class AtomicClaim:
    """An individual factual claim extracted from report prose."""

    claim_id: str
    claim_text: str
    citation_marker: Optional[str] = None
    source_url: Optional[str] = None
    section: Optional[str] = None
    raw_sentence: str = ""
    clause_text: Optional[str] = None
    cited_urls: List[str] = field(default_factory=list)
    constituent_clauses: List[str] = field(default_factory=list)
    unresolved_citations: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ClaimVerificationRecord:
    """Structured audit record for an individual factual claim."""

    claim_id: str
    claim_text: str
    source_url: Optional[str]
    passage_id: Optional[str]
    passage_text: Optional[str]
    verdict: ClaimVerdict
    rationale: str
    action_taken: str
    retrieval_status: Optional[str] = None
    raw_sentence: str = ""
    supporting_urls: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "claim_id": self.claim_id,
            "claim_text": self.claim_text,
            "source_url": self.source_url,
            "passage_id": self.passage_id,
            "passage_text": self.passage_text,
            "verdict": self.verdict.value if isinstance(self.verdict, ClaimVerdict) else str(self.verdict),
            "rationale": self.rationale,
            "action_taken": self.action_taken,
            "retrieval_status": self.retrieval_status,
        }
        if self.supporting_urls:
            d["supporting_urls"] = list(self.supporting_urls)
        return d


@dataclass
class ClaimVerificationSummary:
    """Summary of claim verification outcomes across the report."""

    total_claims: int
    supported_claims_count: int
    contradicted_claims_count: int
    insufficient_evidence_claims_count: int
    records: List[ClaimVerificationRecord] = field(default_factory=list)
    unverified_claims: List[ClaimVerificationRecord] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_claims": self.total_claims,
            "supported_claims_count": self.supported_claims_count,
            "contradicted_claims_count": self.contradicted_claims_count,
            "insufficient_evidence_claims_count": self.insufficient_evidence_claims_count,
            "records": [r.to_dict() for r in self.records],
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Claim Extraction Helpers
# ---------------------------------------------------------------------------

def _is_heading_or_title_fragment(raw_line: str, s_clean: str, clean_stmt: str) -> bool:
    """Detect whether a line or sentence fragment is a heading, title, or attribution rather than a factual claim."""
    s_raw = raw_line.strip()
    s_clean_t = s_clean.strip()
    stmt = clean_stmt.strip()

    # 1. Heading markers (# Title)
    if s_raw.startswith("#"):
        return True

    # 2. Source / citation attribution lines: *Source: [1]*, Source: ..., URL: ...
    if re.match(r"^\*?\s*(?:source|url|citation|reference)s?:\s*.*", s_raw, re.IGNORECASE):
        return True
    if re.match(r"^\*?\s*(?:source|url|citation|reference)s?:\s*.*", s_clean_t, re.IGNORECASE):
        return True

    # 3. Standalone bold or italic heading / title line (e.g. **Title**, 1. **Title**, * **Title**)
    if re.match(r"^(?:(?:[\*\-\•]|\d+[\.\)])\s*)?(?:\*{1,2}|_{1,2})[^\*\_\n]+(?:\*{1,2}|_{1,2}):?\s*$", s_raw):
        return True

    # 4. Heading or section label ending in colon (e.g. "Model Architecture:", "Key Insights:")
    if s_raw.endswith(":") or s_clean_t.endswith(":") or stmt.endswith(":"):
        return True

    # 5. Standalone fragment without terminal punctuation and without inline citation
    has_terminal_punct = bool(re.search(r"[\.\!\?]\s*$", s_clean_t))
    has_bracket_citation = bool(re.search(r"\[[\d\s,;\-]+\]", s_clean_t))
    if not has_terminal_punct and not has_bracket_citation:
        return True

    # 6. Fragment is wrapped entirely in bold or italics without sentence punctuation
    if (s_clean_t.startswith("**") and s_clean_t.endswith("**")) or (
        s_clean_t.startswith("*") and s_clean_t.endswith("*")
    ):
        if not has_terminal_punct:
            return True

    return False


def _get_base_paper_id(url: str) -> str:
    """Extract canonical paper identifier or normalized base URL for deduplicating citations."""
    aid = parse_arxiv_id(url)
    if aid:
        return re.sub(r"v\d+$", "", aid)
    return url.rstrip("/").lower()


def _deduplicate_resolved_urls(resolved_urls: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Deduplicate citations that refer to the same paper or URL within a sentence."""
    seen_canonical: Set[str] = set()
    deduped: List[Tuple[str, str]] = []
    for marker, url in resolved_urls:
        if not url:
            deduped.append((marker, url))
            continue
        base_id = _get_base_paper_id(url)
        if base_id in seen_canonical:
            continue
        seen_canonical.add(base_id)
        deduped.append((marker, url))
    return deduped


def _clean_claim_statement(text: str, source_mapping: Optional[Dict[str, str]] = None) -> str:
    """Strip citation markers, markdown formatting, and dangling punctuation from claim text."""
    clean = re.sub(r"\[[\d\s,;\-]+\]", "", text)
    if source_mapping:
        for k in source_mapping.keys():
            esc = re.escape(k.strip("[]"))
            clean = re.sub(rf"\[\^?{esc}\]", "", clean)
    clean = re.sub(r"\[[a-z0-9]+(?:[\-_][a-z0-9]+)+\]", "", clean)
    clean = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", clean)
    clean = re.sub(r"https?://[^\s\)\]\>\"\'\*`]+", "", clean)
    clean = re.sub(r"[*`]", "", clean)
    clean = re.sub(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", "", clean).strip()
    clean = re.sub(r"\s+([.,;:!?])", r"\1", clean)
    clean = " ".join(clean.split()).rstrip(" .!?:;,")
    return clean


def _resolve_citations(s_clean: str, source_mapping: Dict[str, str]) -> List[Tuple[str, str]]:
    """Resolve bracket, markdown, and raw URL citations against the source mapping."""
    bracket_groups = re.findall(r"\[([a-zA-Z0-9_\-\s,;]+)\]", s_clean)
    bracket_nums: List[str] = []
    named_tags: List[str] = []
    for group in bracket_groups:
        range_match = re.fullmatch(r"\s*(\d+)\s*[-–]\s*(\d+)\s*", group)
        if range_match:
            start_n = int(range_match.group(1))
            end_n = int(range_match.group(2))
            if start_n <= end_n and (end_n - start_n) <= 10:
                bracket_nums.extend(str(i) for i in range(start_n, end_n + 1))
            else:
                bracket_nums.append(str(start_n))
                bracket_nums.append(str(end_n))
        elif re.fullmatch(r"[\d\s,;\-]+", group):
            nums = re.findall(r"\d+", group)
            bracket_nums.extend(nums)
        else:
            parts = [p.strip() for p in re.split(r"[,;]", group) if p.strip()]
            for p in parts:
                clean_p = p.strip()
                if clean_p in source_mapping or f"[{clean_p}]" in source_mapping:
                    named_tags.append(clean_p)
                elif re.match(r"^[a-zA-Z0-9_\-]+$", clean_p):
                    if "-" in clean_p or "_" in clean_p or clean_p[0].islower():
                        named_tags.append(clean_p)

    md_link_citations = re.findall(r"\[([^\]]+)\]\((https?://[^\)]+)\)", s_clean)
    raw_url_citations = re.findall(r"https?://[^\s\)\]\>\"\'\*`]+", s_clean)

    resolved_urls: List[Tuple[str, str]] = []

    for num in bracket_nums:
        marker = f"[{num}]"
        url = source_mapping.get(marker) or source_mapping.get(num)
        if url:
            resolved_urls.append((marker, url))
        else:
            # Missing bibliography entry
            resolved_urls.append((marker, ""))

    for tag in named_tags:
        marker = f"[{tag}]"
        url = source_mapping.get(marker) or source_mapping.get(tag)
        if url:
            resolved_urls.append((marker, url))
        else:
            # Missing bibliography entry
            resolved_urls.append((marker, ""))

    for link_text, link_url in md_link_citations:
        clean_u = link_url.rstrip(".,;:)*_~`\"'")
        resolved_urls.append((f"[{link_text}]", clean_u))

    for raw_u in raw_url_citations:
        clean_u = raw_u.rstrip(".,;:)*_~`\"'")
        if not any(clean_u == u for _, u in resolved_urls):
            resolved_urls.append((clean_u, clean_u))

    return resolved_urls


def _split_sentence_clauses(sentence: str, source_mapping: Optional[Dict[str, str]] = None) -> List[Tuple[str, List[Any]]]:
    """Decompose compound sentence into independently verifiable clauses with their proximate citations.

    If citations are separated across distinct clauses conjoined by contrasting or connecting
    conjunctions (e.g. ';', ', whereas', ', while', ', but', '—'), returns the individual clauses.
    If citations are clustered together (e.g. '[1, 2]'), returns the sentence as a single unit.
    """
    raw_citations = list(re.finditer(r"\[([a-zA-Z0-9_\-\s,;]+)\]|https?://[^\s\)\]\>\"\'\*`]+", sentence))
    citations = []
    for m in raw_citations:
        txt = m.group(0)
        if txt.startswith("http"):
            citations.append(m)
        else:
            tag = m.group(1).strip()
            if re.fullmatch(r"[\d\s,;\-]+", tag):
                citations.append(m)
            elif source_mapping and (tag in source_mapping or f"[{tag}]" in source_mapping):
                citations.append(m)
            elif "-" in tag or "_" in tag or tag.islower():
                citations.append(m)

    if len(citations) <= 1:
        return [(sentence, citations)]

    between_spans = []
    for c1, c2 in zip(citations[:-1], citations[1:]):
        between_spans.append(sentence[c1.end():c2.start()])

    all_clustered = all(re.fullmatch(r"[\s,;\-]*", b) for b in between_spans)
    if all_clustered:
        return [(sentence, citations)]

    sentence_depths = _paren_depth_profile(sentence)

    clauses: List[Tuple[str, List[Any]]] = []
    prev_idx = 0
    for i, c in enumerate(citations):
        if i < len(citations) - 1:
            next_start = citations[i + 1].start()
            between = sentence[c.end():next_start]
            split_m = re.search(
                r"(;|,\s*(?:whereas|while|whilst|but|however|in contrast|although|as well as|and)\b|—|--)",
                between,
                re.IGNORECASE,
            )
            if split_m and sentence_depths[c.end() + split_m.start()] == 0:
                split_pos = c.end() + split_m.start()
                clause_text = sentence[prev_idx:split_pos]
                clauses.append((clause_text, [c]))
                delim_end = c.end() + split_m.end()
                prev_idx = delim_end
            else:
                clause_text = sentence[prev_idx:c.end()]
                clauses.append((clause_text, [c]))
                prev_idx = c.end()
        else:
            clause_text = sentence[prev_idx:]
            clauses.append((clause_text, [c]))

    return clauses


_FINITE_VERBS_PATTERN = (
    r"\b("
    r"is|are|was|were|have|has|had|do|does|did|can|could|will|would|should|may|might|must|"
    r"improve|improves|improved|provide|provides|provided|"
    r"achieve|achieves|achieved|demonstrate|demonstrates|demonstrated|"
    r"employ|employs|employed|rely|relies|relied|use|uses|used|"
    r"utilize|utilizes|utilized|reduce|reduces|reduced|scale|scales|scaled|"
    r"support|supports|supported|exhibit|exhibits|exhibited|require|requires|required|"
    r"enable|enables|enabled|allow|allows|allowed|serve|serves|served|"
    r"eliminate|eliminates|eliminated|guarantee|guarantees|guaranteed|"
    r"feature|features|featured|mitigate|mitigates|mitigated|"
    r"optimize|optimizes|optimized|combine|combines|combined|"
    r"stabilize|stabilizes|stabilized|outperform|outperforms|outperformed|"
    r"propose|proposes|proposed|introduce|introduces|introduced|"
    r"show|shows|showed|shown|find|finds|found|observe|observes|observed"
    r")\b"
)


def _find_parentheticals(text: str) -> List[Tuple[int, int, str]]:
    """Locate top-level balanced parenthetical spans in text."""
    results: List[Tuple[int, int, str]] = []
    stack: List[int] = []
    for idx, ch in enumerate(text):
        if ch == "(":
            stack.append(idx)
        elif ch == ")" and stack:
            start = stack.pop()
            if not stack:
                results.append((start, idx + 1, text[start + 1 : idx]))
    return results


def _paren_depth_profile(text: str) -> List[int]:
    """Per-character parenthesis depth preceding each character (unbalanced closers clamp at zero)."""
    depths: List[int] = []
    depth = 0
    for ch in text:
        depths.append(depth)
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
    return depths


def _split_at_top_level_delimiters(text: str, pattern: "re.Pattern[str]") -> List[str]:
    """re.split semantics, but only at delimiter matches located outside balanced parentheses."""
    depths = _paren_depth_profile(text)
    pieces: List[str] = []
    last = 0
    for m in pattern.finditer(text):
        s, e = m.span()
        if depths[s] == 0:
            pieces.append(text[last:s])
            last = e
    if not pieces:
        return [text]
    pieces.append(text[last:])
    return pieces


def _split_conjunction_items(text: str) -> List[str]:
    """Split comma/and-separated items respecting nested parentheses."""
    items: List[str] = []
    current: List[str] = []
    paren_depth = 0
    words = text.split()
    i = 0
    while i < len(words):
        w = words[i]
        paren_depth += w.count("(") - w.count(")")
        if paren_depth == 0 and (
            w.lower() in ("and", "as")
            and (w.lower() != "as" or (i + 2 < len(words) and words[i + 1].lower() == "well" and words[i + 2].lower() == "as"))
        ):
            if current:
                items.append(" ".join(current).rstrip(","))
            current = []
            if w.lower() == "as":
                i += 3
                continue
            i += 1
            continue
        elif paren_depth == 0 and w.endswith(",") and not w.endswith(".,"):
            current.append(w.rstrip(","))
            items.append(" ".join(current))
            current = []
            i += 1
            continue
        current.append(w)
        i += 1
    if current:
        items.append(" ".join(current))
    return [it.strip() for it in items if it.strip()]


_NON_SPLIT_PARENTICAL_PATTERNS = re.compile(
    r"\b(?:"
    r"plug and play|divide and conquer|branch and bound|read and write|train and test|"
    r"encoder and decoder|client and server|hardware and software|key and value|keys and values|"
    r"bits and bytes|end to end|compare and swap|test and set|fetch and add|load and store|"
    r"stop and wait|publish and subscribe|producer and consumer|request and response|"
    r"weights and biases|between\s+[\w\.\%]+\s*(?:[a-zA-Z\%]+)?\s+and\s+[\w\.\%]+|"
    r"from\s+\w+\s+to\s+\w+|either\s+.*?\s+or|"
    r"and\s+(?:later|above|below|older|newer|others|so on|vice versa|more)"
    r")\b",
    re.IGNORECASE,
)


def _decompose_parenthetical_propositions(claim_text: str) -> List[str]:
    """Decompose substantive conjunctions inside parentheses into independent propositions.

    For example:
    'Direct performance ... between PostgreSQL (including extensions like TimescaleDB and JSONB columns) and MongoDB ...'
    Decomposes into:
    1. 'Direct performance ... between PostgreSQL (including extensions like TimescaleDB) and MongoDB ...'
    2. 'Direct performance ... between PostgreSQL (including extensions like JSONB columns) and MongoDB ...'
    Preserves qualifiers, relationships, and technical terms.
    """
    parentheticals = _find_parentheticals(claim_text)
    if not parentheticals:
        return [claim_text]

    for start, end, inner in parentheticals:
        if not re.search(r"\b(?:and|as well as)\b", inner, re.IGNORECASE):
            continue

        if _NON_SPLIT_PARENTICAL_PATTERNS.search(inner):
            continue

        lead_in = ""
        m_lead = re.match(
            r"^(?:(?:including|such as|e\.g\.,?|for example,?|featuring|with|namely|excluding|without|not including|except for|aside from|supporting|requiring|using)\s+"
            r"(?:(?:extensions|tools|methods|models|frameworks|features|columns|techniques|"
            r"algorithms|systems|variants|architectures|optimizations|primitives|operations|modules)\s+(?:like|such as)\s+)?)",
            inner,
            re.IGNORECASE,
        )
        if m_lead:
            lead_in = m_lead.group(0)
            items_str = inner[len(lead_in):].strip()
        else:
            items_str = inner.strip()

        items = _split_conjunction_items(items_str)
        if len(items) >= 2 and all(len(it) >= 2 for it in items):
            if not lead_in and not all(it[0].isupper() or len(it.split()) > 1 or bool(re.search(r"\d", it)) for it in items):
                continue

            propositions: List[str] = []
            prefix = claim_text[:start]
            suffix = claim_text[end:]
            for item in items:
                new_paren = f"({lead_in}{item})" if lead_in or (item.startswith("(") and item.endswith(")")) else f"({item})"
                prop = f"{prefix}{new_paren}{suffix}".strip()
                if not prop.endswith((".", "!", "?")):
                    prop += "."
                propositions.append(prop)
            return propositions

    return [claim_text]


_SPECIFIC_EVAL_DIMENSIONS = {
    "faithfulness", "relevance", "hallucination", "groundedness", "recall",
    "precision", "accuracy", "coherence", "fluency", "correctness",
}

_GENERIC_DIMENSION_KEYWORDS = {
    "latency", "throughput", "quality", "similarity", "consistency",
    "robustness", "scalability", "complexity", "durability", "isolation", "performance",
}


def _check_unresolved_referents(claim_text: str) -> Tuple[bool, str]:
    """Detect ungrounded anaphoric referents (e.g. 'these dimensions') in a claim."""
    m = re.search(
        r"\b(?:these|those|such)\s+(?:dimensions|metrics|properties|criteria|aspects|attributes|factors|features|characteristics|parameters)\b",
        claim_text,
        re.IGNORECASE,
    )
    if not m:
        return True, "No anaphoric referent"

    phrase = m.group(0)
    # Check if context is provided in brackets
    if re.search(r"\[(?:context|heading):[^\]]+\]", claim_text, re.IGNORECASE):
        return True, f"Referent {phrase!r} grounded in attribution context"

    # Check if there is an explicit enumeration (such as ..., including ...)
    if re.search(r"\b(?:such as|including|namely|e\.g\.)\b", claim_text, re.IGNORECASE):
        return True, f"Referent {phrase!r} grounded in enumeration"

    # Block false grounding on incidental collocations (e.g. 'high performance computing', 'quality assurance/team')
    incidental_collocations = re.search(
        r"\b(?:high\s+performance\s+computing|quality\s+(?:assurance|team|control|management))\b",
        claim_text,
        re.IGNORECASE,
    )

    # Check if substantive dimensions or definitions appear in the claim text
    words = set(re.findall(r"[a-zA-Z]+", claim_text.lower()))
    specific_found = words.intersection(_SPECIFIC_EVAL_DIMENSIONS)
    generic_found = words.intersection(_GENERIC_DIMENSION_KEYWORDS)

    # If specific evaluation dimensions appear (e.g. faithfulness, relevance)
    if specific_found:
        return True, f"Referent {phrase!r} grounded in evaluation dimensions: {specific_found}"

    # For generic attributes, require explicit conjoined definition (e.g. 'latency and throughput') and exclude incidental collocations
    if not incidental_collocations and (len(generic_found) >= 2 or re.search(
        r"\b(?:" + "|".join(re.escape(k) for k in _GENERIC_DIMENSION_KEYWORDS) + r")\s+(?:and|as well as|,)\b",
        claim_text,
        re.IGNORECASE,
    )):
        return True, f"Referent {phrase!r} grounded in claim terms: {generic_found}"

    return (
        False,
        f"Claim contains an unresolved anaphoric referent ({phrase!r}) whose antecedent is not defined in the claim text or available context.",
    )


def _decompose_claim_clauses(claim_text: str) -> List[str]:
    """Decompose a compound claim statement into substantive clauses if conjoined.

    Splits on:
    1. Contrasting or coordinating clause conjunctions like ';', ', whereas',
       ', while', ', but', ', in contrast', '—', '--', or ', as well as'.
    2. Conjoined subjects at start of sentence ('Both X and Y <verb>...', 'X and Y <verb>...')
       reconstructed into full independent propositions.
    3. Independent clauses conjoined by ', and ' where each clause contains a finite verb.
    4. Substantive conjunctions inside parentheses (e.g. '(including extensions like TimescaleDB and JSONB columns)').
    Returns [claim_text] if the statement is a single unified clause.
    """
    text = claim_text.strip().rstrip(".")

    depths = _paren_depth_profile(text)

    def _conjoin_is_top_level(m: "re.Match[str]") -> bool:
        inner = re.search(r"\s+and\s+", m.group(0))
        return bool(inner) and depths[m.start(0) + inner.start()] == 0

    # 1. Contrasting or coordinating clause connectors with punctuation or subordinate clause boundary
    contrast_pattern = re.compile(
        r"(?:;\s*|,\s*(?:whereas|while|whilst|but|however|in contrast|although|as well as|providing|achieving|yielding|resulting in|enabling)\s*|\s+(?:whereas|whilst)\s+|(?<!\bfor\s)(?<!\bin\s)\s+while\s+|—\s*|--\s*)",
        re.IGNORECASE,
    )
    raw_parts = _split_at_top_level_delimiters(text, contrast_pattern)
    parts = [p.strip() for p in raw_parts if p.strip() and len(p.strip()) >= 10]
    if len(parts) > 1:
        return [p + ("." if not p.endswith((".", "!", "?")) else "") for p in parts]

    # 2. Conjoined subjects: Both X and Y <verb>...
    m_both = re.match(
        r"^(?:both\s+)(.+?)\s+and\s+(.+?)\s+(" + _FINITE_VERBS_PATTERN + r"\s+.+)$",
        text,
        re.IGNORECASE,
    )
    if m_both and _conjoin_is_top_level(m_both):
        s1, s2, pred = m_both.group(1).strip(), m_both.group(2).strip(), m_both.group(3).strip()
        if len(s1) >= 2 and len(s2) >= 2:
            return [f"{s1} {pred}.", f"{s2} {pred}."]

    # Conjoined subjects: X and Y <verb>...
    if not _NON_SPLIT_PARENTICAL_PATTERNS.search(text):
        m_subjs = re.match(
            r"^([A-Z][^,]+?)\s+and\s+([A-Z][^,]+?)\s+(" + _FINITE_VERBS_PATTERN + r"\s+.+)$",
            text,
        )
        if m_subjs and _conjoin_is_top_level(m_subjs):
            s1, s2, pred = m_subjs.group(1).strip(), m_subjs.group(2).strip(), m_subjs.group(3).strip()
            if not re.match(r"^(?:between|by|in|with|for|under|from|after|before)\b", s1, re.IGNORECASE):
                first_has_verb = bool(re.search(r"\s+" + _FINITE_VERBS_PATTERN + r"\s+", s1, re.IGNORECASE))
                if (
                    not first_has_verb
                    and len(s1) >= 3
                    and len(s2) >= 3
                    and ":" not in s1
                    and ":" not in s2
                ):
                    return [f"{s1} {pred}.", f"{s2} {pred}."]

    # 3. Independent clauses joined by ', and ' where both parts have finite verbs
    and_parts = _split_at_top_level_delimiters(text, re.compile(r",\s+and\s+", re.IGNORECASE))
    if len(and_parts) >= 2:
        if all(re.search(_FINITE_VERBS_PATTERN, p, re.IGNORECASE) for p in and_parts):
            return [p.strip() + ("." if not p.strip().endswith((".", "!", "?")) else "") for p in and_parts]

    # 4. Parenthetical conjunctions (e.g. '... between PostgreSQL (including extensions like TimescaleDB and JSONB columns) and MongoDB ...')
    parenthetical_propositions = _decompose_parenthetical_propositions(text)
    if len(parenthetical_propositions) > 1:
        return parenthetical_propositions

    return [claim_text]


def extract_atomic_claims(
    report: str,
    source_mapping: Optional[Dict[str, str]] = None,
) -> List[AtomicClaim]:
    """Break report narrative prose into atomic factual claims with citation linkage.

    Extracts factual claims from narrative sections (e.g. Core Foundations,
    Key Insights, Methodology) while skipping tables, bibliography, and footer.
    Excludes Markdown headings, numbered titles, and title-only fragments from claim extraction.
    Supports intra-sentence clause decomposition and citation deduplication.
    """
    if source_mapping is None:
        source_mapping = extract_report_source_mapping(report)

    claims: List[AtomicClaim] = []
    lines = report.splitlines()

    current_section = "General"
    in_code_block = False
    in_skip_section = False
    claim_counter = 1
    seen_heading = False

    def _is_footer_divider(current_idx: int) -> bool:
        for j in range(current_idx + 1, len(lines)):
            nxt = lines[j].strip()
            if not nxt or nxt == "---":
                continue
            if nxt.startswith(("*Report generated", "*Evidence store", "*Citation-level")):
                return True
            break
        return False

    for idx, line in enumerate(lines):
        stripped = line.strip()

        # Handle code fences
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        # Handle metadata footer lines directly
        if stripped.startswith(("*Report generated", "*Evidence store", "*Citation-level")):
            break

        # Before any heading has been reached, skip leading dividers and frontmatter
        if not seen_heading:
            if stripped.startswith("#"):
                seen_heading = True
            else:
                continue

        # Handle genuine footer divider after headings have been encountered
        if stripped == "---":
            if in_skip_section or _is_footer_divider(idx):
                break
            continue

        # Handle headings
        if stripped.startswith("#"):
            heading_text = stripped.lstrip("#").strip()
            current_section = heading_text
            # Skip non-prose sections: tables, bibliography, references
            lower_h = heading_text.lower()
            if any(skip_kw in lower_h for skip_kw in ("sources", "references", "bibliography", "benchmarks & metrics")):
                in_skip_section = True
            else:
                in_skip_section = False
            continue

        if in_skip_section:
            continue

        # Skip markdown table rows
        if stripped.startswith("|") or stripped.endswith("|"):
            continue

        if not stripped:
            continue

        # Skip standalone heading / title lines and attribution lines
        if _is_heading_or_title_fragment(stripped, stripped, stripped):
            continue

        # Strip leading bullet points or numbered list prefixes
        clean_line = re.sub(r"^[\*\-\•]\s+|\b\d+\.\s+", "", stripped).strip()

        # Strip leading title prefixes (e.g. "**Title**: Statement" or "1. **Title** - Statement")
        clean_line = re.sub(
            r"^(?:(?:[\*\-\•]|\d+[\.\)])\s*)?(?:\*{1,2}|_{1,2})[^\*\_\n]+(?:\*{1,2}|_{1,2})\s*[:\-–—]\s*",
            "",
            clean_line,
        ).strip()

        clean_line = re.sub(
            r"^(?:(?:[\*\-\•]|\d+[\.\)])\s*)?(?:\*{1,2}|_{1,2})[^\*\_\n]+?[:\-–—]\s*(?:\*{1,2}|_{1,2})\s*",
            "",
            clean_line,
        ).strip()

        if not clean_line or len(clean_line) < 15:
            continue

        # Split line into sentences
        sentences = re.split(r"(?<=[.!?])\s+", clean_line)

        for sentence in sentences:
            s_clean = sentence.strip()
            if len(s_clean) < 15:
                continue

            # Check if this sentence contains intra-sentence citations split across clauses
            clause_splits = _split_sentence_clauses(s_clean, source_mapping)

            if len(clause_splits) > 1:
                # Compound claim with intra-sentence clauses
                constituent_texts: List[str] = []
                for c_text, _ in clause_splits:
                    clean_c = _clean_claim_statement(c_text, source_mapping)
                    if clean_c:
                        constituent_texts.append(clean_c)

                for c_text, _ in clause_splits:
                    clean_c = _clean_claim_statement(c_text, source_mapping)
                    if not clean_c or len(clean_c) < 10:
                        continue
                    if _is_heading_or_title_fragment(stripped, c_text, clean_c):
                        continue

                    c_resolved = _resolve_citations(c_text, source_mapping)
                    c_resolved = _deduplicate_resolved_urls(c_resolved)

                    if not c_resolved:
                        claims.append(
                            AtomicClaim(
                                claim_id=f"claim_{claim_counter:03d}",
                                claim_text=clean_c,
                                citation_marker=None,
                                source_url=None,
                                section=current_section,
                                raw_sentence=s_clean,
                                clause_text=clean_c,
                                cited_urls=[],
                                constituent_clauses=constituent_texts,
                            )
                        )
                        claim_counter += 1
                    else:
                        all_urls = [u for _, u in c_resolved if u]
                        unresolved = [m for m, u in c_resolved if not u]
                        marker_str = ", ".join(m for m, _ in c_resolved)
                        primary_url = all_urls[0] if all_urls else None
                        claims.append(
                            AtomicClaim(
                                claim_id=f"claim_{claim_counter:03d}",
                                claim_text=clean_c,
                                citation_marker=marker_str,
                                source_url=primary_url,
                                section=current_section,
                                raw_sentence=s_clean,
                                clause_text=clean_c,
                                cited_urls=all_urls,
                                constituent_clauses=constituent_texts,
                                unresolved_citations=unresolved,
                            )
                        )
                        claim_counter += 1
                continue

            # Standard sentence (clustered or single citation)
            resolved_urls = _resolve_citations(s_clean, source_mapping)
            resolved_urls = _deduplicate_resolved_urls(resolved_urls)

            clean_stmt = _clean_claim_statement(s_clean, source_mapping)
            if not clean_stmt or len(clean_stmt) < 10:
                continue

            if _is_heading_or_title_fragment(stripped, s_clean, clean_stmt):
                continue

            if not resolved_urls:
                claims.append(
                    AtomicClaim(
                        claim_id=f"claim_{claim_counter:03d}",
                        claim_text=clean_stmt,
                        citation_marker=None,
                        source_url=None,
                        section=current_section,
                        raw_sentence=s_clean,
                        clause_text=None,
                        cited_urls=[],
                        constituent_clauses=[clean_stmt],
                    )
                )
                claim_counter += 1
            else:
                all_urls = [u for _, u in resolved_urls if u]
                unresolved = [m for m, u in resolved_urls if not u]
                marker_str = ", ".join(m for m, _ in resolved_urls)
                primary_url = all_urls[0] if all_urls else None
                claims.append(
                    AtomicClaim(
                        claim_id=f"claim_{claim_counter:03d}",
                        claim_text=clean_stmt,
                        citation_marker=marker_str,
                        source_url=primary_url,
                        section=current_section,
                        raw_sentence=s_clean,
                        clause_text=None,
                        cited_urls=all_urls,
                        constituent_clauses=[clean_stmt],
                        unresolved_citations=unresolved,
                    )
                )
                claim_counter += 1

    return claims


# ---------------------------------------------------------------------------
# Supporting Passage Search
# ---------------------------------------------------------------------------
# Supporting Passage Search & Context Extraction
# ---------------------------------------------------------------------------

_COMMON_NON_ACRONYMS = {
    "THE", "THIS", "THAT", "WITH", "FROM", "WHAT", "WHEN", "WHERE", "WHICH",
    "AND", "FOR", "NOT", "BUT", "ALL", "SOME", "MANY", "MOST", "SUCH", "BOTH",
    "EACH", "EVERY", "MORE", "LESS", "OVER", "UNDER", "ALSO", "THEN", "THAN",
    "NOTE", "IEEE", "ACM", "URL", "PDF", "HTML", "HTTP", "HTTPS", "PAGE", "FIG",
}

_GENERIC_IDENTIFIERS = {
    "indicates", "extrapolation", "often", "designs", "design", "evaluation",
    "evaluations", "training", "testing", "performance", "analysis", "selection",
    "optimization", "optimizations", "choices", "parameters", "weights",
    "outputs", "inputs", "data", "scale", "scales", "policy", "alignment",
    "learning", "components", "results", "findings", "features", "characteristics",
    "behaviors", "workloads", "requirements", "variations", "comparisons",
    "studies", "benchmarks", "methods", "models", "algorithms", "systems",
    "frameworks", "techniques", "architectures", "approaches", "implementations",
    "properties", "metrics", "criteria", "dimensions", "attributes", "factors",
    "tests", "test", "mean", "baseline", "baselines", "overview", "summary",
    "utilizes", "utilize", "utilized", "employs", "employ", "employed",
    "combines", "combine", "combined", "uses", "use", "used",
    "coordinates", "coordinate", "coordinated", "incorporates", "incorporate",
    "demonstrates", "demonstrate", "proposes", "propose", "provides", "provide",
    "leverages", "leverage", "introduces", "introduce", "evaluates", "evaluate",
    "implements", "implement", "operates", "operate", "achieves", "achieve",
    "develops", "develop", "applies", "apply", "includes", "include",
    "requires", "require", "generates", "generate", "enhances", "enhance",
    "improves", "improve", "yields", "yield", "supports", "support",
    "allows", "allow", "enables", "enable", "shows", "show", "finds", "find",
}


def _extract_acronyms(text: str) -> List[str]:
    """Extract uppercase or hyphenated technical acronyms (e.g. RS-DPO, HPO, RLHF, DPO, LoRA)."""
    candidates = re.findall(
        r"\b[A-Z][a-zA-Z0-9]*(?:-[A-Za-z0-9]+)+\b|\b[A-Z]{2,}s?\b|\b[A-Z][a-z]+[A-Z][a-zA-Z]*\b",
        text,
    )
    acronyms: List[str] = []
    for c in candidates:
        clean = c.strip()
        if clean.upper() in _COMMON_NON_ACRONYMS:
            continue
        if len(clean) >= 2 and clean not in acronyms:
            acronyms.append(clean)
    return acronyms


def _find_acronym_definition(full_text: str, acronym: str) -> Optional[str]:
    """Locate the defining or introductory sentence for an acronym within full source text."""
    base_acronym = acronym.rstrip("s")
    esc_base = re.escape(base_acronym)
    esc_full = re.escape(acronym)

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", full_text) if s.strip()]

    # 1. Full Term (ACRONYM) or ACRONYM (Full Term)
    paren_pat = rf"(?:[A-Z][a-zA-Z\-]+(?:\s+[a-zA-Z\-]+){{0,6}}\s*\(\s*(?:{esc_full}|{esc_base})\s*\)|(?:{esc_full}|{esc_base})\s*\(\s*[A-Z][a-zA-Z\-]+(?:\s+[a-zA-Z\-]+){{0,6}}\s*\))"
    for s in sentences:
        if re.search(paren_pat, s):
            return s

    # 2. ACRONYM stands for / denotes / refers to / is defined as / represents
    def_verb_pat = rf"\b(?:{esc_full}|{esc_base})\b\s+(?:stands for|denotes|refers to|is defined as|represents|is shorthand for|designates)\b"
    for s in sentences:
        if re.search(def_verb_pat, s, re.IGNORECASE):
            return s

    # 3. We propose / introduce / formulate / develop ACRONYM
    intro_pat = rf"\b(?:we propose|we introduce|we formulate|we present|we develop|we call)\s+[^.!?]*\b(?:{esc_full}|{esc_base})\b"
    for s in sentences:
        if re.search(intro_pat, s, re.IGNORECASE):
            return s

    # 4. ACRONYM is a/an [method/algorithm/technique/framework/approach/model]
    is_a_pat = rf"\b(?:{esc_full}|{esc_base})\b\s+(?:is|serves as|acts as)\s+(?:a|an|the)\s+[^.!?]*(?:method|algorithm|technique|approach|framework|optimization|model)"
    for s in sentences:
        if re.search(is_a_pat, s, re.IGNORECASE):
            return s

    return None


def _is_section_heading(line: str) -> bool:
    """Detect whether a line is a markdown or numbered section heading."""
    s = line.strip()
    if not s or "\n" in s:
        return False
    if s.startswith("#") and len(s) <= 120:
        return True
    if len(s) > 100:
        return False
    if re.match(r"^(?:section\s+\d+|\d+(?:\.\d+)*\.?\s+[A-Z])", s, re.IGNORECASE):
        return True
    if not s.endswith((".", "!", "?", ":", ";", ",")) and len(s.split()) <= 8:
        words = [w for w in re.findall(r"[a-zA-Z]+", s) if len(w) > 2]
        if words and sum(1 for w in words if w[0].isupper() or w.isupper()) / len(words) >= 0.7:
            return True
    return False


def _acronym_matches_text(acronym: str, text: str, claim_text: str = "") -> bool:
    """Check if an acronym or technical token appears in text, accounting for inflections,
    hyphenated compounds, and acronym definitions.
    """
    a_lower = acronym.lower()
    t_lower = text.lower()

    if a_lower in t_lower:
        return True

    # Check singular/plural inflections
    if a_lower.endswith("s") and len(a_lower) > 3 and a_lower[:-1] in t_lower:
        return True
    if not a_lower.endswith("s") and (a_lower + "s") in t_lower:
        return True

    # Check hyphenated compounds (e.g. RAG-generated -> RAG + generate/generated)
    if "-" in acronym:
        parts = [p.lower() for p in acronym.split("-") if p.strip()]
        if parts:
            all_parts_matched = True
            for part in parts:
                part_stem = part[:-1] if part.endswith("s") else (part[:-2] if part.endswith("ed") else part)
                if not any(part_stem in pw for pw in re.findall(r"\b[a-zA-Z0-9]+\b", t_lower)):
                    all_parts_matched = False
                    break
            if all_parts_matched:
                return True

    # Check if acronym is defined or expanded in claim_text and grounded in text
    if claim_text:
        # Pattern 1: ACRONYM (...) e.g. IOI (indirect object identification)
        m_def1 = re.search(r"\b" + re.escape(acronym) + r"\b[^\(\[\n]{0,30}[\(\[]([A-Z0-9]{2,})[\)\]]", claim_text)
        if m_def1:
            acr_code = m_def1.group(1).lower()
            if acr_code in t_lower:
                return True
        # Pattern 2: Expansion (ACRONYM) e.g. indirect object identification (IOI)
        m_def2 = re.search(r"([A-Za-z0-9\s\-]{3,60})\s*[\(\[]" + re.escape(acronym) + r"[\)\]]", claim_text)
        if m_def2:
            expansion = m_def2.group(1).strip().lower()
            if expansion in t_lower:
                return True
            exp_words = [w for w in re.findall(r"\b[a-zA-Z0-9]+\b", expansion) if w not in _STOPWORDS]
            if exp_words and all(w in t_lower for w in exp_words):
                return True

    letters = [ch.lower() for ch in acronym if ch.isalnum()]
    if len(letters) >= 2:
        words = re.findall(r"\b[a-zA-Z]+\b", text)
        content_words = [w for w in words if w[0].isupper()]
        for i in range(len(content_words) - len(letters) + 1):
            if [w[0].lower() for w in content_words[i : i + len(letters)]] == letters:
                return True
        # Also check consecutive non-stopwords in text matching initialism (case-insensitive)
        non_stop_words = [w.lower() for w in words if w.lower() not in _STOPWORDS or len(w) > 3]
        for i in range(len(non_stop_words) - len(letters) + 1):
            if [w[0] for w in non_stop_words[i : i + len(letters)]] == letters:
                return True

    return False


def _check_entities_present(claim_text: str, passage_text: str) -> Tuple[bool, str]:
    """Check if all distinctive named entities or methods in the claim are present in the passage."""
    p_lower = passage_text.lower()

    # 1. Patterns like 'Method A', 'Model B', 'Algorithm 1', etc.
    # Keyword is case-insensitive, but entity identifier must start with uppercase letter or digit,
    # and must NOT be a common English stopword or generic technical term.
    labeled_matches = re.finditer(
        r"\b(?i:method|model|algorithm|system|framework|technique|architecture|approach)\s+([A-Z0-9][a-zA-Z0-9\-_]*)\b",
        claim_text,
    )
    for m in labeled_matches:
        ent_id = m.group(1)
        low_id = ent_id.lower()
        if low_id in _STOPWORDS or low_id in _GENERIC_IDENTIFIERS:
            continue
        full_label_pat = rf"\b(?i:method|model|algorithm|system|framework|technique|architecture|approach)\s+{re.escape(ent_id)}\b"
        id_pat = rf"\b{re.escape(ent_id)}\b"
        if not re.search(full_label_pat, passage_text, re.IGNORECASE) and not re.search(id_pat, passage_text, re.IGNORECASE):
            return False, f"Passage does not mention required entity identifier {ent_id!r}"

    # 2. Acronyms & technical proper nouns (e.g. DPO, RLHF, PostgreSQL, MongoDB, TxFS, Citus, Redis)
    raw_tokens = list(re.finditer(r"\b[A-Za-z0-9_\-]+\b", claim_text))
    candidates: List[str] = []
    for idx, m_tok in enumerate(raw_tokens):
        tok = m_tok.group(0)
        low_tok = tok.lower()
        if low_tok in _STOPWORDS or low_tok in _GENERIC_IDENTIFIERS:
            continue
        if re.fullmatch(r"[A-Z][a-zA-Z0-9]*(?:-[A-Za-z0-9]+)+|[A-Z]{2,}s?|[A-Z][a-z]+[A-Z][a-zA-Z]*", tok):
            candidates.append(tok)
        elif idx > 0 and re.fullmatch(r"[A-Z][a-z]{3,}", tok):
            if tok.upper() not in _COMMON_NON_ACRONYMS:
                pre_text = claim_text[:m_tok.start()].rstrip()
                is_clause_initial = not pre_text or pre_text[-1] in {":", ";", "-", "—", "\n", ".", "!", "?"}
                if not is_clause_initial:
                    candidates.append(tok)

    for c in candidates:
        if c.upper() in _COMMON_NON_ACRONYMS:
            continue
        if not _acronym_matches_text(c, passage_text, claim_text):
            return False, f"Passage does not mention required technical entity {c!r}"

    return True, "All entities present"


_EXCLUDED_SYSTEM_TOKENS = {
    "qps", "mb", "gb", "tb", "kb", "ms", "ns", "hz", "khz", "mhz", "ghz",
    "fps", "flops", "tflops", "gflops", "params", "param", "sift1m", "gist1m",
    "math-500", "mmlu", "gsm8k", "imagenet", "glue", "squad", "sift", "gist",
    "bf16", "fp16", "fp32", "fp8", "mxfp4", "int8", "int4", "simd", "avx", "avx2",
    "cpu", "cpus", "gpu", "gpus", "cuda",
}

_ATTRIBUTION_STOPWORDS = _STOPWORDS | {
    "under", "over", "between", "across", "through", "into", "onto", "within",
    "without", "along", "before", "after", "during", "while", "against",
    "about", "above", "below", "toward", "towards", "upon", "per",
}


def _extract_named_systems(text: str) -> List[str]:
    """Extract named technical systems, algorithms, models, and architectures from text."""
    raw_tokens = list(re.finditer(r"\b[A-Za-z0-9_\-]+\b", text))
    candidates: List[str] = []
    for idx, m_tok in enumerate(raw_tokens):
        tok = m_tok.group(0)
        low_tok = tok.lower()
        if (
            low_tok in _ATTRIBUTION_STOPWORDS
            or low_tok in _GENERIC_IDENTIFIERS
            or low_tok in _EXCLUDED_SYSTEM_TOKENS
            or len(tok) < 3
        ):
            continue
        pre_ctx = text[max(0, m_tok.start() - 30):m_tok.start()]
        is_labeled = bool(re.search(r"\b(?i:method|model|algorithm|system|framework|technique|architecture|approach|library|index)\s+$", pre_ctx))
        is_sys_pattern = bool(re.fullmatch(r"[A-Z][a-zA-Z0-9]*(?:-[A-Za-z0-9]+)+|[A-Z]{2,}s?|[A-Z]+[a-z]+[A-Z0-9][a-zA-Z0-9]*|[A-Z]{2,}[a-z]+[a-zA-Z0-9]*", tok))
        if (is_sys_pattern or is_labeled) and tok.upper() not in _COMMON_NON_ACRONYMS:
            candidates.append(tok)
    return list(dict.fromkeys(candidates))


def _check_subject_attribution(
    claim_text: str,
    passage_text: str,
) -> Tuple[bool, str, List[str], List[str]]:
    """Verify that properties claimed for a technical system are not falsely inherited
    from a different named system discussed in the same evidence passage.

    Returns:
        (is_valid_attribution, explanation, compatible_sentences, competing_sentences)
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", passage_text) if s.strip()]
    if not sentences:
        return True, "", [], []

    p_systems = _extract_named_systems(passage_text)
    c_systems = _extract_named_systems(claim_text)

    # Competing systems: named systems in passage that are not the claim's subjects
    competing_systems = [
        s for s in p_systems
        if not any(
            cs.lower() == s.lower()
            or cs.lower().startswith(s.lower())
            or s.lower().startswith(cs.lower())
            or s.lower() in cs.lower()
            or cs.lower() in s.lower()
            for cs in c_systems
        )
    ]

    if not competing_systems or not c_systems:
        return True, "", sentences, []

    c_compatible_sentences: List[str] = []
    competing_sentences: List[str] = []
    sentence_competing_map: Dict[str, str] = {}

    for s in sentences:
        s_competing = [cs for cs in competing_systems if re.search(rf"\b{re.escape(cs)}\b", s, re.IGNORECASE)]
        s_claim = [cs for cs in c_systems if re.search(rf"\b{re.escape(cs)}\b", s, re.IGNORECASE)]
        if s_competing and not s_claim:
            competing_sentences.append(s)
            for cs in s_competing:
                sentence_competing_map[s] = cs
        else:
            c_compatible_sentences.append(s)

    if not competing_sentences:
        return True, "", sentences, []

    # Extract substantive property concepts from claim (excluding entity names themselves)
    c_words = re.findall(r"[a-zA-Z0-9_\-]+", claim_text.lower())
    c_tokens = [w for w in c_words if len(w) >= 3 and w not in _ATTRIBUTION_STOPWORDS and w not in _GENERIC_IDENTIFIERS]
    c_sys_tokens = {w.lower() for cs in c_systems for w in re.findall(r"[a-zA-Z0-9_\-]+", cs.lower())}
    content_tokens = [t for t in c_tokens if t not in c_sys_tokens]

    comp_text = " ".join(competing_sentences).lower()
    compat_text = " ".join(c_compatible_sentences).lower()

    comp_words = set(re.findall(r"[a-zA-Z0-9_\-]+", comp_text))
    compat_words = set(re.findall(r"[a-zA-Z0-9_\-]+", compat_text))

    def _matches_in(t: str, word_set: set) -> bool:
        if t in word_set:
            return True
        for suffix in ("ness", "ance", "ence", "ing", "tion", "tions", "ment", "ments", "ed", "es", "s", "ant", "ent", "ive", "ity", "able", "ible", "e"):
            if t.endswith(suffix) and len(t) - len(suffix) >= 4:
                stem = t[:-len(suffix)]
                for pw in word_set:
                    if pw.startswith(stem) and len(pw) >= len(stem):
                        return True
        return False

    competing_only = [t for t in content_tokens if _matches_in(t, comp_words) and not _matches_in(t, compat_words)]

    # If 2 or more substantive property concepts appear exclusively in competing-only sentences,
    # the claim is attributing properties of another system to the claim subject.
    if len(competing_only) >= 2:
        comp_sys_names = list(dict.fromkeys(sentence_competing_map.values()))
        msg = (
            f"Claim attributes property concepts {sorted(competing_only)!r} to {c_systems[0]!r}, "
            f"but in the evidence they are explicitly attributed to competing system {comp_sys_names[0]!r}"
        )
        return False, msg, c_compatible_sentences, competing_sentences

    return True, "", c_compatible_sentences, competing_sentences


def _is_pure_title_or_metadata(text: str) -> bool:
    """Detect whether a candidate passage is purely a title, author metadata, affiliation, or publication header."""
    t = text.strip()
    if not t:
        return True
    if _is_metadata_or_boilerplate(t):
        return True

    clean_t = re.sub(
        r"^\[(?:bibtex|title|heading:[^\]]+|context:[^\]]+)\]\s*",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()
    if not clean_t:
        return True
    clean_lower = clean_t.lower()

    # 1. Explicit metadata prefixes
    meta_prefixes = (
        "title:", "authors:", "author:", "affiliations:", "affiliation:",
        "published in:", "citation:", "published:", "accepted at:", "doi:", "arxiv:",
        "venue:", "conference:", "journal:", "references:", "bibliography:",
    )
    lines = [l.strip() for l in clean_t.splitlines() if l.strip()]
    if lines and any(lines[0].lower().startswith(p) for p in meta_prefixes):
        if len(lines) <= 6:
            return True

    # 2. Bibliography entries (e.g. [1] Author, A. (2023) or Author et al. 2023)
    bib_pat = r"^(?:\[\d+\]\s*)?[A-Z][a-zA-Z\.\-\s]+(?:et\s+al\.?|,\s*[A-Z]\.|\s+and\s+[A-Z]).*?\b(?:19|20)\d{2}\b.*"
    if re.match(bib_pat, clean_t) and len(clean_t.split()) <= 45:
        if not any(k in clean_lower for k in ("we show", "we find", "we prove", "results show", "our method")):
            return True

    # 3. Author lists (e.g. "Name Name, Name Name, and Name Name")
    tokens = [p.strip() for p in re.split(r",\s*(?:and\s+)?|\s+and\s+", clean_t) if p.strip()]
    name_pat = r"^[A-Z][a-zA-Z\.\-]+\s+[A-Z][a-zA-Z\.\-]+(?:\s+[A-Z][a-zA-Z\.\-]+)?$"
    if len(tokens) >= 2 and sum(1 for tok in tokens if re.match(name_pat, tok)) >= len(tokens) * 0.7:
        return True

    # 4. Affiliations, emails, arXiv / DOI metadata IDs
    inst_keywords = (
        "department of", "university", "institute of", "laboratory",
        "csail", "equal contribution", "correspondence to", "all rights reserved",
    )
    if any(k in clean_lower for k in inst_keywords) and len(clean_t.split()) <= 30:
        if not any(k in clean_lower for k in ("we propose", "we introduce", "yields", "achieves", "improves", "outperforms")):
            return True

    if bool(re.search(r"\b(?:arxiv:\d{4}\.\d{4,5}|doi:\s*10\.\d+|@[\w\.-]+\.(?:edu|com|org))\b", clean_lower)):
        if len(clean_t.split()) <= 30:
            return True

    # 5. Title + Author + Institution combination (e.g. "Title. Author et al. OpenAI, 2023.")
    has_inst = any(
        k in clean_lower
        for k in ("openai", "google", "deepmind", "meta", "microsoft", "stanford", "berkeley", "mit", "cmu", "university")
    )
    has_year = bool(re.search(r"\b(?:19|20)\d{2}\b", clean_t))
    if has_inst and has_year and len(clean_t.split()) <= 40:
        if not any(k in clean_lower for k in ("we show", "we prove", "we propose", "we evaluate", "yields", "achieves", "improves", "outperforms")):
            return True

    # 6. Standalone Title:
    # A title lacks terminal punctuation OR has heavy Title Casing across almost all non-stop words
    if len(clean_t.split()) <= 22:
        words = [w for w in re.findall(r"[a-zA-Z]+", clean_t) if len(w) > 2 and w.lower() not in _STOPWORDS]
        if len(words) >= 3:
            cap_ratio = sum(1 for w in words if w[0].isupper()) / len(words)
            # Standalone Title Case without terminal sentence punctuation
            if not clean_t.endswith((".", "!", "?")) and cap_ratio >= 0.75:
                return True
            # Title: Subtitle structure where both parts are Title Cased or capitalized
            if re.match(r"^[A-Z0-9][A-Za-z0-9\s\-–—]+:\s+[A-Z0-9][A-Za-z0-9\s\-–—]+\.?$", clean_t) and cap_ratio >= 0.75:
                return True
            # Title Case with trailing period but no narrative lowercase verbs
            if clean_t.endswith(".") and cap_ratio >= 0.85:
                return True

    return False


def _is_metadata_or_boilerplate(text: str) -> bool:
    """Detect whether a candidate passage is primarily author headers, affiliations, or boilerplate."""
    t_lower = text.lower()

    # Substantive metrics / numbers guard: empirical benchmark tables and quantitative findings are not boilerplate
    has_empirical_metrics = bool(
        re.search(r"\b\d+(?:\.\d+)?\s*(?:s|ms|ns|mb|gb|tb|qps|hz|khz|mhz|ghz|%|flops|tflops|params?)\b", t_lower)
        or re.search(r"\b\d{1,3},\d{3}\b", text)
        or (re.search(r"\|\s*[^|]+\s*\|", text) and re.search(r"\d", text))
    )
    if has_empirical_metrics:
        return False

    meta_patterns = [
        r"\b(?:department|dept\.) of\b",
        r"\buniversity\b",
        r"\binstitute of\b",
        r"\bschool of\b",
        r"\blaboratory\b",
        r"\bcorrespondence to\b",
        r"\bequal contribution\b",
        r"\bauthor contributions\b",
        r"\baffiliations?\b",
        r"\bemail:\b",
        r"@[\w\.-]+\.(?:edu|com|org|gov|io|ai)\b",
        r"\barxiv:\d{4}\.\d{4,5}\b",
        r"\ball rights reserved\b",
        r"\bunder review\b",
        r"\bpreprint\b",
        r"\bcopyright\b",
        r"\bfootnotemark\b",
        r"\bthanks:\b",
        r"\{[\w\.,\s-]+\}@",
    ]
    meta_hits = sum(1 for p in meta_patterns if re.search(p, t_lower))
    if meta_hits >= 1 and (
        re.search(r"@[\w\.-]+\.(?:edu|com|org|gov|io|ai)\b|\{[\w\.,\s-]+\}@", text)
        or any(k in t_lower for k in ["equal contribution", "correspondence to", "footnotemark", "thanks:"])
    ):
        return True
    if meta_hits >= 2:
        return True
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if lines and len(lines) > 2:
        short_lines = sum(1 for l in lines if len(l.split()) <= 4)
        if short_lines / len(lines) > 0.6 and meta_hits >= 1:
            return True
    return False


def _extract_candidate_passages(full_text: str) -> List[str]:
    """Segment full text into candidate paragraphs and sliding sentence windows across the document,
    preserving heading context and explanatory sentences.
    """
    # If text starts with author header preceding Abstract, separate it
    parts = re.split(r"(?i)\babstract\b[:\s]*", full_text, maxsplit=1)
    if len(parts) > 1 and (_is_metadata_or_boilerplate(parts[0]) or _is_pure_title_or_metadata(parts[0])):
        text_to_process = parts[1]
    else:
        text_to_process = full_text

    raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text_to_process) if p.strip()]
    candidates: List[str] = []
    seen: Set[str] = set()

    def add_candidate(cand: str):
        c = " ".join(cand.split()).strip()
        if len(c) >= 25 and c not in seen and not _is_pure_title_or_metadata(c):
            seen.add(c)
            candidates.append(c)

    current_heading: Optional[str] = None
    for para in raw_paragraphs:
        para_lines = [l.strip() for l in para.splitlines() if l.strip()]
        if len(para_lines) > 1 and _is_section_heading(para_lines[0]):
            current_heading = para_lines[0].lstrip("#").strip()
            para = " ".join(para_lines[1:])
        elif _is_section_heading(para):
            current_heading = para.lstrip("#").strip()
            continue

        prefix = f"[{current_heading}] " if current_heading else ""

        if len(para) <= 1200:
            add_candidate(prefix + para)
            if prefix:
                add_candidate(para)

        # Sentence sliding window within paragraph (2 to 5 sentences, up to 1200 characters)
        para_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", para) if s.strip()]
        if len(para_sentences) > 1:
            for w in (2, 3, 4, 5):
                for i in range(len(para_sentences)):
                    chunk_sentences = list(para_sentences[i : i + w])
                    # If chunk starts with anaphora or demonstrative, include preceding sentence if available
                    if i > 0 and chunk_sentences:
                        first_sent = chunk_sentences[0]
                        if re.match(
                            r"^(?:(?:this|these|the|our|such)\s+(?:method|approach|algorithm|framework|technique|model|system|work|results?|finding|optimization)|it\b|they\b)",
                            first_sent,
                            re.IGNORECASE,
                        ):
                            chunk_sentences.insert(0, para_sentences[i - 1])
                    chunk = " ".join(chunk_sentences)
                    if chunk and len(chunk) <= 1200:
                        add_candidate(prefix + chunk)
                        if prefix:
                            add_candidate(chunk)

        # Line sliding window within paragraph (for tables and lists inside a section)
        if len(para_lines) > 3:
            for w in (4, 6, 8, 12, 16, 20):
                for i in range(len(para_lines) - w + 1):
                    chunk = " ".join(para_lines[i : i + w])
                    if 50 <= len(chunk) <= 1200:
                        add_candidate(prefix + chunk)

    # Line sliding window for single-newline separated documents without multiple double-newline paragraphs
    doc_lines = [l.strip() for l in text_to_process.splitlines() if l.strip()]
    if len(raw_paragraphs) <= 1 and len(doc_lines) >= 3:
        for w in (4, 6, 8, 12, 16, 20, 25):
            for i in range(len(doc_lines) - w + 1):
                chunk_lines = doc_lines[i : i + w]
                chunk = " ".join(chunk_lines)
                if 50 <= len(chunk) <= 1200:
                    add_candidate(chunk)

    # Also sliding window across the entire document if paragraphs were few or single-line separated
    if len(raw_paragraphs) <= 3 and len(text_to_process) > 600:
        all_sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text_to_process) if s.strip()]
        for w in (2, 3, 4, 5):
            for i in range(len(all_sentences)):
                chunk_sentences = list(all_sentences[i : i + w])
                if i > 0 and chunk_sentences:
                    first_sent = chunk_sentences[0]
                    if re.match(
                        r"^(?:(?:this|these|the|our|such)\s+(?:method|approach|algorithm|framework|technique|model|system|work|results?|finding|optimization)|it\b|they\b)",
                        first_sent,
                        re.IGNORECASE,
                    ):
                        chunk_sentences.insert(0, all_sentences[i - 1])
                chunk = " ".join(chunk_sentences)
                if chunk and len(chunk) <= 1200:
                    add_candidate(chunk)

    if not candidates:
        fallback = full_text[:600]
        if not _is_pure_title_or_metadata(fallback):
            candidates = [fallback]

    return candidates


def find_passage_for_url_and_query(
    url: str,
    query_text: str,
    store: EvidenceStore,
) -> Tuple[Optional[str], Optional[str], Optional[EvidenceRecord]]:
    """Locate the most relevant supporting passage in store for a specific URL and query text.

    Preserves relevant context, nearby acronym definitions, headings, and explanatory sentences,
    while disqualifying non-substantive titles and author/publication metadata.

    Returns
    -------
    (passage_text, passage_id, record)
        passage_text: The exact supporting passage extracted from the evidence record.
        passage_id: Deterministic compound hash identifying the passage.
        record: The matching EvidenceRecord from the store (if found).
    """
    record = store.get_by_url(url)
    if record is None:
        return None, None, None

    if record.retrieval_status == RetrievalStatus.FAILED:
        return None, None, record

    # Case 1: SNIPPET_ONLY evidence record
    if record.retrieval_status == RetrievalStatus.SNIPPET_ONLY:
        snippet = (record.snippet_text or record.captured_text or "").strip()
        if not snippet or _is_pure_title_or_metadata(snippet):
            return None, None, record
        p_id = make_passage_id(record.evidence_id, snippet)
        return snippet, p_id, record

    # Case 2: FULL_TEXT evidence record
    full_text = (record.captured_text or "").strip()
    if not full_text or len(full_text) < 20:
        return None, None, record

    candidate_passages = _extract_candidate_passages(full_text)
    if not candidate_passages:
        return None, None, record

    # Extract non-stopword tokens from the query
    words = re.findall(r"[a-zA-Z0-9_\-]+", query_text.lower())
    claim_tokens = {w for w in words if len(w) >= 3 and w not in _STOPWORDS}

    # Decompose query into clauses to detect multi-clause coverage
    query_clauses = _decompose_claim_clauses(query_text)
    clause_token_sets: List[Set[str]] = []
    if len(query_clauses) > 1:
        for cl in query_clauses:
            cl_words = set(re.findall(r"[a-zA-Z0-9_\-]+", cl.lower()))
            cl_toks = {w for w in cl_words if len(w) >= 3 and w not in _STOPWORDS}
            if cl_toks:
                clause_token_sets.append(cl_toks)

    if not claim_tokens:
        for c in candidate_passages:
            if not _is_metadata_or_boilerplate(c) and not _is_pure_title_or_metadata(c):
                return c, make_passage_id(record.evidence_id, c), record
        if candidate_passages and not _is_pure_title_or_metadata(candidate_passages[0]):
            first_p = candidate_passages[0]
            return first_p, make_passage_id(record.evidence_id, first_p), record
        return None, None, record

    best_passage: Optional[str] = None
    best_score = -999.0

    for candidate in candidate_passages:
        cand_lower = candidate.lower()
        cand_words = set(re.findall(r"[a-zA-Z0-9_\-]+", cand_lower))

        def _cand_has_token(tok: str) -> bool:
            if tok in cand_words:
                return True
            for suffix in ("ness", "ance", "ence", "ing", "tion", "tions", "ment", "ments", "ed", "es", "s", "ant", "ent", "ive", "ity", "able", "ible", "e"):
                if tok.endswith(suffix) and len(tok) - len(suffix) >= 4:
                    stem = tok[:-len(suffix)]
                    for cw in cand_words:
                        if cw.startswith(stem) and len(cw) >= len(stem):
                            return True
            return False

        matches = {t for t in claim_tokens if _cand_has_token(t)}
        if not matches:
            continue
        score = len(matches) / max(1, len(claim_tokens))

        # Bigram bonus: award extra score if multi-word terms appear consecutively
        for w1, w2 in zip(words[:-1], words[1:]):
            if f"{w1} {w2}" in cand_lower:
                score += 0.25

        # Distinctive token bonus: words of length >= 6 matching
        distinctive_matches = [w for w in matches if len(w) >= 6]
        score += 0.10 * len(distinctive_matches)

        # Multi-clause coverage bonus: award bonus if candidate covers tokens from ALL distinct clauses
        if clause_token_sets and len(clause_token_sets) > 1:
            covered_clauses = sum(1 for cset in clause_token_sets if cand_words.intersection(cset))
            if covered_clauses == len(clause_token_sets):
                score += 0.35 * covered_clauses

        # Specific model / acronym entity bonus: award bonus when candidate contains query acronyms or model names
        for acr in _extract_acronyms(query_text):
            if acr.lower() in cand_lower:
                score += 0.50

        # Empirical findings bonus: award bonus if candidate contains empirical outcome assertions
        empirical_markers = (
            "results indicate", "we find", "we found", "match perfectly",
            "correlates", "demonstrates that", "outperforms", "achieves",
            "reliable alternative", "empirical", "significant", "superior"
        )
        if any(m in cand_lower for m in empirical_markers):
            score += 0.35

        # Quantitative metric match bonus: award bonus if numbers in query appear in candidate
        query_numbers = set(re.findall(r"\b\d+(?:[\.,]\d+)?\b", query_text))
        cand_numbers = set(re.findall(r"\b\d+(?:[\.,]\d+)?\b", candidate))
        matched_numbers = query_numbers.intersection(cand_numbers)
        if matched_numbers:
            score += 0.40 * len(matched_numbers)

        # Penalty for background or prior-work sections when query does not specifically ask about prior work
        if any(cand_lower.startswith(pw) or f" {pw}" in cand_lower for pw in ("prior work", "related work", "previous studies")):
            if "prior work" not in query_text.lower() and "previous" not in query_text.lower():
                score -= 0.50

        # Severe penalty for author/affiliation/boilerplate metadata or pure title
        if _is_metadata_or_boilerplate(candidate) or _is_pure_title_or_metadata(candidate):
            score -= 1.5

        if score > best_score:
            best_score = score
            best_passage = candidate

    # Minimum relevance threshold: require score >= 0.20
    if best_score < 0.20 or best_passage is None:
        return None, None, record

    # Enrich with acronym definition context if query contains an acronym that is absent from best_passage
    # or if the query directly asserts the acronym's definition
    query_acronyms = _extract_acronyms(query_text)
    for acr in query_acronyms:
        if acr not in best_passage or any(k in query_text.lower() for k in ("stands for", "denotes", "refers to", "is defined as")):
            defn = _find_acronym_definition(full_text, acr)
            if defn and defn not in best_passage and not _is_pure_title_or_metadata(defn):
                clean_defn = re.sub(r"^#+\s*", "", defn.strip())
                best_passage = f"[Context: {clean_defn}]\n\n{best_passage}"
                break

    p_id = make_passage_id(record.evidence_id, best_passage)
    return best_passage, p_id, record


def find_supporting_passage(
    claim: AtomicClaim,
    store: EvidenceStore,
) -> Tuple[Optional[str], Optional[str], Optional[EvidenceRecord]]:
    """Locate the exact supporting passage from the run's EvidenceStore for a claim.

    Returns
    -------
    (passage_text, passage_id, record)
        passage_text: The exact supporting passage extracted from the evidence record.
        passage_id: Deterministic compound hash identifying the passage.
        record: The matching EvidenceRecord from the store (if found).
    """
    if not claim.source_url:
        return None, None, None
    query = claim.clause_text or claim.claim_text
    return find_passage_for_url_and_query(claim.source_url, query, store)


_LATEX_BTRF_RE = re.compile(
    r"^(?:"
    r"b(?:eta|inom|oldsymbol|mathbf|mathbb|bmatrix|bmod|ar[\{\s]|m\{)|"
    r"t(?:heta|au|imes|ilde)\b|"
    r"r(?:ho|angle|ightarrow|Rightarrow|rfloor|rceil)\b|"
    r"f(?:rac(?:[\{\s\d]|$)|forall\b)"
    r")"
)


def repair_json_escapes(raw_json: str) -> str:
    """Repair invalid JSON escapes and unescaped LaTeX math commands in JSON strings.

    Distinguishes genuine JSON control escapes from LaTeX math expressions:
    1. Inside math delimiters ($...$, $$...$$, \\(...\\), \\[...\\]): backslashes
       preceding letters are recognized as LaTeX commands (e.g., \\beta, \\theta,
       \\rho, \\frac) and escaped as \\\\ to prevent silent corruption into JSON control
       characters (backspace, tab, carriage return, form feed).
    2. Outside math delimiters: unambiguous LaTeX commands (\\beta, \\theta, \\rho,
       \\frac, \\forall, etc.) are escaped as \\\\, while ordinary JSON control escapes
       (\\n, \\t, \\b, \\f, \\r) for non-LaTeX text are strictly preserved.
    3. Invalid JSON escape sequences (\\chi, \\alpha, \\sigma, or non-hex \\u...)
       are escaped as \\\\.
    4. Valid JSON escapes (\\", \\\\, \\/, and genuine \\n, \\t, \\b, \\f, \\r, \\uXXXX)
       are preserved.
    """
    out: List[str] = []
    in_string = False
    in_math = False
    i = 0
    n = len(raw_json)

    while i < n:
        c = raw_json[i]
        if not in_string:
            if c == '"':
                in_string = True
                in_math = False
            out.append(c)
            i += 1
            continue

        if c == '"':
            in_string = False
            in_math = False
            out.append(c)
            i += 1
        elif c == "$":
            if i + 1 < n and raw_json[i + 1] == "$":
                in_math = not in_math
                out.append("$$")
                i += 2
            else:
                in_math = not in_math
                out.append(c)
                i += 1
        elif c == "\\":
            if i + 1 >= n:
                # Dangling backslash at end of input inside string: invalid escape
                out.append("\\\\")
                i += 1
                continue

            next_c = raw_json[i + 1]
            remainder = raw_json[i + 1 :]

            # Escaped quotes, backslashes, and slashes are always preserved
            if next_c in ('"', "\\", "/"):
                out.append(c)
                out.append(next_c)
                i += 2
                continue

            # Escaped dollar sign: literal $, do not enter math mode
            if next_c == "$":
                out.append("\\\\$")
                i += 2
                continue

            # LaTeX math delimiters \( \) \[ \]
            if next_c in ("(", "["):
                in_math = True
                out.append("\\\\" + next_c)
                i += 2
                continue
            if next_c in (")", "]"):
                in_math = False
                out.append("\\\\" + next_c)
                i += 2
                continue

            # Unicode escapes: \uXXXX
            if next_c in ("u", "U"):
                hex_part = raw_json[i + 2 : i + 6]
                if len(hex_part) == 4 and all(h in string.hexdigits for h in hex_part):
                    out.append(c)
                    out.append(next_c)
                    out.append(hex_part)
                    i += 6
                else:
                    # Invalid Unicode escape (e.g. \\user or \\underline): escape the backslash
                    out.append("\\\\")
                    i += 1
                continue

            # Detect LaTeX math commands colliding with RFC 8259 escapes (b, t, r, f)
            is_latex_math = False
            if in_math:
                if next_c.isalpha():
                    is_latex_math = True
            else:
                if _LATEX_BTRF_RE.match(remainder):
                    is_latex_math = True

            if is_latex_math:
                out.append("\\\\")
                i += 1
                continue

            # Valid JSON control escapes: \b, \f, \n, \r, \t
            if next_c in ("b", "f", "n", "r", "t"):
                out.append(c)
                out.append(next_c)
                i += 2
                continue

            # All other characters following backslash are invalid JSON escapes (\chi, \alpha, etc.)
            out.append("\\\\")
            i += 1
        else:
            out.append(c)
            i += 1

    return "".join(out)


# ---------------------------------------------------------------------------
# Claim Verifier
# ---------------------------------------------------------------------------

class ClaimVerifier:
    """Judge and verify individual factual claims against cited evidence passages."""

    def __init__(
        self,
        llm: Optional[Any] = None,
        verifier_fn: Optional[Callable[[str, str, Optional[str]], Tuple[ClaimVerdict, str]]] = None,
        action_policy: str = "label",
    ) -> None:
        """Initialize ClaimVerifier.

        Parameters
        ----------
        llm:
            CrewAI LLM instance configured for Mesh API.
        verifier_fn:
            Optional pluggable verification function for deterministic offline testing.
            Signature: (claim_text, passage_text, source_url) -> (verdict, rationale).
        action_policy:
            'label' (default): Annotate unsupported claims with clear unverified warnings.
            'remove': Excise unsupported claims from the report text.
        """
        self.llm = llm
        self.verifier_fn = verifier_fn
        self.action_policy = action_policy

    def _evaluate_passage(
        self,
        eval_text: str,
        passage_text: str,
        source_url: Optional[str],
    ) -> Tuple[ClaimVerdict, str]:
        """Evaluate a text snippet against a candidate passage using the configured verifier."""
        if _is_pure_title_or_metadata(passage_text):
            return (
                ClaimVerdict.INSUFFICIENT_EVIDENCE,
                "Passage contains only title, author metadata, or publication details rather than substantive evidence.",
            )
        if self.verifier_fn is not None:
            return self.verifier_fn(eval_text, passage_text, source_url)
        elif self.llm is not None:
            return self._verify_via_mesh_llm(eval_text, passage_text, source_url)
        else:
            return self._heuristic_fallback_verify(eval_text, passage_text)

    def verify_claim(
        self,
        claim: AtomicClaim,
        store: EvidenceStore,
    ) -> ClaimVerificationRecord:
        """Verify an individual factual claim against evidence in the store."""
        cited_urls = list(dict.fromkeys([u for u in claim.cited_urls if u]))
        if not cited_urls and claim.source_url:
            cited_urls = [claim.source_url]

        # 1. Missing citation marker or unresolvable URL
        if not cited_urls:
            return ClaimVerificationRecord(
                claim_id=claim.claim_id,
                claim_text=claim.claim_text,
                source_url=None,
                passage_id=None,
                passage_text=None,
                verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
                rationale="Claim lacks a valid, resolvable source URL citation in the report text.",
                action_taken=ClaimAction.REMOVED.value if self.action_policy == "remove" else ClaimAction.LABELED_UNVERIFIED.value,
                retrieval_status=None,
                raw_sentence=claim.raw_sentence,
                supporting_urls=None,
            )

        # Check for unresolved anaphoric referents (e.g. 'these dimensions')
        referent_ok, referent_msg = _check_unresolved_referents(claim.claim_text)
        if not referent_ok:
            return ClaimVerificationRecord(
                claim_id=claim.claim_id,
                claim_text=claim.claim_text,
                source_url=cited_urls[0] if cited_urls else None,
                passage_id=None,
                passage_text=None,
                verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
                rationale=referent_msg,
                action_taken=ClaimAction.REMOVED.value if self.action_policy == "remove" else ClaimAction.LABELED_UNVERIFIED.value,
                retrieval_status=None,
                raw_sentence=claim.raw_sentence,
                supporting_urls=None,
            )

        # Check if this is a compound claim where distinct clauses require joint verification
        clauses = claim.constituent_clauses
        if not clauses or len(clauses) <= 1 or claim.claim_text in clauses:
            clauses = _decompose_claim_clauses(claim.claim_text)

        # 2. Single-source, single-clause claim verification (ordinary regression path)
        if len(cited_urls) == 1 and len(clauses) <= 1:
            target_url = cited_urls[0]
            eval_query = claim.clause_text or claim.claim_text
            passage_text, passage_id, rec = find_passage_for_url_and_query(target_url, eval_query, store)

            if rec is None:
                return ClaimVerificationRecord(
                    claim_id=claim.claim_id,
                    claim_text=claim.claim_text,
                    source_url=target_url,
                    passage_id=None,
                    passage_text=None,
                    verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
                    rationale=f"Cited source URL {target_url!r} was not captured in the run's EvidenceStore.",
                    action_taken=ClaimAction.REMOVED.value if self.action_policy == "remove" else ClaimAction.LABELED_UNVERIFIED.value,
                    retrieval_status=None,
                    raw_sentence=claim.raw_sentence,
                    supporting_urls=None,
                )

            if rec.retrieval_status == RetrievalStatus.FAILED:
                return ClaimVerificationRecord(
                    claim_id=claim.claim_id,
                    claim_text=claim.claim_text,
                    source_url=target_url,
                    passage_id=None,
                    passage_text=None,
                    verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
                    rationale=f"Cited source URL {target_url!r} has FAILED retrieval status in EvidenceStore.",
                    action_taken=ClaimAction.REMOVED.value if self.action_policy == "remove" else ClaimAction.LABELED_UNVERIFIED.value,
                    retrieval_status=rec.retrieval_status.value,
                    raw_sentence=claim.raw_sentence,
                    supporting_urls=None,
                )

            if passage_text is None:
                return ClaimVerificationRecord(
                    claim_id=claim.claim_id,
                    claim_text=claim.claim_text,
                    source_url=target_url,
                    passage_id=None,
                    passage_text=None,
                    verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
                    rationale=f"Cited source {target_url!r} contains no relevant passage supporting the claim.",
                    action_taken=ClaimAction.REMOVED.value if self.action_policy == "remove" else ClaimAction.LABELED_UNVERIFIED.value,
                    retrieval_status=rec.retrieval_status.value,
                    raw_sentence=claim.raw_sentence,
                    supporting_urls=None,
                )

            verdict, rationale = self._evaluate_passage(eval_query, passage_text, target_url)

            if verdict == ClaimVerdict.SUPPORTED:
                return ClaimVerificationRecord(
                    claim_id=claim.claim_id,
                    claim_text=claim.claim_text,
                    source_url=target_url,
                    passage_id=passage_id,
                    passage_text=passage_text,
                    verdict=ClaimVerdict.SUPPORTED,
                    rationale=rationale,
                    action_taken=ClaimAction.RETAINED.value,
                    retrieval_status=rec.retrieval_status.value if rec else None,
                    raw_sentence=claim.raw_sentence,
                    supporting_urls=[target_url],
                )

            if verdict == ClaimVerdict.CONTRADICTED:
                action_taken = (
                    ClaimAction.REMOVED.value
                    if self.action_policy == "remove"
                    else ClaimAction.LABELED_CONTRADICTED.value
                )
                return ClaimVerificationRecord(
                    claim_id=claim.claim_id,
                    claim_text=claim.claim_text,
                    source_url=target_url,
                    passage_id=passage_id,
                    passage_text=passage_text,
                    verdict=ClaimVerdict.CONTRADICTED,
                    rationale=rationale,
                    action_taken=action_taken,
                    retrieval_status=rec.retrieval_status.value if rec else None,
                    raw_sentence=claim.raw_sentence,
                    supporting_urls=None,
                )

            # Check if this is a compound claim where distinct clauses are supported by different passages
            clauses = claim.constituent_clauses
            if not clauses or len(clauses) <= 1:
                clauses = _decompose_claim_clauses(claim.claim_text)

            if len(clauses) > 1:
                clause_support: Dict[str, Tuple[str, str, str]] = {}
                contradictions: List[Tuple[str, str]] = []

                for clause in clauses:
                    c_text, c_pid, c_rec = find_passage_for_url_and_query(target_url, clause, store)
                    if c_text:
                        c_v, c_rat = self._evaluate_passage(clause, c_text, target_url)
                        if c_v == ClaimVerdict.CONTRADICTED:
                            contradictions.append((c_pid or "", c_rat))
                        elif c_v == ClaimVerdict.SUPPORTED:
                            clause_support[clause] = (c_text, c_pid or "", c_rat)

                if contradictions:
                    c_pid, c_rat = contradictions[0]
                    action_taken = (
                        ClaimAction.REMOVED.value
                        if self.action_policy == "remove"
                        else ClaimAction.LABELED_CONTRADICTED.value
                    )
                    return ClaimVerificationRecord(
                        claim_id=claim.claim_id,
                        claim_text=claim.claim_text,
                        source_url=target_url,
                        passage_id=c_pid,
                        passage_text=passage_text,
                        verdict=ClaimVerdict.CONTRADICTED,
                        rationale=f"Cited source {target_url!r} directly contradicts a clause in the claim: {c_rat}",
                        action_taken=action_taken,
                        retrieval_status=rec.retrieval_status.value if rec else None,
                        raw_sentence=claim.raw_sentence,
                        supporting_urls=None,
                    )

                unsupported_clauses = [c for c in clauses if c not in clause_support]
                if not unsupported_clauses:
                    primary_pid = clause_support[clauses[0]][1]
                    provenance_passage = "\n\n".join(
                        f"[{target_url}] (supports: {c[:60]}...):\n{pt}"
                        for c, (pt, _, _) in clause_support.items()
                    )
                    clause_summary = "; ".join(
                        f"'{c[:40]}...'"
                        for c in clause_support.keys()
                    )
                    return ClaimVerificationRecord(
                        claim_id=claim.claim_id,
                        claim_text=claim.claim_text,
                        source_url=target_url,
                        passage_id=primary_pid,
                        passage_text=provenance_passage,
                        verdict=ClaimVerdict.SUPPORTED,
                        rationale=f"Compound claim is fully supported across multiple passages from {target_url} ({clause_summary}).",
                        action_taken=ClaimAction.RETAINED.value,
                        retrieval_status=rec.retrieval_status.value if rec else None,
                        raw_sentence=claim.raw_sentence,
                        supporting_urls=[target_url],
                    )
                else:
                    supported_summary = (
                        "; ".join(f"'{c[:80]}...'" for c in clause_support.keys())
                        if clause_support
                        else "None"
                    )
                    missing_summary = ", ".join(f"'{c[:50]}...'" for c in unsupported_clauses)
                    action_taken = (
                        ClaimAction.REMOVED.value
                        if self.action_policy == "remove"
                        else ClaimAction.LABELED_UNVERIFIED.value
                    )
                    return ClaimVerificationRecord(
                        claim_id=claim.claim_id,
                        claim_text=claim.claim_text,
                        source_url=target_url,
                        passage_id=passage_id,
                        passage_text=passage_text,
                        verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
                        rationale=(
                            f"Compound claim is partially unsupported: clause(s) [{missing_summary}] "
                            f"lack supporting evidence in cited source {target_url} (supported: {supported_summary})."
                        ),
                        action_taken=action_taken,
                        retrieval_status=rec.retrieval_status.value if rec else None,
                        raw_sentence=claim.raw_sentence,
                        supporting_urls=[target_url] if clause_support else None,
                    )

            action_taken = (
                ClaimAction.REMOVED.value
                if self.action_policy == "remove"
                else ClaimAction.LABELED_UNVERIFIED.value
            )
            return ClaimVerificationRecord(
                claim_id=claim.claim_id,
                claim_text=claim.claim_text,
                source_url=target_url,
                passage_id=passage_id,
                passage_text=passage_text,
                verdict=verdict,
                rationale=rationale,
                action_taken=action_taken,
                retrieval_status=rec.retrieval_status.value if rec else None,
                raw_sentence=claim.raw_sentence,
                supporting_urls=None,
            )

        # 3. Multi-source claim evaluation
        # Step A: Query each cited source for candidate passages
        source_passages: Dict[str, Tuple[Optional[str], Optional[str], Optional[EvidenceRecord]]] = {}
        for u in cited_urls:
            source_passages[u] = find_passage_for_url_and_query(
                u,
                claim.clause_text or claim.claim_text,
                store,
            )

        # Step B: Check for CONTRADICTION across all cited sources with retrieved passages
        contradictions: List[Tuple[str, str, str]] = []
        for u in cited_urls:
            p_text, p_id, rec = source_passages[u]
            if p_text:
                v, rat = self._evaluate_passage(claim.claim_text, p_text, u)
                if v == ClaimVerdict.CONTRADICTED:
                    contradictions.append((u, p_id or "", rat))

        if contradictions:
            conflicting_url, conf_pid, conf_rat = contradictions[0]
            action_taken = (
                ClaimAction.REMOVED.value
                if self.action_policy == "remove"
                else ClaimAction.LABELED_CONTRADICTED.value
            )
            return ClaimVerificationRecord(
                claim_id=claim.claim_id,
                claim_text=claim.claim_text,
                source_url=conflicting_url,
                passage_id=conf_pid,
                passage_text=source_passages[conflicting_url][0],
                verdict=ClaimVerdict.CONTRADICTED,
                rationale=f"Cited source {conflicting_url!r} directly contradicts the claim: {conf_rat}",
                action_taken=action_taken,
                retrieval_status=source_passages[conflicting_url][2].retrieval_status.value if source_passages[conflicting_url][2] else None,
                raw_sentence=claim.raw_sentence,
                supporting_urls=None,
            )

        # Step C: Check if ANY SINGLE SOURCE fully entails the complete claim (single-clause claims only)
        if len(clauses) <= 1:
            fully_supported: List[Tuple[str, str, str, str]] = []
            for u in cited_urls:
                p_text, p_id, rec = source_passages[u]
                if p_text:
                    v, rat = self._evaluate_passage(claim.claim_text, p_text, u)
                    if v == ClaimVerdict.SUPPORTED:
                        fully_supported.append((u, p_id or "", p_text, rat))

            if fully_supported:
                supporting_urls = [u for u, _, _, _ in fully_supported]
                primary_u, primary_pid, primary_ptext, primary_rat = fully_supported[0]

                if len(fully_supported) == 1:
                    provenance_passage = primary_ptext
                    rationale = f"Claim is fully supported by cited source {primary_u} ({primary_rat})."
                else:
                    provenance_passage = "\n\n".join(
                        f"[{u}]:\n{pt}" for u, _, pt, _ in fully_supported
                    )
                    rationale = f"Claim is fully supported across {len(supporting_urls)} cited sources: {', '.join(supporting_urls)}."

                return ClaimVerificationRecord(
                    claim_id=claim.claim_id,
                    claim_text=claim.claim_text,
                    source_url=primary_u,
                    passage_id=primary_pid,
                    passage_text=provenance_passage,
                    verdict=ClaimVerdict.SUPPORTED,
                    rationale=rationale,
                    action_taken=ClaimAction.RETAINED.value,
                    retrieval_status=source_passages[primary_u][2].retrieval_status.value if source_passages[primary_u][2] else None,
                    raw_sentence=claim.raw_sentence,
                    supporting_urls=supporting_urls,
                )

        # Step D: Compound / Joint Entailment across multiple sources
        clauses = claim.constituent_clauses
        if not clauses or len(clauses) <= 1 or claim.claim_text in clauses:
            clauses = _decompose_claim_clauses(claim.claim_text)

        if len(clauses) > 1:
            clause_support: Dict[str, Tuple[str, str, str, str]] = {}
            clause_contradictions: List[Tuple[str, str, str, str]] = []
            clause_evaluations: List[Tuple[str, str, str, str, str]] = []

            for clause in clauses:
                for u in cited_urls:
                    p_text, p_id, rec = find_passage_for_url_and_query(u, clause, store)
                    if p_text:
                        v, rat = self._evaluate_passage(clause, p_text, u)
                        if v == ClaimVerdict.CONTRADICTED:
                            clause_contradictions.append((u, p_id or "", p_text, rat))
                        elif v == ClaimVerdict.SUPPORTED and clause not in clause_support:
                            clause_support[clause] = (u, p_id or "", p_text, rat)
                            break
                        elif v == ClaimVerdict.INSUFFICIENT_EVIDENCE:
                            clause_evaluations.append((clause, u, p_id or "", p_text, rat))

            if clause_contradictions:
                u, p_id, p_text, rat = clause_contradictions[0]
                action_taken = (
                    ClaimAction.REMOVED.value
                    if self.action_policy == "remove"
                    else ClaimAction.LABELED_CONTRADICTED.value
                )
                return ClaimVerificationRecord(
                    claim_id=claim.claim_id,
                    claim_text=claim.claim_text,
                    source_url=u,
                    passage_id=p_id,
                    passage_text=p_text,
                    verdict=ClaimVerdict.CONTRADICTED,
                    rationale=f"Cited source {u!r} directly contradicts a clause in the claim: {rat}",
                    action_taken=action_taken,
                    retrieval_status=source_passages[u][2].retrieval_status.value if source_passages.get(u) and source_passages[u][2] else None,
                    raw_sentence=claim.raw_sentence,
                    supporting_urls=None,
                )

            unsupported_clauses = [c for c in clauses if c not in clause_support]

            if not unsupported_clauses:
                supporting_urls = list(dict.fromkeys(u for u, _, _, _ in clause_support.values()))
                primary_u = supporting_urls[0]
                primary_pid = clause_support[clauses[0]][1]
                provenance_passage = "\n\n".join(
                    f"[{u}] (supports: {c[:60]}...):\n{pt}"
                    for c, (u, _, pt, _) in clause_support.items()
                )
                clause_summary = "; ".join(
                    f"'{c[:80]}...'"
                    for c in clause_support.keys()
                )
                if len(supporting_urls) == 1:
                    rationale = f"Compound claim is fully supported across multiple passages from {primary_u} ({clause_summary})."
                else:
                    clause_provenance = "; ".join(
                        f"'{c[:60]}...' supported by {u}"
                        for c, (u, _, _, _) in clause_support.items()
                    )
                    rationale = f"Compound claim jointly supported across cited sources ({clause_provenance})."

                return ClaimVerificationRecord(
                    claim_id=claim.claim_id,
                    claim_text=claim.claim_text,
                    source_url=primary_u,
                    passage_id=primary_pid,
                    passage_text=provenance_passage,
                    verdict=ClaimVerdict.SUPPORTED,
                    rationale=rationale,
                    action_taken=ClaimAction.RETAINED.value,
                    retrieval_status=source_passages[primary_u][2].retrieval_status.value if source_passages[primary_u][2] else None,
                    raw_sentence=claim.raw_sentence,
                    supporting_urls=supporting_urls,
                )
            else:
                source_label = f"cited source {cited_urls[0]}" if len(cited_urls) == 1 else "cited sources"
                supported_summary = (
                    "; ".join(f"'{c[:140]}...'" if len(c) > 140 else f"'{c}'" for c in clause_support.keys())
                    if clause_support
                    else "None"
                )
                missing_summary = ", ".join(f"'{c[:140]}...'" if len(c) > 140 else f"'{c}'" for c in unsupported_clauses)
                evaluated_missing = [e for e in clause_evaluations if e[0] in unsupported_clauses]
                provenance_pid: Optional[str] = None
                provenance_ptext: Optional[str] = None
                evaluation_note = ""
                if evaluated_missing:
                    eval_clause, eval_url, eval_pid, eval_ptext, eval_rat = evaluated_missing[0]
                    provenance_pid = eval_pid or None
                    provenance_ptext = eval_ptext
                    evaluation_note = (
                        f" Assessed passage from {eval_url!r} for clause "
                        f"'{eval_clause[:80]}': {eval_rat}"
                    )
                action_taken = (
                    ClaimAction.REMOVED.value
                    if self.action_policy == "remove"
                    else ClaimAction.LABELED_UNVERIFIED.value
                )
                return ClaimVerificationRecord(
                    claim_id=claim.claim_id,
                    claim_text=claim.claim_text,
                    source_url=cited_urls[0],
                    passage_id=provenance_pid,
                    passage_text=provenance_ptext,
                    verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
                    rationale=(
                        f"Compound claim is partially unsupported: clause(s) [{missing_summary}] "
                        f"lack supporting evidence in {source_label} (supported: {supported_summary})."
                        f"{evaluation_note}"
                    ),
                    action_taken=action_taken,
                    retrieval_status=source_passages[cited_urls[0]][2].retrieval_status.value if source_passages[cited_urls[0]][2] else None,
                    raw_sentence=claim.raw_sentence,
                    supporting_urls=None,
                )

        # Step E: Unified statement without clear clause boundaries
        evaluated_pid: Optional[str] = None
        evaluated_ptext: Optional[str] = None
        evaluated_note = ""
        valid_passages = [(u, p_id, pt, rec) for u, (pt, p_id, rec) in source_passages.items() if pt]
        if valid_passages:
            combined_ptext = "\n\n".join(f"[{u}]:\n{pt}" for u, _, pt, _ in valid_passages)
            evaluated_pid = valid_passages[0][1]
            evaluated_ptext = combined_ptext
            v_comb, rat_comb = self._evaluate_passage(claim.claim_text, combined_ptext, cited_urls[0])
            evaluated_note = f" Combined evidence assessment from {len(valid_passages)} source(s): {rat_comb}"
            if v_comb == ClaimVerdict.SUPPORTED:
                contributing_urls: List[str] = []
                for u, _, pt, _ in valid_passages:
                    words = re.findall(r"[a-zA-Z0-9_\-]+", claim.claim_text.lower())
                    tokens = {w for w in words if len(w) >= 3 and w not in _STOPWORDS}
                    pt_lower = pt.lower()
                    overlap = sum(1 for t in tokens if t in pt_lower)
                    if overlap >= 1:
                        contributing_urls.append(u)

                if contributing_urls:
                    primary_u = contributing_urls[0]
                    return ClaimVerificationRecord(
                        claim_id=claim.claim_id,
                        claim_text=claim.claim_text,
                        source_url=primary_u,
                        passage_id=valid_passages[0][1],
                        passage_text=combined_ptext,
                        verdict=ClaimVerdict.SUPPORTED,
                        rationale=f"Claim is jointly supported across combined evidence from cited sources: {rat_comb}",
                        action_taken=ClaimAction.RETAINED.value,
                        retrieval_status=source_passages[primary_u][2].retrieval_status.value if source_passages[primary_u][2] else None,
                        raw_sentence=claim.raw_sentence,
                        supporting_urls=contributing_urls,
                    )

        action_taken = (
            ClaimAction.REMOVED.value
            if self.action_policy == "remove"
            else ClaimAction.LABELED_UNVERIFIED.value
        )
        return ClaimVerificationRecord(
            claim_id=claim.claim_id,
            claim_text=claim.claim_text,
            source_url=cited_urls[0],
            passage_id=evaluated_pid,
            passage_text=evaluated_ptext,
            verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
            rationale=(
                f"None of the {len(cited_urls)} cited sources ({', '.join(cited_urls)}) "
                f"provide sufficient evidence to substantiate the claim.{evaluated_note}"
            ),
            action_taken=action_taken,
            retrieval_status=source_passages[cited_urls[0]][2].retrieval_status.value if source_passages[cited_urls[0]][2] else None,
            raw_sentence=claim.raw_sentence,
            supporting_urls=None,
        )


    def _verify_via_mesh_llm(
        self,
        claim_text: str,
        passage_text: str,
        source_url: Optional[str],
    ) -> Tuple[ClaimVerdict, str]:
        """Perform claim verification using the shared Mesh API LLM configuration."""
        prompt = (
            "You are an expert factual claim verification judge for an academic research report.\n"
            "Task: Compare the claim below against the supporting passage retrieved from the cited source.\n\n"
            f'Claim: "{claim_text}"\n'
            f'Source URL: "{source_url}"\n'
            f'Supporting Passage:\n"""\n{passage_text}\n"""\n\n'
            "Classify the relationship into exactly one of three verdicts:\n"
            "1. 'supported': The passage directly substantiates and provides evidence for the factual claim.\n"
            "2. 'contradicted': The passage directly contradicts, refutes, or conflicts with the factual claim.\n"
            "3. 'insufficient_evidence': The passage does not contain sufficient factual evidence to prove or disprove the claim.\n\n"
            "CRITICAL GUIDELINES:\n"
            "- Compare the underlying factual assertions and their relationships, not rhetorical surface structure.\n"
            "- Differences in clause ordering, subordinate clause positioning, active vs. passive voice, or equivalent rhetorical structure (for example, 'While method enhances X, it offers limited gains on Y' versus 'Although method offers limited gains on Y, it enhances X') do NOT constitute contradictions if the underlying factual assertions are entailed. Do not treat variations in clause order or syntactic presentation as swapped causal associations unless the claim explicitly asserts a causal dependency that the passage directly denies.\n"
            "- If all key factual assertions in the claim are affirmed by the passage, classify the verdict as 'supported'.\n"
            "- A wording mismatch, difference in emphasis, lack of specific details, or partial discussion must NEVER be classified as 'contradicted'. If the passage does not directly refute or state facts opposite to the claim, classify it as 'insufficient_evidence'.\n"
            "- Only classify as 'contradicted' when there is an explicit, genuine factual conflict (e.g. claim says 'linear complexity' but passage says 'quadratic complexity'; claim says 'accuracy increased by 20%' but passage says 'accuracy decreased by 5%').\n"
            "- Titles, author names, affiliations, publication metadata, or mere mentions of a topic/method name in a title or heading do NOT substantiate technical claims. Metadata alone must be classified as 'insufficient_evidence'. A claim is only 'supported' if the passage provides substantive statements, definitions, descriptions, empirical results, or explanations establishing the claim.\n"
            "- When context is provided in brackets (such as '[Heading: ...]' or '[Context: ...]'), use that context to resolve antecedents, acronym definitions, or section scope (e.g., establishing which method is being discussed), but require that the substantive assertions in the claim are substantiated by the evidence.\n\n"
            "Respond strictly in valid JSON format with properly escaped strings (escape all backslashes in mathematical symbols or LaTeX expressions, e.g., use \\\\beta, \\\\theta, \\\\rho, \\\\frac, \\\\chi, \\\\alpha, or write plain words):\n"
            "{\n"
            '  "verdict": "supported" | "contradicted" | "insufficient_evidence",\n'
            '  "rationale": "<1-2 sentence explanation of your classification>"\n'
            "}"
        )

        try:
            raw_resp = self.llm.call(prompt)
            resp_str = str(raw_resp).strip()
            parsed: Optional[Dict[str, Any]] = None

            # Attempt JSON decoding with context-aware repair to prevent silent LaTeX corruption
            json_match = re.search(r"\{[\s\S]*\}", resp_str)
            if json_match:
                json_text = json_match.group(0)
                repaired_text = repair_json_escapes(json_text)
                try:
                    parsed = json.loads(repaired_text)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    try:
                        parsed = json.loads(json_text)
                    except Exception:
                        pass

            if isinstance(parsed, dict):
                v_str = str(parsed.get("verdict", "")).lower().strip()
                rat = str(parsed.get("rationale", "")).strip()

                if "support" in v_str and "insufficient" not in v_str:
                    ent_ok, ent_msg = _check_entities_present(claim_text, passage_text)
                    if not ent_ok:
                        return ClaimVerdict.INSUFFICIENT_EVIDENCE, f"LLM reported support, but {ent_msg}."
                    attr_ok, attr_msg, _, _ = _check_subject_attribution(claim_text, passage_text)
                    if not attr_ok:
                        return ClaimVerdict.INSUFFICIENT_EVIDENCE, f"LLM reported support, but {attr_msg}."
                    return ClaimVerdict.SUPPORTED, rat or "Passage provides direct supporting evidence."
                elif "contradict" in v_str:
                    rat_lower = rat.lower()
                    # Check for false contradiction due to clause ordering / syntactic reordering
                    syntax_order_phrases = [
                        "swapping", "reversing the order", "clause order", "swapping the",
                        "order of clauses", "separated as distinct", "swapped", "reordered",
                        "swapping the causal association", "swapped the order", "reverse order",
                    ]
                    # If the rationale cites clause swapping/ordering rather than opposing factual claims,
                    # recheck if key claim concepts are substantiated in the passage
                    if any(sp in rat_lower for sp in syntax_order_phrases) and not any(
                        opp in rat_lower for opp in [
                            "opposite", "conflicts with", "refutes", "decreased", "increased",
                            "instead of", "direct conflict", "contradicts the claim that",
                        ]
                    ):
                        c_words = set(re.findall(r"[a-zA-Z0-9_\-]+", claim_text.lower())) - _STOPWORDS
                        p_lower = passage_text.lower()
                        overlap = sum(1 for w in c_words if len(w) >= 3 and w in p_lower)
                        if overlap / max(1, len(c_words)) >= 0.50:
                            return ClaimVerdict.SUPPORTED, f"Passage substantiates underlying factual assertions (clause order variation reconciled: {rat})"

                    insufficient_phrases = [
                        "does not mention", "does not discuss", "does not contain",
                        "only discusses", "rather than identifying", "no mention",
                        "lacks evidence", "not enough information", "does not explicitly state",
                        "does not provide evidence", "merely fails to", "unclear whether"
                    ]
                    if any(phrase in rat_lower for phrase in insufficient_phrases) and not any(
                        conflict in rat_lower for conflict in ["opposite", "conflicts with", "refutes", "contradicts", "instead states that", "direct conflict"]
                    ):
                        return ClaimVerdict.INSUFFICIENT_EVIDENCE, rat or "Passage lacks sufficient detail to substantiate the claim."
                    return ClaimVerdict.CONTRADICTED, rat or "Passage directly contradicts the claimed statement."
                else:
                    return ClaimVerdict.INSUFFICIENT_EVIDENCE, rat or "Passage does not substantiate the claim."
        except Exception as exc:
            logger.warning("Mesh API LLM verification call failed or could not be parsed: %s", exc)

        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Automated LLM verification produced an unparseable response."

    def _heuristic_fallback_verify(
        self,
        claim_text: str,
        passage_text: str,
    ) -> Tuple[ClaimVerdict, str]:
        """Deterministic rule-based fallback when LLM is unavailable."""
        if _is_pure_title_or_metadata(passage_text):
            return (
                ClaimVerdict.INSUFFICIENT_EVIDENCE,
                "Passage contains only title, author metadata, or publication details rather than substantive evidence.",
            )

        antonym_pairs = [
            (r"\blinear\b", r"\bquadratic\b"),
            (r"\bquadratic\b", r"\blinear\b"),
            (r"\bincreased?\b", r"\bdecreased?\b"),
            (r"\bdecreased?\b", r"\bincreased?\b"),
            (r"\bhigher\b", r"\blower\b"),
            (r"\blower\b", r"\bhigher\b"),
            (r"\bmore\b", r"\bless\b"),
            (r"\bless\b", r"\bmore\b"),
            (r"\breliable\b", r"\bunreliable\b"),
            (r"\bunreliable\b", r"\breliable\b"),
            (r"\bstable\b", r"\bunstable\b"),
            (r"\bunstable\b", r"\bstable\b"),
            (r"\bsupervised\b", r"\bunsupervised\b"),
            (r"\bunsupervised\b", r"\bsupervised\b"),
            (r"\blabeled\b", r"\bunlabeled\b"),
            (r"\bunlabeled\b", r"\blabeled\b"),
            (r"\bconsistent\b", r"\binconsistent\b"),
            (r"\binconsistent\b", r"\bconsistent\b"),
            (r"\bdeterministic\b", r"\bnondeterministic\b"),
            (r"\bnondeterministic\b", r"\bdeterministic\b"),
            (r"\bsynchronous\b", r"\basynchronous\b"),
            (r"\basynchronous\b", r"\bsynchronous\b"),
            (r"\bdurable\b", r"\bnondurable\b"),
            (r"\bnondurable\b", r"\bdurable\b"),
            (r"\bmutable\b", r"\bimmutable\b"),
            (r"\bimmutable\b", r"\bmutable\b"),
            (r"\baccurate\b", r"\binaccurate\b"),
            (r"\binaccurate\b", r"\baccurate\b"),
            (r"\beffective\b", r"\bineffective\b"),
            (r"\bineffective\b", r"\beffective\b"),
            (r"\befficient\b", r"\binefficient\b"),
            (r"\binefficient\b", r"\befficient\b"),
            (r"\blossless\b", r"\blossy\b"),
            (r"\blossy\b", r"\blossless\b"),
        ]
        c_lower = claim_text.lower()
        p_lower = passage_text.lower()

        for c_pattern, p_pattern in antonym_pairs:
            if re.search(c_pattern, c_lower) and re.search(p_pattern, p_lower):
                return (
                    ClaimVerdict.CONTRADICTED,
                    f"Heuristic contradiction detected: claim contains {c_pattern} but passage states {p_pattern}.",
                )

        ent_ok, ent_msg = _check_entities_present(claim_text, passage_text)
        if not ent_ok:
            return ClaimVerdict.INSUFFICIENT_EVIDENCE, f"Heuristic check: {ent_msg}."

        attr_ok, attr_msg, compat_sentences, comp_sentences = _check_subject_attribution(claim_text, passage_text)
        if not attr_ok:
            return ClaimVerdict.INSUFFICIENT_EVIDENCE, f"Heuristic attribution check: {attr_msg}."

        words = re.findall(r"[a-zA-Z0-9_\-]+", c_lower)
        tokens = {w for w in words if len(w) >= 3 and w not in _STOPWORDS}
        if comp_sentences:
            passage_words = set(re.findall(r"[a-zA-Z0-9_\-]+", " ".join(compat_sentences).lower()))
        else:
            passage_words = set(re.findall(r"[a-zA-Z0-9_\-]+", p_lower))

        # Check for explicit negation in the passage of affirmative claim assertions
        for t in tokens:
            c_neg = bool(re.search(r"\b(?:not|never|no|cannot|fails? to|unable to)\s+(?:\w+\s+){0,2}\b" + re.escape(t) + r"\b", c_lower))
            p_neg = bool(re.search(r"\b(?:not|never|no|cannot|fails? to|unable to|does not|did not|is not|are not|was not|were not)\s+(?:\w+\s+){0,2}\b" + re.escape(t) + r"\b", p_lower))
            if not c_neg and p_neg:
                return (
                    ClaimVerdict.CONTRADICTED,
                    f"Heuristic contradiction detected: claim asserts {t!r} affirmatively, but passage explicitly negates it.",
                )

        def _token_matches(t: str) -> bool:
            if t in passage_words:
                return True
            # Block antonym prefixes from falsely counting as positive matches
            for prefix in ("un", "in", "non", "dis", "ir", "im"):
                if (prefix + t) in passage_words and t not in passage_words:
                    return False
            for suffix in ("ness", "ance", "ence", "ing", "tion", "tions", "ment", "ments", "ed", "es", "s", "ant", "ent", "ive", "ity", "able", "ible", "e"):
                if t.endswith(suffix) and len(t) - len(suffix) >= 4:
                    stem = t[:-len(suffix)]
                    for pw in passage_words:
                        if pw.startswith(stem) and len(pw) >= len(stem):
                            if not any(pw.startswith(pre + stem) for pre in ("un", "in", "non", "dis", "ir", "im")):
                                return True
            return False

        overlap = sum(1 for t in tokens if _token_matches(t))
        ratio = overlap / max(1, len(tokens))

        if ratio >= 0.60:
            return ClaimVerdict.SUPPORTED, f"Heuristic match: {overlap}/{len(tokens)} claim concepts confirmed in passage."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, f"Heuristic match insufficient: only {overlap}/{len(tokens)} concepts present in passage."


# ---------------------------------------------------------------------------
# Report Mutation and Application
# ---------------------------------------------------------------------------

def apply_verification_to_report(
    report: str,
    records: List[ClaimVerificationRecord],
    action_policy: str = "label",
) -> str:
    """Update report text by labeling or excising unsupported/contradicted claims.

    Guarantees:
    - Never retains an unsupported claim as verified merely because its citation URL exists.
    - Preserves supported claims.
    - Groups evaluations by sentence so a sentence supported across clauses/citations is retained,
      and unsupported sentences receive at most one clear badge without duplicate stacking.
    """
    modified_report = report

    from collections import OrderedDict
    groups: Dict[str, List[ClaimVerificationRecord]] = OrderedDict()

    for rec in records:
        key = (rec.raw_sentence or rec.claim_text).strip()
        if not key:
            continue
        if key not in groups:
            groups[key] = []
        groups[key].append(rec)

    for sentence_key, group_records in groups.items():
        # 1. Contradiction takes precedence
        contradicted_recs = [r for r in group_records if r.verdict == ClaimVerdict.CONTRADICTED]
        if contradicted_recs:
            first_c = contradicted_recs[0]
            badge = f" *(⚠️ Contradicted by cited source: {first_c.rationale})*"
            if action_policy == "remove":
                if sentence_key in modified_report:
                    modified_report = modified_report.replace(sentence_key, "", 1)
                else:
                    esc = re.escape(first_c.claim_text.strip())
                    pattern = re.compile(rf"[^\n\.\?!]*?{esc}[^\n\.\?!]*?(?:[\.\?!]|\n|$)", re.IGNORECASE)
                    modified_report = pattern.sub("", modified_report, count=1)
            else:
                if sentence_key in modified_report:
                    modified_report = modified_report.replace(sentence_key, f"{sentence_key}{badge}", 1)
                else:
                    esc = re.escape(first_c.claim_text.strip())
                    pattern = re.compile(rf"({esc}(?:\s*\[[\d\s,;\-]+\])?)", re.IGNORECASE)
                    if pattern.search(modified_report):
                        modified_report = pattern.sub(rf"\1{badge}", modified_report, count=1)
            continue

        # 2. Check if all constituent clauses are supported, or if at least one cited source supports the claim
        supported_recs = [r for r in group_records if r.verdict == ClaimVerdict.SUPPORTED]
        insufficient_recs = [r for r in group_records if r.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE]

        # If every claim record for this sentence is supported, sentence is fully supported!
        if len(supported_recs) == len(group_records) and len(supported_recs) > 0:
            continue

        # If this was a single statement citing multiple sources (not multiple clauses), and at least one source supports it:
        unique_claim_texts = {r.claim_text.strip() for r in group_records}
        if len(unique_claim_texts) == 1 and supported_recs:
            continue

        # Otherwise, if some or all clauses lack evidence:
        first_insufficient = insufficient_recs[0] if insufficient_recs else group_records[0]
        badge = f" *(⚠️ Unverified claim: {first_insufficient.rationale})*"

        if action_policy == "remove":
            if sentence_key in modified_report:
                modified_report = modified_report.replace(sentence_key, "", 1)
            else:
                esc = re.escape(first_insufficient.claim_text.strip())
                pattern = re.compile(rf"[^\n\.\?!]*?{esc}[^\n\.\?!]*?(?:[\.\?!]|\n|$)", re.IGNORECASE)
                modified_report = pattern.sub("", modified_report, count=1)
        else:
            if sentence_key in modified_report:
                modified_report = modified_report.replace(sentence_key, f"{sentence_key}{badge}", 1)
            else:
                esc = re.escape(first_insufficient.claim_text.strip())
                pattern = re.compile(rf"({esc}(?:\s*\[[\d\s,;\-]+\])?)", re.IGNORECASE)
                if pattern.search(modified_report):
                    modified_report = pattern.sub(rf"\1{badge}", modified_report, count=1)

    return modified_report



def verify_report_claims(
    report: str,
    store: EvidenceStore,
    verifier: Optional[ClaimVerifier] = None,
    llm: Optional[Any] = None,
    verifier_fn: Optional[Callable[[str, str, Optional[str]], Tuple[ClaimVerdict, str]]] = None,
    action_policy: str = "label",
) -> Tuple[str, ClaimVerificationSummary]:
    """Execute complete claim-level extraction, passage matching, verification, and report updating.

    Parameters
    ----------
    report:
        Draft research report markdown text.
    store:
        The run's EvidenceStore.
    verifier:
        Optional pre-configured ClaimVerifier.
    llm:
        Optional Mesh API LLM instance.
    verifier_fn:
        Optional pluggable/mock verifier function.
    action_policy:
        'label' (default) or 'remove'.

    Returns
    -------
    (updated_report, summary)
    """
    if verifier is None:
        verifier = ClaimVerifier(
            llm=llm,
            verifier_fn=verifier_fn,
            action_policy=action_policy,
        )

    claims = extract_atomic_claims(report)
    records: List[ClaimVerificationRecord] = []
    unverified: List[ClaimVerificationRecord] = []
    warnings: List[str] = []

    supported_count = 0
    contradicted_count = 0
    insufficient_count = 0

    for claim in claims:
        rec = verifier.verify_claim(claim, store)
        records.append(rec)

        if rec.verdict == ClaimVerdict.SUPPORTED:
            supported_count += 1
        elif rec.verdict == ClaimVerdict.CONTRADICTED:
            contradicted_count += 1
            unverified.append(rec)
            warnings.append(
                f"Claim '{claim.claim_text[:60]}...' CONTRADICTED by cited source {claim.source_url}: {rec.rationale}"
            )
        else:
            insufficient_count += 1
            unverified.append(rec)
            warnings.append(
                f"Claim '{claim.claim_text[:60]}...' lacks supporting evidence in source {claim.source_url}: {rec.rationale}"
            )

    updated_report = apply_verification_to_report(report, records, action_policy=action_policy)

    summary = ClaimVerificationSummary(
        total_claims=len(records),
        supported_claims_count=supported_count,
        contradicted_claims_count=contradicted_count,
        insufficient_evidence_claims_count=insufficient_count,
        records=records,
        unverified_claims=unverified,
        warnings=warnings,
    )

    return updated_report, summary


def save_verification_records(
    output_path: Path,
    summary: ClaimVerificationSummary,
) -> None:
    """Save structured verification records to a JSON file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(summary.to_dict(), fh, indent=2, ensure_ascii=False)
    logger.info("Saved claim verification records to %s", output_path)
