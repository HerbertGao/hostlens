#!/usr/bin/env python3
"""Probe a DeepSeek Anthropic-compatible endpoint's multi-turn tool loop.

Simulates: user -> tool_use -> tool_result -> continuation, printing the
content block types returned on each turn. Surfaces whether the endpoint
forces `thinking` blocks or returns a 400 on the continuation turn.

Credentials are read from environment variables (never from cc-switch.db):

    HOSTLENS_DEEPSEEK_TOKEN     API key for the DeepSeek Anthropic-compat endpoint
    HOSTLENS_DEEPSEEK_BASE_URL  Base URL of the endpoint
    HOSTLENS_DEEPSEEK_MODELS    Optional comma-separated model list
                                (default: deepseek-chat,deepseek-reasoner)

This module performs no network calls at import time. Run it as a script.
"""

from __future__ import annotations

import os
import sys

import anthropic

_DEFAULT_MODELS = ("deepseek-chat", "deepseek-reasoner")

TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


def load_creds() -> tuple[str, str]:
    token = os.environ.get("HOSTLENS_DEEPSEEK_TOKEN")
    base_url = os.environ.get("HOSTLENS_DEEPSEEK_BASE_URL")
    if not token or not base_url:
        print(
            "Set HOSTLENS_DEEPSEEK_TOKEN and HOSTLENS_DEEPSEEK_BASE_URL to run this probe.",
            file=sys.stderr,
        )
        sys.exit(2)
    return base_url, token


def models() -> tuple[str, ...]:
    raw = os.environ.get("HOSTLENS_DEEPSEEK_MODELS")
    if not raw:
        return _DEFAULT_MODELS
    return tuple(m.strip() for m in raw.split(",") if m.strip())


def probe_multiturn(base_url: str, api_key: str, model: str) -> None:
    client = anthropic.Anthropic(base_url=base_url, api_key=api_key)
    messages: list = [{"role": "user", "content": "What's the weather in Paris?"}]
    resp = client.messages.create(model=model, max_tokens=1024, tools=TOOLS, messages=messages)
    print(f"=== {model} turn 1 ===")
    for block in resp.content:
        print(f"  block type: {block.type}")
    tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_use is None:
        print("  no tool_use returned")
        return
    messages.append({"role": "assistant", "content": resp.content})
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": "Sunny, 22C",
                }
            ],
        }
    )
    resp2 = client.messages.create(model=model, max_tokens=1024, tools=TOOLS, messages=messages)
    print(f"=== {model} turn 2 ===")
    for block in resp2.content:
        print(f"  block type: {block.type}")


def main() -> None:
    base_url, api_key = load_creds()
    for model in models():
        probe_multiturn(base_url, api_key, model)


if __name__ == "__main__":
    main()
