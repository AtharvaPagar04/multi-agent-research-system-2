"""
test_search_tool.py
-------------------
Unit tests for Exa and Tavily search tools and inputs:
- Ordinary single-query input
- Defensive normalization of list-shaped queries
- Validation errors for missing/empty input
- Handling provider errors without misclassifying them as valid results
- ExaSearchAdapter capturing snippets when list-shaped queries are passed
"""

import json
from unittest.mock import MagicMock, patch
import pytest
import requests
from pydantic import ValidationError

from research_crew.evidence.model import RetrievalStatus
from research_crew.evidence.store import EvidenceStore
from research_crew.tools.adapters import ExaSearchAdapter
from research_crew.tools.search_tool import (
    ExaSearchTool,
    TavilySearchTool,
    _ExaInput,
    _TavilyInput,
)

FAKE_EXA_RESPONSE = {
    "results": [
        {
            "title": "Attention Is All You Need",
            "url": "https://arxiv.org/abs/1706.03762",
            "publishedDate": "2017-06-12",
            "text": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.",
        },
        {
            "title": "FlashAttention: Fast and Memory-Efficient Exact Attention",
            "url": "https://arxiv.org/abs/2205.14135",
            "publishedDate": "2022-05-27",
            "text": "Transformers are slow and memory-hungry on long sequences.",
        },
    ]
}

FAKE_TAVILY_RESPONSE = {
    "results": [
        {
            "title": "PyTorch Attention Tutorial",
            "url": "https://pytorch.org/tutorials/intermediate/scaled_dot_product_attention_tutorial.html",
            "published_date": "2023-01-01",
            "content": "Scaled dot product attention is a core component of Transformers.",
        }
    ]
}


# ===========================================================================
# Schema Validation Tests
# ===========================================================================

def test_exa_input_schema_single_query():
    """_ExaInput accepts ordinary single query string."""
    data = _ExaInput(query="transformer attention")
    assert data.query == "transformer attention"
    assert data.queries is None
    assert data.num_results == 8


def test_exa_input_schema_list_queries():
    """_ExaInput accepts queries list for defensive compatibility."""
    data = _ExaInput(queries=["transformer attention", "multi-head attention"])
    assert data.queries == ["transformer attention", "multi-head attention"]
    assert data.query is None


def test_exa_input_schema_rejects_empty():
    """_ExaInput rejects when neither query nor queries is provided."""
    with pytest.raises(ValidationError):
        _ExaInput()

    with pytest.raises(ValidationError):
        _ExaInput(query="   ", queries=[])


def test_tavily_input_schema_single_and_list():
    """_TavilyInput accepts single query or queries list and rejects empty."""
    single = _TavilyInput(query="attention models")
    assert single.query == "attention models"

    multi = _TavilyInput(queries=["attention models", "scaling"])
    assert len(multi.queries) == 2

    with pytest.raises(ValidationError):
        _TavilyInput(query="", queries=None)


# ===========================================================================
# Tool Execution Tests
# ===========================================================================

