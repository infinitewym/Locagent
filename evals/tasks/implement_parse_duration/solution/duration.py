import re

UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
SEGMENT = re.compile(r"(\d+)([dhms])\s*")


def parse_duration(text):
    """把时长文本解析成秒数（int）。

    规则：
    - 由一段或多段“非负整数 + 单位”组成，单位：d（天）、h（小时）、m（分钟）、s（秒），
      例如 "1h30m"、"2d"、"45s"、"1d2h3m4s"
    - 单位不区分大小写；段与段之间可以有空格，例如 "1H 30M"；首尾空格忽略
    - 每个单位最多出现一次，并且必须按 d、h、m、s 的顺序出现，例如 "30m1h" 不合法
    - 只有一个整数、没有单位时按秒处理，例如 "90" 返回 90
    - 空字符串、未知单位、负数、小数、单位重复、顺序错误、缺少数字（例如 "h"）都抛 ValueError
    """
    text = text.strip().lower()
    if text.isdigit():
        return int(text)
    if not text:
        raise ValueError("empty duration")
    total = 0
    position = 0
    last_order = -1
    order = list(UNITS)
    while position < len(text):
        match = SEGMENT.match(text, position)
        if not match:
            raise ValueError(f"invalid duration: {text!r}")
        unit_order = order.index(match.group(2))
        if unit_order <= last_order:
            raise ValueError(f"units out of order or repeated: {text!r}")
        last_order = unit_order
        total += int(match.group(1)) * UNITS[match.group(2)]
        position = match.end()
    return total
