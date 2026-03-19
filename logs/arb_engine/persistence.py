import warnings

warnings.warn("DEPRECATED: use bot.core.storage.sqlite_store", UserWarning, stacklevel=2)

from bot.core.storage.sqlite_store import *  # noqa: F401,F403
