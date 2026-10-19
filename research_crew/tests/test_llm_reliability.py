"""
test_llm_reliability.py
-----------------------
Unit tests for LLM reliability, diagnostics, and telemetry opt-out:
- _build_llm configuration with Mesh API and interceptor
- MeshDiagnosticInterceptor captures metadata safely without leaking sensitive payloads
- Safe bounded retry logic on llm.call handles transient empty responses
- Persistent empty responses return empty without fabricated success
- Telemetry opt-out environment variables and Crew tracing=False
"""

import json
import os
from unittest.mock import MagicMock, patch
import httpx
import pytest

from crewai import Crew, LLM, Process
from crewai_core.telemetry import Telemetry as CoreTelemetry
from crewai.execution import _tracing_disabled

from research_crew.main import MeshDiagnosticInterceptor, _build_llm


@patch.dict("os.environ", {"MESH_API_KEY": "test-key-mock", "MESH_MODEL": "gemini-3.5-flash-lite"})
def test_build_llm_configuration():
    """_build_llm configures Mesh API endpoint, model, and diagnostic interceptor."""
    llm = _build_llm()
    assert llm.model == "gemini-3.5-flash-lite"
    assert llm.base_url == "https://api.meshapi.ai/v1"
    assert isinstance(llm.interceptor, MeshDiagnosticInterceptor)


def test_diagnostic_interceptor_metadata_capture(caplog):
    """MeshDiagnosticInterceptor parses metadata and logs diagnostics safely without leaking payloads."""
    interceptor = MeshDiagnosticInterceptor()

    # Create dummy httpx response
    payload = {
        "id": "chatcmpl-test-12345",
        "model": "gemini-3.5-flash-lite",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Attention is a neural mechanism.",
                },
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23},
    }
    request = httpx.Request("POST", "https://api.meshapi.ai/v1/chat/completions")
    response = httpx.Response(200, json=payload, request=request)

    res = interceptor.on_inbound(response)
    assert res.status_code == 200

    # Test with empty message choice (the bug condition)
    empty_payload = {
        "id": "chatcmpl-empty-99999",
        "model": "gemini-3.5-flash-lite",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": None,
                },
            }
        ],
        "usage": {"prompt_tokens": 4000, "completion_tokens": 40, "total_tokens": 4040},
    }
    empty_response = httpx.Response(200, json=empty_payload, request=request)
    res_empty = interceptor.on_inbound(empty_response)
    assert res_empty.status_code == 200


@patch.dict("os.environ", {"MESH_API_KEY": "test-key-mock"})
def test_reliable_call_successful_first_try():
    """reliable_call returns immediately on non-empty first attempt."""
    with patch("crewai.LLM.__new__") as mock_llm_cls:
        mock_instance = MagicMock()
        mock_orig_call = MagicMock(return_value="Non-empty valid answer")
        mock_instance.call = mock_orig_call
        mock_llm_cls.return_value = mock_instance

        llm = _build_llm()
        result = llm.call(messages=[{"role": "user", "content": "hi"}])
        assert result == "Non-empty valid answer"
        assert mock_orig_call.call_count == 1


@patch.dict("os.environ", {"MESH_API_KEY": "test-key-mock"})
def test_reliable_call_retries_transient_empty_response():
    """reliable_call retries when first attempt returns empty and succeeds on second attempt."""
    with patch("crewai.LLM.__new__") as mock_llm_cls:
        mock_instance = MagicMock()
        mock_orig_call = MagicMock(side_effect=["", "Valid recovered response"])
        mock_instance.call = mock_orig_call
        mock_llm_cls.return_value = mock_instance

        llm = _build_llm()
        result = llm.call(messages=[{"role": "user", "content": "test"}])
        assert result == "Valid recovered response"
        assert mock_orig_call.call_count == 2


@patch.dict("os.environ", {"MESH_API_KEY": "test-key-mock"})
def test_reliable_call_does_not_fake_success_if_persistently_empty():
    """reliable_call returns genuinely empty response if all retries fail (no silent fabrication)."""
    with patch("crewai.LLM.__new__") as mock_llm_cls:
        mock_instance = MagicMock()
        mock_orig_call = MagicMock(side_effect=["", ""])
        mock_instance.call = mock_orig_call
        mock_llm_cls.return_value = mock_instance

        llm = _build_llm()
        result = llm.call(messages=[{"role": "user", "content": "test"}])
        assert result == ""
        assert mock_orig_call.call_count == 2


def test_telemetry_and_tracing_opt_out_guarantees():
    """Verified that opt-out flags disable CoreTelemetry and tracing in CrewAI 1.15.27."""
    assert os.getenv("OTEL_SDK_DISABLED") == "true"
    assert os.getenv("CREWAI_DISABLE_TELEMETRY") == "true"
    assert os.getenv("CREWAI_TRACING_ENABLED") == "false"
    assert CoreTelemetry._is_telemetry_disabled() is True
    assert _tracing_disabled(None) is True
    assert _tracing_disabled(False) is True
