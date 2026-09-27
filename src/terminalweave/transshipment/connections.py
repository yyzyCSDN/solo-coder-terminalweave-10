from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from terminalweave.core.clock import ensure_utc
from terminalweave.core.errors import ValidationError, ConflictError

@dataclass(frozen=True)
class ServiceCall:
    call_id: str
    service: str
    arrival: datetime
    departure: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, 'arrival', ensure_utc(self.arrival))
        object.__setattr__(self, 'departure', ensure_utc(self.departure))
        if self.departure <= self.arrival:
            raise ValidationError('service call departure must follow arrival')

@dataclass(frozen=True)
class Connection:
    container_id: str
    inbound_call: str
    outbound_call: str
    minimum_transfer: timedelta
    yard_buffer: timedelta = timedelta()

    def __post_init__(self) -> None:
        if self.minimum_transfer.total_seconds() < 0 or self.yard_buffer.total_seconds() < 0:
            raise ValidationError('transfer durations cannot be negative')

class ConnectionGraph:

    def __init__(self):
        self.calls: dict[str, ServiceCall] = {}
        self.connections: list[Connection] = []
        self._slack_by_container: dict[str, timedelta] = {}
        self._connections_by_container: dict[str, Connection] = {}

    def add_call(self, call: ServiceCall) -> None:
        if call.call_id in self.calls:
            raise ValidationError('duplicate service call')
        self.calls[call.call_id] = call

    def update_call(self, call: ServiceCall) -> None:
        if call.call_id not in self.calls:
            raise ValidationError('unknown service call')
        self.calls[call.call_id] = call
        # Every slack that depends on this call must be recomputed; a stale
        # cached value would make an already-too-tight connection look open.
        self._recompute_slacks(call.call_id)

    def feasible(self, connection: Connection) -> bool:
        return self._calculate_slack(connection) >= timedelta()

    def ready_at(self, connection: Connection) -> datetime:
        inbound = self.calls[connection.inbound_call]
        return inbound.arrival + connection.minimum_transfer + connection.yard_buffer

    def add_connection(self, connection: Connection) -> None:
        if connection.inbound_call == connection.outbound_call:
            raise ConflictError('connection cannot loop to same call')
        if connection.inbound_call not in self.calls or connection.outbound_call not in self.calls:
            raise ValidationError('unknown service call')
        if connection.container_id in self._connections_by_container:
            raise ConflictError('container already has a transshipment connection')
        if not self.feasible(connection):
            raise ConflictError('minimum connection time cannot be met')
        self.connections.append(connection)
        self._connections_by_container[connection.container_id] = connection
        self._slack_by_container[connection.container_id] = self._calculate_slack(connection)

    def connections_touching(self, call_id: str) -> list[Connection]:
        """Connections whose inbound or outbound leg is the given call.

        Returned in container order so schedule adjustments process
        affected containers deterministically.
        """
        return sorted(
            (c for c in self.connections if c.inbound_call == call_id or c.outbound_call == call_id),
            key=lambda c: c.container_id,
        )

    def connection_for(self, container_id: str) -> Connection | None:
        return self._connections_by_container.get(container_id)

    def replace_outbound(self, connection: Connection, new_outbound_call: str) -> Connection:
        """Rebook a container onto another outbound call.

        The replacement is validated against the live schedule (the new
        call must exist and still meet the minimum transfer time), and the
        slack cache is refreshed atomically with the swap.
        """
        current = self._connections_by_container.get(connection.container_id)
        if current is None:
            raise ValidationError('unknown connection')
        if new_outbound_call == current.outbound_call:
            return current
        if new_outbound_call not in self.calls:
            raise ValidationError('unknown service call')
        if new_outbound_call == current.inbound_call:
            raise ConflictError('connection cannot loop to same call')
        rebooked = Connection(
            current.container_id,
            current.inbound_call,
            new_outbound_call,
            current.minimum_transfer,
            current.yard_buffer,
        )
        if not self.feasible(rebooked):
            raise ConflictError('minimum connection time cannot be met')
        index = self.connections.index(current)
        self.connections[index] = rebooked
        self._connections_by_container[rebooked.container_id] = rebooked
        self._slack_by_container[rebooked.container_id] = self._calculate_slack(rebooked)
        return rebooked

    def service_edges(self) -> dict[str, set[str]]:
        edges = {call_id: set() for call_id in self.calls}
        for c in self.connections:
            edges[c.inbound_call].add(c.outbound_call)
        return edges

    def detect_cycle(self) -> list[str] | None:
        edges = self.service_edges()
        visiting = set()
        visited = set()
        stack = []

        def dfs(node: str):
            if node in visiting:
                idx = stack.index(node)
                return stack[idx:] + [node]
            if node in visited:
                return None
            visiting.add(node)
            stack.append(node)
            for nxt in sorted(edges[node]):
                cycle = dfs(nxt)
                if cycle:
                    return cycle
            stack.pop()
            visiting.remove(node)
            visited.add(node)
            return None
        for node in sorted(edges):
            cycle = dfs(node)
            if cycle:
                return cycle
        return None

    def validate_acyclic(self) -> None:
        cycle = self.detect_cycle()
        if cycle:
            raise ConflictError('service connection cycle: ' + ' -> '.join(cycle))

    def _recompute_slacks(self, call_id: str) -> None:
        for c in self.connections:
            if c.inbound_call == call_id or c.outbound_call == call_id:
                self._slack_by_container[c.container_id] = self._calculate_slack(c)

    def _calculate_slack(self, connection: Connection) -> timedelta:
        outbound = self.calls[connection.outbound_call]
        return outbound.departure - self.ready_at(connection)

    def slack(self, connection: Connection) -> timedelta:
        # Always derived from the live schedule: callers may hold a stale
        # Connection object after a rebook, so resolve the current connection
        # for the container first.
        live = self._connections_by_container.get(connection.container_id, connection)
        return self._calculate_slack(live)
