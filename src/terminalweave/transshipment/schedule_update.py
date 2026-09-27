from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import timedelta
from terminalweave.core.errors import ValidationError, ConflictError
from terminalweave.transshipment.connections import Connection, ConnectionGraph, ServiceCall
from terminalweave.transshipment.dwell import DwellRecord
from terminalweave.transshipment.lineage import ContainerLeg, LineageGraph
from terminalweave.transshipment.rebooking import BookingLedger, RebookingPlanner, Sailing

@dataclass(frozen=True)
class ScheduleUpdateResult:
    call_id: str
    rebooked: tuple[tuple[str, str, str], ...] = ()
    """(container_id, old_outbound_call, new_outbound_call) per rebook."""

class TransshipmentPlan:
    """Coordinates connection, booking, lineage and dwell state so a vessel
    schedule change propagates consistently to every affected container.

    The same adjustment applied twice is a no-op the second time: slack is
    recomputed from the live schedule, bookings move TEU exactly once, and
    lineage legs are rebased rather than duplicated.
    """

    def __init__(self, graph: ConnectionGraph, ledger: BookingLedger | None = None, lineage: LineageGraph | None = None):
        self.graph = graph
        self.ledger = ledger or BookingLedger()
        self.lineage = lineage or LineageGraph()
        self.dwell_records: dict[str, DwellRecord] = {}
        self._outbound_leg_by_container: dict[str, str] = {}
        self._planner = RebookingPlanner()

    # -- registration -------------------------------------------------

    def register_dwell(self, record: DwellRecord) -> None:
        self.dwell_records[record.container_id] = record

    def register_outbound_leg(self, container_id: str, leg: ContainerLeg) -> None:
        if leg.container_id != container_id:
            raise ValidationError('leg container mismatch')
        self.lineage.add(leg)
        self._outbound_leg_by_container[container_id] = leg.leg_id

    # -- schedule adjustment ------------------------------------------

    def adjust_call(self, call: ServiceCall, *, max_wait: timedelta = timedelta(days=7)) -> ScheduleUpdateResult:
        """Apply a new arrival/departure window to an existing call.

        Recomputes slack for every connection touching the call, rebooks
        containers whose minimum transfer time no longer fits onto the
        earliest eligible sailing, and keeps dwell anchors and lineage
        legs aligned with the new times.
        """
        previous = self.graph.calls.get(call.call_id)
        if previous is None:
            raise ValidationError('unknown service call')
        if previous == call:
            return ScheduleUpdateResult(call.call_id)

        self.graph.update_call(call)
        self._sync_sailing_departure(call)

        rebooked: list[tuple[str, str, str]] = []
        for connection in self.graph.connections_touching(call.call_id):
            if self.graph.feasible(connection):
                self._reschedule_dwell(connection)
                continue
            rebooked.append(self._rebook(connection, max_wait=max_wait))
        return ScheduleUpdateResult(call.call_id, tuple(rebooked))

    def _sync_sailing_departure(self, call: ServiceCall) -> None:
        try:
            sailing = self.ledger.sailing(call.call_id)
        except ValidationError:
            return
        if sailing.departure != call.departure:
            self.ledger.update_sailing(replace(sailing, departure=call.departure))

    def _rebook(self, connection: Connection, *, max_wait: timedelta) -> tuple[str, str, str]:
        container_id = connection.container_id
        booking = self.ledger.booking_for(container_id)
        if booking is None:
            raise ConflictError(f'container {container_id} has no booking to rebook')
        current_sailing_id, teu = booking
        old_outbound = connection.outbound_call

        ready_at = self.graph.ready_at(connection)
        destination = self._destination_for(container_id, current_sailing_id)
        # Exclude the sailing the container is already on: even if its
        # departure were nudged later, re-selecting it would double-book.
        excluded = frozenset({current_sailing_id, old_outbound})
        choice = self._planner.choose(
            destination,
            ready_at,
            teu,
            self.ledger.sailings(),
            preferred_service=self._preferred_service(old_outbound),
            max_wait=max_wait,
            exclude=excluded,
        )

        tracked_leg = self._build_replacement_leg(container_id, choice)
        if tracked_leg is not None:
            leg_id, new_leg = tracked_leg
            # Fail before anything moves if the new route cannot connect.
            self.lineage.validate_rebase(leg_id, new_leg)
        new_connection = self.graph.replace_outbound(connection, choice.sailing_id)
        self.ledger.rebook(container_id, choice.sailing_id)
        if tracked_leg is not None:
            leg_id, new_leg = tracked_leg
            self.lineage.rebase(leg_id, new_leg)
            self._outbound_leg_by_container[container_id] = new_leg.leg_id
        self._reschedule_dwell(new_connection)
        return (container_id, old_outbound, choice.sailing_id)

    def _destination_for(self, container_id: str, sailing_id: str) -> str:
        record = self.dwell_records.get(container_id)
        if record is not None:
            return record.destination
        return self.ledger.sailing(sailing_id).destination

    def _preferred_service(self, call_id: str) -> str | None:
        call = self.graph.calls.get(call_id)
        return call.service if call else None

    def _build_replacement_leg(self, container_id: str, sailing: Sailing) -> tuple[str, ContainerLeg] | None:
        leg_id = self._outbound_leg_by_container.get(container_id)
        if leg_id is None:
            return None
        old_leg = self.lineage.legs.get(leg_id)
        if old_leg is None:
            return None
        # Deterministic id per target sailing: repeated rebook attempts on
        # the same live leg are caught as duplicates instead of minting
        # parallel legs that would split the container's route.
        new_leg_id = f'{leg_id}::{sailing.sailing_id}'
        if new_leg_id in self.lineage.legs:
            raise ConflictError('container already rebased onto sailing')
        new_leg = ContainerLeg(
            leg_id=new_leg_id,
            container_id=container_id,
            mode=old_leg.mode,
            origin=old_leg.origin,
            destination=sailing.destination,
            departure=sailing.departure,
            arrival=sailing.departure + (old_leg.arrival - old_leg.departure),
            parent_leg_id=old_leg.parent_leg_id,
        )
        return (leg_id, new_leg)

    def _reschedule_dwell(self, connection: Connection) -> None:
        record = self.dwell_records.get(connection.container_id)
        if record is None:
            return
        inbound = self.graph.calls[connection.inbound_call]
        outbound = self.graph.calls[connection.outbound_call]
        self.dwell_records[connection.container_id] = record.reschedule(
            discharged_at=inbound.arrival,
            loaded_at=outbound.departure,
        )
