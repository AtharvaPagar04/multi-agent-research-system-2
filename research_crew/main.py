"""
main.py
-------
Entry point for the Multi-Agent Research Citation Engine.

Usage
-----
    python -m research_crew.main --topic "attention mechanisms in transformers"
    python -m research_crew.main   # will prompt for a topic interactively

Pipeline
--------
    User Topic
        → Planner Agent   (generate queries)
        → Search Agent    (retrieve sources via Exa / Tavily)
        → Validator Agent (score & filter to top 5)
        → Extractor Agent (fetch content & extract evidence)
        → Synthesizer Agent (produce final Markdown report)

Environment variables required
-------------------------------
    OPENAI_API_KEY   – OpenAI API key (used by all LLM calls)
    EXA_API_KEY      – Exa neural search API key
    TAVILY_API_KEY   – Tavily search API key (fallback)

Optional
--------
    LLM_MODEL        – Override model name (default: gpt-4o)
    LLM_TEMPERATURE  – Override temperature (default: 0.3)
    OUTPUT_FILE      – Path to save the final report (default: research_report.md)
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# Load .env before anything that reads environment variables
load_dotenv()

# Strict telemetry & tracing opt-out configuration for CrewAI (privacy compliance)
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
# Pin fallback OpenAI endpoint to Mesh API to prevent bypassing Mesh
os.environ.setdefault("OPENAI_BASE_URL", "https://api.meshapi.ai/v1")
os.environ.setdefault("OPENAI_API_BASE", "https://api.meshapi.ai/v1")

import httpx
from crewai import Crew, LLM, Process
from crewai.llms.hooks.base import BaseInterceptor

from research_crew.evidence.store import EvidenceStore
from research_crew.evidence.validator import (
    ReportAuditSummary,
    audit_report_citations,
    derive_topic_dimensions,
)
from research_crew.evidence.verifier import (
    ClaimVerificationSummary,
    save_verification_records,
    verify_report_claims,
)
from research_crew.feedback_loop import (
    FeedbackLoopRunLog,
    execute_research_feedback_loop,
    save_feedback_run_log,
)
from research_crew.pipeline_flow import (
    PipelineFlowCoordinator,
    extract_pseudo_tool_calls,
)
from research_crew.tools.adapters import (
    create_adapted_extractor_tools,
    create_adapted_search_tools,
)

# Agents
from research_crew.agents.planner_agent     import build_planner_agent
from research_crew.agents.search_agent      import build_search_agent
from research_crew.agents.validator_agent   import build_validator_agent
from research_crew.agents.extractor_agent   import build_extractor_agent
from research_crew.agents.synthesizer_agent import build_synthesizer_agent

# Tasks
from research_crew.tasks.planning_task    import build_planning_task
from research_crew.tasks.search_task      import build_search_task
from research_crew.tasks.validation_task  import build_validation_task
from research_crew.tasks.extraction_task  import build_extraction_task
from research_crew.tasks.summary_task     import build_summary_task

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("research_crew.log", mode="a"),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Diagnostic Interceptor for Mesh API
# ---------------------------------------------------------------------------
class MeshDiagnosticInterceptor(BaseInterceptor[httpx.Request, httpx.Response]):
    """Captures safe, non-sensitive response metadata from Mesh API completions."""

    def on_outbound(self, request: httpx.Request) -> httpx.Request:
        return request

    def on_inbound(self, response: httpx.Response) -> httpx.Response:
        try:
            raw_bytes = response.read()
            data = json.loads(raw_bytes)
            if isinstance(data, dict) and data.get("choices"):
                choice = data["choices"][0]
                msg = choice.get("message", {})
                content = msg.get("content")
                tool_calls = msg.get("tool_calls")
                finish_reason = choice.get("finish_reason")
                usage = data.get("usage")

                content_empty = (content is None or content == "")
                tool_calls_empty = (not tool_calls)

                # Convert pseudo tool calls emitted by Gemini on Mesh API into standard tool_calls
                if tool_calls_empty and content and "[Assistant called tool" in content:
                    pseudo_calls = extract_pseudo_tool_calls(content)
                    if pseudo_calls:
                        logger.info(
                            "Mesh API diagnostic: converted %d pseudo tool call(s) into standard tool_calls format",
                            len(pseudo_calls),
                        )
                        msg["tool_calls"] = pseudo_calls
                        msg["content"] = None
                        choice["finish_reason"] = "tool_calls"
                        new_bytes = json.dumps(data).encode("utf-8")
                        new_headers = dict(response.headers)
                        new_headers["content-length"] = str(len(new_bytes))
                        return httpx.Response(
                            status_code=response.status_code,
                            headers=new_headers,
                            content=new_bytes,
                            request=response.request,
                        )

                if content_empty and tool_calls_empty:
                    logger.warning(
                        "Mesh API diagnostic: received HTTP 200 with empty message "
                        "(finish_reason=%s, usage=%s, response_id=%s)",
                        finish_reason,
                        usage,
                        data.get("id"),
                    )
                else:
                    logger.debug(
                        "Mesh API diagnostic: response_id=%s finish_reason=%s "
                        "content_len=%d tool_calls=%d",
                        data.get("id"),
                        finish_reason,
                        len(content or ""),
                        len(tool_calls or []),
                    )
        except Exception as exc:
            logger.debug("Mesh diagnostic interceptor could not read response: %s", exc)
        return response


# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------
def _check_env() -> None:
    """Fail early with a helpful message if required env vars are missing."""
    missing = []
    for var in ("MESH_API_KEY", "EXA_API_KEY"):
        if not os.getenv(var):
            missing.append(var)
    if missing:
        logger.error(
            "Missing required environment variables: %s\n"
            "Create a .env file (see .env.example) or export them in your shell.",
            ", ".join(missing),
        )
        sys.exit(1)

    if not os.getenv("TAVILY_API_KEY"):
        logger.warning(
            "TAVILY_API_KEY not set — Tavily fallback search will be unavailable."
        )


# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------
def _build_llm() -> LLM:
    api_key = os.getenv("MESH_API_KEY")
    if not api_key:
        raise RuntimeError("MESH_API_KEY is required")

    model = os.getenv("MESH_MODEL", "openai/gpt-4o")
    temperature = float(os.getenv("LLM_TEMPERATURE", "0"))

    logger.info(
        "Using Mesh API | model=%s | temperature=%.1f",
        model,
        temperature,
    )

    interceptor = MeshDiagnosticInterceptor()
    llm = LLM(
        model=model,
        base_url="https://api.meshapi.ai/v1",
        api_key=api_key,
        provider="openai",
        temperature=temperature,
        timeout=60,
        max_retries=2,
        interceptor=interceptor,
    )

    # Wrap llm.call with a safe, bounded retry for transient empty completions
    orig_call = llm.call

    def safe_call(*args: Any, **kwargs: Any) -> Any:
        max_attempts = 2
        for attempt in range(max_attempts):
            result = orig_call(*args, **kwargs)
            if result is not None and result != "":
                return result
            if attempt + 1 < max_attempts:
                logger.warning(
                    "Mesh API returned empty LLM response on attempt %d/%d; retrying...",
                    attempt + 1,
                    max_attempts,
                )
                time.sleep(1.0)
            else:
                logger.warning(
                    "Mesh API returned empty LLM response after %d attempts; returning empty result to caller.",
                    max_attempts,
                )
        return result

    llm.call = safe_call
    return llm

# ---------------------------------------------------------------------------
# Metadata formatting helper
# ---------------------------------------------------------------------------
def _build_report_metadata(
    audit: ReportAuditSummary,
    store: EvidenceStore,
    elapsed: float,
    model: str = "openai/gpt-4o",
    claim_summary: Optional[ClaimVerificationSummary] = None,
    loop_log: Optional[FeedbackLoopRunLog] = None,
) -> str:
    """Format diagnostic runtime metadata and deterministic audit summary for report footer."""
    metadata = (
        f"\n\n---\n"
        f"*Report generated on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
        f"in {elapsed:.0f}s using model `{model}`.*\n"
        f"*Evidence store run ID: `{store.run_id}` ({len(store)} sources captured).*\n"
        f"*Citation-level evidence depth: {audit.matched_sources_count}/{audit.total_citations_found} cited source URLs verified in store "
        f"({audit.full_text_sources_count} full-text sources, {audit.snippet_only_sources_count} snippet-only sources).*\n"
    )
    if loop_log:
        metadata += (
            f"*Verification feedback loop: {loop_log.total_rounds_executed} additional research round(s) executed "
            f"(outcome: {loop_log.terminated_reason}, budget: {loop_log.max_rounds} rounds).*\n"
        )
    summary_to_use = claim_summary or getattr(audit, "claim_verification_summary", None)
    if summary_to_use and summary_to_use.total_claims > 0:
        metadata += (
            f"*Claim verification: {summary_to_use.supported_claims_count}/{summary_to_use.total_claims} "
            f"factual claims supported by cited source evidence "
            f"({summary_to_use.contradicted_claims_count} contradicted, "
            f"{summary_to_use.insufficient_evidence_claims_count} unverified/insufficient evidence).*\n"
        )
    if getattr(audit, "coverage_summary", None):
        cov = audit.coverage_summary
        cov_details = f"{cov.gap_dimensions_count} gaps identified"
        if getattr(cov, "partially_covered_dimensions_count", 0) > 0:
            cov_details += f", {cov.partially_covered_dimensions_count} partial"
        metadata += (
            f"*Topic dimension coverage: {cov.covered_dimensions_count}/{cov.total_dimensions} "
            f"requested dimensions covered ({cov_details}).*\n"
        )
        if getattr(cov, "missing_comparisons", None):
            for mc in cov.missing_comparisons:
                metadata += f"*Warning: {mc}*\n"
    if audit.metrics_summary and audit.metrics_summary.total_metrics_found > 0:
        metrics = audit.metrics_summary
        metadata += (
            f"*Metrics audit: {metrics.traceable_metrics_count}/{metrics.total_metrics_found} "
            f"reported values found in captured source text; this does not establish full "
            f"experimental verification.*\n"
            f"*Metric breakdown: {metrics.performance_metrics_count} performance benchmarks, "
            f"{metrics.dataset_scale_metrics_count} dataset-scale statistics.*\n"
            f"*Baseline status: {metrics.specified_baseline_count} specified, "
            f"{metrics.explicitly_unspecified_baseline_count} explicitly unspecified, "
            f"{metrics.unlinked_prose_baseline_count} unlinked prose, "
            f"{metrics.missing_baseline_count} missing.*\n"
            f"*Metric-row evidence depth: {metrics.full_text_metric_rows_count}/{metrics.total_metrics_found} metric rows cite full-text sources "
            f"({metrics.snippet_only_metric_rows_count} metric rows cite snippet-only sources).*\n"
        )
    if not audit.has_foundations_section:
        metadata += "*Warning: Report is missing the required 'Core Foundations & Mechanism' section.*\n"
    if audit.unmatched_sources_count > 0:
        metadata += f"*Warning: {audit.unmatched_sources_count} cited source URL(s) were not found in the evidence store.*\n"
    if audit.failed_sources_count > 0:
        metadata += f"*Warning: {audit.failed_sources_count} cited source(s) have FAILED retrieval status.*\n"
    if audit.is_thin_evidence:
        metadata += "*Warning: Evidence coverage flagged as thin for this run.*\n"
    if summary_to_use and summary_to_use.contradicted_claims_count > 0:
        metadata += (
            f"*Warning: {summary_to_use.contradicted_claims_count} claim(s) "
            "directly contradicted by cited source text.*\n"
        )
    if summary_to_use and summary_to_use.insufficient_evidence_claims_count > 0:
        metadata += (
            f"*Warning: {summary_to_use.insufficient_evidence_claims_count} claim(s) "
            "lack supporting evidence in cited sources.*\n"
        )
    if audit.metrics_summary and audit.metrics_summary.explicitly_unspecified_performance_baseline_count > 0:
        metadata += (
            f"*Warning: {audit.metrics_summary.explicitly_unspecified_performance_baseline_count} performance metric(s) "
            "explicitly disclose that the comparison baseline is unspecified.*\n"
        )
    if audit.metrics_summary and audit.metrics_summary.unlinked_prose_baseline_count > 0:
        metadata += (
            f"*Warning: {audit.metrics_summary.unlinked_prose_baseline_count} metric(s) "
            "cite a general prose baseline not explicitly tied to an experimental result.*\n"
        )
    if audit.metrics_summary and audit.metrics_summary.missing_performance_baseline_count > 0:
        metadata += (
            f"*Warning: {audit.metrics_summary.missing_performance_baseline_count} performance metric(s) "
            "omit both a comparison baseline and an explicit disclosure.*\n"
        )
    if audit.metrics_summary and audit.metrics_summary.untraceable_metrics_count > 0:
        metadata += f"*Warning: {audit.metrics_summary.untraceable_metrics_count} metric(s) could not be traced to captured source text.*\n"
    if audit.metrics_summary and audit.metrics_summary.possible_mismatch_count > 0:
        metadata += (
            f"*Warning: {audit.metrics_summary.possible_mismatch_count} metric(s) "
            "flagged with possible claim-to-evidence relevance mismatch.*\n"
        )
    if audit.metrics_summary and audit.metrics_summary.insufficient_evidence_count > 0:
        metadata += (
            f"*Warning: {audit.metrics_summary.insufficient_evidence_count} metric(s) "
            "have insufficient or snippet-only evidence for relevance evaluation.*\n"
        )
    if audit.prose_comparative_warnings:
        for pw in audit.prose_comparative_warnings:
            metadata += f"*Warning: {pw}*\n"
    metadata += "*Audit note: Provenance matching verifies retrieval records; it does not establish semantic factual truth.*\n"
    metadata += (
        "*Prose audit limitation: Heuristic pattern auditing detects unsupported comparative assertions in prose, "
        "but does not perform full natural language semantic verification or entailment on free text; "
        "prose claims not flagged are not guaranteed to be factually verified.*\n"
    )
    return metadata



# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------
def run_research_pipeline(topic: str, store: Optional[EvidenceStore] = None) -> str:
    """Execute the full multi-agent research pipeline for *topic*.

    Parameters
    ----------
    topic:
        The research question or subject to investigate.
    store:
        Optional EvidenceStore instance. If None, a new run store is created
        in EVIDENCE_DIR (default: ./evidence_runs).

    Returns
    -------
    str
        The final Markdown research report.
    """
    logger.info("=" * 70)
    logger.info("Starting research pipeline for topic: %s", topic)
    logger.info("=" * 70)
    start_time = time.time()

    # ── Initialize run evidence store ────────────────────────────────────────
    if store is None:
        evidence_dir = Path(os.getenv("EVIDENCE_DIR", "evidence_runs"))
        store = EvidenceStore.create(output_dir=evidence_dir)
    logger.info("Active evidence store: run_id=%s path=%s", store.run_id, store.filepath)

    try:
        # ── Build shared LLM ────────────────────────────────────────────────────
        llm = _build_llm()

        # ── Instantiate tools with evidence adapters ────────────────────────────
        search_tools = create_adapted_search_tools(store)
        extractor_tools = create_adapted_extractor_tools(store)

        # ── Instantiate agents ──────────────────────────────────────────────────
        logger.info("[1/5] Initialising Planner Agent …")
        planner_agent     = build_planner_agent(llm)

        logger.info("[2/5] Initialising Search Agent …")
        search_agent      = build_search_agent(llm, tools=search_tools)

        logger.info("[3/5] Initialising Validator Agent …")
        validator_agent   = build_validator_agent(llm)

        logger.info("[4/5] Initialising Extractor Agent …")
        extractor_agent   = build_extractor_agent(llm, tools=extractor_tools)

        logger.info("[5/5] Initialising Synthesizer Agent …")
        synthesizer_agent = build_synthesizer_agent(llm)

        # ── Instantiate flow coordinator ────────────────────────────────────────
        flow_coordinator = PipelineFlowCoordinator(store=store, topic=topic)

        # ── Build tasks (chained via context) ───────────────────────────────────
        logger.info("Building task pipeline …")
        specs, _, _ = derive_topic_dimensions(topic=topic)
        resolved_dimensions = [s[0] for s in specs] if specs else None
        planning_task   = build_planning_task(planner_agent, topic)
        search_task     = build_search_task(search_agent, planning_task)
        validation_task = build_validation_task(validator_agent, search_task)
        extraction_task = build_extraction_task(extractor_agent, validation_task)
        summary_task    = build_summary_task(synthesizer_agent, extraction_task, topic, required_dimensions=resolved_dimensions)

        # ── Attach flow callbacks ───────────────────────────────────────────────
        planning_task.callback = flow_coordinator.on_planning_completed
        search_task.callback = flow_coordinator.on_search_completed
        validation_task.callback = flow_coordinator.on_validation_completed
        extraction_task.callback = flow_coordinator.on_extraction_completed
        summary_task.callback = flow_coordinator.on_summary_completed

        # ── Assemble the Crew ───────────────────────────────────────────────────
        crew = Crew(
            agents=[
                planner_agent,
                search_agent,
                validator_agent,
                extractor_agent,
                synthesizer_agent,
            ],
            tasks=[
                planning_task,
                search_task,
                validation_task,
                extraction_task,
                summary_task,
            ],
            process=Process.sequential,   # tasks execute in the order listed
            verbose=True,
            memory=False,                 # keep stateless; evidence flows via task context
            tracing=False,                # disable tracing/spans export for privacy compliance
        )

        # ── Execute ─────────────────────────────────────────────────────────────
        logger.info("Kicking off CrewAI pipeline …")
        result = crew.kickoff()

        elapsed = time.time() - start_time
        logger.info("Pipeline completed in %.1f seconds.", elapsed)

        # CrewAI returns a CrewOutput object; extract the string
        report = str(result)

        # ── Verify pipeline output integrity ────────────────────────────────────
        flow_coordinator.verify_final_report(report)

        # ── Bounded Verification-to-Research Feedback Loop ──────────────────────
        logger.info("Executing bounded verification-to-research feedback loop …")
        action_policy = os.getenv("CLAIM_VERIFICATION_POLICY", "label")
        max_feedback_rounds = int(os.getenv("MAX_RESEARCH_ROUNDS", "2"))
        claim_summary: Optional[ClaimVerificationSummary] = None
        loop_log: Optional[FeedbackLoopRunLog] = None

        try:
            report, claim_summary, loop_log = execute_research_feedback_loop(
                initial_report=report,
                store=store,
                topic=topic,
                llm=llm,
                search_tools=search_tools,
                extractor_tools=extractor_tools,
                max_rounds=max_feedback_rounds,
                action_policy=action_policy,
                planned_queries=getattr(flow_coordinator, "planned_queries", None),
                required_dimensions=resolved_dimensions,
            )

            # Persist feedback loop auditable run log
            loop_log_path = store.filepath.parent / f"feedback_loop_{store.run_id}.json"
            try:
                save_feedback_run_log(loop_log_path, loop_log)
                logger.info("Saved feedback loop run log to %s", loop_log_path)
            except Exception as log_err:
                logger.warning("Failed to save feedback loop run log: %s", log_err)

            # Persist structured claim verification records
            claim_rec_path = store.filepath.parent / f"claim_verifications_{store.run_id}.json"
            try:
                save_verification_records(claim_rec_path, claim_summary)
                logger.info("Saved claim verification records to %s", claim_rec_path)
            except Exception as save_err:
                logger.warning("Failed to save claim verification records: %s", save_err)

        except Exception as loop_err:
            logger.error(
                "Verification feedback loop failed, failing safely: %s",
                loop_err,
                exc_info=True,
            )
            # Fail safely: attempt standalone verification
            try:
                report, claim_summary = verify_report_claims(
                    report=report,
                    store=store,
                    llm=llm,
                    action_policy=action_policy,
                )
            except Exception as verif_err:
                logger.error("Fallback standalone verification failed: %s", verif_err)

        if claim_summary:
            logger.info(
                "Claim verification completed: %d total claims (%d supported, %d contradicted, %d insufficient). Policy: %s",
                claim_summary.total_claims,
                claim_summary.supported_claims_count,
                claim_summary.contradicted_claims_count,
                claim_summary.insufficient_evidence_claims_count,
                action_policy,
            )

        # ── Deterministic Evidence Citation Audit Gate ──────────────────────────
        audit = audit_report_citations(
            report,
            store,
            claim_verification_summary=claim_summary,
            topic=topic,
            planned_queries=getattr(flow_coordinator, "planned_queries", None),
            required_dimensions=resolved_dimensions,
        )

        if audit.total_citations_found == 0:
            raise RuntimeError(
                "Research pipeline failed: final report contains zero citations."
            )
        logger.info(
            "Evidence citation audit: %d citations checked, %d matched in store "
            "(%d full_text, %d snippet_only, %d failed). Thin evidence flag: %s. Warnings: %d",
            audit.total_citations_found,
            audit.matched_sources_count,
            audit.full_text_sources_count,
            audit.snippet_only_sources_count,
            audit.failed_sources_count,
            audit.is_thin_evidence,
            len(audit.warnings),
        )
        for warn in audit.warnings:
            logger.warning("Report evidence audit warning: %s", warn)

        if not audit.has_foundations_section:
            logger.warning(
                "Report quality audit: Report is missing a dedicated '## Core Foundations & Mechanism' section."
            )

        if audit.metrics_summary and audit.metrics_summary.total_metrics_found > 0:
            logger.info(
                "Evidence metrics audit: %d metrics found (%d values traceable to source text; "
                "%d baselines specified, %d explicitly unspecified, %d unlinked prose, %d missing; "
                "relevance: %d supported, %d possible mismatches, %d insufficient; "
                "depth: %d full_text, %d snippet_only). Warnings: %d",
                audit.metrics_summary.total_metrics_found,
                audit.metrics_summary.traceable_metrics_count,
                audit.metrics_summary.specified_baseline_count,
                audit.metrics_summary.explicitly_unspecified_baseline_count,
                audit.metrics_summary.unlinked_prose_baseline_count,
                audit.metrics_summary.missing_baseline_count,
                audit.metrics_summary.supported_relevance_count,
                audit.metrics_summary.possible_mismatch_count,
                audit.metrics_summary.insufficient_evidence_count,
                audit.metrics_summary.full_text_supported_count,
                audit.metrics_summary.snippet_only_supported_count,
                len(audit.metrics_summary.warnings),
            )
            for m_warn in audit.metrics_summary.warnings:
                logger.warning("Report metric audit warning: %s", m_warn)

        # ── Append runtime metadata & audit verification note ───────────────────
        metadata = _build_report_metadata(
            audit=audit,
            store=store,
            elapsed=elapsed,
            model=os.getenv("MESH_MODEL", "openai/gpt-4o"),
            claim_summary=claim_summary,
            loop_log=loop_log,
        )
        return report + metadata
    finally:
        # Guarantee evidence persistence even if execution raised an exception
        try:
            store.flush()
            logger.info("Persisted evidence store (%d sources) to %s", len(store), store.filepath)
        except Exception as flush_err:
            logger.error("Failed to flush evidence store on exit: %s", flush_err)


# ---------------------------------------------------------------------------
# Output helper
# ---------------------------------------------------------------------------
def _save_report(report: str, topic: str) -> Path:
    output_path = Path(os.getenv("OUTPUT_FILE", "research_report.md"))
    output_path.write_text(report, encoding="utf-8")
    logger.info("Report saved to: %s", output_path.resolve())
    return output_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    _check_env()

    parser = argparse.ArgumentParser(
        description="Multi-Agent Research Citation Engine (CrewAI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python -m research_crew.main --topic "transformer attention mechanisms"\n'
            '  python -m research_crew.main --topic "diffusion models for image synthesis" '
            "--output diffusion_report.md\n"
        ),
    )
    parser.add_argument(
        "--topic",
        type=str,
        default=None,
        help="Research topic to investigate (prompted interactively if omitted)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output file path for the Markdown report (overrides OUTPUT_FILE env var)",
    )
    args = parser.parse_args()

    # Override output path if passed on CLI
    if args.output:
        os.environ["OUTPUT_FILE"] = args.output

    # Get topic
    topic = args.topic
    if not topic:
        topic = input("\nEnter research topic: ").strip()
    if not topic:
        logger.error("No research topic provided. Exiting.")
        sys.exit(1)

    # Run
    try:
        report = run_research_pipeline(topic)
    except Exception as exc:
        logger.error("Research pipeline execution failed: %s", exc)
        sys.exit(1)

    # Save and print
    output_path = _save_report(report, topic)
    print("\n" + "=" * 70)
    print("RESEARCH REPORT")
    print("=" * 70)
    print(report)
    print("=" * 70)
    print(f"\nReport saved to: {output_path.resolve()}")


if __name__ == "__main__":
    main()
