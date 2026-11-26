"""Groq Chat Completions adapter for one private draft."""

from time import sleep

from groq import APIError, APIStatusError, DefaultHttpxClient, Groq

from .debate import ORDER, safe_line, turn_payload
from .github import AppError, HTTPStatusError

DEFAULT_GROQ_MODEL = "qwen/qwen3.8-27b"
GROQ_MODELS = {
    DEFAULT_GROQ_MODEL: {"reasoning_effort": "none", "temperature": 0.9, "max_completion_tokens": 512},
    "openai/gpt-oss-20b": {"reasoning_effort": "low", "temperature": 0.7, "max_completion_tokens": 1024,
                          "include_reasoning": False},
}
GROQ_BASE_URL = "https://api.groq.com"
GROQ_TIMEOUT_SECONDS = 18.0
MAX_RESPONSE_BYTES = 24_000
MAX_RETRY_WAIT_SECONDS = 5


class GroqWriter:
    def __init__(self, model, key, *, smoke=False):
        self.model = model
        self.key = key
        self.remaining = 1 if smoke else len(ORDER)
        self.retried = False
        self.usage = None

    def __call__(self, prompt, context):
        if not self.remaining:
            return None
        data = {"model": self.model, **GROQ_MODELS[self.model], "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": turn_payload(prompt, context)},
        ]}
        try:
            completion = self.send(data)
        except HTTPStatusError as error:
            if error.status != 429 or self.retried or not self.remaining:
                raise
            try:
                wait = float(error.retry_after)
            except (TypeError, ValueError):
                wait = -1
            if not 0 <= wait <= MAX_RETRY_WAIT_SECONDS:
                raise
            self.retried = True
            sleep(wait)
            completion = self.send(data)
        try:
            self.usage = completion.usage
            choice = completion.choices[0]
            if choice.finish_reason == "stop" and choice.message.role == "assistant":
                return safe_line(choice.message.content)
        except (AttributeError, IndexError, TypeError):
            raise AppError("Malformed Groq completion response.") from None
        return None

    def send(self, data):
        self.remaining -= 1
        self.usage = None
        try:
            with Groq(api_key=self.key, base_url=GROQ_BASE_URL, max_retries=0,
                      timeout=GROQ_TIMEOUT_SECONDS, http_client=DefaultHttpxClient(follow_redirects=False)) as client:
                raw = client.chat.completions.with_raw_response.create(**data)
                if len(raw.read()) > MAX_RESPONSE_BYTES:
                    raise AppError("Groq response exceeded the size limit.")
                return raw.parse()
        except APIStatusError as error:
            raise HTTPStatusError(error.status_code, error.response.headers.get("Retry-After")) from None
        except (APIError, ValueError):
            raise AppError("Groq request failed or returned invalid JSON.") from None
