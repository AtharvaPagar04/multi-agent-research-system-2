"""
feedback_loop.py
----------------
Bounded verification-to-research feedback loop for the research pipeline.

Core Capabilities:
1. After draft generation, inspect claim verification results and identify gaps/sub-questions.
2. Send targeted queries to search tools / research agent for additional retrieval.
3. Automatically store newly retrieved evidence records in the run's EvidenceStore.
4. Regenerate relevant report sections and reverify affected claims.
5. Limit the process to a configurable maximum number of rounds (default: 2).
6. Terminate early if all retained claims become supported, or safely exhaust retries.
7. Record an auditable log of iterations, queries, new evidence IDs, verdicts, and actions.
8. Enforce that exhausted retries never leave unresolved claims presented as verified.
"""

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from research_crew.evidence.model import (
    EvidenceRecord,
    RetrievalStatus,
    make_passage_id,
    parse_arxiv_id,
)
from research_crew.evidence.store import EvidenceStore
from research_crew.evidence.validator import _STOPWORDS
from research_crew.evidence.verifier import (
    AtomicClaim,
    ClaimAction,
    ClaimVerdict,
    ClaimVerificationRecord,
    ClaimVerificationSummary,
    ClaimVerifier,
    apply_verification_to_report,
    extract_atomic_claims,
    find_supporting_passage,
    verify_report_claims,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auditable Feedback Run Log Structures
# ---------------------------------------------------------------------------

@dataclass
class ClaimResolutionAudit:
    """Tracks whether an individual targeted unverified claim gained relevant new evidence."""

    claim_id: str
    claim_text: str
    gained_relevant_evidence: bool
    evidence_id: Optional[str] = None
    source_url: Optional[str] = None
    verdict_after_recheck: Optional[str] = None
    notes: str = ""
    recheck_claim_id: Optional[str] = None  # claim_id of the re-extracted record matched for THIS round's recheck; positional per round, not stable across rounds.

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim_text": self.claim_text,
            "gained_relevant_evidence": self.gained_relevant_evidence,
            "evidence_id": self.evidence_id,
            "source_url": self.source_url,
            "verdict_after_recheck": self.verdict_after_recheck,
            "notes": self.notes,
            "recheck_claim_id": self.recheck_claim_id,
        }


@dataclass
class IterationLogRecord:
    """Audit record for a single iteration of the feedback loop."""

    iteration_index: int
    queries_issued: List[str]
    new_evidence_ids: List[str]
    claims_checked_count: int
    verdict_breakdown: Dict[str, int]
    records: List[Dict[str, Any]]
    action_taken: str
    targeted_claim_resolutions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iteration_index": self.iteration_index,
            "queries_issued": list(self.queries_issued),
            "new_evidence_ids": list(self.new_evidence_ids),
            "claims_checked_count": self.claims_checked_count,
            "verdict_breakdown": dict(self.verdict_breakdown),
            "records": list(self.records),
            "action_taken": self.action_taken,
            "targeted_claim_resolutions": [
                r if isinstance(r, dict) else r.to_dict()
                for r in self.targeted_claim_resolutions
            ],
        }


@dataclass
class FeedbackLoopRunLog:
    """Complete auditable log of the verification-to-research feedback process."""

    run_id: str
    max_rounds: int
    total_rounds_executed: int
    terminated_reason: str
    iterations: List[IterationLogRecord] = field(default_factory=list)
    final_claims_supported_count: int = 0
    final_unresolved_claims_count: int = 0
    final_action_policy: str = "label"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "max_rounds": self.max_rounds,
            "total_rounds_executed": self.total_rounds_executed,
            "terminated_reason": self.terminated_reason,
            "final_claims_supported_count": self.final_claims_supported_count,
            "final_unresolved_claims_count": self.final_unresolved_claims_count,
            "final_action_policy": self.final_action_policy,
            "iterations": [it.to_dict() for it in self.iterations],
        }


