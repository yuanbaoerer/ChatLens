"""Tests for typed generation providers, schema validation and budgets."""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from analytics.analyzer import AnalysisCancelled, Cancellation
from analytics.providers.generation import (
    DEFAULT_API_KEY_ENV,
    GenerationBudget,
    GenerationBudgetExceeded,
    GenerationConfigurationError,
    GenerationError,
    GenerationHTTPError,
    GenerationMessage,
    GenerationNetworkError,
    GenerationRequest,
    GenerationRequestError,
    GenerationResponseError,
    GenerationSchemaError,
    GenerationTimeout,
    MockGenerationProvider,
    OpenAICompatibleGenerationProvider,
    ProviderDisabled,
    TokenUsage,
    estimate_tokens,
)
from analytics.providers.schema import SchemaValidationError, validate_json_schema

SCHEMA = {
    "type": "object",
    "required": ["summary", "evidence_ids"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}
VALID_OUTPUT = {"summary": "synthetic summary", "evidence_ids": ["message-1"], "confidence": 0.8}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            server = self.server
            server.requests.append({"headers": dict(self.headers), "body": json.loads(raw)})
            if server.redirect_to:
                self.send_response(302)
                self.send_header("Location", server.redirect_to)
                self.end_headers()
                return
            if server.delay:
                time.sleep(server.delay)
            status, payload = server.response
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, _format, *_args):
        pass


