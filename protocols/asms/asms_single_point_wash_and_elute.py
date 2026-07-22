"""AS-MS single-point plate wash and methanol elution on Opentrons Flex.

Prepared from the operator-supplied workflow for connector validation.  The
physical custom labware definitions must be uploaded with this file.
"""

from opentrons import protocol_api
from opentrons.types import Point

metadata = {
    "protocolName": "ASMS single point wash and elution via Flex connector",
    "description": (
        "Wash two AS-MS columns, move bead-containing wash 2 into the elution "
        "plate, elute with 80% methanol, and transfer eluate to the MS plate."
    ),
    "author": "HS David Kim; connector validation edits by Acceleration Consortium",
}
requirements = {"robotType": "Flex", "apiLevel": "2.27"}


def add_parameters(parameters: protocol_api.Parameters) -> None:
    """Expose a fast, one-column connector test without changing wet-run defaults."""
    parameters.add_bool(
        display_name="Connector test mode",
        variable_name="connector_test_mode",
        default=False,
        description="Use test liquid and shorten long waits. Disable for scientific runs.",
    )
    parameters.add_int(
        display_name="Number of columns",
        variable_name="number_of_columns",
        default=2,
        minimum=1,
        maximum=2,
        description="Use one column for first hardware contact or two for the full workflow.",
        unit="columns",
    )
    parameters.add_bool(
        display_name="Mutation checkpoints",
        variable_name="enable_mutation_checkpoints",
        default=False,
        description="Pause at audited phase boundaries for validated Protocol Engine steps.",
    )


