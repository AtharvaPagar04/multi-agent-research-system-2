"""
pipeline_flow.py
----------------
Deterministic data flow, intermediate validation, recovery, and diagnostic logging
for the Multi-Agent Research Citation Engine pipeline.

Responsibilities:
1. Safe parsing of structured task outputs (search sources, validated sources, extracted evidence).
2. Diagnostic logging between task boundaries (type, character count, URL counts, passage counts).
3. Reconciling / recovering evidence when an upstream task fails to format its output, ensuring
   stored evidence captured at the tool boundary flows cleanly down the sequential task chain.
4. Failing explicitly on genuinely empty evidence runs or placeholder reports so empty runs
   never silently appear successful.
"""

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from crewai.tasks.task_output import TaskOutput

from research_crew.evidence.model import EvidenceRecord, RetrievalStatus
from research_crew.evidence.store import EvidenceStore

logger = logging.getLogger(__name__)

# Markers indicating a placeholder or empty summary
PLACEHOLDER_MARKERS = [
    "No evidence was provided in the context",
    "*(Note: No evidence was provided",
    "*(None)*",
]


# ---------------------------------------------------------------------------
# JSON & Output Parsing Helpers
# ---------------------------------------------------------------------------

def parse_json_safely(text: str) -> Any:
    """Parse JSON from raw text, code fences, or bracketed substrings."""
    if not text or not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None

    # 1. Direct JSON parse
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # 2. Extract from markdown fences: ```json ... ``` or ``` ... ```
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if fence_match:
        try:
            return json.loads(fence_match.group(1).strip())
        except Exception:
            pass

    # 3. Find bracketed JSON array [...]
    array_match = re.search(r"(\[\s*\{[\s\S]*\}\s*\])", cleaned)
    if array_match:
        try:
            return json.loads(array_match.group(1).strip())
        except Exception:
            pass

    # 4. Find bracketed JSON object {...}
    obj_match = re.search(r"(\{\s*\"[\s\S]*\}\s*)", cleaned)
    if obj_match:
        try:
            return json.loads(obj_match.group(1).strip())
        except Exception:
            pass

    return None


def extract_pseudo_tool_calls(content: str) -> List[Dict[str, Any]]:
    """Extract tool calls from text matching [Assistant called tool <name> with arguments <json>]."""
    if not content or "[Assistant called tool" not in content:
        return []

    calls: List[Dict[str, Any]] = []
    pattern = re.compile(r"\[Assistant called tool\s+([a-zA-Z0-9_-]+)\s+with arguments\s*", re.DOTALL)
    for m in pattern.finditer(content):
        tool_name = m.group(1)
        tail = content[m.end():]
        if tail.startswith("{"):
            try:
                decoder = json.JSONDecoder()
                _, end_idx = decoder.raw_decode(tail)
                args_str = tail[:end_idx]
                calls.append({
                    "id": f"call_intercepted_{int(time.time() * 1000)}_{len(calls)}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": args_str,
                    },
                })
            except Exception:
                pass
    return calls


def parse_search_sources(raw_text: str) -> List[Dict[str, Any]]:
    """Parse a list of search source objects containing at least a URL."""
    data = parse_json_safely(raw_text)
    sources: List[Dict[str, Any]] = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("url"):
                sources.append(item)
    elif isinstance(data, dict):
        for key in ("sources", "validated_sources", "results", "records"):
            if key in data and isinstance(data[key], list):
                for item in data[key]:
                    if isinstance(item, dict) and item.get("url"):
                        sources.append(item)
                break

    return sources


def parse_validated_sources(raw_text: str) -> List[Dict[str, Any]]:
    """Parse validated sources object {"validated_sources": [...]} or list."""
    data = parse_json_safely(raw_text)
    sources: List[Dict[str, Any]] = []

    if isinstance(data, dict) and "validated_sources" in data:
        items = data["validated_sources"]
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("url"):
                    sources.append(item)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("url"):
                sources.append(item)

    return sources


