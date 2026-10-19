"""
research_crew.tools
-------------------
Tools for web search, document scraping, and PDF extraction.
"""

from research_crew.tools.adapters import (
    ExaSearchAdapter,
    PDFExtractorAdapter,
    TavilySearchAdapter,
    WebParserAdapter,
    create_adapted_extractor_tools,
    create_adapted_search_tools,
)
from research_crew.tools.tool_result import ToolFetchResult

__all__ = [
    "ToolFetchResult",
    "ExaSearchAdapter",
    "TavilySearchAdapter",
    "PDFExtractorAdapter",
    "WebParserAdapter",
    "create_adapted_search_tools",
    "create_adapted_extractor_tools",
]