def run(protocol: protocol_api.ProtocolContext) -> None:
    """Execute the guarded wash and elution workflow."""
    connector_test_mode = protocol.params.connector_test_mode
    number_of_columns = protocol.params.number_of_columns
    enable_mutation_checkpoints = protocol.params.enable_mutation_checkpoints

    def pause(seconds: float) -> None:
        """Preserve scientific waits while keeping dry connector runs fast."""
        protocol.delay(seconds=0.1 if connector_test_mode else seconds)

    def mutation_checkpoint(identifier: str) -> None:
        """Expose a connector-recognized insertion point without enabling it by default."""
        if enable_mutation_checkpoints:
            protocol.pause(f"UNITELABS_MUTATION_CHECKPOINT:{identifier}")

    if connector_test_mode:
        protocol.comment(
            "CONNECTOR TEST MODE: use prepared non-hazardous test liquid only; scientific timing is shortened."
        )

    tips_1000_c3 = protocol.load_labware("opentrons_flex_96_tiprack_1000ul", "C3", label="Right pipette tips C3")
    tips_1000_a3 = protocol.load_labware("opentrons_flex_96_tiprack_1000ul", "A3", label="Right pipette tips A3")
    temperature_module = protocol.load_module("temperature module gen2", location="C1")
    reagent_reservoir = temperature_module.load_labware("nest_12_reservoir_22ml", label="Wash and elution reagents")
    magnetic_block = protocol.load_module("magneticBlockV1", location="B2")
    asms_plate = protocol.load_labware("axygen_96_wellplate_500ul", "A1", label="AS-MS plate")
    elution_plate = protocol.load_labware("axygen_96_wellplate_500ul", "B1", label="Elution plate")
    protocol.load_trash_bin("D1")
    waste_plate = protocol.load_labware(
        "thermokingfisherdeepwell_96_wellplate_2000ul",
        "C2",
        label="Waste plate",
    )
    ms_plate = protocol.load_labware(
        "azenta_96_wellplate_200ul_pcr",
        "A2",
        label="Azenta MS plate",
    )
    right = protocol.load_instrument(
        "flex_8channel_1000",
        "right",
        tip_racks=[tips_1000_c3, tips_1000_a3],
    )

    column_names = [f"A{column}" for column in range(1, number_of_columns + 1)]
    asms_columns = [asms_plate.wells_by_name()[name] for name in column_names]
    elution_columns = [elution_plate.wells_by_name()[name] for name in column_names]
    ms_columns = [ms_plate.wells_by_name()[name] for name in column_names]
    wash_1_tip_anchors = [tips_1000_c3.wells_by_name()[f"A{column}"] for column in range(5, 7)]
    wash_2_tip_anchors = [tips_1000_c3.wells_by_name()[f"A{column}"] for column in range(7, 9)]

    fresh_tip_anchors = []
    for tip_rack in (tips_1000_c3, tips_1000_a3):
        for column in range(1, 13):
            if tip_rack is tips_1000_c3 and column in {5, 6, 7, 8}:
                continue
            fresh_tip_anchors.append(tip_rack.wells_by_name()[f"A{column}"])
    fresh_tip_index = 0

    sample_liquid = protocol.define_liquid(
        name="AS-MS sample",
        description="Starting AS-MS sample volume used for connector resource accounting.",
        display_color="#7A5AF8",
    )
    wash_1_liquid = protocol.define_liquid(
        name="Wash buffer 1",
        description="Prepared wash buffer 1; fill reservoir A2 with exactly 4000 µL.",
        display_color="#2E90FA",
    )
    wash_2_liquid = protocol.define_liquid(
        name="Wash buffer 2",
        description="Prepared wash buffer 2; fill reservoir A3 with exactly 2000 µL.",
        display_color="#12B76A",
    )
    methanol_liquid = protocol.define_liquid(
        name="80% methanol",
        description="Prepared 80% methanol; fill reservoir A4 with exactly 2400 µL.",
        display_color="#F79009",
    )
    tracked_empty = protocol.define_liquid(
        name="Tracked empty volume",
        description="Zero-volume marker enabling fail-closed destination and waste capacity accounting.",
        display_color="#D0D5DD",
    )
    reagent_reservoir.wells_by_name()["A2"].load_liquid(liquid=wash_1_liquid, volume=4000)
    reagent_reservoir.wells_by_name()["A3"].load_liquid(liquid=wash_2_liquid, volume=2000)
    reagent_reservoir.wells_by_name()["A4"].load_liquid(liquid=methanol_liquid, volume=2400)
    for column in range(1, number_of_columns + 1):
        for row in "ABCDEFGH":
            asms_plate.wells_by_name()[f"{row}{column}"].load_liquid(liquid=sample_liquid, volume=250)
            elution_plate.wells_by_name()[f"{row}{column}"].load_liquid(liquid=tracked_empty, volume=0)
            waste_plate.wells_by_name()[f"{row}{column}"].load_liquid(liquid=tracked_empty, volume=0)
            ms_plate.wells_by_name()[f"{row}{column}"].load_liquid(liquid=tracked_empty, volume=0)

    def pick_fresh_tip_column() -> None:
        nonlocal fresh_tip_index
        if fresh_tip_index >= len(fresh_tip_anchors):
            raise RuntimeError(
                "Not enough full tip columns. Install complete racks in A3 and C3; "
                "C3 columns 5-8 are reserved for returned wash-mixing tips."
            )
        right.pick_up_tip(fresh_tip_anchors[fresh_tip_index])
        fresh_tip_index += 1

    def move_to_magnetic_block(plate) -> None:
        protocol.move_labware(plate, magnetic_block, use_gripper=True)
        pause(90)

    def move_to_deck(plate, slot: str) -> None:
        protocol.move_labware(plate, slot, use_gripper=True)
        pause(2)

    def remove_supernatant(
        source_columns,
        volume: float,
        *,
        keep_residual: float = 0,
        side_offset_y: float = 0,
        dregs_air: float = 30,
    ) -> None:
        if not 0 <= keep_residual < volume:
            raise ValueError("keep_residual must be at least 0 and less than the source volume.")
        remove_volume = volume - keep_residual
        offset = Point(y=side_offset_y)

        for source_column in source_columns:
            pick_fresh_tip_column()
            right.move_to(source_column.top(-2))
            first = remove_volume * 0.60
            second = remove_volume * 0.25
            third = remove_volume - first - second

            right.aspirate(first, source_column.bottom(3.0).move(offset), flow_rate=30)
            pause(1)
            right.aspirate(second, source_column.bottom(1.2).move(offset), flow_rate=12)
            pause(1)
            pause(2)
            right.aspirate(third, source_column.bottom(0.4).move(offset), flow_rate=4)
            pause(1)

            if dregs_air > 0:
                right.air_gap(dregs_air)
                pause(2)
            right.air_gap(10)

            source_well_name = source_column.display_name.split(" of ", 1)[0]
            waste_well = waste_plate.wells_by_name()[source_well_name]
            right.dispense(remove_volume + dregs_air + 10, waste_well.top(-5), flow_rate=80)
            right.blow_out(waste_well.top(-2))
            right.touch_tip(waste_well, v_offset=-2, speed=20)
            right.drop_tip()

    def add_wash_buffer(
        source_well,
        destination_columns,
        volume: float,
        *,
        mix_repetitions: int,
        parked_mix_tip_anchors,
    ) -> None:
        for index, destination_column in enumerate(destination_columns):
            pick_fresh_tip_column()
            right.aspirate(volume, source_well.bottom(1), flow_rate=10)
            right.dispense(volume, destination_column.bottom(10), flow_rate=10)
            right.touch_tip(destination_column, v_offset=-2, speed=20)
            right.drop_tip()

            right.pick_up_tip(parked_mix_tip_anchors[index])
            saved_aspirate = right.flow_rate.aspirate
            saved_dispense = right.flow_rate.dispense
            try:
                right.flow_rate.aspirate = 20
                right.flow_rate.dispense = 20
                right.mix(mix_repetitions, min(volume * 0.5, 200), destination_column.bottom(2))
            finally:
                right.flow_rate.aspirate = saved_aspirate
                right.flow_rate.dispense = saved_dispense
            right.touch_tip(destination_column, v_offset=-2, speed=20)
            right.return_tip()

    def transfer_columns(source_columns, destination_columns, volume: float) -> None:
        for source_column, destination_column in zip(source_columns, destination_columns):
            pick_fresh_tip_column()
            right.aspirate(volume, source_column.bottom(1), flow_rate=10)
            right.dispense(volume, destination_column.bottom(1), flow_rate=10)
            right.blow_out(destination_column.top(-2))
            right.touch_tip(destination_column, v_offset=-2, speed=20)
            right.drop_tip()

    def add_methanol_to_elution_plate(source_well, destination_columns, volume: float) -> None:
        for destination_column in destination_columns:
            pick_fresh_tip_column()
            for _ in range(3):
                right.aspirate(volume, source_well.bottom(1), flow_rate=10)
                right.dispense(volume, source_well.bottom(1), flow_rate=10)
            right.aspirate(volume, source_well.bottom(1), flow_rate=10)
            right.dispense(volume, destination_column.bottom(1), flow_rate=10)
            saved_aspirate = right.flow_rate.aspirate
            saved_dispense = right.flow_rate.dispense
            try:
                right.flow_rate.aspirate = 20
                right.flow_rate.dispense = 20
                right.mix(5, min(volume * 0.5, 200), destination_column.bottom(2))
            finally:
                right.flow_rate.aspirate = saved_aspirate
                right.flow_rate.dispense = saved_dispense
            right.touch_tip(destination_column, v_offset=-2, speed=20)
            right.drop_tip()

    def transfer_methanol_to_ms_plate(source_columns, destination_columns, volume: float) -> None:
        for source_column, destination_column in zip(source_columns, destination_columns):
            pick_fresh_tip_column()
            for _ in range(2):
                right.aspirate(volume, source_column.bottom(1), flow_rate=10)
                right.dispense(volume, source_column.bottom(1), flow_rate=10)
            right.aspirate(volume, source_column.bottom(1), flow_rate=10)
            right.dispense(volume, destination_column.bottom(1), flow_rate=10)
            right.blow_out(destination_column.top(-2))
            right.touch_tip(destination_column, v_offset=-2, speed=20)
            right.drop_tip()

    try:
        temperature_module.set_temperature(4)
        mutation_checkpoint("ready-before-initial-separation")
        protocol.comment("Separate initial AS-MS supernatant")
        move_to_magnetic_block(asms_plate)
        remove_supernatant(asms_columns, 250)
        move_to_deck(asms_plate, "A1")

        mutation_checkpoint("before-wash-1-round-1")
        protocol.comment("Wash 1, round 1")
        add_wash_buffer(
            reagent_reservoir.wells_by_name()["A2"],
            asms_columns,
            100,
            mix_repetitions=3,
            parked_mix_tip_anchors=wash_1_tip_anchors,
        )
        move_to_magnetic_block(asms_plate)
        remove_supernatant(asms_columns, 100)
        move_to_deck(asms_plate, "A1")

        mutation_checkpoint("before-wash-1-round-2")
        protocol.comment("Wash 1, round 2")
        add_wash_buffer(
            reagent_reservoir.wells_by_name()["A2"],
            asms_columns,
            100,
            mix_repetitions=5,
            parked_mix_tip_anchors=wash_1_tip_anchors,
        )
        move_to_magnetic_block(asms_plate)
        remove_supernatant(asms_columns, 100)
        move_to_deck(asms_plate, "A1")

        mutation_checkpoint("before-wash-2-transfer")
        protocol.comment("Wash 2 and move bead-containing liquid to the elution plate")
        add_wash_buffer(
            reagent_reservoir.wells_by_name()["A3"],
            asms_columns,
            100,
            mix_repetitions=5,
            parked_mix_tip_anchors=wash_2_tip_anchors,
        )
        transfer_columns(asms_columns, elution_columns, 100)

        mutation_checkpoint("before-elution-plate-separation")
        protocol.comment("Separate beads in the elution plate and remove wash 2")
        move_to_magnetic_block(elution_plate)
        remove_supernatant(elution_columns, 100)
        move_to_deck(elution_plate, "B1")

        mutation_checkpoint("before-methanol-elution")
        protocol.comment("Elute beads in 80% methanol")
        add_methanol_to_elution_plate(reagent_reservoir.wells_by_name()["A4"], elution_columns, 120)
        pause(120)
        move_to_magnetic_block(elution_plate)

        mutation_checkpoint("before-ms-plate-transfer")
        protocol.comment("Transfer clarified methanol eluate from the elution plate to the MS plate")
        transfer_methanol_to_ms_plate(elution_columns, ms_columns, 100)
    finally:
        temperature_module.deactivate()