class GenerationProviderTests(unittest.TestCase):
    def start_server(
        self,
        response,
        *,
        status: int = 200,
        delay: float = 0.0,
        redirect_to: str | None = None,
    ):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        server.requests = []
        server.response = (status, response)
        server.delay = delay
        server.redirect_to = redirect_to
        server.block_on_close = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.addCleanup(stop)
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        return server, endpoint

    def provider(self, endpoint: str, **kwargs):
        return OpenAICompatibleGenerationProvider(
            endpoint,
            "local-model",
            **kwargs,
        )

    def request(self, *, schema=SCHEMA, max_output_tokens=100):
        return GenerationRequest(
            messages=(
                GenerationMessage("system", "Return the requested JSON."),
                GenerationMessage("user", "Summarize this synthetic evidence."),
            ),
            response_schema=schema,
            max_output_tokens=max_output_tokens,
            temperature=0.2,
        )

    def success_payload(self, content: str | None = None):
        return {
            "model": "local-model",
            "choices": [{
                "message": {"role": "assistant", "content": content or json.dumps(VALID_OUTPUT)},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    def test_schema_validator_supports_structured_narratives(self):
        validate_json_schema(VALID_OUTPUT, SCHEMA)
        for invalid, path in (
            ({"summary": 1, "evidence_ids": ["message-1"]}, "$.summary"),
            ({"summary": "ok", "evidence_ids": []}, "$.evidence_ids"),
            ({"summary": "ok", "evidence_ids": [1]}, "$.evidence_ids[0]"),
            ({"summary": "ok", "evidence_ids": ["message-1"], "extra": True}, "$.extra"),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(SchemaValidationError) as caught:
                    validate_json_schema(invalid, SCHEMA)
                self.assertEqual(caught.exception.path, path)

    def test_mock_provider_is_deterministic_and_schema_validated(self):
        provider = MockGenerationProvider([VALID_OUTPUT], usage=TokenUsage(2, 3))
        first = provider.generate(self.request())
        self.assertEqual(dict(first.output), VALID_OUTPUT)
        self.assertEqual(first.usage, TokenUsage(2, 3))
        self.assertEqual(provider.calls, 1)
        with self.assertRaises(GenerationError):
            provider.generate(self.request())

        bad = MockGenerationProvider([{"summary": 1, "evidence_ids": ["message-1"]}])
        with self.assertRaises(GenerationSchemaError):
            bad.generate(self.request())

    def test_mock_provider_honors_cancellation_and_budget(self):
        cancellation = Cancellation()
        cancellation.cancel()
        with self.assertRaises(AnalysisCancelled):
            MockGenerationProvider([VALID_OUTPUT]).generate(self.request(), cancellation)

        budget = GenerationBudget(max_calls=1)
        provider = MockGenerationProvider([VALID_OUTPUT], budget=budget)
        provider.generate(self.request())
        with self.assertRaises(GenerationBudgetExceeded):
            provider.generate(self.request())
        self.assertEqual(budget.snapshot()["calls"], 1)

    def test_budget_rejects_calls_and_tokens(self):
        budget = GenerationBudget(max_calls=1)
        budget.begin_call(1, 1)
        with self.assertRaises(GenerationBudgetExceeded):
            budget.begin_call(1, 1)

        budget = GenerationBudget(max_input_tokens=3)
        with self.assertRaises(GenerationBudgetExceeded):
            budget.begin_call(4, 0)

        budget = GenerationBudget(max_output_tokens=2)
        with self.assertRaises(GenerationBudgetExceeded):
            budget.begin_call(0, 3)

        budget = GenerationBudget(max_total_tokens=3)
        with self.assertRaises(GenerationBudgetExceeded):
            budget.begin_call(2, 2)

        budget = GenerationBudget(max_input_tokens=1)
        budget.begin_call(1, 0)
        with self.assertRaises(GenerationBudgetExceeded):
            budget.record(TokenUsage(2, 0))

    def test_budget_reservations_prevent_concurrent_overspend(self):
        budget = GenerationBudget(max_total_tokens=150)
        first = budget.begin_call(100, 50)
        with self.assertRaises(GenerationBudgetExceeded):
            budget.begin_call(100, 50)
        budget.release(first)
        second = budget.begin_call(100, 50)
        budget.record(TokenUsage(100, 50), reservation=second)
        self.assertEqual(budget.snapshot()["total_tokens"], 150)

    def test_output_limit_is_required_for_output_budget(self):
        budget = GenerationBudget(max_total_tokens=100)
        provider = MockGenerationProvider([VALID_OUTPUT], budget=budget)
        with self.assertRaises(GenerationBudgetExceeded):
            provider.generate(self.request(max_output_tokens=None))

    def test_token_estimation_is_conservative_for_cjk(self):
        self.assertEqual(estimate_tokens(""), 1)
        self.assertEqual(estimate_tokens("abcd"), 4)
        self.assertEqual(estimate_tokens("abcde"), 5)
        self.assertEqual(estimate_tokens("中文测试"), 12)
        self.assertGreater(self.request().estimated_input_tokens(), 0)

    def test_real_provider_requires_explicit_opt_in_and_does_not_call(self):
        server, endpoint = self.start_server(self.success_payload())
        provider = self.provider(endpoint, enabled=False)
        with self.assertRaises(ProviderDisabled):
            provider.generate(self.request())
        self.assertEqual(server.requests, [])
        self.assertFalse(provider.enabled)

    def test_real_provider_sends_bounded_json_request(self):
        server, endpoint = self.start_server(self.success_payload())
        budget = GenerationBudget(max_calls=2, max_total_tokens=1000)
        provider = self.provider(
            endpoint,
            enabled=True,
            api_key="synthetic-secret",
            budget=budget,
        )

        response = provider.generate(self.request())

        self.assertEqual(dict(response.output), VALID_OUTPUT)
        self.assertEqual(response.usage, TokenUsage(10, 5))
        self.assertEqual(response.model_id, "local-model")
        self.assertEqual(budget.snapshot()["calls"], 1)
        self.assertNotIn("synthetic-secret", repr(provider))
        self.assertEqual(len(server.requests), 1)
        request = server.requests[0]
        self.assertEqual(request["headers"].get("Authorization"), "Bearer synthetic-secret")
        self.assertEqual(request["body"]["model"], "local-model")
        self.assertEqual(request["body"]["max_tokens"], 100)
        self.assertEqual(request["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(request["body"]["messages"][0]["role"], "system")
        self.assertEqual(request["body"]["stream"], False)

    def test_missing_usage_is_estimated_for_budget(self):
        payload = self.success_payload()
        payload.pop("usage")
        server, endpoint = self.start_server(payload)
        budget = GenerationBudget(max_total_tokens=1000)
        provider = self.provider(endpoint, enabled=True, budget=budget)
        response = provider.generate(self.request())
        self.assertIn("usage_estimated", response.diagnostics)
        self.assertGreater(response.usage.total_tokens, 0)
        self.assertGreater(budget.snapshot()["total_tokens"], 0)

    def test_partial_usage_estimates_the_missing_side(self):
        payload = self.success_payload()
        payload["usage"] = {"prompt_tokens": 10}
        server, endpoint = self.start_server(payload)
        provider = self.provider(endpoint, enabled=True)
        response = provider.generate(self.request())
        self.assertEqual(response.usage.input_tokens, 10)
        self.assertGreater(response.usage.output_tokens, 0)
        self.assertIn("usage_estimated", response.diagnostics)

    def test_redirects_are_refused_before_forwarding_credentials(self):
        target, target_endpoint = self.start_server(self.success_payload())
        redirect, redirect_endpoint = self.start_server(
            self.success_payload(),
            redirect_to=target_endpoint,
        )
        provider = self.provider(redirect_endpoint, enabled=True, api_key="synthetic-secret")
        with self.assertRaises(GenerationHTTPError) as caught:
            provider.generate(self.request())
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(target.requests, [])
        self.assertEqual(len(redirect.requests), 1)

    def test_request_size_is_bounded_before_network(self):
        server, endpoint = self.start_server(self.success_payload())
        provider = self.provider(endpoint, enabled=True, max_request_bytes=20)
        with self.assertRaises(GenerationRequestError):
            provider.generate(self.request())
        self.assertEqual(server.requests, [])

    def test_http_truncation_is_mapped_to_response_error(self):
        class BrokenResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                raise IncompleteRead(b"partial", 20)

        provider = self.provider("http://127.0.0.1:1/v1/chat/completions", enabled=True)
        with patch.object(provider._opener, "open", return_value=BrokenResponse()):
            with self.assertRaises(GenerationResponseError):
                provider.generate(self.request())

    def test_api_key_can_come_from_environment(self):
        server, endpoint = self.start_server(self.success_payload())
        provider = self.provider(endpoint, enabled=True)
        with patch.dict(os.environ, {DEFAULT_API_KEY_ENV: "env-secret"}):
            provider.generate(self.request())
        self.assertEqual(
            server.requests[0]["headers"].get("Authorization"),
            "Bearer env-secret",
        )

    def test_real_provider_rejects_schema_and_envelope_failures(self):
        server, endpoint = self.start_server(self.success_payload())
        provider = self.provider(endpoint, enabled=True)
        with self.assertRaises(GenerationSchemaError):
            provider.generate(self.request(schema={
                "type": "object",
                "required": ["summary"],
                "properties": {"summary": {"type": "string"}},
                "additionalProperties": False,
            }))

        server.response = (200, {})
        with self.assertRaises(GenerationResponseError):
            provider.generate(self.request())

        server.response = (200, self.success_payload("not-json"))
        with self.assertRaises(GenerationSchemaError):
            provider.generate(self.request())

    def test_real_provider_reports_http_errors_without_body(self):
        server, endpoint = self.start_server({"error": "TOPSECRET_RESPONSE"}, status=500)
        provider = self.provider(endpoint, enabled=True)
        with self.assertRaises(GenerationHTTPError) as caught:
            provider.generate(self.request())
        self.assertEqual(caught.exception.status, 500)
        self.assertNotIn("TOPSECRET_RESPONSE", str(caught.exception))

    def test_real_provider_timeout_is_explicit(self):
        server, endpoint = self.start_server(self.success_payload(), delay=0.3)
        provider = self.provider(endpoint, enabled=True, timeout=0.05)
        with self.assertRaises(GenerationTimeout):
            provider.generate(self.request())

    def test_real_provider_reports_network_error(self):
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        endpoint = f"http://127.0.0.1:{port}/v1/chat/completions"
        provider = self.provider(endpoint, enabled=True, timeout=0.2)
        with self.assertRaises(GenerationNetworkError):
            provider.generate(self.request())

    def test_real_provider_cancellation_skips_network(self):
        server, endpoint = self.start_server(self.success_payload())
        provider = self.provider(endpoint, enabled=True)
        cancellation = Cancellation()
        cancellation.cancel()
        with self.assertRaises(AnalysisCancelled):
            provider.generate(self.request(), cancellation)
        self.assertEqual(server.requests, [])

    def test_real_provider_enforces_call_budget_before_network(self):
        server, endpoint = self.start_server(self.success_payload())
        budget = GenerationBudget(max_calls=1)
        provider = self.provider(endpoint, enabled=True, budget=budget)
        provider.generate(self.request())
        with self.assertRaises(GenerationBudgetExceeded):
            provider.generate(self.request())
        self.assertEqual(len(server.requests), 1)

    def test_real_provider_rejects_oversized_response(self):
        payload = self.success_payload(json.dumps({"summary": "x" * 1000, "evidence_ids": ["message-1"]}))
        server, endpoint = self.start_server(payload)
        provider = self.provider(endpoint, enabled=True, max_response_bytes=100)
        with self.assertRaises(GenerationResponseError):
            provider.generate(self.request())

    def test_provider_configuration_validation(self):
        for kwargs in (
            {"endpoint": "", "model": "m"},
            {"endpoint": "file:///tmp/x", "model": "m"},
            {"endpoint": "http://example.com/x", "model": "m"},
            {"endpoint": "https://user:pass@example.com/x", "model": "m"},
            {"endpoint": "https://example.com/x", "model": ""},
            {"endpoint": "https://example.com/x", "model": "m", "timeout": 0},
            {"endpoint": "https://example.com/x", "model": "m", "max_concurrency": 0},
            {"endpoint": "https://example.com/x", "model": "m", "max_request_bytes": 0},
            {"endpoint": "https://example.com/x", "model": "m", "max_response_bytes": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(GenerationConfigurationError):
                    OpenAICompatibleGenerationProvider(**kwargs)


if __name__ == "__main__":
    unittest.main()
