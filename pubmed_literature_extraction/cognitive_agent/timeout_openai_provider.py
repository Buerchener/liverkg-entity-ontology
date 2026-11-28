"""Bounded LangExtract OpenAI provider for unreliable compatible proxies."""

from __future__ import annotations

from dataclasses import replace
import json
import re

import httpx
from langextract import data
from langextract.providers import router
from langextract.providers.openai import OpenAILanguageModel
from openai import OpenAI


def _unwrap_complete_json(output: str) -> str:
    """Accept JSON behind a Markdown opening fence when the whole body parses."""
    if not isinstance(output, str):
        return output
    text = output.strip()
    opening = re.match(r"(?i)\A([\x60]{3}|~{3})(?:json)?[ \t]*\r?\n", text)
    if opening is None:
        return output
    fence = opening.group(1)
    body = text[opening.end():].strip()
    if body.endswith(fence):
        body = body[:-len(fence)].rstrip()
    try:
        content = json.loads(body)
    except json.JSONDecodeError:
        return output
    return body if isinstance(content, (dict, list)) else output


@router.register(r"^liverkg_timeout_openai$", priority=100)
class LiverKGTimeoutOpenAIModel(OpenAILanguageModel):
    """Use a real HTTP deadline instead of the SDK's unbounded default."""

    def __init__(
        self,
        *args,
        connect_timeout_s: float = 20.0,
        request_timeout_s: float = 180.0,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._client.close()
        timeout = httpx.Timeout(
            connect=max(1.0, float(connect_timeout_s)),
            read=max(1.0, float(request_timeout_s)),
            write=max(1.0, float(request_timeout_s)),
            pool=max(1.0, float(connect_timeout_s)),
        )
        self._http_client = httpx.Client(
            timeout=timeout,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            organization=self.organization,
            max_retries=0,
            timeout=timeout,
            http_client=self._http_client,
        )

    def _process_single_prompt(self, prompt, config):
        result = super()._process_single_prompt(prompt, config)
        if self.format_type == data.FormatType.JSON:
            return replace(result, output=_unwrap_complete_json(result.output))
        return result

    def close(self) -> None:
        self._client.close()
        self._http_client.close()
