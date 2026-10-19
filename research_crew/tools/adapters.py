"""
research_crew/tools/adapters.py
-------------------------------
Explicit adapters wrapping existing search, web-fetch, and PDF-fetch tools.

Key Responsibilities
--------------------
1. Intercepts raw tool outputs at the tool boundary BEFORE agents summarize or transform them.
2. Converts raw responses into typed ToolFetchResult instances with accurate requested/final URLs,
   status, and provider provenance.
3. Automatically records EvidenceRecords into the run's EvidenceStore.
4. Detects known legacy error strings and empty responses, recording them as FAILED rather than
   misclassifying them as successful FULL_TEXT.
5. Returns original tool output strings untouched so downstream CrewAI agents receive expected contracts.
"""

import json
import logging
from typing import Any, List, Optional, Type

from pydantic import BaseModel

try:
    from crewai.tools import BaseTool
except ImportError:
    from langchain.tools import BaseTool  # type: ignore[no-redef]

from research_crew.evidence.model import EvidenceRecord, RetrievalStatus
from research_crew.evidence.store import EvidenceStore
from research_crew.tools.pdf_extractor import PDFExtractorTool, _PDFInput
from research_crew.tools.search_tool import ExaSearchTool, TavilySearchTool, _ExaInput, _TavilyInput
from research_crew.tools.tool_result import ToolFetchResult
from research_crew.tools.web_parser import WebParserTool, _WebInput

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Search Tool Adapters
# ---------------------------------------------------------------------------

class ExaSearchAdapter(BaseTool):
    """Adapter wrapping ExaSearchTool to capture search snippets into EvidenceStore."""

    name: str = "Exa Academic Search"
    description: str = (
        "Primary academic search tool using the Exa neural search API. "
        "Returns a JSON list of structured sources (title, url, source_type, snippet). "
        "Use this as your FIRST CHOICE for papers and repositories."
    )
    args_schema: Type[BaseModel] = _ExaInput
    model_config = {"arbitrary_types_allowed": True}

    store: Optional[EvidenceStore] = None
    underlying_tool: Optional[ExaSearchTool] = None

    def __init__(
        self,
        store: Optional[EvidenceStore] = None,
        tool: Optional[ExaSearchTool] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            store=store,
            underlying_tool=tool or ExaSearchTool(),
            **kwargs,
        )

    def _run(
        self,
        query: Optional[str] = None,
        num_results: int = 8,
        queries: Optional[List[str]] = None,
    ) -> str:
        tool = self.underlying_tool or ExaSearchTool()
        call_kwargs = {"query": query, "num_results": num_results}
        if queries is not None:
            call_kwargs["queries"] = queries
        raw_output = tool._run(**call_kwargs)

        if self.store is not None:
            try:
                data = json.loads(raw_output)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            url = item.get("url")
                            snippet = item.get("snippet", "")
                            title = item.get("title")
                            if url and snippet and snippet.strip():
                                fetch_res = ToolFetchResult.snippet(
                                    url=url,
                                    snippet=snippet,
                                    title=title,
                                    provider="exa",
                                )
                                rec = EvidenceRecord.from_tool_result(fetch_res, provider="exa")
                                self.store.add(rec)
            except Exception as exc:
                logger.warning("ExaSearchAdapter failed to capture snippets to store: %s", exc)

        return raw_output


class TavilySearchAdapter(BaseTool):
    """Adapter wrapping TavilySearchTool to capture search snippets into EvidenceStore."""

    name: str = "Tavily Academic Search"
    description: str = (
        "Fallback search tool using the Tavily API. Returns structured source metadata. "
        "Use when Exa is unavailable, rate-limited, or returns zero results."
    )
    args_schema: Type[BaseModel] = _TavilyInput
    model_config = {"arbitrary_types_allowed": True}

    store: Optional[EvidenceStore] = None
    underlying_tool: Optional[TavilySearchTool] = None

    def __init__(
        self,
        store: Optional[EvidenceStore] = None,
        tool: Optional[TavilySearchTool] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            store=store,
            underlying_tool=tool or TavilySearchTool(),
            **kwargs,
        )

    def _run(
        self,
        query: Optional[str] = None,
        num_results: int = 8,
        queries: Optional[List[str]] = None,
    ) -> str:
        tool = self.underlying_tool or TavilySearchTool()
        call_kwargs = {"query": query, "num_results": num_results}
        if queries is not None:
            call_kwargs["queries"] = queries
        raw_output = tool._run(**call_kwargs)

        if self.store is not None:
            try:
                data = json.loads(raw_output)
                # Narrow check: ignore legacy error dictionary (e.g. {"error": "..."})
                if isinstance(data, dict) and "error" in data:
                    logger.info("Tavily returned error response: %s", data["error"])
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            url = item.get("url")
                            snippet = item.get("snippet", "")
                            title = item.get("title")
                            if url and snippet and snippet.strip():
                                fetch_res = ToolFetchResult.snippet(
                                    url=url,
                                    snippet=snippet,
                                    title=title,
                                    provider="tavily",
                                )
                                rec = EvidenceRecord.from_tool_result(fetch_res, provider="tavily")
                                self.store.add(rec)
            except Exception as exc:
                logger.warning("TavilySearchAdapter failed to capture snippets to store: %s", exc)

        return raw_output


# ---------------------------------------------------------------------------
# Content Extractor Tool Adapters
# ---------------------------------------------------------------------------

