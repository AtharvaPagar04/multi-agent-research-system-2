"""
research_crew/tools/tool_result.py
-----------------------------------
Typed result structure for tool-boundary fetches.
Distinguishes successful retrievals from fetch failures at the tool boundary
without relying on fragile string matching.
"""

from dataclasses import dataclass
from typing import Optional

from research_crew.evidence.model import RetrievalStatus


@dataclass(frozen=True)
class ToolFetchResult:
    """Explicit typed outcome of a tool fetch operation.

    Attributes
    ----------
    url:
        The requested URL.
    status:
        RetrievalStatus enum indicating FULL_TEXT, SNIPPET_ONLY, or FAILED.
    raw_content:
        The exact text retrieved. Forced to empty string when status is FAILED.
    final_url:
        The final response URL after any redirects. Defaults to url if omitted.
    title:
        Document or page title if available.
    error_message:
        Explanation if status is FAILED; None otherwise.
    provider:
        Source provider name (e.g. "exa", "tavily", "pdf_extractor", "web_parser").
    """

    url: str
    status: RetrievalStatus
    raw_content: str = ""
    final_url: Optional[str] = None
    title: Optional[str] = None
    error_message: Optional[str] = None
    provider: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status == RetrievalStatus.FAILED:
            # Enforce invariant: failed fetches never retain content
            if self.raw_content != "":
                object.__setattr__(self, "raw_content", "")
        if self.final_url is None:
            object.__setattr__(self, "final_url", self.url)

    @classmethod
    def success(
        cls,
        url: str,
        content: str,
        status: RetrievalStatus = RetrievalStatus.FULL_TEXT,
        final_url: Optional[str] = None,
        title: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> "ToolFetchResult":
        """Factory for successful retrievals."""
        if status == RetrievalStatus.FAILED:
            raise ValueError("status cannot be FAILED for success factory; use failure() instead")
        return cls(
            url=url,
            status=status,
            raw_content=content,
            final_url=final_url or url,
            title=title,
            error_message=None,
            provider=provider,
        )

    @classmethod
    def snippet(
        cls,
        url: str,
        snippet: str,
        final_url: Optional[str] = None,
        title: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> "ToolFetchResult":
        """Factory for search-result snippet retrievals."""
        return cls.success(
            url=url,
            content=snippet,
            status=RetrievalStatus.SNIPPET_ONLY,
            final_url=final_url or url,
            title=title,
            provider=provider,
        )

    @classmethod
    def failure(
        cls,
        url: str,
        error_message: str,
        final_url: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> "ToolFetchResult":
        """Factory for failed retrievals."""
        return cls(
            url=url,
            status=RetrievalStatus.FAILED,
            raw_content="",
            final_url=final_url or url,
            title=None,
            error_message=error_message,
            provider=provider,
        )
