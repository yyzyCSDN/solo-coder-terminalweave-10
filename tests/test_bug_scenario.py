from datetime import datetime, timedelta, timezone

import pytest

from terminalweave.core.errors import CapacityError, ConflictError
from terminalweave.transshipment import (
    BookingLedger,
    ContainerLeg,
    DwellRecord,
    LineageGraph,
    Sailing,
    TransshipmentPlan,
)
from terminalweave.transshipment.connections import Connection, ConnectionGraph, ServiceCall
from terminalweave.transshipment.rebooking import RebookingPlanner


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


def test_inbound_delay_recomputes_slack_as_well():
    graph = ConnectionGraph()
    graph.add_call(ServiceCall("IN", "S1", D, D + timedelta(hours=1)))
    graph.add_call(ServiceCall("OUT", "S2", D + timedelta(hours=5), D + timedelta(hours=8)))
    connection = Connection("CONT1", "IN", "OUT", timedelta(hours=4))
    graph.add_connection(connection)
    graph.update_call(ServiceCall("IN", "S1", D + timedelta(hours=2), D + timedelta(hours=3)))
    # ready D+2 + 4h transfer = D+6, outbound departs D+8 -> 2h slack.
    assert graph.slack(connection) == timedelta(hours=2)


def _build_plan():
    graph = ConnectionGraph()
    graph.add_call(ServiceCall("IN", "S1", D, D + timedelta(hours=1)))
    graph.add_call(ServiceCall("OUT1", "S2", D + timedelta(hours=5), D + timedelta(hours=8)))
    # A same-service sailing that departs before the container can be ready.
    graph.add_call(ServiceCall("BAD", "S2", D + timedelta(hours=2), D + timedelta(hours=3)))
    graph.add_call(ServiceCall("NEXT", "S3", D + timedelta(hours=9), D + timedelta(hours=10)))
    connection = Connection("CONT1", "IN", "OUT1", timedelta(hours=4))
    graph.add_connection(connection)

    ledger = BookingLedger()
    for sailing in (
        Sailing("OUT1", "S2", "B", D + timedelta(hours=8), 10, 0),
        Sailing("BAD", "S2", "B", D + timedelta(hours=3), 10, 0),
        Sailing("NEXT", "S3", "B", D + timedelta(hours=10), 10, 0),
    ):
        ledger.register(sailing)
    ledger.book("CONT1", "OUT1", 1)

    lineage = LineageGraph()
    plan = TransshipmentPlan(graph, ledger, lineage)
    plan.register_dwell(DwellRecord("CONT1", D, D + timedelta(hours=8), "B"))
    lineage.add(ContainerLeg("INL", "CONT1", "sea", "P", "T", D - timedelta(days=2), D))
    plan.register_outbound_leg(
        "CONT1",
        ContainerLeg("OUT1L", "CONT1", "sea", "T", "B", D + timedelta(hours=8), D + timedelta(days=2), "INL"),
    )
    lineage.add(
        ContainerLeg("RAIL", "CONT1", "rail", "B", "I", D + timedelta(days=3), D + timedelta(days=4), "OUT1L")
    )
    return plan, graph, ledger, lineage


def test_rebook_skips_already_too_late_sailing_and_keeps_route_intact():
    plan, graph, ledger, lineage = _build_plan()

    result = plan.adjust_call(
        ServiceCall("OUT1", "S2", D + timedelta(hours=2), D + timedelta(hours=3))
    )

    # BAD departs at D+3 but the container is only ready at D+4: NEXT wins.
    assert result.rebooked == (("CONT1", "OUT1", "NEXT"),)
    live = graph.connection_for("CONT1")
    assert live.outbound_call == "NEXT"
    assert graph.slack(live) == timedelta(hours=6)
    # TEU moved exactly once.
    assert ledger.sailing("OUT1").booked_teu == 0
    assert ledger.sailing("NEXT").booked_teu == 1
    # Dwell anchors follow the new outbound call.
    record = plan.dwell_records["CONT1"]
    assert record.loaded_at == D + timedelta(hours=10)
    # The original route is re-wired rather than severed.
    new_leg_id = plan._outbound_leg_by_container["CONT1"]
    assert [leg.leg_id for leg in lineage.chain("RAIL")] == ["INL", new_leg_id, "RAIL"]
    assert lineage.legs["RAIL"].parent_leg_id == new_leg_id
    assert "OUT1L" not in lineage.legs


def test_repeated_schedule_update_does_not_rebook_twice():
    plan, graph, ledger, lineage = _build_plan()
    updated = ServiceCall("OUT1", "S2", D + timedelta(hours=2), D + timedelta(hours=3))

    first = plan.adjust_call(updated)
    second = plan.adjust_call(updated)

    assert first.rebooked == (("CONT1", "OUT1", "NEXT"),)
    assert second.rebooked == ()
    assert ledger.sailing("OUT1").booked_teu == 0
    assert ledger.sailing("NEXT").booked_teu == 1
    assert graph.connection_for("CONT1").outbound_call == "NEXT"
    new_leg_id = plan._outbound_leg_by_container["CONT1"]
    assert [leg.leg_id for leg in lineage.chain("RAIL")] == ["INL", new_leg_id, "RAIL"]