class PDFExtractorAdapter(BaseTool):
    """Adapter wrapping PDFExtractorTool to capture full text into EvidenceStore."""

    name: str = "PDF Text Extractor"
    description: str = (
        "Download a PDF from a URL and extract readable plain text. "
        "Returns at most 3 000 characters of the most relevant content. "
        "Works best with arXiv, IEEE, and ACL papers."
    )
    args_schema: Type[BaseModel] = _PDFInput
    model_config = {"arbitrary_types_allowed": True}

    store: Optional[EvidenceStore] = None
    underlying_tool: Optional[PDFExtractorTool] = None

    def __init__(
        self,
        store: Optional[EvidenceStore] = None,
        tool: Optional[PDFExtractorTool] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            store=store,
            underlying_tool=tool or PDFExtractorTool(),
            **kwargs,
        )

    def _run(self, url: str) -> str:
        from urllib.parse import urlparse
        from research_crew.evidence.model import parse_arxiv_id

        requested_url = url.strip()
        # Compute known redirect endpoint for arXiv abstract/html/pdf links
        final_url = requested_url
        arxiv_id = parse_arxiv_id(requested_url)
        if arxiv_id:
            parsed = urlparse(requested_url)
            scheme = parsed.scheme or "https"
            netloc = parsed.netloc
            if netloc in ("ar5iv.labs.arxiv.org", "ar5iv.org"):
                netloc = "arxiv.org"
            final_url = f"{scheme}://{netloc}/pdf/{arxiv_id}.pdf"
        elif "/abs/" in requested_url:
            final_url = requested_url.replace("/abs/", "/pdf/") + (".pdf" if not requested_url.endswith(".pdf") else "")

        tool = self.underlying_tool or PDFExtractorTool()
        raw_output = ""
        fetch_res: ToolFetchResult

        try:
            raw_output = tool._run(final_url)
        except Exception as exc:
            if self.store is not None:
                fetch_res = ToolFetchResult.failure(
                    url=requested_url,
                    error_message=f"PDF extraction exception: {exc}",
                    final_url=final_url,
                    provider="pdf_extractor",
                )
                self.store.add(EvidenceRecord.from_tool_result(fetch_res, provider="pdf_extractor"))
            raise

        # Defend against empty strings and known legacy error messages
        if not raw_output or not raw_output.strip():
            fetch_res = ToolFetchResult.failure(
                url=requested_url,
                error_message="PDF extractor returned empty content",
                final_url=final_url,
                provider="pdf_extractor",
            )
        elif raw_output.startswith("Error: PyMuPDF") or raw_output.startswith("Error: "):
            fetch_res = ToolFetchResult.failure(
                url=requested_url,
                error_message=raw_output,
                final_url=final_url,
                provider="pdf_extractor",
            )
        else:
            fetch_res = ToolFetchResult.success(
                url=requested_url,
                content=raw_output,
                status=RetrievalStatus.FULL_TEXT,
                final_url=final_url,
                provider="pdf_extractor",
            )

        if self.store is not None:
            self.store.add(EvidenceRecord.from_tool_result(fetch_res, provider="pdf_extractor"))

        return raw_output


class WebParserAdapter(BaseTool):
    """Adapter wrapping WebParserTool to capture full text into EvidenceStore."""

    name: str = "Webpage Text Parser"
    description: str = (
        "Fetch a webpage and extract clean, readable text. "
        "Handles documentation sites, arXiv abstracts, and GitHub repositories. "
        "Returns at most 3 000 characters of the primary content."
    )
    args_schema: Type[BaseModel] = _WebInput
    model_config = {"arbitrary_types_allowed": True}

    store: Optional[EvidenceStore] = None
    underlying_tool: Optional[WebParserTool] = None

    def __init__(
        self,
        store: Optional[EvidenceStore] = None,
        tool: Optional[WebParserTool] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            store=store,
            underlying_tool=tool or WebParserTool(),
            **kwargs,
        )

    def _run(self, url: str) -> str:
        requested_url = url.strip()
        tool = self.underlying_tool or WebParserTool()
        raw_output = ""
        fetch_res: ToolFetchResult

        try:
            raw_output = tool._run(requested_url)
        except Exception as exc:
            if self.store is not None:
                fetch_res = ToolFetchResult.failure(
                    url=requested_url,
                    error_message=f"Web parser exception: {exc}",
                    provider="web_parser",
                )
                self.store.add(EvidenceRecord.from_tool_result(fetch_res, provider="web_parser"))
            raise

        # Defend against empty strings and known legacy error messages
        if not raw_output or not raw_output.strip():
            fetch_res = ToolFetchResult.failure(
                url=requested_url,
                error_message="Webpage parser returned empty content",
                provider="web_parser",
            )
        elif (
            raw_output.startswith("Could not fetch README for ")
            or raw_output.startswith("Could not parse GitHub repo URL:")
        ):
            fetch_res = ToolFetchResult.failure(
                url=requested_url,
                error_message=raw_output,
                provider="web_parser",
            )
        else:
            fetch_res = ToolFetchResult.success(
                url=requested_url,
                content=raw_output,
                status=RetrievalStatus.FULL_TEXT,
                provider="web_parser",
            )

        if self.store is not None:
            self.store.add(EvidenceRecord.from_tool_result(fetch_res, provider="web_parser"))

        return raw_output


# ---------------------------------------------------------------------------
# Factory Helpers
# ---------------------------------------------------------------------------

def create_adapted_search_tools(store: EvidenceStore) -> List[BaseTool]:
    """Return adapted search tools hooked into *store*."""
    return [
        ExaSearchAdapter(store=store),
        TavilySearchAdapter(store=store),
    ]


def create_adapted_extractor_tools(store: EvidenceStore) -> List[BaseTool]:
    """Return adapted content extraction tools hooked into *store*."""
    return [
        PDFExtractorAdapter(store=store),
        WebParserAdapter(store=store),
    ]
