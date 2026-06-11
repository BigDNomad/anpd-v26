#!/usr/bin/env python3
# RULE 1: Do not modify this ANPD pipeline file without explicit permission from Dave.
# RULE 2: If this component fails, write STOP_REPORT.json and halt immediately.
# RULE 3: Do not skip, comment out, or work around this component.
# RULE 4: Do not modify master_controller.py to bypass this component.
# RULE 5: Report the exact error. Do not attempt a fix.

"""
ANPD V24 LLM Client — Multi-Provider Wrapper (2026-05-07 T1345)

Single interface for Anthropic and OpenAI completions. Used by writer and auditor
components to allow independence-of-perspective in multi-model audits.

Usage:
    from llm_client import call_llm
    response = call_llm(
        provider="openai",        # or "anthropic"
        model="gpt-4o",           # or "claude-sonnet-4-5", etc.
        system="You are an editor that...",
        user="Audit this manuscript:\\n\\n" + manuscript_text,
        max_tokens=4000,
        temperature=0.3,
    )
    # response.text → str
    # response.input_tokens → int
    # response.output_tokens → int
    # response.provider → "openai" or "anthropic"
"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    provider: str
    model: str
    stop_reason: str = ""


def _read_key(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"API key not found at {path}")
    return p.read_text().strip()


def call_llm(
    provider: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 4000,
    temperature: float = 0.3,
    stream: bool = False,
    timeout_seconds: float = 300,
) -> LLMResponse:
    if provider == "anthropic":
        return _call_anthropic(model, system, user, max_tokens, temperature, stream, timeout_seconds)
    elif provider == "openai":
        return _call_openai(model, system, user, max_tokens, temperature, stream, timeout_seconds)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def _call_anthropic(model, system, user, max_tokens, temperature, stream=False, timeout_seconds=300):
    import anthropic
    import httpx
    client = anthropic.Anthropic(
        api_key=_read_key("/home/anpd/.anthropic/api_key"),
        timeout=httpx.Timeout(timeout_seconds, connect=30.0),
    )

    if stream:
        # Streaming keeps the connection alive for long generations (>1 min)
        # that can time out with non-streaming requests. We collect the full
        # response and return a normal LLMResponse.
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as s:
            text_parts = []
            for chunk in s.text_stream:
                text_parts.append(chunk)
            # get_final_message() returns the completed Message with usage info
            final = s.get_final_message()
        return LLMResponse(
            text="".join(text_parts),
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens,
            provider="anthropic",
            model=model,
            stop_reason=final.stop_reason or "",
        )

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return LLMResponse(
        text=response.content[0].text,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        provider="anthropic",
        model=model,
        stop_reason=response.stop_reason or "",
    )


def _call_openai(model, system, user, max_tokens, temperature, stream=False, timeout_seconds=300):
    from openai import OpenAI
    import httpx
    client = OpenAI(
        api_key=_read_key("/home/anpd/.openai/api_key"),
        timeout=httpx.Timeout(timeout_seconds, connect=30.0),
    )

    if stream:
        # OpenAI streaming: collect chunks, extract usage from final chunk.
        chunks = []
        text_parts = []
        completion = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
            stream_options={"include_usage": True},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        input_tokens = 0
        output_tokens = 0
        stop_reason = ""
        for chunk in completion:
            if chunk.choices and chunk.choices[0].delta.content:
                text_parts.append(chunk.choices[0].delta.content)
            if chunk.choices and chunk.choices[0].finish_reason:
                stop_reason = chunk.choices[0].finish_reason
            if chunk.usage:
                input_tokens = chunk.usage.prompt_tokens
                output_tokens = chunk.usage.completion_tokens
        return LLMResponse(
            text="".join(text_parts),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider="openai",
            model=model,
            stop_reason=stop_reason or "",
        )

    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return LLMResponse(
        text=response.choices[0].message.content,
        input_tokens=response.usage.prompt_tokens,
        output_tokens=response.usage.completion_tokens,
        provider="openai",
        model=model,
        stop_reason=response.choices[0].finish_reason or "",
    )


if __name__ == "__main__":
    # Smoke test both providers
    import sys
    test_system = "You are a helpful assistant. Respond in one sentence."
    test_user = "What is the capital of France?"

    for prov, mdl in [("anthropic", "claude-sonnet-4-5"), ("openai", "gpt-4o-mini")]:
        try:
            r = call_llm(prov, mdl, test_system, test_user, max_tokens=100, temperature=0.0)
            print(f"[OK] {prov}/{mdl}: {r.text.strip()[:120]}")
            print(f"     tokens in={r.input_tokens} out={r.output_tokens}")
        except Exception as e:
            print(f"[FAIL] {prov}/{mdl}: {e}")
            sys.exit(1)
