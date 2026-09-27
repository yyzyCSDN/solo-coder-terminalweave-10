from datetime import datetime, timedelta, timezone

import pytest

from terminalweave.core.errors import ConflictError
from terminalweave.transshipment.connections import Connection, ConnectionGraph, ServiceCall


UTC = timezone.utc
D = datetime(2026, 1, 1, tzinfo=UTC)


def _graph_with_connection():
    graph = ConnectionGraph()
    graph.add_call(ServiceCall("IN", "S1", D, D + timedelta(hours=1)))
    graph.add_call(ServiceCall("OUT", "S2", D + timedelta(hours=5), D + timedelta(hours=8)))
    connection = Connection("CONT1", "IN", "OUT", timedelta(hours=4))
    graph.add_connection(connection)
    return graph, connection


def test_schedule_update_recomputes_existing_connection_slack():
    graph = ConnectionGraph()
    inbound = ServiceCall("IN", "S1", D, D + timedelta(hours=1))
    outbound = ServiceCall("OUT", "S2", D + timedelta(hours=5), D + timedelta(hours=8))
    graph.add_call(inbound)
    graph.add_call(outbound)
    connection = Connection("CONT1", "IN", "OUT", timedelta(hours=4))
    graph.add_connection(connection)
    assert graph.slack(connection) == timedelta(hours=4)

    graph.update_call(
        ServiceCall("OUT", "S2", D + timedelta(hours=5), D + timedelta(hours=6))
    )
    assert graph.slack(connection) == timedelta(hours=2)


def test_inbound_schedule_update_recomputes_slack():
    graph, connection = _graph_with_connection()

    graph.update_call(ServiceCall("IN", "S1", D + timedelta(hours=2), D + timedelta(hours=3)))
    assert graph.slack(connection) == timedelta(hours=2)


def test_repeated_schedule_update_is_idempotent():
    graph, connection = _graph_with_connection()
    updated = ServiceCall("OUT", "S2", D + timedelta(hours=5), D + timedelta(hours=6))

    graph.update_call(updated)
    graph.update_call(updated)
    assert graph.slack(connection) == timedelta(hours=2)
    assert len(graph.connections) == 1


def test_connection_update_keeps_transfer_buffer_and_slack_consistent():
    graph, _ = _graph_with_connection()
    updated = Connection("CONT1", "IN", "OUT", timedelta(hours=5), timedelta(hours=1))

    graph.update_connection(updated)
    assert graph.slack(updated) == timedelta(hours=2)

    graph.update_connection(updated)
    assert graph.slack(updated) == timedelta(hours=2)
    assert len(graph.connections) == 1


def test_rebooking_updates_route_and_slack_together():
    graph, _ = _graph_with_connection()
    graph.add_call(ServiceCall("OUT2", "S2", D + timedelta(hours=6), D + timedelta(hours=9)))
    rebooked = Connection("CONT1", "IN", "OUT2", timedelta(hours=4))

    graph.update_connection(rebooked)
    assert graph.connections == [rebooked]
    assert graph.slack(rebooked) == timedelta(hours=5)

    graph.update_connection(rebooked)
    assert graph.connections == [rebooked]
    assert graph.slack(rebooked) == timedelta(hours=5)


def test_rebooking_to_missed_sailing_is_rejected_and_route_kept():
    graph, connection = _graph_with_connection()
    graph.add_call(ServiceCall("LATE", "S2", D + timedelta(hours=2), D + timedelta(hours=3)))

    with pytest.raises(ConflictError):
        graph.update_connection(Connection("CONT1", "IN", "LATE", timedelta(hours=4)))
    assert graph.connections == [connection]
    assert graph.slack(connection) == timedelta(hours=4)


def test_repeated_add_does_not_double_rebook():
    graph, connection = _graph_with_connection()

    with pytest.raises(ConflictError):
        graph.add_connection(connection)
    assert graph.connections == [connection]
    assert graph.slack(connection) == timedelta(hours=4)
