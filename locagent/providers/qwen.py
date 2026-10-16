"""通义千问（qwen）模型接入。

通过阿里云百炼的 Anthropic 兼容接口调用 qwen 模型，使用接口原生的工具调用（tools / tool_use）。
"""

import copy
import json
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request

from ..config import provider_env
from .types import ModelResponse, ToolCall

DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/apps/anthropic"
DEFAULT_QWEN_MODEL = "qwen3.6-plus"
QWEN_API_KEY_ENV = "QWEN_API_KEY"
QWEN_API_BASE_ENV = "QWEN_API_BASE"
QWEN_MODEL_ENV = "QWEN_MODEL"
# 请求失败时的重试：限流（429）、服务端错误（5xx）、网络错误都属于临时故障，等一会儿再试通常就好了。
# 第 n 次重试前等待 RETRY_BASE_DELAY * 2^(n-1) 秒（2、4、8、16 秒），单次最多等 RETRY_MAX_DELAY 秒；
# 服务端在 Retry-After 响应头里给了等待时间时，按它的来
MAX_RETRIES = 4
RETRY_BASE_DELAY = 2
RETRY_MAX_DELAY = 30


def qwen_settings(model=None, base_url=None):
    """确定本次使用的模型名、接口地址和密钥。

    优先级：调用方显式传入（命令行参数）> 环境变量（.env）> 代码默认值。
    密钥只从环境变量读取。
    """
    return {
        "model": model or provider_env(QWEN_MODEL_ENV, default=DEFAULT_QWEN_MODEL),
        "base_url": base_url or provider_env(QWEN_API_BASE_ENV, default=DEFAULT_QWEN_BASE_URL),
        "api_key": provider_env(QWEN_API_KEY_ENV),
    }


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _retry_delay(retry, retry_after=None):
    # retry 从 1 开始；retry_after 是服务端给的建议等待秒数（字符串），没有或无法解析时按指数退避
    if retry_after and retry_after.strip().isdigit():
        return min(int(retry_after), RETRY_MAX_DELAY)
    return min(RETRY_BASE_DELAY * 2 ** (retry - 1), RETRY_MAX_DELAY)


def _extract_text(data):
    texts = []
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
    return "\n".join(texts)


def _extract_tool_calls(data):
    # 模型要调用工具时，返回内容里有 tool_use 块：{"type":"tool_use","id":...,"name":...,"input":{...}}
    calls = []
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "tool_use":
            args = item.get("input")
            calls.append(ToolCall(id=str(item.get("id", "")), name=str(item.get("name", "")), args=args if isinstance(args, dict) else {}))
    return calls


def _with_cache_breakpoint(messages):
    # 在最后一条消息的最后一个内容块上加 cache_control，开启显式缓存。
    # 缓存的是“工具定义 + system + 到这个位置为止的全部消息”；下一轮请求只是在后面追加消息，
    # 前面的内容不变，服务端会从标记位置往前找到上一轮写入的缓存并命中。
    # 写入按 125% 计费，命中按 10% 计费，有效期 5 分钟（命中会重新计时），少于 1024 token 不会创建缓存。
    # 复制一份再改，不污染调用方的 messages
    messages = copy.deepcopy(messages)
    last_block = messages[-1]["content"][-1]
    last_block["cache_control"] = {"type": "ephemeral"}
    return messages


def _extract_metadata(data):
    content_block_types = []
    for item in data.get("content", []):
        if isinstance(item, dict):
            block_type = str(item.get("type", "")).strip()
            if block_type:
                content_block_types.append(block_type)
    usage = data.get("usage") or {}
    # 百炼 Anthropic 兼容接口用这两个字段报告上下文缓存：
    # cache_read_input_tokens 是命中缓存的 token，cache_creation_input_tokens 是本次写入缓存的 token。
    # cached_tokens / cache_hit 沿用 trace 汇总统计（aggregate_run_artifacts）读取的字段名。
    cache_read_input_tokens = int(usage.get("cache_read_input_tokens") or 0)
    cache_creation_input_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    return {
        "stop_reason": str(data.get("stop_reason", "") or ""),
        "content_block_types": content_block_types,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cached_tokens": cache_read_input_tokens,
        "cache_hit": cache_read_input_tokens > 0,
    }


class QwenModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        if not api_key:
            raise RuntimeError(f"{QWEN_API_KEY_ENV} is not set; add it to the project .env file")
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.last_completion_metadata = {}

    def complete(self, system, messages, tools, max_new_tokens, tool_choice=None):
        """发送一次请求，返回 ModelResponse（文字 + 工具调用）。

        system：system prompt（身份、规则、工作区信息）
        messages：Anthropic 格式的消息列表，内容都是内容块列表
        tools：工具定义列表 [{"name", "description", "input_schema"}]，为空时不传
        tool_choice：例如 {"type": "none"} 表示本次只能回答、不能调用工具；None 时不传，由模型自己决定
        """
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "system": system,
            "messages": _with_cache_breakpoint(messages),
            "max_tokens": max_new_tokens,
            "stream": False,
            # 开启思考后，带工具调用的多轮对话需要把思考块原样回传，处理更复杂；
            # 思考还会占用 max_tokens，所以固定关闭
            "thinking": {"type": "disabled"},
        }
        if tools:
            # 生成对话摘要时不需要工具，不传 tools 参数
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        retry = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                # 429 限流和 5xx 服务端错误可以重试；400、401 这类是请求本身的问题，重试也没用
                if (exc.code == 429 or exc.code >= 500) and retry < MAX_RETRIES:
                    retry += 1
                    time.sleep(_retry_delay(retry, exc.headers.get("Retry-After")))
                    continue
                raise RuntimeError(f"Qwen request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected, TimeoutError) as exc:
                # 连不上、连接被断开、读取超时
                if retry < MAX_RETRIES:
                    retry += 1
                    time.sleep(_retry_delay(retry))
                    continue
                raise RuntimeError(
                    "Could not reach the Qwen backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Qwen error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Qwen error: {data['error']}")
        self.last_completion_metadata = _extract_metadata(data)
        if (self.last_completion_metadata["output_tokens"] or 0) >= max_new_tokens:
            # 输出写满上限时，百炼仍可能返回 stop_reason=tool_use，工具参数是写了一半的 JSON（变成 raw_arguments）。
            # 统一记为 max_tokens，主循环据此丢弃这次回复并提示模型分段写
            self.last_completion_metadata["stop_reason"] = "max_tokens"
        return ModelResponse(
            text=_extract_text(data),
            tool_calls=_extract_tool_calls(data),
            stop_reason=self.last_completion_metadata["stop_reason"],
            usage=dict(self.last_completion_metadata),
        )
