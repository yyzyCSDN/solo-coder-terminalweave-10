from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError, CapacityError

@dataclass(frozen=True)
class Sailing:
    sailing_id: str
    service: str
    destination: str
    departure: datetime
    capacity_teu: int
    booked_teu: int = 0

    def __post_init__(self):
        object.__setattr__(self, 'departure', ensure_utc(self.departure))
        if self.capacity_teu <= 0 or not 0 <= self.booked_teu <= self.capacity_teu:
            raise ValidationError('invalid sailing capacity')

    @property
    def remaining_teu(self) -> int:
        return self.capacity_teu - self.booked_teu

class RebookingPlanner:
    """Chooses the next sailing for a container that missed its connection.

    ``ready_at`` is the earliest moment the container can be loaded, i.e.
    inbound arrival plus minimum transfer time plus yard buffer. A sailing
    departing at or before that moment is already too late and must never
    be selected, no matter how much nominal capacity it has left.
    """

    def choose(
        self,
        destination: str,
        ready_at: datetime,
        teu: int,
        sailings: list[Sailing],
        preferred_service: str | None = None,
        max_wait: timedelta = timedelta(days=7),
        minimum_transfer: timedelta = timedelta(),
        yard_buffer: timedelta = timedelta(),
        exclude: frozenset[str] | None = None,
    ) -> Sailing:
        ready_at = ensure_utc(ready_at)
        if teu <= 0:
            raise ValidationError('TEU must be positive')
        if minimum_transfer.total_seconds() < 0 or yard_buffer.total_seconds() < 0:
            raise ValidationError('transfer durations cannot be negative')
        if max_wait.total_seconds() < 0:
            raise ValidationError('max wait cannot be negative')
        excluded = exclude or frozenset()
        earliest = ready_at + minimum_transfer + yard_buffer
        eligible = [
            s for s in sailings
            if s.destination == destination
            and s.sailing_id not in excluded
            and s.departure >= earliest
            and s.departure - earliest <= max_wait
            and s.remaining_teu >= teu
        ]
        if not eligible:
            raise CapacityError('no eligible rebooking sailing')
        return min(eligible, key=lambda s: (0 if preferred_service and s.service == preferred_service else 1, s.departure, -s.remaining_teu, s.sailing_id))

class BookingLedger:
    """Tracks which sailing each container is booked on.

    Rebooking moves the TEU from the old sailing to the new one exactly
    once; booking a container that is already on the target sailing is a
    no-op, so repeated schedule updates cannot double-book capacity.
    """

    def __init__(self):
        self._sailings: dict[str, Sailing] = {}
        self._booking_by_container: dict[str, tuple[str, int]] = {}

    def register(self, sailing: Sailing) -> None:
        existing = self._sailings.get(sailing.sailing_id)
        if existing is not None and existing != sailing:
            raise ValidationError('conflicting sailing registration')
        self._sailings[sailing.sailing_id] = sailing

    def update_sailing(self, sailing: Sailing) -> None:
        if sailing.sailing_id not in self._sailings:
            raise ValidationError('unknown sailing')
        self._sailings[sailing.sailing_id] = sailing

    def sailing(self, sailing_id: str) -> Sailing:
        try:
            return self._sailings[sailing_id]
        except KeyError:
            raise ValidationError('unknown sailing') from None

    def sailings(self) -> list[Sailing]:
        return [self._sailings[k] for k in sorted(self._sailings)]

    def booking_for(self, container_id: str) -> tuple[str, int] | None:
        return self._booking_by_container.get(container_id)

    def book(self, container_id: str, sailing_id: str, teu: int) -> Sailing:
        if teu <= 0:
            raise ValidationError('TEU must be positive')
        if container_id in self._booking_by_container:
            raise ValidationError('container already booked')
        sailing = self.sailing(sailing_id)
        if sailing.remaining_teu < teu:
            raise CapacityError('insufficient sailing capacity')
        updated = replace(sailing, booked_teu=sailing.booked_teu + teu)
        self._sailings[sailing_id] = updated
        self._booking_by_container[container_id] = (sailing_id, teu)
        return updated

    def rebook(self, container_id: str, new_sailing_id: str) -> Sailing:
        """Move a container's booking to another sailing.

        Idempotent: if the container is already on ``new_sailing_id`` the
        ledger is returned unchanged, so a repeated schedule adjustment
        cannot consume capacity twice.
        """
        current = self._booking_by_container.get(container_id)
        if current is None:
            raise ValidationError('container has no booking to move')
        old_sailing_id, teu = current
        if old_sailing_id == new_sailing_id:
            return self.sailing(new_sailing_id)
        old = self.sailing(old_sailing_id)
        new = self.sailing(new_sailing_id)
        if new.remaining_teu < teu:
            raise CapacityError('insufficient sailing capacity')
        self._sailings[old_sailing_id] = replace(old, booked_teu=old.booked_teu - teu)
        updated = replace(new, booked_teu=new.booked_teu + teu)
        self._sailings[new_sailing_id] = updated
        self._booking_by_container[container_id] = (new_sailing_id, teu)
        return updated
