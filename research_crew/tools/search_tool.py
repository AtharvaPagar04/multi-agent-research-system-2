"""
search_tool.py
--------------
CrewAI-compatible tools for searching academic and technical sources.

Two search backends are provided:
  * ExaSearchTool  – uses the Exa neural search API (preferred for papers/repos)
  * TavilySearchTool – uses the Tavily API as a fallback

Both tools return a JSON-serialised list of SourceResult dicts so that
downstream agents receive **structured data only**, never raw HTML.

Source-type heuristics
----------------------
URLs are classified as "paper", "repo", or "documentation" based on domain.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Type

import requests
from pydantic import BaseModel, Field, model_validator
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

try:
    from crewai.tools import BaseTool
except ImportError:
    from langchain.tools import BaseTool  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain → source-type mapping
# ---------------------------------------------------------------------------
PAPER_DOMAINS = ("arxiv.org", "ieee.org", "acl.org", "aclweb.org", "semanticscholar.org",
                 "papers.nips.cc", "openreview.net", "dl.acm.org", "pubmed.ncbi.nlm.nih.gov")
REPO_DOMAINS   = ("github.com", "gitlab.com", "huggingface.co")
DOC_DOMAINS    = ("docs.", "readthedocs.io", "pytorch.org", "tensorflow.org",
                  "scikit-learn.org", "numpy.org", "scipy.org", "kubernetes.io",
                  "developer.mozilla.org")

JUNK_DOMAINS = ("medium.com", "towardsdatascience.com", "analyticsvidhya.com",
                "machinelearningmastery.com", "kdnuggets.com", "datacamp.com",
                "listicle", "buzzfeed", "quora.com", "reddit.com")


def _classify_source(url: str) -> str:
    url_lower = url.lower()
    if any(d in url_lower for d in PAPER_DOMAINS):
        return "paper"
    if any(d in url_lower for d in REPO_DOMAINS):
        return "repo"
    if any(d in url_lower for d in DOC_DOMAINS):
        return "documentation"
    return "web"


def _is_junk(url: str) -> bool:
    return any(d in url.lower() for d in JUNK_DOMAINS)


# ---------------------------------------------------------------------------
# Shared result schema
# ---------------------------------------------------------------------------
class SourceResult(BaseModel):
    title: str
    url: str
    source_type: str        # paper | repo | documentation | web
    published_date: str
    snippet: str


# ---------------------------------------------------------------------------
# Exa Search Tool
# ---------------------------------------------------------------------------
class _ExaInput(BaseModel):
    query: Optional[str] = Field(
        None,
        description="A single academic or technical search query string. Do NOT pass a list.",
    )
    queries: Optional[List[str]] = Field(
        None,
        description="Optional list of query strings (normalized defensively at boundary).",
    )
    num_results: int = Field(8, description="Number of results to return (max 10)")

    @model_validator(mode="after")
    def validate_query_present(self) -> "_ExaInput":
        has_query = bool(self.query and self.query.strip())
        has_queries = bool(
            self.queries
            and any(isinstance(q, str) and q.strip() for q in self.queries)
        )
        if not has_query and not has_queries:
            raise ValueError(
                "Either 'query' (single non-empty string) or 'queries' (list of non-empty strings) must be provided."
            )
        return self


class ExaSearchTool(BaseTool):
    name: str = "Exa Academic Search"
    description: str = (
        "Search academic papers, technical repos, and official docs using the Exa neural "
        "search API. Returns structured source metadata. Prefers arXiv, IEEE, ACL, GitHub."
    )
    args_schema: Type[BaseModel] = _ExaInput

    @retry(
        retry=retry_if_exception_type(requests.exceptions.HTTPError),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _run(
        self,
        query: Optional[str] = None,
        num_results: int = 8,
        queries: Optional[List[str]] = None,
    ) -> str:
        api_key = os.getenv("EXA_API_KEY")
        if not api_key:
            return json.dumps({"error": "EXA_API_KEY not set in environment"})

        target_queries: List[str] = []
        if query and isinstance(query, str) and query.strip():
            target_queries.append(query.strip())
        if queries and isinstance(queries, list):
            for q in queries:
                if isinstance(q, str) and q.strip() and q.strip() not in target_queries:
                    target_queries.append(q.strip())

        if not target_queries:
            return json.dumps({"error": "No valid search queries provided."})

        max_total_results = min(num_results, 10)
        per_query_limit = (
            max(2, max_total_results // len(target_queries))
            if len(target_queries) > 1
            else max_total_results
        )

        combined_results: List[Dict[str, Any]] = []
        seen_urls = set()

        for current_query in target_queries:
            if len(combined_results) >= max_total_results:
                break

            payload = {
                "query": current_query,
                "numResults": per_query_limit,
                "useAutoprompt": True,
                "type": "neural",
                "contents": {
                    "text": {"maxCharacters": 800},
                },
                "includeDomains": list(PAPER_DOMAINS) + list(REPO_DOMAINS),
            }

            resp = requests.post(
                "https://api.exa.ai/search",
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=20,
            )

            if resp.status_code == 429:
                logger.warning("Exa rate limit hit – retrying with backoff.")
                resp.raise_for_status()

            resp.raise_for_status()
            data = resp.json()

            for item in data.get("results", []):
                url = item.get("url", "")
                if _is_junk(url) or url in seen_urls:
                    continue
                seen_urls.add(url)
                combined_results.append(
                    SourceResult(
                        title=item.get("title", "Untitled"),
                        url=url,
                        source_type=_classify_source(url),
                        published_date=item.get("publishedDate", "Unknown"),
                        snippet=(item.get("text") or item.get("snippet", ""))[:500],
                    ).model_dump()
                )
                if len(combined_results) >= max_total_results:
                    break

            logger.info("Exa returned %d usable results for query: %s", len(combined_results), current_query)
            if len(target_queries) > 1:
                time.sleep(0.1)

        return json.dumps(combined_results, indent=2)


# ---------------------------------------------------------------------------
# Tavily Search Tool  (fallback / complementary)
# ---------------------------------------------------------------------------
class _TavilyInput(BaseModel):
    query: Optional[str] = Field(
        None,
        description="A single academic or technical search query string. Do NOT pass a list.",
    )
    queries: Optional[List[str]] = Field(
        None,
        description="Optional list of query strings (normalized defensively at boundary).",
    )
    num_results: int = Field(8, description="Number of results to return")

    @model_validator(mode="after")
    def validate_query_present(self) -> "_TavilyInput":
        has_query = bool(self.query and self.query.strip())
        has_queries = bool(
            self.queries
            and any(isinstance(q, str) and q.strip() for q in self.queries)
        )
        if not has_query and not has_queries:
            raise ValueError(
                "Either 'query' (single non-empty string) or 'queries' (list of non-empty strings) must be provided."
            )
        return self


class TavilySearchTool(BaseTool):
    name: str = "Tavily Academic Search"
    description: str = (
        "Fallback search tool using the Tavily API. Returns structured source metadata. "
        "Use when Exa results are insufficient."
    )
    args_schema: Type[BaseModel] = _TavilyInput

    @retry(
        retry=retry_if_exception_type(requests.exceptions.HTTPError),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _run(
        self,
        query: Optional[str] = None,
        num_results: int = 8,
        queries: Optional[List[str]] = None,
    ) -> str:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            return json.dumps({"error": "TAVILY_API_KEY not set in environment"})

        target_queries: List[str] = []
        if query and isinstance(query, str) and query.strip():
            target_queries.append(query.strip())
        if queries and isinstance(queries, list):
            for q in queries:
                if isinstance(q, str) and q.strip() and q.strip() not in target_queries:
                    target_queries.append(q.strip())

        if not target_queries:
            return json.dumps({"error": "No valid search queries provided."})

        max_total_results = min(num_results, 10)
        per_query_limit = (
            max(2, max_total_results // len(target_queries))
            if len(target_queries) > 1
            else max_total_results
        )

        combined_results: List[Dict[str, Any]] = []
        seen_urls = set()

        for current_query in target_queries:
            if len(combined_results) >= max_total_results:
                break

            payload = {
                "api_key": api_key,
                "query": current_query,
                "search_depth": "advanced",
                "max_results": per_query_limit,
                "include_answer": False,
                "include_raw_content": False,
            }

            resp = requests.post(
                "https://api.tavily.com/search",
                json=payload,
                timeout=20,
            )

            if resp.status_code == 429:
                logger.warning("Tavily rate limit hit – retrying with backoff.")
                resp.raise_for_status()

            resp.raise_for_status()
            data = resp.json()

            for item in data.get("results", []):
                url = item.get("url", "")
                if _is_junk(url) or url in seen_urls:
                    continue
                seen_urls.add(url)
                combined_results.append(
                    SourceResult(
                        title=item.get("title", "Untitled"),
                        url=url,
                        source_type=_classify_source(url),
                        published_date=item.get("published_date", "Unknown"),
                        snippet=item.get("content", "")[:500],
                    ).model_dump()
                )
                if len(combined_results) >= max_total_results:
                    break

            logger.info("Tavily returned %d usable results for query: %s", len(combined_results), current_query)

        return json.dumps(combined_results, indent=2)
