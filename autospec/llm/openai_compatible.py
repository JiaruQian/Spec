"""Minimal OpenAI-compatible chat client."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


@dataclass
class ChatConfig:
    """Configuration for a chat completion request."""

    model: str
    endpoint: str
    temperature: float = 0.1
    max_tokens: int = 1024
    api_key: Optional[str] = None
    site_url: Optional[str] = None
    app_name: Optional[str] = None
    timeout_seconds: int = 120


class OpenAICompatibleClient:
    """Tiny client for OpenAI-style chat completion APIs."""

    def __init__(self, config: ChatConfig):
        self.config = config

    def chat(self, system_prompt: str, user_prompt: str) -> str:
        """Send a chat completion request and return assistant text."""
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }

        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.site_url:
            headers["HTTP-Referer"] = self.config.site_url
        if self.config.app_name:
            headers["X-Title"] = self.config.app_name

        request = urllib.request.Request(
            self.config.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"LLM request failed with HTTP {exc.code}: {err_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc

        try:
            data = json.loads(body)
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unexpected LLM response format: {body}") from exc
