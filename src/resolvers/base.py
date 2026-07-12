"""
resolvers/base.py -- the interface every catalog source implements.

This is the seam. Spotify is no longer special; it's one Resolver among many.
Hand this file to Wavlake and say: "implement this."
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RightsClass(str, Enum):
    """Decides which report a play lands in. Set once, at resolve time."""
    COLLECTING_SOCIETY = "collecting_society"   # BUMA/STEMRA + SENA reportable
    V4V = "v4v"                                 # Wavlake etc — sats to the artist
    OWNED = "owned"                             # bought outright (Bandcamp)
    PUBLIC_DOMAIN = "public_domain"
    UNKNOWN = "unknown"                         # must be resolved before airing


class Availability(str, Enum):
    PLAYABLE = "playable"        # we can air it right now, no extra cost
    PURCHASABLE = "purchasable"  # we could buy it — costs sats, needs consent
    METADATA_ONLY = "metadata_only"  # we know OF it but cannot air it (Spotify)
    MISS = "miss"


@dataclass
class Track:
    """The single shape every source returns."""
    title: str
    artist: str
    source: str                   # "local" | "wavlake" | "spotify" | "bandcamp"
    source_uri: str               # how THIS source finds it again
    rights_class: RightsClass = RightsClass.UNKNOWN
    availability: Availability = Availability.MISS

    duration_s: Optional[int] = None
    isrc: Optional[str] = None    # SENA reports at recording level. Chase this.
    album: Optional[str] = None
    year: Optional[int] = None

    ln_address: Optional[str] = None         # V4V: where the artist's sats go
    acquire_cost_sats: Optional[int] = None  # PURCHASABLE: cost to buy
    local_path: Optional[str] = None         # set once it's on disk
    sha256: Optional[str] = None             # dedup + integrity (Cardinal Rule)

    extra: dict = field(default_factory=dict)

    def display(self) -> str:
        return f"{self.artist} - {self.title}"

    def key(self) -> str:
        """Normalised identity for dedup across sources."""
        return f"{self.artist.strip().lower()}|{self.title.strip().lower()}"


class Resolver(ABC):
    """
    A catalog source.

    Contract:
      search()  -- cheap, may return several candidates, [] on miss, NEVER raises
      resolve() -- commit to one track; fill duration/isrc; None if it slipped away
      acquire() -- only for PURCHASABLE, only AFTER the requester has funded it
      payout()  -- only for V4V sources
    """

    name: str = "base"
    rights_class: RightsClass = RightsClass.UNKNOWN

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[Track]:
        ...

    @abstractmethod
    async def resolve(self, track: Track) -> Optional[Track]:
        ...

    async def acquire(self, track: Track) -> Optional[Track]:
        """Default: this source cannot buy anything."""
        return None

    async def payout(self, track: Track, sats: int) -> bool:
        """Default: this source has no payout channel."""
        return False
