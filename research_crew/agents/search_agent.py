"""
search_agent.py
---------------
Search Agent — Academic Source Finder

Responsibilities
----------------
Accepts the query list produced by the Planner Agent and executes each
query against the Exa (primary) and Tavily (fallback) search APIs.

For each query the agent should return up to 8 structured sources.
Sources are filtered by the tools to exclude junk domains (SEO blogs,
listicles, etc.).

Output contract
---------------
The agent returns a JSON array of SourceResult objects:

    [
        {
            "title": "...",
            "url": "...",
            "source_type": "paper | repo | documentation | web",
            "published_date": "...",
            "snippet": "..."
        },
        ...
    ]

Design notes
------------
* The agent is armed with both ExaSearchTool and TavilySearchTool so it
  can fall back automatically when one API fails or is rate-limited.
* max_iter is capped at 15 to prevent runaway tool loops on edge cases.
"""

import logging

from typing import List, Optional

from crewai import Agent, LLM

try:
    from crewai.tools import BaseTool
except ImportError:
    from langchain.tools import BaseTool  # type: ignore[no-redef]

from research_crew.tools.search_tool import ExaSearchTool, TavilySearchTool

logger = logging.getLogger(__name__)


def build_search_agent(llm: LLM, tools: Optional[List[BaseTool]] = None) -> Agent:
    """Construct and return the Search Agent.

    Parameters
    ----------
    llm:
        A configured CrewAI LLM instance.
    tools:
        Optional list of tools. Defaults to [ExaSearchTool(), TavilySearchTool()].

    Returns
    -------
    Agent
        Ready-to-use CrewAI Agent with search tools attached.
    """
    logger.info("Initialising Search Agent.")

    if tools is None:
        tools = [ExaSearchTool(), TavilySearchTool()]

    return Agent(
        role="Academic Source Finder",
        goal=(
            "Execute each research query against the available search APIs and collect "
            "a diverse set of high-quality sources. For each query, retrieve up to 8 "
            "sources. Always pass one single query string under argument 'query' to search tools. "
            "Prefer arXiv papers, IEEE publications, ACL anthology papers, "
            "GitHub repositories, and official project documentation. "
            "Return all results as a single JSON array of structured source objects."
        ),
        backstory=(
            "You are a specialist academic librarian with deep knowledge of scientific "
            "databases, pre-print servers, and open-source repositories. You excel at "
            "combining search terms to find the most relevant and credible sources. "
            "When calling search tools, you pass each individual search phrase as a single "
            "string under 'query', never an array under 'queries'. "
            "You always prioritise primary sources (papers, official docs, codebases) "
            "over secondary commentary. When one search API is unavailable, you "
            "immediately switch to the backup without complaint."
        ),
        tools=tools,
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=15,
        max_retry_limit=3,
    )
