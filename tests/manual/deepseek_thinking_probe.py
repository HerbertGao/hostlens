#!/usr/bin/env python3
"""Probe DeepSeek Anthropic-compatible endpoints for thinking-block behavior.

Single-turn probe: send a "think step by step" prompt and print the content
block types returned by each model. Useful for confirming whether a given
DeepSeek endpoint forces `thinking` blocks.

Credentials are read from environment variables (never from cc-switch.db):

    HOSTLENS_DEEPSEEK_TOKEN     API key for the DeepSeek Anthropic-compat endpoint
    HOSTLENS_DEEPSEEK_BASE_URL  Base URL of the endpoint
    HOSTLENS_DEEPSEEK_MODELS    Optional comma-separated model list
                                (default: deepseek-chat,deepseek-reasoner)

This module performs no network calls at import time. Run it as a script.
"""

from __future__ import annotations

import json
import os
import sys

import anthropic

_DEFAULT_MODELS = ("deepseek-chat", "deepseek-reasoner")


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


def probe(base_url: str, api_key: str, model: str) -> None:
    client = anthropic.Anthropic(base_url=base_url, api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": "What is 2+2? Think step by step."}],
    )
    print(f"=== {model} ===")
    for block in resp.content:
        print(f"  block type: {block.type}")
    print(json.dumps(resp.model_dump(), indent=2, default=str))


def main() -> None:
    base_url, api_key = load_creds()
    for model in models():
        probe(base_url, api_key, model)


if __name__ == "__main__":
    main()