def test_still_feasible_update_only_refreshes_slack_and_dwell():
    plan, graph, ledger, _ = _build_plan()
    result = plan.adjust_call(
        ServiceCall("OUT1", "S2", D + timedelta(hours=5), D + timedelta(hours=7))
    )
    assert result.rebooked == ()
    assert graph.slack(graph.connection_for("CONT1")) == timedelta(hours=3)
    assert plan.dwell_records["CONT1"].loaded_at == D + timedelta(hours=7)
    assert ledger.sailing("OUT1").booked_teu == 1


def test_lineage_conflict_blocks_rebook_before_anything_moves():
    plan, graph, ledger, lineage = _build_plan()
    # Downstream feeder departs after the old outbound leg arrives (D+2d)
    # but before the replacement would arrive (D+2d+2h): the swap would
    # overlap it, so the rebook must fail before anything moves.
    lineage.add(
        ContainerLeg(
            "FEEDER", "CONT1", "sea", "B", "Q",
            D + timedelta(days=2, hours=1), D + timedelta(days=2, hours=1, minutes=30), "OUT1L",
        )
    )
    with pytest.raises(ConflictError):
        plan.adjust_call(
            ServiceCall("OUT1", "S2", D + timedelta(hours=2), D + timedelta(hours=3))
        )
    # Pre-validation failed: no store observed the rebook.
    assert graph.connection_for("CONT1").outbound_call == "OUT1"
    assert ledger.booking_for("CONT1")[0] == "OUT1"
    assert "OUT1L" in lineage.legs


def test_no_eligible_sailing_leaves_connection_and_booking_intact():
    plan, graph, ledger, _ = _build_plan()
    with pytest.raises(CapacityError):
        plan.adjust_call(
            ServiceCall("OUT1", "S2", D + timedelta(hours=2), D + timedelta(hours=3)),
            max_wait=timedelta(hours=2),
        )
    # Selection fails before any mutation: the broken status quo is preserved.
    assert graph.connection_for("CONT1").outbound_call == "OUT1"
    assert ledger.sailing("OUT1").booked_teu == 1
    assert ledger.sailing("NEXT").booked_teu == 0


def test_planner_never_picks_sailing_departing_before_ready_plus_transfer():
    bad = Sailing("BAD", "S2", "B", D + timedelta(hours=3), 10, 0)
    good = Sailing("NEXT", "S3", "B", D + timedelta(hours=10), 10, 0)
    picked = RebookingPlanner().choose(
        "B", D, 1, [bad, good], minimum_transfer=timedelta(hours=4)
    )
    assert picked.sailing_id == "NEXT"


def test_ledger_rebook_is_idempotent():
    ledger = BookingLedger()
    ledger.register(Sailing("OLD", "S2", "B", D + timedelta(hours=8), 10, 0))
    ledger.register(Sailing("NEW", "S3", "B", D + timedelta(days=1), 10, 0))
    ledger.book("CONT1", "OLD", 2)
    ledger.rebook("CONT1", "NEW")
    ledger.rebook("CONT1", "NEW")
    assert ledger.sailing("OLD").booked_teu == 0
    assert ledger.sailing("NEW").booked_teu == 2
    assert ledger.booking_for("CONT1") == ("NEW", 2)


def test_lineage_rebase_reconnects_downstream_legs():
    lineage = LineageGraph()
    lineage.add(ContainerLeg("A", "C", "sea", "P", "T", D, D + timedelta(days=1)))
    lineage.add(ContainerLeg("B", "C", "sea", "T", "X", D + timedelta(days=2), D + timedelta(days=3), "A"))
    lineage.add(ContainerLeg("R", "C", "rail", "X", "I", D + timedelta(days=4), D + timedelta(days=5), "B"))

    lineage.rebase(
        "B",
        ContainerLeg("B2", "C", "sea", "T", "Y", D + timedelta(days=2), D + timedelta(days=3), "A"),
    )

    assert [leg.leg_id for leg in lineage.chain("R")] == ["A", "B2", "R"]
    # Downstream leg origin is kept continuous with the new route.
    assert lineage.legs["R"].origin == "Y"


def test_rebase_onto_existing_replacement_is_idempotent_for_reparent():
    lineage = LineageGraph()
    lineage.add(ContainerLeg("A", "C", "sea", "P", "T", D, D + timedelta(days=1)))
    lineage.add(ContainerLeg("B", "C", "sea", "T", "X", D + timedelta(days=2), D + timedelta(days=3), "A"))
    lineage.add(ContainerLeg("R", "C", "rail", "X", "I", D + timedelta(days=4), D + timedelta(days=5), "B"))
    replacement = ContainerLeg("B2", "C", "sea", "T", "X", D + timedelta(days=2), D + timedelta(days=3), "A")

    lineage.rebase("B", replacement)
    # Re-delivering the same replacement re-parents again without duplicating.
    lineage.rebase("B2", replacement)

    assert [leg.leg_id for leg in lineage.chain("R")] == ["A", "B2", "R"]