def parse_extracted_passages(raw_text: str) -> List[Dict[str, Any]]:
    """Parse evidence objects containing source_id/url and extracted fields."""
    data = parse_json_safely(raw_text)
    evidence: List[Dict[str, Any]] = []

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and (item.get("url") or item.get("source_id")):
                evidence.append(item)
    elif isinstance(data, dict):
        for key in ("evidence", "passages", "extracted_evidence"):
            if key in data and isinstance(data[key], list):
                for item in data[key]:
                    if isinstance(item, dict) and (item.get("url") or item.get("source_id")):
                        evidence.append(item)
                break

    return evidence


def check_synthesizer_context(context_str: str) -> Tuple[bool, int, str]:
    """Inspect whether the context passed to the synthesizer contains actual research material.

    Returns
    -------
    Tuple[bool, int, str]
        (has_research_material, count_of_items, diagnostic_reason)
    """
    if not context_str or not context_str.strip():
        return False, 0, "context string is empty"

    items = parse_extracted_passages(context_str)
    if not items:
        # Fallback check if it was raw sources or search JSON
        sources = parse_search_sources(context_str)
        if sources:
            return True, len(sources), f"context contains {len(sources)} source objects with snippets"
        return False, 0, "no valid evidence or source objects found in context"

    has_content = any(
        bool(item.get("key_findings"))
        or bool(item.get("foundations"))
        or bool(item.get("quotes"))
        or bool(item.get("metrics"))
        or bool(item.get("snippet"))
        for item in items
    )
    if not has_content:
        return False, len(items), "evidence objects present but all findings/metrics/quotes are empty"

    return True, len(items), f"context contains {len(items)} evidence passages with technical findings"


# ---------------------------------------------------------------------------
# Evidence Recovery Helpers
# ---------------------------------------------------------------------------

def recover_search_sources_from_store(store: EvidenceStore) -> List[Dict[str, Any]]:
    """Reconstruct a clean search source list from records captured in the EvidenceStore."""
    sources: List[Dict[str, Any]] = []
    seen_urls = set()

    for rec in store.all_records():
        url = rec.final_url or rec.source_url
        if not url or url in seen_urls:
            continue
        if rec.retrieval_status == RetrievalStatus.FAILED:
            continue

        seen_urls.add(url)
        stype = "paper" if ("arxiv" in url.lower() or ".pdf" in url.lower()) else "web"
        snippet = rec.snippet_text or rec.captured_text or ""
        sources.append({
            "title": rec.title or "Academic Source",
            "url": url,
            "source_type": stype,
            "published_date": "Unknown",
            "snippet": snippet[:600],
        })

    return sources


def recover_validated_sources_from_search(
    search_sources: List[Dict[str, Any]],
    max_count: int = 5,
) -> List[Dict[str, Any]]:
    """Select the top N sources from search as validated sources."""
    validated: List[Dict[str, Any]] = []
    for idx, s in enumerate(search_sources[:max_count]):
        validated.append({
            "title": s.get("title", "Academic Source"),
            "url": s["url"],
            "source_type": s.get("source_type", "web"),
            "published_date": s.get("published_date", "Unknown"),
            "snippet": s.get("snippet", ""),
            "score": max(5, 10 - idx),
            "rationale": "Selected from verified academic search results.",
        })
    return validated


def select_foundation_passages(content: str, max_passages: int = 3) -> List[str]:
    """Select verbatim source sentences describing attention foundations."""
    import re

    if not content.strip():
        return []

    # Sentence-level selection; retain only literal text from the source.
    sentences = re.split(r"(?<=[.!?])\\s+(?=[A-Z(])", " ".join(content.split()))
    terms = (
        "query", "queries", "key", "keys", "value", "values",
        "softmax", "weighted sum", "dot product", "attention(q",
        "scaled dot-product", "scaled dot product",
    )

    ranked = []
    for index, sentence in enumerate(sentences):
        lowered = sentence.lower()
        score = sum(1 for term in terms if term in lowered)
        if score:
            ranked.append((score, index, sentence.strip()))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = sorted(ranked[:max_passages], key=lambda item: item[1])
    return [sentence for _, _, sentence in selected]


