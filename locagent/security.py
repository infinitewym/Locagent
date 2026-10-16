"""安全相关：命令执行环境的变量白名单、密钥识别与脱敏。

两道防线：
1. run_shell 执行的命令只能看到白名单里的环境变量，看不到 QWEN_API_KEY 这类密钥
2. 工具结果在交给模型和写进 trace 之前，把密钥的值替换成 <redacted>
   （例如模型读了 .env，或者命令输出里带出了密钥）
"""

import os

REDACTED_VALUE = "<redacted>"

# 命令执行时保留的环境变量。Windows 上的变量名不区分大小写，os.environ 里统一是大写。
# SYSTEMROOT、COMSPEC、PATHEXT 缺了很多程序无法启动；LOCALAPPDATA、APPDATA、USERPROFILE 缺了 uv 找不到缓存目录
SHELL_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "USERNAME",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TMPDIR",
    "LANG",
    "LC_ALL",
)

# 默认当作密钥的变量名；还可以在 .env 里用 SECRET_ENV_NAMES=A,B 追加
DEFAULT_SECRET_ENV_NAMES = ("QWEN_API_KEY", "DASHSCOPE_API_KEY", "GITHUB_PAT", "GH_PAT")
SECRET_ENV_NAMES_VAR = "SECRET_ENV_NAMES"
# 变量名以这些结尾的也当作密钥
SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
# 太短的值不做替换：例如某个 TOKEN 变量的值是 "1"，替换后会把所有的 1 都改掉
MIN_SECRET_LENGTH = 8


def shell_env():
    # 从当前进程的环境变量里只挑白名单里的，再强制子进程里的 python 用 UTF-8 输出，避免中文乱码
    env = {name: os.environ[name] for name in SHELL_ENV_ALLOWLIST if name in os.environ}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _secret_names():
    names = set(DEFAULT_SECRET_ENV_NAMES)
    extra = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    names.update(item.strip().upper() for item in extra.split(",") if item.strip())
    return names


def is_secret_env_name(name):
    upper = str(name).upper()
    return upper in _secret_names() or upper.endswith(SENSITIVE_ENV_NAME_MARKERS)


def secret_values():
    # 当前环境里所有密钥的值，按长度从长到短排，替换时长的先换，避免短值把长值切碎
    values = {
        value
        for name, value in os.environ.items()
        if is_secret_env_name(name) and len(value) >= MIN_SECRET_LENGTH
    }
    return sorted(values, key=len, reverse=True)


def redact_text(text):
    for value in secret_values():
        text = text.replace(value, REDACTED_VALUE)
    return text
