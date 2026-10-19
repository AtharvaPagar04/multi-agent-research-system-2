"""
summary_task.py
---------------
Task definition for the Research Synthesizer Agent.

This is the final task in the pipeline. It takes the structured evidence
from the Extraction Task and produces the polished Markdown research report.

Anti-hallucination enforcement
-------------------------------
The task description contains explicit hard rules that the agent must obey:
  1. Every factual claim cites a source from the evidence list.
  2. No external knowledge may be used unless it is already present in evidence.
  3. If evidence is absent for a section, the section is omitted.
"""

from typing import List, Optional

from crewai import Task


def build_summary_task(
    synthesizer_agent,
    extraction_task: Task,
    topic: str,
    required_dimensions: Optional[List[str]] = None,
) -> Task:
    """Create the Research Summary Task.

    Parameters
    ----------
    synthesizer_agent:
        The Research Synthesizer Agent instance.
    extraction_task:
        The completed Evidence Extraction Task whose output is the evidence array.
    topic:
        The original user research topic (inserted into the report title).
    required_dimensions:
        Optional list of resolved topic dimension keys that must be substantively addressed.

    Returns
    -------
    Task
        Configured CrewAI Task — the terminal node of the pipeline.
    """
    if required_dimensions is None and topic:
        try:
            from research_crew.evidence.validator import derive_topic_dimensions
            specs, _, _ = derive_topic_dimensions(topic=topic)
            if specs:
                required_dimensions = [s[0] for s in specs]
        except Exception:
            pass

    dim_block = ""
    if required_dimensions:
        dim_lines = [f"  • {d.replace('_', ' ').title()}" for d in required_dimensions]
        dim_block = (
            "REQUIRED TOPIC DIMENSIONS — you must address each of these in the report:\n"
            + "\n".join(dim_lines) + "\n\n"
            "Dimension Coverage Requirements:\n"
            "  • Substantive Coverage: Every required dimension above must receive substantive discussion backed by retrieved evidence.\n"
            "  • Explicit Limitation: If the retrieved evidence is insufficient to address a required dimension, you MUST explicitly state that available evidence is insufficient or limited in the retrieved literature (e.g. 'Evidence on [Dimension] is limited in the retrieved sources; empirical measurements were not reported.'). Do NOT omit the dimension silently, do NOT invent estimates, and do NOT fill gaps with ungrounded general knowledge.\n"
            "  • Special Rule for Computational Cost / Resource Overhead (when applicable):\n"
            "    Explicitly distinguish measured quantitative costs (e.g. GPU hours, VRAM consumption, peak memory, throughput), memory usage, runtime, training compute, and qualitative claims (e.g. 'RL-free', 'simpler architecture'). If quantitative benchmarks are absent in the retrieved evidence, explicitly state that empirical cost measurements were not reported in the evaluated sources.\n\n"
        )

    return Task(
        description=(
            f"You will receive a JSON array of evidence objects from the Extraction Agent.\n"
            f"The research topic is: '{topic}'\n\n"

            "HARD RULES — violating any of these is a critical failure:\n"
            "  1. ONLY cite sources whose 'url' appears in the evidence array.\n"
            "  2. NEVER add facts that are not present in the evidence.\n"
            "  3. Every factual sentence must end with an inline citation [N].\n"
            "  4. Directly answer the user's topic first before discussing optimizations.\n"
            "  5. If evidence for a required dimension is absent, explicitly state that evidence is limited rather than inventing facts.\n"
            "  6. Traceability: Every metric in the metrics table must come from the evidence with its comparison baseline.\n\n"

            f"{dim_block}"

            "Produce a research summary in the following Markdown structure:\n\n"

            "---\n"
            f"# Research Summary: {topic}\n\n"
            "## Core Foundations & Mechanism\n"
            "<Directly address and answer the user's research topic before presenting optimizations, variants, or benchmarks. "
            "For topics explaining an architecture or mechanism (such as transformer attention), provide a clear, concise "
            "explanation of the foundational concepts whenever supported by the retrieved evidence:\n"
            "  • Query, Key, and Value (Q, K, V) vectors / projections and their functional roles.\n"
            "  • Scaled Dot-Product Attention: the mathematical formulation calculating pairwise affinities scaled by sqrt(d_k).\n"
            "  • Softmax Normalization: mapping similarity scores into a probability distribution over sequence positions.\n"
            "  • Multi-Head Attention: projecting queries, keys, and values into multiple representation subspaces to attend jointly.\n"
            "Ensure every sentence cites its supporting evidence source [N]. If specific foundational details are not present in the "
            "retrieved evidence, state clearly what the evidence supports rather than inventing facts.>\n\n"

            "## Key Insights\n"
            "1. **<Headline>**\n"
            "   <Evidence: 2–4 sentences focusing first on operational principles, followed by practical implications or optimizations.>\n"
            "   *Source: [1]*\n\n"
            "2. ...\n\n"
            "## Methodology Overview  *(omit if no methodology evidence)*\n"
            "<Concise description drawn from key_findings and methodology details in the evidence.>\n\n"
            "## Benchmarks & Metrics  *(omit if no metrics evidence)*\n"
            "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
            "|----------------------|-------|-------------------------------|--------|\n"
            "| ...                  | ...   | ...                           | [N]    |\n\n"
            "Rules for reported metrics:\n"
            "  • Traceability: Every reported metric MUST be traceable to a specific source passage or extraction record.\n"
            "  • Baseline & Context: Explicitly specify what the metric measures, the model/dataset, and its comparison baseline "
            "(e.g., 'Speedup vs standard PyTorch eager attention on GPT-2 1.5B', 'Perplexity on OpenWebText vs 125M dense Transformer').\n"
            "  • Ambiguity Handling: If the evidence does not establish what a metric measures or its comparison baseline, "
            "either omit the metric or explicitly record the ambiguity under Context & Comparison Baseline (e.g., '[Baseline unspecified in source text]').\n"
            "  • Contradictions: Never invent evidence or silently reconcile contradictory values; report discrepancies explicitly.\n\n"
            "## Sources\n"
            "[1] <title>\n"
            "    <url>\n\n"
            "[2] ...\n"
            "---\n\n"

            "Formatting rules:\n"
            "  • Minimum 3 Key Insights\n"
            "  • Source index [N] corresponds to position in the evidence array (1-based)\n"
            "  • URLs must be copied verbatim from the evidence — do not paraphrase\n"
            "  • Do not include a 'References' section separate from 'Sources'\n"
        ),
        expected_output=(
            "A complete Markdown research report with sections: "
            "Core Foundations & Mechanism, Key Insights (≥3), optional Methodology Overview, "
            "optional Benchmarks & Metrics table (with Context & Comparison Baseline column), "
            "and a numbered Sources list. "
            "All facts cite evidence-list sources only. No hallucinated citations or untraceable metrics."
        ),
        agent=synthesizer_agent,
        context=[extraction_task],
    )