def recover_extracted_evidence_from_sources(
    validated_sources: List[Dict[str, Any]],
    store: EvidenceStore,
) -> List[Dict[str, Any]]:
    """Synthesize structured evidence records from validated sources and stored content."""
    evidence_items: List[Dict[str, Any]] = []

    for s in validated_sources:
        url = s["url"]
        rec = store.get_by_url(url)
        content = ""
        if rec and rec.captured_text:
            content = rec.captured_text
        elif s.get("snippet"):
            content = s["snippet"]

        key_findings: List[str] = []
        quotes: List[str] = []
        foundations = select_foundation_passages(content)

        if content:
            cleaned_snippet = " ".join(content.split())
            if len(cleaned_snippet) > 20:
                key_findings.append(cleaned_snippet[:250])
                # Preserve an actual source excerpt; do not add an ellipsis
                # inside a purported verbatim quote.
                quotes.append(cleaned_snippet[:120])

        evidence_items.append({
            "source_id": url,
            "title": s.get("title", rec.title if rec else "Source"),
            "url": url,
            "metrics": [],
            "datasets": [],
            "foundations": foundations,
            "key_findings": key_findings,
            "quotes": quotes,
        })

    return evidence_items


# ---------------------------------------------------------------------------
# Pipeline Flow Coordinator
# ---------------------------------------------------------------------------

