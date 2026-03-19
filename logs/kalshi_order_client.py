import warnings

warnings.warn("DEPRECATED: use bot.core.execution.kalshi_client", UserWarning, stacklevel=2)

from bot.core.execution.kalshi_client import *  # noqa: F401,F403
