"""判分：隐藏用例覆盖文档里的每条规则。"""

from duration import parse_duration

valid = {
    "1h30m": 5400,
    "2d": 172800,
    "45s": 45,
    "1d2h3m4s": 93784,
    "1H 30M": 5400,
    "  10m  ": 600,
    "90": 90,
    "0s": 0,
    "1h0m": 3600,
    "3D 4s": 259204,
}
for text, expected in valid.items():
    got = parse_duration(text)
    assert got == expected and isinstance(got, int), f"parse_duration({text!r}) = {got!r}, expected {expected}"

invalid = ["", "   ", "1x", "-5m", "1.5h", "1h1h", "30m1h", "h", "1hm", "m30", "5 6"]
for text in invalid:
    try:
        parse_duration(text)
    except ValueError:
        continue
    except Exception as exc:
        raise AssertionError(f"parse_duration({text!r}) raised {type(exc).__name__}, expected ValueError")
    raise AssertionError(f"parse_duration({text!r}) should raise ValueError")
print("ok")
