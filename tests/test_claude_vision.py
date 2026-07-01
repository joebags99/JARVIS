"""Tests for ClaudeClient.send(image_b64=...) — vision content-block shape.

Driven with a fake transport (same pattern as tests/test_claude_retry.py) so
these run without a real Anthropic key or network call.
"""

from __future__ import annotations

from app.claude_client import ClaudeClient


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Final:
    stop_reason = "end_turn"

    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeStream:
    def __init__(self, text):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        yield self._text

    def get_final_message(self):
        return _Final(self._text)


class _FakeMessages:
    def __init__(self, reply_text="OK"):
        self.reply_text = reply_text
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStream(self.reply_text)


class _FakeClient:
    def __init__(self, reply_text="OK"):
        self.messages = _FakeMessages(reply_text)


def _client_with_fake(reply_text="OK") -> tuple[ClaudeClient, _FakeClient]:
    client = ClaudeClient()
    fake = _FakeClient(reply_text)
    client._client = fake  # force ready + fake transport
    return client, fake


def test_send_without_image_uses_plain_string_content():
    client, fake = _client_with_fake()
    client.send("what's the weather?")
    # Stored history keeps the plain-string shape (unchanged from before).
    assert client.history[0] == {"role": "user", "content": "what's the weather?"}
    # with_message_cache_breakpoint() wraps it into a single cached text block
    # on the way to the API — that's existing, correct, unrelated behavior.
    sent_content = fake.messages.calls[0]["messages"][0]["content"]
    assert sent_content == [
        {"type": "text", "text": "what's the weather?", "cache_control": {"type": "ephemeral"}},
    ]


def test_send_with_image_builds_image_then_text_blocks():
    client, fake = _client_with_fake()
    client.send("what's on my screen?", image_b64="ZmFrZS1wbmctYnl0ZXM=")
    content = client.history[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {
        "type": "image",
        "source": {
            "type": "base64", "media_type": "image/png", "data": "ZmFrZS1wbmctYnl0ZXM=",
        },
    }
    assert content[1] == {"type": "text", "text": "what's on my screen?"}

    # The exact same shape is what actually gets sent to the API transport.
    sent_messages = fake.messages.calls[0]["messages"]
    sent_content = sent_messages[0]["content"]
    assert sent_content[0]["type"] == "image"
    assert sent_content[-1]["type"] == "text"


def test_send_with_image_cache_breakpoint_lands_on_text_block():
    # with_message_cache_breakpoint() marks the *last* block — confirm that's
    # the text block, not the image, since cache_control on an image block
    # isn't the documented/supported shape.
    client, fake = _client_with_fake()
    client.send("describe this", image_b64="Zm9v")
    sent_content = fake.messages.calls[0]["messages"][0]["content"]
    assert "cache_control" in sent_content[-1]
    assert sent_content[-1]["type"] == "text"
    assert "cache_control" not in sent_content[0]
