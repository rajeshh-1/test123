import warnings

warnings.warn("DEPRECATED: use bot.core.*", UserWarning, stacklevel=2)

from bot.core import *  # noqa: F401,F403