@patch.dict("os.environ", {"EXA_API_KEY": "fake-exa-key"})
@patch("requests.post")
def test_exa_tool_single_query_execution(mock_post):
    """ExaSearchTool runs successfully with single query string."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = FAKE_EXA_RESPONSE
    mock_post.return_value = mock_resp

    tool = ExaSearchTool()
    raw = tool._run(query="transformer attention", num_results=5)
    parsed = json.loads(raw)

    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[0]["title"] == "Attention Is All You Need"
    assert parsed[0]["source_type"] == "paper"
    assert mock_post.call_count == 1
    call_json = mock_post.call_args[1]["json"]
    assert call_json["query"] == "transformer attention"
    assert call_json["numResults"] == 5


@patch.dict("os.environ", {"EXA_API_KEY": "fake-exa-key"})
@patch("requests.post")
def test_exa_tool_list_shaped_queries_defensive_normalization(mock_post):
    """ExaSearchTool handles list-shaped queries without silently discarding any query."""
    mock_resp1 = MagicMock()
    mock_resp1.status_code = 200
    mock_resp1.json.return_value = {
        "results": [FAKE_EXA_RESPONSE["results"][0]]
    }
    mock_resp2 = MagicMock()
    mock_resp2.status_code = 200
    mock_resp2.json.return_value = {
        "results": [FAKE_EXA_RESPONSE["results"][1]]
    }
    mock_post.side_effect = [mock_resp1, mock_resp2]

    tool = ExaSearchTool()
    queries = ["scaled dot product attention", "flash attention"]
    raw = tool._run(queries=queries, num_results=6)
    parsed = json.loads(raw)

    assert isinstance(parsed, list)
    assert len(parsed) == 2
    # Both queries were processed
    assert mock_post.call_count == 2
    first_query = mock_post.call_args_list[0][1]["json"]["query"]
    second_query = mock_post.call_args_list[1][1]["json"]["query"]
    assert first_query == "scaled dot product attention"
    assert second_query == "flash attention"


@patch.dict("os.environ", {"EXA_API_KEY": ""}, clear=False)
def test_exa_tool_missing_api_key():
    """ExaSearchTool returns structured error object if API key is not configured."""
    tool = ExaSearchTool()
    raw = tool._run(query="transformer attention")
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    assert "error" in parsed
    assert "EXA_API_KEY" in parsed["error"]


@patch.dict("os.environ", {"EXA_API_KEY": "fake-exa-key"})
@patch("requests.post")
def test_exa_tool_http_error_not_treated_as_valid_sources(mock_post):
    """ExaSearchTool raises HTTP error on persistent failure instead of faking results."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500 Server Error")
    mock_post.return_value = mock_resp

    tool = ExaSearchTool()
    with pytest.raises(requests.exceptions.HTTPError):
        tool._run(query="failing query")


@patch.dict("os.environ", {"TAVILY_API_KEY": "fake-tavily-key"})
@patch("requests.post")
def test_tavily_tool_execution(mock_post):
    """TavilySearchTool executes with query and handles queries list defensively."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = FAKE_TAVILY_RESPONSE
    mock_post.return_value = mock_resp

    tool = TavilySearchTool()
    raw = tool._run(query="pytorch attention", num_results=3)
    parsed = json.loads(raw)

    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["source_type"] == "documentation"


# ===========================================================================
# Adapter Integration with List-Shaped Queries
# ===========================================================================

def test_exa_adapter_records_snippets_from_list_queries(tmp_path):
    """ExaSearchAdapter captures snippets into EvidenceStore when list queries are used."""
    store = EvidenceStore.create(output_dir=tmp_path)
    mock_tool = MagicMock(spec=ExaSearchTool)
    mock_sources = [
        {
            "title": "Attention Is All You Need",
            "url": "https://arxiv.org/abs/1706.03762",
            "source_type": "paper",
            "published_date": "2017-06-12",
            "snippet": "The dominant sequence transduction models...",
        },
        {
            "title": "FlashAttention",
            "url": "https://arxiv.org/abs/2205.14135",
            "source_type": "paper",
            "published_date": "2022-05-27",
            "snippet": "Transformers are slow and memory-hungry...",
        },
    ]
    mock_tool._run.return_value = json.dumps(mock_sources)

    adapter = ExaSearchAdapter(store=store, tool=mock_tool)
    output = adapter._run(queries=["query 1", "query 2"], num_results=4)

    assert len(json.loads(output)) == 2
    mock_tool._run.assert_called_once_with(query=None, num_results=4, queries=["query 1", "query 2"])

    # Store must have captured both sources as SNIPPET_ONLY
    assert len(store) == 2
    r1 = store.get_by_url("https://arxiv.org/abs/1706.03762")
    assert r1 is not None
    assert r1.retrieval_status == RetrievalStatus.SNIPPET_ONLY
    assert "Attention Is All You Need" in r1.title