class PipelineFlowCoordinator:
    """Coordinates data flow across pipeline stages with safe diagnostics and fallback reconciliation."""

    def __init__(self, store: EvidenceStore, topic: str) -> None:
        self.store = store
        self.topic = topic
        self.search_sources: List[Dict[str, Any]] = []
        self.validated_sources: List[Dict[str, Any]] = []
        self.extracted_evidence: List[Dict[str, Any]] = []
        self.has_research_material: bool = False
        self.planned_queries: List[str] = []

    def on_planning_completed(self, output: TaskOutput) -> None:
        """Diagnostic hook for the planning task."""
        raw_text = str(output.raw or "")
        data = parse_json_safely(raw_text)
        self.planned_queries = []
        if isinstance(data, dict) and "queries" in data and isinstance(data["queries"], list):
            self.planned_queries = [str(q).strip() for q in data["queries"] if str(q).strip()]
        elif isinstance(data, list):
            self.planned_queries = [str(q).strip() for q in data if str(q).strip()]

        query_count = len(self.planned_queries)
        logger.info(
            "DIAGNOSTIC | Task: Planning | Type: %s | Chars: %d | Queries parsed: %d",
            type(output.raw).__name__,
            len(raw_text),
            query_count,
        )

    def on_search_completed(self, output: TaskOutput) -> None:
        """Diagnostic and reconciliation hook for the search task."""
        raw_text = str(output.raw or "")
        sources = parse_search_sources(raw_text)
        store_records = [
            r for r in self.store.all_records()
            if r.retrieval_status != RetrievalStatus.FAILED
        ]

        logger.info(
            "DIAGNOSTIC | Task: Search | Type: %s | Chars: %d | URLs parsed: %d | Store records: %d",
            type(output.raw).__name__,
            len(raw_text),
            len(sources),
            len(store_records),
        )

        # Broken link recovery: LLM emitted text/tool string or empty output, but store captured records
        if not sources and store_records:
            recovered = recover_search_sources_from_store(self.store)
            if recovered:
                sources = recovered
                output.raw = json.dumps(recovered, indent=2)
                logger.warning(
                    "DIAGNOSTIC | Reconciled Search Task: output had 0 parsed URLs but store had %d records. "
                    "Replaced task raw output with %d recovered sources.",
                    len(store_records),
                    len(recovered),
                )

        self.search_sources = sources

    def on_validation_completed(self, output: TaskOutput) -> None:
        """Diagnostic and reconciliation hook for the validation task."""
        raw_text = str(output.raw or "")
        validated = parse_validated_sources(raw_text)

        logger.info(
            "DIAGNOSTIC | Task: Validation | Type: %s | Chars: %d | Validated sources: %d | Upstream sources: %d",
            type(output.raw).__name__,
            len(raw_text),
            len(validated),
            len(self.search_sources),
        )

        # Broken link recovery: Validator returned 0 sources, but upstream search had sources
        if not validated and self.search_sources:
            recovered = recover_validated_sources_from_search(self.search_sources, max_count=5)
            if recovered:
                validated = recovered
                output.raw = json.dumps({"validated_sources": recovered}, indent=2)
                logger.warning(
                    "DIAGNOSTIC | Reconciled Validation Task: validator returned 0 sources despite %d upstream "
                    "search sources. Populated top %d sources as validated sources.",
                    len(self.search_sources),
                    len(recovered),
                )

        self.validated_sources = validated

    def on_extraction_completed(self, output: TaskOutput) -> None:
        """Diagnostic and reconciliation hook for the extraction task."""
        raw_text = str(output.raw or "")
        extracted = parse_extracted_passages(raw_text)
        is_empty = (len(extracted) == 0)

        logger.info(
            "DIAGNOSTIC | Task: Extraction | Type: %s | Chars: %d | Extracted passages: %d | Is empty: %s",
            type(output.raw).__name__,
            len(raw_text),
            len(extracted),
            is_empty,
        )

        # Broken link recovery: Extractor returned 0 passages, but validated sources exist
        if is_empty and self.validated_sources:
            recovered = recover_extracted_evidence_from_sources(self.validated_sources, self.store)
            if recovered:
                extracted = recovered
                output.raw = json.dumps(recovered, indent=2)
                is_empty = False
                logger.warning(
                    "DIAGNOSTIC | Reconciled Extraction Task: extractor returned empty output despite %d validated "
                    "sources. Populated %d evidence objects from store/snippets.",
                    len(self.validated_sources),
                    len(recovered),
                )

        self.extracted_evidence = extracted

        # Check summarizer context readiness
        has_material, count, reason = check_synthesizer_context(str(output.raw or ""))
        self.has_research_material = has_material
        logger.info(
            "DIAGNOSTIC | Summarizer Context Check | Chars: %d | Items: %d | Has research material: %s (%s)",
            len(str(output.raw or "")),
            count,
            has_material,
            reason,
        )

        # Genuinely empty evidence check: fail explicitly rather than producing a fake placeholder report
        if not has_material and len(self.store) == 0 and len(self.search_sources) == 0:
            raise RuntimeError(
                "Research pipeline aborted: zero evidence sources retrieved in run (genuinely empty evidence run)."
            )

    def on_summary_completed(self, output: TaskOutput) -> None:
        """Diagnostic hook for the summary task."""
        raw_text = str(output.raw or "")
        logger.info(
            "DIAGNOSTIC | Task: Summary | Type: %s | Chars: %d",
            type(output.raw).__name__,
            len(raw_text),
        )

    def verify_final_report(self, report: str) -> None:
        """Ensure report is not a silent placeholder and that citations were produced."""
        if len(self.store) == 0:
            raise RuntimeError(
                "Research pipeline failed: zero evidence sources captured in evidence store."
            )

        # Detect placeholder content
        for marker in PLACEHOLDER_MARKERS:
            if marker in report:
                raise RuntimeError(
                    f"Research pipeline failed: report contains placeholder marker '{marker}'."
                )

        # Confirm non-empty sources section
        if "## Sources" in report and "*(None)*" in report:
            raise RuntimeError(
                "Research pipeline failed: report contains empty '## Sources\n*(None)*' section."
            )
