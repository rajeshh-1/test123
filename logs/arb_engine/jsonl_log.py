import warnings

warnings.warn("DEPRECATED: use bot.core.storage.jsonl_logger", UserWarning, stacklevel=2)

from bot.core.storage.jsonl_logger import *  # noqa: F401,F403
