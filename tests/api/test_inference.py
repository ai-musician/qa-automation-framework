"""The OpenAI-compatible chat endpoint of the mock inference service: the reply's shape, streaming, and validation.
Its performance under load (time to first token, tokens per second) is in tests/perf/test_performance.py."""
import json

import pytest
from utils.assertions import assert_api_error, assert_expected_error, assert_status
from utils.factories import SENDER_ID
import config

pytestmark = pytest.mark.api

CHAT = "/v1/chat/completions"


def chat_request(**overrides) -> dict:
    return {"model": "mock-llm-small", "max_tokens": 5,
            "messages": [{"role": "user", "content": "hello there"}], **overrides}


def events(res) -> list:
    """The data of each server-sent event: parsed JSON chunks, and the final "[DONE]"."""
    data = [line.removeprefix("data: ") for line in res.text.splitlines() if line.startswith("data: ")]
    return [d if d == "[DONE]" else json.loads(d) for d in data]


def test_chat_reply(user_api):
    res = user_api.post(CHAT, chat_request())
    assert_status(res, 200)
    body = res.json()
    assert body["object"] == "chat.completion" and body["id"].startswith("chatcmpl-")
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"].startswith("hello ")      # the reply begins with the prompt's first word
    assert len(choice["message"]["content"].split()) == 5          # max_tokens tokens, one word each
    assert choice["finish_reason"] == "length"
    assert body["usage"] == {"prompt_tokens": 2, "completion_tokens": 5, "total_tokens": 7}


def test_streamed_reply_matches_the_whole_reply(user_api):
    """Streaming changes how the reply arrives, not what it says."""
    whole = user_api.post(CHAT, chat_request()).json()["choices"][0]["message"]["content"]
    res = user_api.post(CHAT, chat_request(stream=True))
    assert_status(res, 200)
    assert res.headers["Content-Type"].startswith("text/event-stream")
    chunks = events(res)
    assert chunks[-1] == "[DONE]", "the stream must end with data: [DONE]"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert all(c["object"] == "chat.completion.chunk" and c["id"] == chunks[0]["id"] for c in chunks[:-1])
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks[:-1]) == whole
    last = chunks[-2]
    assert last["choices"][0]["finish_reason"] == "length"
    assert last["usage"]["completion_tokens"] == 5


def test_chat_accepts_the_api_key_as_bearer_token(client):
    """OpenAI-style clients send the API key itself. Then an inference pod needs no login session, so any replica can
    answer any request (the chaos test runs two and stops one)."""
    res = client.post(CHAT, chat_request(), headers={"Authorization": f"Bearer {config.env.api_key}"})
    assert_status(res, 200)


@pytest.mark.negative
def test_api_key_as_bearer_token_only_works_for_inference(client):
    """The /api routes still need a token from POST /api/auth/token."""
    res = client.get(f"/api/users/{SENDER_ID}", headers={"Authorization": f"Bearer {config.env.api_key}"})
    assert_api_error(res, "INVALID_TOKEN", 401)


@pytest.mark.negative
def test_chat_with_unknown_bearer_returns_401(client):
    res = client.post(CHAT, chat_request(), headers={"Authorization": "Bearer not-a-key-or-token"})
    assert_api_error(res, "INVALID_TOKEN", 401)


@pytest.mark.negative
def test_crash_endpoint_does_not_exist_without_fault_injection(client):
    """POST /debug/crash, for the chaos tests, only exists where FAULT_INJECTION=on (k8s/inference.yaml)."""
    res = client.post("/debug/crash", headers={"Authorization": f"Bearer {config.env.admin_api_key}"})
    assert_status(res, 404)


@pytest.mark.negative
def test_chat_without_token_returns_401(client):
    assert_api_error(client.post(CHAT, chat_request()), "UNAUTHENTICATED", 401)


@pytest.mark.negative
@pytest.mark.parametrize("overrides, field", [
    ({"model": "gpt-9"}, "model"),
    ({"messages": []}, "messages"),
    ({"messages": [{"role": "user"}]}, "messages"),
    ({"messages": [{"role": "robot", "content": "hi"}]}, "messages"),
    ({"max_tokens": 0}, "max_tokens"),
    ({"max_tokens": 513}, "max_tokens"),
    ({"max_tokens": True}, "max_tokens"),
    ({"stream": "yes"}, "stream"),
], ids=["unknown model", "no messages", "message without content", "unknown role",
        "zero tokens", "too many tokens", "boolean tokens", "stream not a boolean"])
def test_invalid_chat_request_returns_400(user_api, overrides, field):
    res = user_api.post(CHAT, chat_request(**overrides))
    assert_expected_error(res, {"status": 400, "code": "VALIDATION_ERROR", "fields": [field]})
