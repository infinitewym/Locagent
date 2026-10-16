"""模型返回值的统一结构。

使用接口原生的工具调用后，模型的一次回复不再只是一段文字，
而是“文字 + 若干个工具调用”。真实模型和假模型都返回 ModelResponse，
主循环只依赖这个结构。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolCall:
    # id 由模型生成，回传工具结果时用 tool_use_id 对应上
    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    # end_turn：正常结束；tool_use：要求调用工具；max_tokens：输出被截断
    stop_reason: str = ""
    # 本次请求的用量：input_tokens、output_tokens、cache_read_input_tokens、cache_creation_input_tokens 等
    usage: dict = field(default_factory=dict)
