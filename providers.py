"""Remote model providers and grouped API-key rotation for Kurt."""

import json
import threading
import time
import urllib.error
import urllib.request


PROVIDER_SPECS = {
    "openai": {"label": "OpenAI", "mode": "openai", "base_url": "https://api.openai.com/v1"},
    "anthropic": {"label": "Anthropic / Claude", "mode": "anthropic", "base_url": "https://api.anthropic.com/v1"},
    "xai": {"label": "xAI / Grok", "mode": "openai", "base_url": "https://api.x.ai/v1"},
    "openrouter": {"label": "OpenRouter", "mode": "openai", "base_url": "https://openrouter.ai/api/v1"},
    "google": {"label": "Google Gemini", "mode": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "deepseek": {"label": "DeepSeek", "mode": "openai", "base_url": "https://api.deepseek.com/v1"},
    "groq": {"label": "Groq", "mode": "openai", "base_url": "https://api.groq.com/openai/v1"},
    "mistral": {"label": "Mistral", "mode": "openai", "base_url": "https://api.mistral.ai/v1"},
    "together": {"label": "Together AI", "mode": "openai", "base_url": "https://api.together.xyz/v1"},
    "fireworks": {"label": "Fireworks AI", "mode": "openai", "base_url": "https://api.fireworks.ai/inference/v1"},
    "perplexity": {"label": "Perplexity", "mode": "openai", "base_url": "https://api.perplexity.ai"},
    "custom": {"label": "Custom OpenAI-compatible", "mode": "openai", "base_url": ""},
}


def provider_label(provider):
    spec = PROVIDER_SPECS.get(str(provider).lower())
    return spec["label"] if spec else str(provider).title()


def provider_mode(provider):
    spec = PROVIDER_SPECS.get(str(provider).lower())
    if not spec:
        raise ValueError("Unsupported provider. Choose a listed provider or Custom.")
    return spec["mode"]


def default_base_url(provider):
    return PROVIDER_SPECS.get(str(provider).lower(), {}).get("base_url", "")


def mask_key(value):
    value = str(value or "")
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * min(16, len(value) - 8) + value[-4:]


class ProviderError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status

    @property
    def should_rotate(self):
        return self.status in (401, 402, 403, 429)


def _read_error_body(body):
    try:
        data = json.loads(body)
    except Exception:
        return body.strip()[:1200]
    err = data.get("error", data)
    if isinstance(err, dict):
        message = err.get("message") or err.get("detail") or err.get("type")
        code = err.get("code")
        return (f"{code}: " if code else "") + str(message or err)
    return str(err)[:1200]


def _request(url, payload, headers, timeout, stream=False):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = _read_error_body(exc.read().decode("utf-8", "replace"))
        raise ProviderError(f"Provider returned HTTP {exc.code}: {detail}", exc.code) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Could not reach provider: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ProviderError(f"Provider request timed out after {timeout}s") from exc


def _quota_available(model, key, base_url, timeout=5):
    model_name = str(model).lower()
    if not (model_name.endswith(":free") or model_name == "openrouter/free"):
        return True
    if "openrouter.ai" not in str(base_url).lower():
        return True
    url = base_url.rstrip("/") + "/key"
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
        quota = (data.get("data") or {}).get("free_model_daily_requests") or {}
        used, limit = quota.get("used"), quota.get("limit")
        if used is not None and limit is not None:
            return int(used) < int(limit)
    except Exception:
        pass
    return True


def _messages_for_openai(messages):
    return [
        {"role": item.get("role", "user"), "content": item.get("content", "") or ""}
        for item in messages
        if item.get("role") in ("system", "user", "assistant")
    ]


def _anthropic_messages(messages):
    system = []
    converted = []
    for item in messages:
        role = item.get("role")
        content = item.get("content", "") or ""
        if role == "system":
            if content:
                system.append(content)
        elif role in ("user", "assistant"):
            if converted and converted[-1]["role"] == role:
                converted[-1]["content"] += "\n\n" + content
            else:
                converted.append({"role": role, "content": content})
    if not converted:
        converted = [{"role": "user", "content": "Continue."}]
    return "\n\n".join(system), converted


def _endpoint(model_config):
    provider = str(model_config.get("provider", "")).lower()
    mode = provider_mode(provider)
    base = str(model_config.get("base_url") or default_base_url(provider) or "").strip().rstrip("/")
    if not base:
        raise ProviderError("This model has no API base URL. Edit the model and add its endpoint.")
    suffix = "/messages" if mode == "anthropic" else "/chat/completions"
    url = base if base.endswith(suffix) else base + suffix
    return mode, url, base


def re_model_reasoning(model):
    return model.startswith(("o1", "o3", "o4"))


class ProviderClient:
    """Calls configured remote models and rotates keys within provider/group pools."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next_index = {}
        self._quota_cache = {}

    def _key_has_quota(self, model, key, base_url):
        model_name = str(model).lower()
        if not (model_name.endswith(":free") or model_name == "openrouter/free"):
            return True
        if "openrouter.ai" not in str(base_url).lower():
            return True
        with self._lock:
            cached = self._quota_cache.get(key)
            if cached and time.time() - cached[0] < 60:
                return cached[1]
        available = _quota_available(model, key, base_url)
        with self._lock:
            self._quota_cache[key] = (time.time(), available)
        return available

    def _advance_pool(self, pool_id, matching, key_entry):
        key_id = key_entry.get("id")
        position = next((
            i for i, item in enumerate(matching)
            if item is key_entry or (key_id and item.get("id") == key_id)
        ), 0)
        with self._lock:
            self._next_index[pool_id] = position % len(matching)

    @staticmethod
    def _matching_keys(model_config, keys):
        provider = str(model_config.get("provider", "")).lower()
        group = str(model_config.get("key_group") or "default")
        return [
            key for key in keys
            if str(key.get("provider", "")).lower() == provider
            and str(key.get("group") or "default") == group
            and key.get("value")
        ]

    def _ordered_keys(self, model_config, keys):
        matching = self._matching_keys(model_config, keys)
        if not matching:
            raise ProviderError(
                f"No API keys are configured for {provider_label(model_config.get('provider'))} "
                f"group '{model_config.get('key_group') or 'default'}'. Add a key with /keys."
            )
        pool_id = (
            str(model_config.get("provider", "")).lower(),
            str(model_config.get("key_group") or "default"),
        )
        with self._lock:
            start = self._next_index.get(pool_id, 0) % len(matching)
        rotated = matching[start:] + matching[:start]
        _mode, _url, base = _endpoint(model_config)
        available = [
            key for key in rotated
            if self._key_has_quota(model_config.get("model"), key["value"], base)
        ]
        skipped = len(rotated) - len(available)
        return pool_id, available, skipped, matching

    def _headers_and_payload(self, model_config, key, messages, stream, temperature, max_tokens):
        mode, url, _base = _endpoint(model_config)
        provider = str(model_config.get("provider", "")).lower()
        model = str(model_config.get("model", "")).strip()
        if not model:
            raise ProviderError("The configured remote model ID is empty.")
        if mode == "anthropic":
            system, converted = _anthropic_messages(messages)
            payload = {
                "model": model,
                "messages": converted,
                "max_tokens": max_tokens or 4096,
                "temperature": temperature,
                "stream": stream,
            }
            if system:
                payload["system"] = system
            headers = {
                "Authorization": "Bearer " + key,
                "anthropic-version": "2026-10-21",
                "content-type": "application/json",
            }
        else:
            payload = {
                "model": model,
                "messages": _messages_for_openai(messages),
                "temperature": temperature,
                "max_tokens": max_tokens or 4096,
                "stream": stream,
            }
            model_lower = model.lower()
            if provider in ("openai",) and (model_lower.startswith("gpt-5") or re_model_reasoning(model_lower)):
                payload["max_completion_tokens"] = payload.pop("max_tokens")
                if model_lower.startswith(("o1", "o3", "o4")):
                    payload.pop("temperature", None)
            if stream and provider in ("openai", "openrouter"):
                payload["stream_options"] = {"include_usage": True}
            headers = {
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            }
            if str(model_config.get("provider", "")).lower() == "openrouter":
                headers["HTTP-Referer"] = "https://github.com/"
                headers["X-Title"] = "Kurt"
        return url, headers, payload, mode

    def stream_chat(self, model_config, messages, keys, timeout=900, temperature=0.2, max_tokens=8192):
        pool_id, ordered, skipped, matching = self._ordered_keys(model_config, keys)
        if not ordered:
            raise ProviderError("Every API key in this group has exhausted its OpenRouter free-model quota.")
        last_error = None
        for index, key_entry in enumerate(ordered):
            key = str(key_entry["value"])
            try:
                url, headers, payload, mode = self._headers_and_payload(
                    model_config, key, messages, True, temperature, max_tokens
                )
                response = _request(url, payload, headers, timeout, stream=True)
            except ProviderError as exc:
                last_error = exc
                retryable = exc.should_rotate or (exc.status is not None and exc.status >= 500) or exc.status is None
                if retryable and index + 1 < len(ordered):
                    continue
                if retryable:
                    raise ProviderError(
                        f"All {len(ordered)} keys in pool '{model_config.get('key_group') or 'default'}' "
                        f"failed. Last error: {exc}", exc.status
                    ) from exc
                raise

            emitted = False
            try:
                if mode == "anthropic":
                    for kind, chunk in self._read_anthropic_stream(response):
                        if kind in ("content", "thinking") and chunk:
                            emitted = True
                        yield kind, chunk
                else:
                    for kind, chunk in self._read_openai_stream(response):
                        if kind in ("content", "thinking") and chunk:
                            emitted = True
                        yield kind, chunk
                self._advance_pool(pool_id, matching, key_entry)
                return
            except ProviderError as exc:
                last_error = exc
                if emitted or not exc.should_rotate or index + 1 >= len(ordered):
                    raise
            finally:
                response.close()
        if last_error:
            raise ProviderError(
                f"All keys in group '{model_config.get('key_group') or 'default'}' failed. "
                f"Last error: {last_error}", last_error.status
            )
        if skipped:
            raise ProviderError("All keys in this group have exhausted their OpenRouter free-model quota.")
        raise ProviderError("No API key in this group could complete the request.")

    @staticmethod
    def _read_openai_stream(response):
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if event.get("error"):
                err = event["error"]
                message = str(err.get("message") if isinstance(err, dict) else err)
                raw_status = (err.get("status") or err.get("code")) if isinstance(err, dict) else None
                try:
                    status = int(raw_status)
                except (TypeError, ValueError):
                    status = None
                if status not in (401, 402, 403, 429):
                    status = None
                raise ProviderError(message, status)
            choices = event.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                thinking = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
                content = delta.get("content")
                if isinstance(thinking, str) and thinking:
                    yield "thinking", thinking
                if isinstance(content, str) and content:
                    yield "content", content
            usage = event.get("usage")
            if usage:
                yield "stats", {
                    "eval_count": usage.get("completion_tokens"),
                    "prompt_eval_count": usage.get("prompt_tokens"),
                }

    @staticmethod
    def _read_anthropic_stream(response):
        event_name = ""
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                event_name = ""
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue
            if not line.startswith("data:"):
                continue
            try:
                data = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if event_name == "error" or data.get("type") == "error":
                err = data.get("error") or data
                message = err.get("message") if isinstance(err, dict) else str(err)
                raise ProviderError(message or "Anthropic stream error")
            kind = data.get("type") or event_name
            if kind == "content_block_delta":
                delta = data.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    yield "content", delta["text"]
                elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    yield "thinking", delta["thinking"]
            elif kind == "message_delta":
                usage = data.get("usage") or {}
                if usage:
                    yield "stats", {"eval_count": usage.get("output_tokens")}

    def chat_once(self, model_config, messages, keys, timeout=900, temperature=0.2, max_tokens=8192):
        pool_id, ordered, skipped, matching = self._ordered_keys(model_config, keys)
        if not ordered:
            raise ProviderError("Every API key in this group has exhausted its OpenRouter free-model quota.")
        last_error = None
        for index, key_entry in enumerate(ordered):
            try:
                url, headers, payload, mode = self._headers_and_payload(
                    model_config, str(key_entry["value"]), messages, False, temperature, max_tokens
                )
                with _request(url, payload, headers, timeout) as response:
                    data = json.loads(response.read().decode("utf-8") or "{}")
                if mode == "anthropic":
                    content = "".join(
                        block.get("text", "") for block in data.get("content", [])
                        if block.get("type") == "text"
                    )
                else:
                    choices = data.get("choices") or []
                    content = choices[0].get("message", {}).get("content", "") if choices else ""
                    if isinstance(content, list):
                        content = "".join(
                            part.get("text", "") for part in content if isinstance(part, dict)
                        )
                self._advance_pool(pool_id, matching, key_entry)
                return str(content or "").strip()
            except ProviderError as exc:
                last_error = exc
                retryable = exc.should_rotate or (exc.status is not None and exc.status >= 500) or exc.status is None
                if retryable and index + 1 < len(ordered):
                    continue
                if retryable:
                    raise ProviderError(
                        f"All {len(ordered)} keys in pool '{model_config.get('key_group') or 'default'}' "
                        f"failed. Last error: {exc}", exc.status
                    ) from exc
                raise
        if last_error:
            raise ProviderError(
                f"All keys in group '{model_config.get('key_group') or 'default'}' failed. "
                f"Last error: {last_error}", last_error.status
            )
        if skipped:
            raise ProviderError("All keys in this group have exhausted their OpenRouter free-model quota.")
        raise ProviderError("No API key in this group could complete the request.")
