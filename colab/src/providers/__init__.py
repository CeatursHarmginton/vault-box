from __future__ import annotations

from .base import BaseProvider, ProviderFailure
from .drive import DriveProvider
from .pikpak import PikPakProvider
from .terabox import TeraBoxProvider

PROVIDERS: dict[str, BaseProvider] = {
    "pikpak": PikPakProvider(),
    "drive": DriveProvider(),
    "terabox": TeraBoxProvider(),
}