def save_feedback_run_log(output_path: Path, run_log: FeedbackLoopRunLog) -> None:
    """Save the auditable feedback loop log to a JSON file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(run_log.to_dict(), fh, indent=2, ensure_ascii=False)
    logger.info("Saved feedback loop run log to %s", output_path)


# ---------------------------------------------------------------------------
# Gap Analysis & Query Generation
# ---------------------------------------------------------------------------

def _normalize_query(q: str) -> str:
    """Normalize query for robust deduplication across case and whitespace differences."""
    return " ".join(str(q).strip().lower().split())


def identify_gap_queries(
    unverified_claims: List[ClaimVerificationRecord],
    topic: str,
    llm: Optional[Any] = None,
    coverage_gaps: Optional[List[str]] = None,
    executed_queries: Optional[List[str]] = None,
) -> List[str]:
    """Identify specific sub-questions and search queries for unverified claims and coverage gaps."""
    if not unverified_claims and not coverage_gaps:
        return []

    executed_norms = {_normalize_query(q) for q in (executed_queries or [])}

    # 1. LLM-based query formulation (via Mesh API)
    if llm is not None:
        try:
            claim_bullets = "\n".join(
                f"- Claim {i+1}: {c.claim_text} (Section: {getattr(c, 'section', 'Prose')}, Rationale: {c.rationale})"
                for i, c in enumerate(unverified_claims[:5])
            )
            gap_bullets = ""
            if coverage_gaps:
                gap_bullets = "\n" + "\n".join(
                    f"- Missing or partially covered dimension: {g}" for g in coverage_gaps[:5]
                )

            prior_queries_text = ""
            if executed_queries:
                prior_queries_bullet = "\n".join(f"- {q}" for q in executed_queries[-6:])
                prior_queries_text = (
                    f"\nThe following queries have ALREADY been executed and did not yield sufficient new evidence:\n"
                    f"{prior_queries_bullet}\n"
                    "Do NOT repeat these queries or make trivial variations. Formulate NEW, alternative, and more specific "
                    "search queries (e.g. focusing on specific authors, datasets, benchmarks, architectural variants, or formal terminology).\n"
                )

            prompt = (
                f"You are an expert academic research investigator focusing on '{topic}'.\n"
                "The following claims in our draft report lack sufficient evidence or were unverified:\n"
                f"{claim_bullets}\n"
                f"{gap_bullets}\n"
                f"{prior_queries_text}\n"
                "Formulate 1 to 3 specific, targeted academic search queries to retrieve verifiable source evidence.\n"
                "Respond strictly in valid JSON format:\n"
                "{\n"
                '  "queries": [\n'
                '    "<specific search query 1>",\n'
                '    "<specific search query 2>"\n'
                "  ]\n"
                "}"
            )
            resp = llm.call(prompt)
            match = re.search(r"\{[\s\S]*\}", str(resp))
            if match:
                data = json.loads(match.group(0))
                qs = data.get("queries", [])
                if isinstance(qs, list) and qs:
                    clean_qs = []
                    for q in qs:
                        q_str = str(q).strip()
                        if q_str and _normalize_query(q_str) not in executed_norms:
                            clean_qs.append(q_str)
                            executed_norms.add(_normalize_query(q_str))
                    if clean_qs:
                        return clean_qs[:3]
        except Exception as exc:
            logger.warning("LLM gap identification failed: %s; falling back to heuristic query generation", exc)

    # 2. Deterministic heuristic query generation
    queries: List[str] = []
    for idx, c in enumerate(unverified_claims[:3]):
        words = re.findall(r"[a-zA-Z0-9_\-]+", c.claim_text)
        sub_words = [w for w in words if len(w) >= 3 and w.lower() not in _STOPWORDS]

        candidates = []
        if len(sub_words) >= 5:
            candidates.append(f"{topic} " + " ".join(sub_words[:5]))
            candidates.append(f"{topic} " + " ".join(sub_words[2:7]))
            candidates.append(f"{topic} " + " ".join(sub_words[-4:]))
        elif sub_words:
            candidates.append(f"{topic} " + " ".join(sub_words))
            candidates.append(f"{topic} empirical evaluation " + " ".join(sub_words[:3]))
        else:
            candidates.append(f"{topic} {c.claim_text[:50]}")
            candidates.append(f"{topic} benchmark evaluation {c.claim_text[:30]}")

        chosen = None
        for cand in candidates:
            if _normalize_query(cand) not in executed_norms:
                chosen = cand
                break
        if chosen and chosen not in queries:
            queries.append(chosen)
            executed_norms.add(_normalize_query(chosen))

    if coverage_gaps:
        for gap in coverage_gaps[:3]:
            gap_terms = gap.replace("_", " ").strip()
            gap_cands = [
                f"{topic} {gap_terms}",
                f"{topic} empirical {gap_terms} benchmarks",
                f"{topic} {gap_terms} quantitative results",
            ]
            chosen_gap = None
            for gcand in gap_cands:
                if _normalize_query(gcand) not in executed_norms:
                    chosen_gap = gcand
                    break
            if chosen_gap and chosen_gap not in queries:
                queries.append(chosen_gap)
                executed_norms.add(_normalize_query(chosen_gap))

    return queries


identify_research_gaps = identify_gap_queries


# ---------------------------------------------------------------------------
# Targeted Retrieval Execution
# ---------------------------------------------------------------------------

def _route_and_extract_url(url: str, extractor_tools: List[Any], store: EvidenceStore) -> None:
    """Route *url* to the most appropriate extractor tool, with safe fallback.

    Academic paper URLs (e.g. arXiv /abs/, /html/, /pdf/) and direct PDF URLs
    prefer PDF extractor tools first. Generic web URLs prefer web parsers first.
    If the preferred tool fails or is unavailable, falls back safely to secondary extractors.
    """
    pdf_tools = [t for t in extractor_tools if "PDF" in getattr(t, "name", "")]
    web_tools = [t for t in extractor_tools if "Web" in getattr(t, "name", "")]
    other_tools = [t for t in extractor_tools if t not in pdf_tools and t not in web_tools]

    is_arxiv = parse_arxiv_id(url) is not None
    is_pdf = is_arxiv or "/pdf/" in url or url.endswith(".pdf") or "/abs/" in url

    if is_pdf:
        primary_tools = pdf_tools
        fallback_tools = web_tools + other_tools
    else:
        primary_tools = web_tools
        fallback_tools = pdf_tools + other_tools

    extracted = False
    for tool in primary_tools:
        try:
            tool._run(url=url)
            rec = store.get_by_url(url)
            if rec and rec.retrieval_status == RetrievalStatus.FULL_TEXT:
                extracted = True
                break
        except Exception as exc:
            logger.warning("Primary extractor %s failed for %s: %s", getattr(tool, "name", "tool"), url, exc)

    if not extracted and fallback_tools:
        logger.info("Primary extraction unsuccessful for %s; attempting fallback extractor.", url)
        for tool in fallback_tools:
            try:
                tool._run(url=url)
                rec = store.get_by_url(url)
                if rec and rec.retrieval_status == RetrievalStatus.FULL_TEXT:
                    break
            except Exception as exc:
                logger.warning("Fallback extractor %s failed for %s: %s", getattr(tool, "name", "tool"), url, exc)


def execute_targeted_retrieval(
    queries: List[str],
    store: EvidenceStore,
    search_tools: Optional[List[Any]] = None,
    extractor_tools: Optional[List[Any]] = None,
    research_fn: Optional[Callable[[List[str]], List[EvidenceRecord]]] = None,
    upgrade_urls: Optional[List[str]] = None,
) -> List[str]:
    """Execute targeted search queries and record new evidence records in store.

    Args:
        upgrade_urls: Cited source URLs (typically from unverified claims) whose store
            records are SNIPPET_ONLY; each is routed to the best extractor for a
            full-text upgrade. Full-text records are skipped, FAILED records are not
            retried, and at most 12 cited URLs are routed per call.

    Returns
    -------
    List[str]
        IDs of newly added or upgraded evidence records in this retrieval step.
    """
    prior_records: Dict[str, RetrievalStatus] = {r.evidence_id: r.retrieval_status for r in store}

    # Mode A: Mocked / custom research function (for offline testing)
    if research_fn is not None:
        try:
            new_records = research_fn(queries)
            for r in new_records:
                store.add(r)
        except Exception as exc:
            logger.warning("Custom research_fn failed: %s", exc)

    # Mode B: Live adapted search tools and/or extractor tools
    elif search_tools or extractor_tools:
        if search_tools:
            for q in queries:
                for tool in search_tools:
                    try:
                        tool._run(query=q, num_results=5)
                    except Exception as t_err:
                        logger.warning("Search tool %s failed for query %r: %s", getattr(tool, "name", "tool"), q, t_err)

        # For any newly captured snippet records or unextracted URLs, route to best extractor
        if extractor_tools:
            urls_to_extract: List[str] = []
            for rec in store:
                if rec.evidence_id not in prior_records and rec.retrieval_status == RetrievalStatus.SNIPPET_ONLY and rec.source_url:
                    if rec.source_url not in urls_to_extract:
                        urls_to_extract.append(rec.source_url)

            for q in queries:
                q_clean = q.strip()
                if (q_clean.startswith("http://") or q_clean.startswith("https://")) and q_clean not in urls_to_extract:
                    existing_entry = store.get_by_url(q_clean)
                    if existing_entry is None or existing_entry.retrieval_status != RetrievalStatus.FULL_TEXT:
                        urls_to_extract.append(q_clean)

            # Task 13: upgrade cited snippet-only sources of unverified claims.
            # Full-text records are skipped, FAILED records are not retried, and
            # routing is capped at a deterministic 12 URLs per retrieval step.
            _MAX_UPGRADE_URLS_PER_ROUND = 12
            upgrade_candidates: List[str] = []
            for raw_url in upgrade_urls or []:
                candidate = str(raw_url).strip()
                if not candidate or candidate in upgrade_candidates or candidate in urls_to_extract:
                    continue
                existing_entry = store.get_by_url(candidate)
                if existing_entry is None or existing_entry.retrieval_status != RetrievalStatus.SNIPPET_ONLY:
                    continue
                upgrade_candidates.append(candidate)
                if len(upgrade_candidates) >= _MAX_UPGRADE_URLS_PER_ROUND:
                    break
            urls_to_extract.extend(upgrade_candidates)

            for url in urls_to_extract:
                _route_and_extract_url(url, extractor_tools, store)

    new_ids = [
        r.evidence_id for r in store
        if r.evidence_id not in prior_records
        or (prior_records[r.evidence_id] != RetrievalStatus.FULL_TEXT and r.retrieval_status == RetrievalStatus.FULL_TEXT)
    ]
    return new_ids


def evaluate_targeted_claim_evidence_gain(
    claim_rec: ClaimVerificationRecord,
    new_evidence_ids: List[str],
    store: EvidenceStore,
) -> Tuple[bool, Optional[str], Optional[str], str]:
    """Determine whether a targeted unverified claim gained relevant new evidence in this round.

    Rules:
    - If no new evidence was added to store: gained_relevant_evidence = False.
    - Distinguishes FULL_TEXT, SNIPPET_ONLY, and FAILED retrieval.
    - Requires genuine relevance for both full text and search snippets (never accepts
      an off-topic or irrelevant snippet merely because it was captured).
    - Prefers FULL_TEXT evidence over SNIPPET_ONLY evidence.
    - If all new records failed to fetch: records explicit failed retrieval note.
    """
    if not new_evidence_ids:
        return (
            False,
            None,
            None,
            "No new evidence records added in this round (retrieval returned no results or duplicates).",
        )

    full_text_candidate = None
    snippet_candidate = None
    all_failed = True

    for eid in new_evidence_ids:
        rec = store.get(eid)
        if rec is None:
            continue
        if rec.retrieval_status != RetrievalStatus.FAILED:
            all_failed = False

        if rec.retrieval_status == RetrievalStatus.FAILED:
            continue

        temp_claim = AtomicClaim(
            claim_id=claim_rec.claim_id,
            claim_text=claim_rec.claim_text,
            source_url=rec.source_url or rec.final_url,
        )

        # 1. Full-text record evaluation
        if rec.retrieval_status == RetrievalStatus.FULL_TEXT:
            passage, p_id, _ = find_supporting_passage(temp_claim, store)
            if passage is not None:
                full_text_candidate = (
                    rec.evidence_id,
                    rec.source_url,
                    f"Gained relevant new evidence (full_text) from {rec.source_url} (passage {p_id}).",
                )
                break  # Best possible evidence tier found

        # 2. Snippet-only record evaluation: require strict semantic token overlap
        elif rec.retrieval_status == RetrievalStatus.SNIPPET_ONLY and full_text_candidate is None:
            snippet = (rec.snippet_text or rec.captured_text or "").strip()
            if snippet:
                words = re.findall(r"[a-zA-Z0-9_\-]+", claim_rec.claim_text.lower())
                claim_tokens = {w for w in words if len(w) >= 3 and w not in _STOPWORDS}
                snip_lower = snippet.lower()
                snip_words = set(re.findall(r"[a-zA-Z0-9_\-]+", snip_lower))
                matches = claim_tokens.intersection(snip_words)
                score = len(matches) / max(1, len(claim_tokens))
                for w1, w2 in zip(words[:-1], words[1:]):
                    if f"{w1} {w2}" in snip_lower:
                        score += 0.25
                min_matches = 1 if len(claim_tokens) <= 2 else 2
                if score >= 0.20 and len(matches) >= min_matches:
                    p_id = make_passage_id(rec.evidence_id, snippet)
                    snippet_candidate = (
                        rec.evidence_id,
                        rec.source_url,
                        f"Gained relevant new evidence (snippet_only) from {rec.source_url} (passage {p_id}).",
                    )

    if full_text_candidate is not None:
        return (True, full_text_candidate[0], full_text_candidate[1], full_text_candidate[2])

    if snippet_candidate is not None:
        return (True, snippet_candidate[0], snippet_candidate[1], snippet_candidate[2])

    if all_failed:
        return (
            False,
            None,
            None,
            "Retrieved source fetches failed (retrieval status: failed).",
        )

    return (
        False,
        None,
        None,
        "Retrieved new evidence records were irrelevant to this claim (no supporting passage found).",
    )


# ---------------------------------------------------------------------------
# Claim Recheck Matching
# ---------------------------------------------------------------------------
#
# Claim IDs are positional counters reset on every extract_atomic_claims call,
# so identity across feedback rounds rests on claim text. Thresholds below were
# calibrated on 30 historical feedback artifacts (164 targeted rechecks):
# genuine re-extracted pairs reached >= 0.64 token overlap (min observed margin
# 0.14), while the strongest ambiguous/unknown candidate pair was a 0.57/0.57
# tie; 0.55/0.10 sits inside that gap. Exact wording always wins first.
_RECHECK_OVERLAP_CUTOFF = 0.55
_RECHECK_AMBIGUITY_MARGIN = 0.10


def _recheck_token_overlap(a: str, b: str) -> float:
    """Token-set overlap coefficient |A inter B| / min(|A|, |B|) for recheck matching."""
    tokens_a = {w for w in re.findall(r"[a-zA-Z0-9_\-]+", a.lower()) if len(w) >= 3}
    tokens_b = {w for w in re.findall(r"[a-zA-Z0-9_\-]+", b.lower()) if len(w) >= 3}
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / min(len(tokens_a), len(tokens_b))


def _match_recheck_record(
    claim_text: str,
    new_records: List[ClaimVerificationRecord],
    claimed_claim_ids: Optional[Set[str]] = None,
) -> Tuple[Optional[ClaimVerificationRecord], str]:
    """Match a previous-round claim against the current round's re-extracted records.

    Preference order: unique normalized-text match, then a fuzzy token-overlap match
    that is strong, clearly separated from the runner-up, and not already claimed by
    another previous-round record. Claims split into fragments, near-duplicate
    candidates, weak candidates, and claimed collisions are refused.

    Returns
    -------
    Tuple[Optional[ClaimVerificationRecord], str]
        (matched record or None, "" on match or a concise unresolved reason).
    """
    claimed = claimed_claim_ids or set()

    def _norm(text: str) -> str:
        return _normalize_query(text)

    exact_matches = [r for r in new_records if _norm(r.claim_text) == _norm(claim_text)]
    if exact_matches:
        if len(exact_matches) == 1 and exact_matches[0].claim_id not in claimed:
            return exact_matches[0], ""
        return None, f"ambiguous ({max(1, len(exact_matches))} similar re-extracted claim(s))"

    scored = sorted(
        ((_recheck_token_overlap(claim_text, r.claim_text), r) for r in new_records),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_overlap, best_record = scored[0] if scored else (0.0, None)
    runner_overlap = scored[1][0] if len(scored) > 1 else 0.0

    if best_overlap < _RECHECK_OVERLAP_CUTOFF:
        return None, "no matching re-extracted claim"

    if best_overlap - runner_overlap < _RECHECK_AMBIGUITY_MARGIN:
        similar = sum(
            1
            for overlap, _ in scored
            if overlap >= _RECHECK_OVERLAP_CUTOFF and best_overlap - overlap < _RECHECK_AMBIGUITY_MARGIN
        )
        return None, f"ambiguous ({max(1, similar)} similar re-extracted claim(s))"

    if best_record.claim_id in claimed:
        return None, "ambiguous (candidate claim already matched this round)"

    return best_record, ""


# ---------------------------------------------------------------------------
# Report Section Regeneration & Focused Evidence Selection
# ---------------------------------------------------------------------------

def select_focused_evidence_passages(
    record: EvidenceRecord,
    unverified_claims: List[ClaimVerificationRecord],
    coverage_gaps: Optional[List[str]] = None,
    max_length: int = 1200,
) -> str:
    """Select deterministically focused, relevant passages from *record* for *unverified_claims*.

    Guarantees:
    - Retains full text if total content is short (<= 800 chars).
    - If content is longer, segments into paragraphs/sentences and scores relevance
      against non-stopword tokens from unverified claims and coverage gaps.
    - Selects the top relevant passage(s) up to *max_length* characters, preserving
      surrounding context to interpret empirical metrics.
    - Falls back to the opening abstract / summary if no specific keyword match is found.
    - Never dumps entire papers into the prompt.
    """
    text = (record.captured_text or record.snippet_text or "").strip()
    if not text:
        return ""

    if len(text) <= 800:
        return text

    raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    candidate_passages: List[str] = []

    for para in raw_paragraphs:
        if len(para) <= 600:
            candidate_passages.append(para)
        else:
            para_sentences = re.split(r"(?<=[.!?])\s+", para)
            cur_chunk: List[str] = []
            cur_len = 0
            for sent in para_sentences:
                cur_chunk.append(sent)
                cur_len += len(sent)
                if cur_len >= 300:
                    candidate_passages.append(" ".join(cur_chunk))
                    cur_chunk = cur_chunk[-1:]
                    cur_len = len(cur_chunk[0])
            if cur_chunk:
                candidate_passages.append(" ".join(cur_chunk))

    if not candidate_passages:
        candidate_passages = [text[:800]]

    query_texts = [c.claim_text for c in unverified_claims]
    if coverage_gaps:
        query_texts.extend(coverage_gaps)

    combined_query = " ".join(query_texts).lower()
    target_words = re.findall(r"[a-zA-Z0-9_\-]+", combined_query)
    target_tokens = {w for w in target_words if len(w) >= 3 and w not in _STOPWORDS}

    if not target_tokens:
        return candidate_passages[0][:800]

    scored: List[Tuple[float, str]] = []
    for cand in candidate_passages:
        cand_lower = cand.lower()
        cand_words = set(re.findall(r"[a-zA-Z0-9_\-]+", cand_lower))
        matches = target_tokens.intersection(cand_words)
        score = len(matches) / max(1, len(target_tokens))

        for q in query_texts:
            q_words = re.findall(r"[a-zA-Z0-9_\-]+", q.lower())
            for w1, w2 in zip(q_words[:-1], q_words[1:]):
                if f"{w1} {w2}" in cand_lower:
                    score += 0.25

        if any(char.isdigit() for char in combined_query) and any(char.isdigit() for char in cand):
            score += 0.10

        scored.append((score, cand))

    scored.sort(key=lambda x: (x[0], -len(x[1])), reverse=True)

    best_score = scored[0][0] if scored else 0.0
    selected: List[str] = []
    total_len = 0
    for sc, cand in scored:
        if sc <= 0 and selected:
            break
        # Do not include significantly weaker passages if we already have a strong match
        if selected and (sc < best_score * 0.70 or sc < 0.20):
            break
        # Skip author/affiliation metadata blocks
        if re.search(r"@[a-zA-Z0-9_\-\.]+\.[a-zA-Z]{2,}", cand) and re.search(
            r"(?:department|university|author|contact:)", cand, re.IGNORECASE
        ):
            continue
        if any(cand in s or s in cand for s in selected):
            continue
        if total_len + len(cand) > max_length and selected:
            break
        selected.append(cand)
        total_len += len(cand)
        if len(selected) >= 2 or total_len >= max_length:
            break

    if not selected:
        return candidate_passages[0][:800]

    return "\n\n[...]\n\n".join(selected)


def regenerate_report_with_evidence(
    draft_report: str,
    unverified_claims: List[ClaimVerificationRecord],
    new_evidence_records: List[EvidenceRecord],
    topic: str,
    llm: Optional[Any] = None,
    synthesizer_fn: Optional[Callable[..., str]] = None,
    coverage_gaps: Optional[List[str]] = None,
    required_dimensions: Optional[List[str]] = None,
) -> str:
    """Regenerate or update relevant report sections incorporating newly retrieved evidence."""
    if synthesizer_fn is not None:
        try:
            import inspect
            sig = inspect.signature(synthesizer_fn)
            if "required_dimensions" in sig.parameters and ("coverage_gaps" in sig.parameters or len(sig.parameters) >= 5):
                return synthesizer_fn(draft_report, unverified_claims, new_evidence_records, topic, coverage_gaps, required_dimensions)
            elif "coverage_gaps" in sig.parameters or len(sig.parameters) >= 5:
                return synthesizer_fn(draft_report, unverified_claims, new_evidence_records, topic, coverage_gaps)
            return synthesizer_fn(draft_report, unverified_claims, new_evidence_records, topic)
        except Exception as exc:
            logger.warning("Custom synthesizer_fn failed: %s; keeping draft report", exc)
            return draft_report

    if not new_evidence_records:
        return draft_report

    if llm is not None:
        try:
            useful_records = [
                r for r in new_evidence_records
                if r.retrieval_status != RetrievalStatus.FAILED and (r.captured_text or r.snippet_text)
            ]
            ev_bullets = "\n\n".join(
                f"- Source: {r.source_url or r.final_url}"
                + (f" (Title: {r.title})" if r.title else "")
                + f" [{r.retrieval_status.value}]\n"
                + f"  Evidence Passage:\n  {select_focused_evidence_passages(r, unverified_claims, coverage_gaps)}"
                for r in useful_records[:5]
            )
            unver_bullets = "\n".join(f"- {c.claim_text}" for c in unverified_claims[:5])
            gap_bullets = ""
            if coverage_gaps:
                gap_bullets = "\nUncovered topic dimensions needing coverage:\n" + "\n".join(
                    f"- {g}" for g in coverage_gaps[:5]
                )

            dim_instruction = ""
            if required_dimensions:
                dim_names = ", ".join(d.replace("_", " ").title() for d in required_dimensions)
                dim_instruction = (
                    f"\nRequired Topic Dimensions: {dim_names}\n"
                    "Ensure every required dimension is substantively addressed in the report narrative or explicitly documented as evidence-limited.\n"
                    "When addressing computational cost / efficiency, explicitly distinguish measured costs, memory usage, runtime, training compute, and qualitative claims.\n"
                )

            prompt = (
                f"You are an expert research synthesizer updating a draft report on '{topic}'.\n\n"
                f"Previous draft report:\n\"\"\"\n{draft_report}\n\"\"\"\n\n"
                f"Unverified claims needing evidence:\n{unver_bullets}\n"
                f"{gap_bullets}\n"
                f"{dim_instruction}\n"
                f"Newly retrieved evidence:\n{ev_bullets}\n\n"
                "Task:\n"
                "Update the relevant sections of the draft report to incorporate this new evidence. "
                "Ensure every claim cites its supporting source URL inline [N] and add newly cited sources "
                "to the Sources section.\n"
                "Output the complete updated Markdown report."
            )
            resp = llm.call(prompt)
            resp_str = str(resp).strip()
            if resp_str and len(resp_str) > 100:
                return resp_str
        except Exception as exc:
            logger.warning("LLM report regeneration call failed: %s; retaining previous draft", exc)

    return draft_report


update_report_with_evidence = regenerate_report_with_evidence


# ---------------------------------------------------------------------------
# Feedback Loop Orchestrator
# ---------------------------------------------------------------------------

def execute_research_feedback_loop(
    initial_report: str,
    store: EvidenceStore,
    topic: str,
    llm: Optional[Any] = None,
    search_tools: Optional[List[Any]] = None,
    extractor_tools: Optional[List[Any]] = None,
    max_rounds: int = 2,
    action_policy: str = "label",
    verifier: Optional[ClaimVerifier] = None,
    verifier_fn: Optional[Callable[[str, str, Optional[str]], Tuple[ClaimVerdict, str]]] = None,
    research_fn: Optional[Callable[[List[str]], List[EvidenceRecord]]] = None,
    synthesizer_fn: Optional[Callable[..., str]] = None,
    planned_queries: Optional[List[str]] = None,
    required_dimensions: Optional[List[str]] = None,
) -> Tuple[str, ClaimVerificationSummary, FeedbackLoopRunLog]:
    """Execute a bounded verification-to-research feedback loop.

    Parameters
    ----------
    initial_report:
        The initial synthesized research report draft.
    store:
        The active EvidenceStore for this run.
    topic:
        The research topic string.
    llm:
        The shared Mesh API LLM instance.
    search_tools:
        Adapted search tools hooked into store.
    extractor_tools:
        Adapted content extraction tools hooked into store.
    max_rounds:
        Maximum allowed research feedback rounds (default: 2).
    action_policy:
        Final policy for unresolved claims: 'label' (default) or 'remove'.
    verifier:
        Optional pre-configured ClaimVerifier instance.
    verifier_fn:
        Optional mock/deterministic claim verification judge function for offline tests.
    research_fn:
        Optional mock/deterministic retrieval function for offline tests.
    synthesizer_fn:
        Optional mock/deterministic report regeneration function for offline tests.
    planned_queries:
        Optional research plan query strings.
    required_dimensions:
        Optional explicitly requested dimension keys.

    Returns
    -------
    Tuple[final_report, claim_summary, loop_log]
    """
    from research_crew.evidence.validator import audit_report_dimension_coverage

    current_report = initial_report
    loop_log = FeedbackLoopRunLog(
        run_id=store.run_id,
        max_rounds=max_rounds,
        total_rounds_executed=0,
        terminated_reason="pending",
        final_action_policy=action_policy,
    )

    if verifier is None:
        verifier = ClaimVerifier(
            llm=llm,
            verifier_fn=verifier_fn,
            action_policy=action_policy,
        )

    # ── Round 0: Evaluate draft claims and coverage before feedback ──────────
    claims = extract_atomic_claims(current_report)
    initial_records = [verifier.verify_claim(c, store) for c in claims]
    current_records = initial_records
    current_claims = claims
    current_unverified = [r for r in current_records if r.verdict != ClaimVerdict.SUPPORTED]

    # Evaluate topic-aware dimension coverage
    initial_coverage = audit_report_dimension_coverage(
        current_report,
        required_dimensions=required_dimensions,
        topic=topic,
        planned_queries=planned_queries,
    )
    current_coverage_gaps = list(initial_coverage.gap_dimensions) + list(
        getattr(initial_coverage, "partially_covered_dimensions", [])
    )

    initial_breakdown = {
        "supported": sum(1 for r in current_records if r.verdict == ClaimVerdict.SUPPORTED),
        "contradicted": sum(1 for r in current_records if r.verdict == ClaimVerdict.CONTRADICTED),
        "insufficient_evidence": sum(1 for r in current_records if r.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE),
    }

    loop_log.iterations.append(
        IterationLogRecord(
            iteration_index=0,
            queries_issued=[],
            new_evidence_ids=[],
            claims_checked_count=len(current_records),
            verdict_breakdown=initial_breakdown,
            records=[r.to_dict() for r in current_records],
            action_taken="no_claims_extracted" if len(current_records) == 0 else "initial_draft_verification",
        )
    )

    # If no verifiable claims were extracted from the report (Phase 1 zero-claims fail-safe)
    if len(current_records) == 0:
        logger.warning("No factual claims extracted from report; feedback loop terminated without verification.")
        loop_log.terminated_reason = "no_claims_extracted"
        loop_log.total_rounds_executed = 0
        loop_log.final_claims_supported_count = 0
        loop_log.final_unresolved_claims_count = 0

        summary = ClaimVerificationSummary(
            total_claims=0,
            supported_claims_count=0,
            contradicted_claims_count=0,
            insufficient_evidence_claims_count=0,
            records=[],
            unverified_claims=[],
            warnings=["No verifiable factual claims extracted from report."],
        )
        return current_report, summary, loop_log

    # If all claims are already supported AND no coverage gaps remain, terminate immediately
    if len(current_unverified) == 0 and len(current_coverage_gaps) == 0:
        logger.info("All draft claims already supported and coverage complete; feedback loop completed with 0 additional rounds.")
        loop_log.terminated_reason = "all_claims_supported"
        loop_log.total_rounds_executed = 0
        loop_log.final_claims_supported_count = len(current_records)
        loop_log.final_unresolved_claims_count = 0

        final_report = apply_verification_to_report(current_report, current_records, action_policy=action_policy)
        summary = ClaimVerificationSummary(
            total_claims=len(current_records),
            supported_claims_count=len(current_records),
            contradicted_claims_count=0,
            insufficient_evidence_claims_count=0,
            records=current_records,
            unverified_claims=[],
            warnings=[],
        )
        return final_report, summary, loop_log

    all_executed_queries: List[str] = []
    if required_dimensions is None and topic:
        try:
            from research_crew.evidence.validator import derive_topic_dimensions
            specs, _, _ = derive_topic_dimensions(topic=topic, planned_queries=planned_queries, report=initial_report)
            if specs:
                required_dimensions = [s[0] for s in specs]
        except Exception:
            pass

    # ── Iterative Feedback Rounds ───────────────────────────────────────────
    round_idx = 0
    while round_idx < max_rounds and (len(current_unverified) > 0 or len(current_coverage_gaps) > 0):
        round_idx += 1
        logger.info(
            "Feedback loop round %d/%d: %d unverified claims remaining, %d coverage gaps remaining.",
            round_idx,
            max_rounds,
            len(current_unverified),
            len(current_coverage_gaps),
        )

        # 1. Identify sub-questions / queries
        queries = identify_gap_queries(
            current_unverified,
            topic,
            llm=llm,
            coverage_gaps=current_coverage_gaps,
            executed_queries=all_executed_queries,
        )

        # Deduplicate robustly against history and within round
        unique_queries: List[str] = []
        seen_norms = {_normalize_query(q) for q in all_executed_queries}
        for q in queries:
            norm = _normalize_query(q)
            if norm and norm not in seen_norms:
                seen_norms.add(norm)
                unique_queries.append(q)
        all_executed_queries.extend(unique_queries)

        if not unique_queries:
            logger.info("Feedback loop round %d: no new distinct queries could be generated; terminating loop.", round_idx)
            resolutions_zero = [
                ClaimResolutionAudit(
                    claim_id=tc.claim_id,
                    claim_text=tc.claim_text,
                    gained_relevant_evidence=False,
                    evidence_id=None,
                    source_url=None,
                    verdict_after_recheck=tc.verdict.value if isinstance(tc.verdict, ClaimVerdict) else str(tc.verdict),
                    notes="No new distinct queries could be generated (retrieval options exhausted).",
                )
                for tc in current_unverified
            ]
            loop_log.iterations.append(
                IterationLogRecord(
                    iteration_index=round_idx,
                    queries_issued=[],
                    new_evidence_ids=[],
                    claims_checked_count=len(current_records),
                    verdict_breakdown={
                        "supported": sum(1 for r in current_records if r.verdict == ClaimVerdict.SUPPORTED),
                        "contradicted": sum(1 for r in current_records if r.verdict == ClaimVerdict.CONTRADICTED),
                        "insufficient_evidence": sum(1 for r in current_records if r.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE),
                    },
                    records=[r.to_dict() for r in current_records],
                    action_taken="no_new_queries_available",
                    targeted_claim_resolutions=[r.to_dict() for r in resolutions_zero],
                )
            )
            loop_log.terminated_reason = "exhausted_retries"
            break

        # 2. Targeted retrieval
        # Task 13: pre-existing snippet-only sources cited by currently unverified
        # claims are upgraded to full text (stable dedup order; capped in retrieval).
        unverified_claim_ids = {r.claim_id for r in current_unverified}
        upgrade_urls: List[str] = []
        for claim in current_claims:
            if claim.claim_id not in unverified_claim_ids:
                continue
            for cited in claim.cited_urls or []:
                candidate = cited.strip() if isinstance(cited, str) else ""
                if candidate and candidate not in upgrade_urls:
                    upgrade_urls.append(candidate)

        new_ids = execute_targeted_retrieval(
            queries=unique_queries,
            store=store,
            search_tools=search_tools,
            extractor_tools=extractor_tools,
            research_fn=research_fn,
            upgrade_urls=upgrade_urls,
        )

        if len(new_ids) == 0:
            logger.info("Feedback loop round %d yielded zero new evidence records; terminating loop early.", round_idx)
            resolutions_zero = [
                ClaimResolutionAudit(
                    claim_id=tc.claim_id,
                    claim_text=tc.claim_text,
                    gained_relevant_evidence=False,
                    evidence_id=None,
                    source_url=None,
                    verdict_after_recheck=tc.verdict.value if isinstance(tc.verdict, ClaimVerdict) else str(tc.verdict),
                    notes="No new evidence records added in this round (retrieval returned no results or duplicates).",
                )
                for tc in current_unverified
            ]
            loop_log.iterations.append(
                IterationLogRecord(
                    iteration_index=round_idx,
                    queries_issued=unique_queries,
                    new_evidence_ids=[],
                    claims_checked_count=len(current_records),
                    verdict_breakdown={
                        "supported": sum(1 for r in current_records if r.verdict == ClaimVerdict.SUPPORTED),
                        "contradicted": sum(1 for r in current_records if r.verdict == ClaimVerdict.CONTRADICTED),
                        "insufficient_evidence": sum(1 for r in current_records if r.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE),
                    },
                    records=[r.to_dict() for r in current_records],
                    action_taken="no_new_evidence_found",
                    targeted_claim_resolutions=[r.to_dict() for r in resolutions_zero],
                )
            )
            loop_log.terminated_reason = "no_new_evidence"
            break

        # Evaluate evidence gain for each targeted claim before regeneration
        targeted_evals = []
        for tc in current_unverified:
            gained, ev_id, src_url, note = evaluate_targeted_claim_evidence_gain(
                tc, new_ids, store
            )
            targeted_evals.append({
                "claim_id": tc.claim_id,
                "claim_text": tc.claim_text,
                "gained_relevant_evidence": gained,
                "evidence_id": ev_id,
                "source_url": src_url,
                "notes": note,
            })

        new_evidence_recs = [rec for nid in new_ids if (rec := store.get(nid)) is not None]

        # 3. Regenerate / update report sections with newly retrieved evidence
        updated_report = regenerate_report_with_evidence(
            draft_report=current_report,
            unverified_claims=current_unverified,
            new_evidence_records=new_evidence_recs,
            topic=topic,
            llm=llm,
            synthesizer_fn=synthesizer_fn,
            coverage_gaps=current_coverage_gaps,
            required_dimensions=required_dimensions,
        )
        current_report = updated_report

        # 4. Reverify affected claims
        new_claims = extract_atomic_claims(current_report)
        new_records = [verifier.verify_claim(c, store) for c in new_claims]
        current_records = new_records
        current_claims = new_claims
        current_unverified = [r for r in new_records if r.verdict != ClaimVerdict.SUPPORTED]

        # 5. Re-evaluate coverage after follow-up research
        current_coverage = audit_report_dimension_coverage(
            current_report,
            required_dimensions=required_dimensions,
            topic=topic,
            planned_queries=planned_queries,
        )
        current_coverage_gaps = list(current_coverage.gap_dimensions) + list(
            getattr(current_coverage, "partially_covered_dimensions", [])
        )

        targeted_resolutions: List[ClaimResolutionAudit] = []
        claimed_recheck_ids: Set[str] = set()
        for ev in targeted_evals:
            cid = ev["claim_id"]
            ctext = ev["claim_text"]
            matched_record, match_reason = _match_recheck_record(
                ctext, new_records, claimed_claim_ids=claimed_recheck_ids
            )
            if matched_record is not None:
                claimed_recheck_ids.add(matched_record.claim_id)
                recheck_verdict = matched_record.verdict.value
                recheck_claim_id = matched_record.claim_id
            else:
                recheck_verdict = "unresolved_or_removed"
                recheck_claim_id = None

            note = ev["notes"]
            if matched_record is None:
                note += f"; recheck reason: {match_reason}"
            if not ev["gained_relevant_evidence"] and recheck_verdict == "supported":
                note += " (Notice: Claim recheck passed without gaining relevant new evidence from this round)."

            res_audit = ClaimResolutionAudit(
                claim_id=cid,
                claim_text=ctext,
                gained_relevant_evidence=ev["gained_relevant_evidence"],
                evidence_id=ev["evidence_id"],
                source_url=ev["source_url"],
                verdict_after_recheck=recheck_verdict,
                notes=note,
                recheck_claim_id=recheck_claim_id,
            )
            targeted_resolutions.append(res_audit)

        v_breakdown = {
            "supported": sum(1 for r in new_records if r.verdict == ClaimVerdict.SUPPORTED),
            "contradicted": sum(1 for r in new_records if r.verdict == ClaimVerdict.CONTRADICTED),
            "insufficient_evidence": sum(1 for r in new_records if r.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE),
        }

        action_taken = (
            "all_claims_supported"
            if (len(current_unverified) == 0 and len(current_coverage_gaps) == 0)
            else "additional_research_completed"
        )
        loop_log.iterations.append(
            IterationLogRecord(
                iteration_index=round_idx,
                queries_issued=queries,
                new_evidence_ids=new_ids,
                claims_checked_count=len(new_records),
                verdict_breakdown=v_breakdown,
                records=[r.to_dict() for r in new_records],
                action_taken=action_taken,
                targeted_claim_resolutions=[r.to_dict() for r in targeted_resolutions],
            )
        )

        if len(new_records) > 0 and len(current_unverified) == 0 and len(current_coverage_gaps) == 0:
            logger.info("All claims successfully supported and all dimensions covered after round %d.", round_idx)
            loop_log.terminated_reason = "all_claims_supported"
            break
        elif len(new_records) == 0:
            logger.warning("Report regeneration in round %d yielded 0 claims.", round_idx)
            loop_log.terminated_reason = "no_claims_extracted"
            break

    loop_log.total_rounds_executed = round_idx
    if loop_log.terminated_reason == "pending":
        loop_log.terminated_reason = "exhausted_retries"

    # ── Final Enforcement: Never retain unresolved claims as verified ───────
    final_report = apply_verification_to_report(current_report, current_records, action_policy=action_policy)

    supp_count = sum(1 for r in current_records if r.verdict == ClaimVerdict.SUPPORTED)
    contra_count = sum(1 for r in current_records if r.verdict == ClaimVerdict.CONTRADICTED)
    insuff_count = sum(1 for r in current_records if r.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE)

    loop_log.final_claims_supported_count = supp_count
    loop_log.final_unresolved_claims_count = contra_count + insuff_count

    warnings: List[str] = []
    for r in current_unverified:
        warnings.append(f"Unresolved claim after feedback loop: '{r.claim_text[:60]}...' ({r.verdict.value})")
    for g in current_coverage_gaps:
        warnings.append(f"Unresolved coverage gap after feedback loop: dimension '{g}' remained incomplete.")

    summary = ClaimVerificationSummary(
        total_claims=len(current_records),
        supported_claims_count=supp_count,
        contradicted_claims_count=contra_count,
        insufficient_evidence_claims_count=insuff_count,
        records=current_records,
        unverified_claims=current_unverified,
        warnings=warnings,
    )

    return final_report, summary, loop_log
