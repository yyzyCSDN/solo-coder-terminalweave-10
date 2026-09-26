from datetime import datetime, timedelta, timezone

from terminalweave.transshipment.connections import Connection, ConnectionGraph, ServiceCall


UTC = timezone.utc
D = datetime(2026, 1, 1, tzinfo=UTC)


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
