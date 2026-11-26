"""A small Flex protocol for the step-console import test: one single-tip transfer."""

from opentrons import protocol_api

requirements = {"robotType": "Flex", "apiLevel": "2.20"}


def run(protocol: protocol_api.ProtocolContext) -> None:
    tips = protocol.load_labware("opentrons_flex_96_tiprack_1000ul", "C3")
    plate = protocol.load_labware("nest_96_wellplate_2ml_deep", "C2")
    trash = protocol.load_trash_bin("D1")
    pipette = protocol.load_instrument("flex_8channel_1000", "right", tip_racks=[tips])
    water = protocol.define_liquid("Water", display_color="#5385BD")
    plate["A1"].load_liquid(water, 500)
    pipette.configure_nozzle_layout(protocol_api.SINGLE, start="A1", tip_racks=[tips])
    pipette.pick_up_tip(tips["H1"])
    pipette.aspirate(100, plate["A1"])
    pipette.dispense(100, plate["A2"])
    pipette.drop_tip(trash)
