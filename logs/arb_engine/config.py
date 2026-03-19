import warnings

warnings.warn("DEPRECATED: use bot.core.config", UserWarning, stacklevel=2)

from bot.core.config import *  # noqa: F401,F403
