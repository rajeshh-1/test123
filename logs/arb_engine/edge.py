import warnings

warnings.warn("DEPRECATED: use bot.core.edge", UserWarning, stacklevel=2)

from bot.core.edge import *  # noqa: F401,F403
