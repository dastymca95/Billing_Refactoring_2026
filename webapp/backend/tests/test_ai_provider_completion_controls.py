from webapp.backend.services.ai_provider import (
    AIProviderInvalidJSON,
    _completion_controls,
    _frozen_cache_payload,
    _response_char_limit,
    _safe_http_error_fields,
)


def test_cache_request_identity_is_immutable_across_repair_mutation():
    request = {"messages": [{"role": "user", "content": "original"}]}
    frozen = _frozen_cache_payload("vision-profile", request)

    request["messages"][0]["content"] = "repair"

    assert frozen["request"]["messages"][0]["content"] == "original"


def test_openai_reasoning_models_receive_modern_completion_controls():
    controls = _completion_controls("openai", 4096)

    assert controls == {
        "max_completion_tokens": 4096,
        "reasoning_effort": "low",
    }
    assert "max_tokens" not in controls
    assert "temperature" not in controls


def test_openai_compatible_providers_keep_legacy_completion_controls():
    controls = _completion_controls("openai_compatible", 4096)

    assert controls == {"max_tokens": 4096, "temperature": 0}
    assert "max_completion_tokens" not in controls


def test_response_character_cap_covers_requested_vision_token_budget(monkeypatch):
    from webapp.backend.services import ai_provider

    monkeypatch.setattr(ai_provider.settings, "AI_MAX_OUTPUT_CHARS", 20_000)

    assert _response_char_limit({"max_completion_tokens": 8192}) == 65_536
    assert _response_char_limit({"max_tokens": 4096}) == 32_768


def test_provider_error_diagnostics_extract_codes_without_message_or_secret():
    body = '{"error":{"message":"secret-value","type":"invalid_request_error","code":"unsupported_parameter","param":"max_tokens"}}'

    assert _safe_http_error_fields(body) == (
        "invalid_request_error",
        "unsupported_parameter",
        "max_tokens",
    )
    assert "secret-value" not in " ".join(_safe_http_error_fields(body))


def test_invalid_provider_content_has_specific_safe_failure_code():
    error = AIProviderInvalidJSON("AI provider response content was empty.")

    assert error.safe_diagnostic()["failure_code"] == "provider_invalid_json"


def test_vision_retries_one_transient_401(monkeypatch):
    import io
    import json
    import urllib.error

    from webapp.backend.services import ai_provider

    attempts = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({"choices": [{"message": {"content": '{"ok":true}'}}]}).encode()

    def send(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError(
                "https://api.openai.com/v1/chat/completions",
                401,
                "Unauthorized",
                {},
                io.BytesIO(b'{"error":{"type":"invalid_request_error"}}'),
            )
        return Response()

    monkeypatch.setattr(ai_provider.urllib.request, "urlopen", send)
    monkeypatch.setattr(ai_provider.time, "sleep", lambda _seconds: None)

    assert ai_provider._send_chat_completion(
        provider="openai",
        payload={"model": "model", "messages": []},
        vision=True,
        api_key_override="private-test-key",
        base_url_override="https://api.openai.com/v1",
        max_attempts_override=2,
    ) == '{"ok":true}'
    assert attempts == 2
