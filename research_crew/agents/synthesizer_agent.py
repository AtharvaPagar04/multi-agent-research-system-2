"""
synthesizer_agent.py
--------------------
Research Synthesizer Agent — Research Writer

Responsibilities
----------------
Receives the structured evidence records from the Extraction Agent and
produces a well-organised research summary with inline citations.

Anti-hallucination rules (enforced in task prompt and backstory)
----------------------------------------------------------------
* ONLY cite sources that appear in the provided evidence JSON.
* Every factual claim must be followed by [N] where N is the source index.
* If evidence is missing for a claim, omit the claim entirely.

Output format
-------------
The synthesizer emits plain Markdown:

    # Research Summary: <Topic>

    ## Key Insights

    1. **<Insight headline>**
       <Supporting evidence, 2–4 sentences.>
       *Source: [1]*

    2. ...

    ## Methodology Overview
    <Optional section if methodology snippets were extracted.>

    ## Benchmarks & Metrics
    <Table or bullet list of quantitative results, if available.>

    ## Sources

    [1] <Title>
        <URL>

    [2] ...

Design notes
------------
* No external tools — the agent works purely from the evidence JSON context.
* The task's expected_output schema further constrains the format.
* Temperature is low (0.2) to minimise hallucination risk.
"""

import logging

from crewai import Agent, LLM

logger = logging.getLogger(__name__)


def build_synthesizer_agent(llm: LLM) -> Agent:
    """Construct and return the Research Synthesizer Agent.

    Parameters
    ----------
    llm:
        A configured CrewAI LLM instance.

    Returns
    -------
    Agent
        Ready-to-use CrewAI Agent.
    """
    logger.info("Initialising Research Synthesizer Agent.")

    return Agent(
        role="Research Writer",
        goal=(
            "Synthesise the extracted evidence into a clear, well-structured research "
            "summary in Markdown format. First directly address and explain the core topic and its "
            "foundational mechanism (e.g. Query/Key/Value vectors, scaled dot-product attention, "
            "softmax normalization, and multi-head attention when supported by evidence) in a "
            "dedicated 'Core Foundations & Mechanism' section before discussing optimizations or benchmarks. "
            "Every factual claim must be supported by an inline citation [N] referring to a source in the "
            "provided evidence list. Never introduce facts that are not present in the evidence. "
            "Ensure every reported metric includes its comparison baseline and measurement context, "
            "explicitly noting any ambiguities when the baseline is not established by the evidence."
        ),
        backstory=(
            "You are a senior research writer who has authored survey papers for top-tier "
            "AI conferences. You prioritize foundational clarity: you always explain how the core "
            "mechanism works before delving into advanced optimizations, variants, or hardware efficiency. "
            "In benchmark tables, you ensure every quantitative claim has a clear comparison baseline and "
            "measurement context; if a baseline is missing in the source, you flag it explicitly as unspecified "
            "rather than guessing. Your cardinal rule is: if it is not in the evidence, it does not go in the report. "
            "You cite inline with bracketed numbers [1], [2] etc. and list all sources at the end "
            "with their full titles and URLs. You write for a technical audience: precise, "
            "concise, and jargon-aware without being inaccessible."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_retry_limit=3,
    )
