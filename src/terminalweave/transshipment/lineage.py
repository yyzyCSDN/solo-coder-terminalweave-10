from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError, ConflictError

@dataclass(frozen=True)
class ContainerLeg:
    leg_id: str
    container_id: str
    mode: str
    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    parent_leg_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, 'departure', ensure_utc(self.departure))
        object.__setattr__(self, 'arrival', ensure_utc(self.arrival))
        if self.arrival <= self.departure:
            raise ValidationError('arrival must follow departure')

class LineageGraph:

    def __init__(self):
        self.legs: dict[str, ContainerLeg] = {}

    def add(self, leg: ContainerLeg) -> None:
        if leg.leg_id in self.legs:
            raise ValidationError('duplicate leg')
        self._validate_link(leg)
        self.legs[leg.leg_id] = leg

    def _validate_link(self, leg: ContainerLeg) -> None:
        if leg.parent_leg_id:
            parent = self.legs.get(leg.parent_leg_id)
            if not parent:
                raise ValidationError('parent leg does not exist')
            if parent.container_id != leg.container_id:
                raise ConflictError('lineage cannot cross containers')
            if parent.destination != leg.origin:
                raise ConflictError('lineage location discontinuity')
            if parent.arrival > leg.departure:
                raise ConflictError('lineage time overlap')

    def validate_rebase(self, old_leg_id: str, new_leg: ContainerLeg) -> ContainerLeg:
        """Normalize and validate a replacement leg without mutating the graph.

        Returns the leg as it would be stored (parent inherited from the leg
        being replaced); raises if the swap would break the route.
        """
        old = self.legs.get(old_leg_id)
        if old is None:
            raise ValidationError('unknown leg')
        if new_leg.container_id != old.container_id:
            raise ConflictError('lineage cannot cross containers')
        existing = self.legs.get(new_leg.leg_id)
        if existing is not None:
            if existing.parent_leg_id != old.parent_leg_id:
                raise ConflictError('replacement leg parent mismatch')
            normalized = existing
        else:
            normalized = replace(new_leg, parent_leg_id=old.parent_leg_id)
            self._validate_link(normalized)
        # Downstream legs must still follow the replacement in time.
        for leg in self.legs.values():
            if leg.parent_leg_id == old_leg_id and leg.departure < normalized.arrival:
                raise ConflictError('lineage time overlap')
        return normalized

    def rebase(self, old_leg_id: str, new_leg: ContainerLeg) -> ContainerLeg:
        """Swap one leg for another while keeping the whole route connected.

        The new leg must carry the same container and attach to the same
        parent as the leg it replaces; any downstream legs are re-parented
        onto it. Re-delivering a replacement that is already in place only
        re-connects children, so a repeated schedule update never severs or
        duplicates the container's route.
        """
        normalized = self.validate_rebase(old_leg_id, new_leg)
        if normalized.leg_id != old_leg_id:
            self.legs[normalized.leg_id] = normalized
            self._reparent_children(old_leg_id, normalized.leg_id)
            del self.legs[old_leg_id]
        else:
            # Same leg re-delivered in place: only re-connect children that
            # were left pointing at the removed predecessor.
            self._reparent_children(old_leg_id, normalized.leg_id)
        return normalized

    def _reparent_children(self, old_parent_id: str, new_parent_id: str) -> None:
        new_parent = self.legs[new_parent_id]
        for leg_id, leg in list(self.legs.items()):
            if leg.parent_leg_id == old_parent_id:
                reparented = replace(
                    leg,
                    origin=new_parent.destination,
                    parent_leg_id=new_parent_id,
                )
                if reparented.departure < new_parent.arrival:
                    raise ConflictError('lineage time overlap')
                self.legs[leg_id] = reparented

    def chain(self, leg_id: str) -> list[ContainerLeg]:
        result = []
        current = self.legs[leg_id]
        seen = set()
        while current:
            if current.leg_id in seen:
                raise ConflictError('lineage cycle')
            seen.add(current.leg_id)
            result.append(current)
            current = self.legs.get(current.parent_leg_id) if current.parent_leg_id else None
        return list(reversed(result))

    def total_transit_seconds(self, leg_id: str) -> float:
        chain = self.chain(leg_id)
        return (chain[-1].arrival - chain[0].departure).total_seconds()
