### Python Script for preparing a KingFisher Plate with OpenTrons Flex, script performs addition of and incubates with magnetic beads and compounds ###

###############################################################################
# WORKTABLE SETUP
#
# Deck layout (viewed from front):
#
#   +-------+-------+-------+
#   |  A1   |  A2   |  A3   |
#   | KF    | Tube  | 50 µL |
#   | Plate | Rack  | Tips  |
#   +-------+-------+-------+
#   |  B1   |  B2   |  B3   |
#   | (open)| (open)| (open)|
#   +-------+-------+-------+
#   |  C1   |  C2   |  C3   |
#   | Temp  | (open)| 1000µL|
#   | Mod + |       | Tips  |
#   | Res.  |       |       |
#   +-------+-------+-------+
#   |  D1   |  D2   |  D3   |
#   | Trash | (open)| (open)|
#   +-------+-------+-------+
#
# Slot A1  - KingFisher Deep Well Plate (ThermoFisher, 96-well, 2000 µL)
#            Row A: for 2 pool analysis, wells A1 to A6 are for negative control (magnetic beads only) and wells A7 and A12 will have protein + beads + compound pool
#            Row B and C: wash buffer #1 (1000 µL/well)
#            Row D: wash buffer #2 (1000 µL/well)
#            Row E: 80% methanol (120 µL/well)
#
# Slot A2  - Opentrons 24-well tube rack with 1.5 mL snap cap adapter (999-00030)
#            A1: 1.03 µM protein solution in incubation buffer (pre-loaded; 242.5 µL dispensed per corresponding wells) in 750 µL
#            A2: 1.03 µM protein solution in incubation buffer (pre-loaded; 242.5 µL dispensed per corresponding wells) in 750 µL
#            A3: magnetic beads (pre-loaded; ~35 µL dispensed per set of 6 wells)
#            A4: compound pool 1 (pre-loaded; ~15 µL dispensed per set of 6 wells) (can be pool 3, 5, or 7)
#            A5: compound pool 2 (pre-loaded; ~15 µL dispensed per set of 6 wells) (can be pool 4, 6, or 8)
#
# Slot A3  - Opentrons Flex 96 tip rack (50 µL)
#            H1: Used for bead addition to A1-A6
#            G1: Used for bead addition to A7-A12
#            F1: Used for compound pool addition to A1-A6
#            E1: Used for compound pool addition to A7-A12
#
# Slot C1  - Temperature Module Gen2 (set to 4 °C)
#            Loaded with NEST 12-column reservoir (22 mL):
#            A1: Incubation buffer (~1455 µL needed)
#            A2: wash buffer for rows B1-B6  (~6 x 1000 µL needed)
#            A3: wash buffer for rows B7-B12 (~6 x 1000 µL needed)
#            A4: elution buffer              (~12 x 1000 µL needed)
#            A5: 80% methanol               (~960 µL needed)
#
# Slot C3  - Opentrons Flex 96 tip rack (1000 µL)
#            H1: Used for incubation buffer dispense into KF plate A1 to A6
#            G1: Used for 1.03 µM protein solution dispense into KF plate A7 to A12
#            E1: Used for wash buffer #1 (rows B1-B12 and rows C1-C12) This should pick up 2 tips
#            D1: Used for wash buffer #2 (row D)
#            C1: Used for 80% methanol (row E)
#
# Slot D1  - Trash bin
#
# Pipettes:
#   Left mount:  Flex 8-channel 50 µL  (tip rack: A3)
#   Right mount: Flex 8-channel 1000 µL (tip rack: C3)
###############################################################################

from opentrons import protocol_api

metadata = {
    "protocolName": "ASMS single point analysis plate preparation",
    "description": "Prepares a KingFisher Plate with a fixed concentration of protein target and incubates with magnetic beads and compound pools for single point analysis. Method good for analyzing two pools, one protein/target",
    "author": "HS David Kim"
}
requirements = {"robotType": "Flex", "apiLevel": "2.24"}

def run(protocol: protocol_api.ProtocolContext):
   
    # Load labware
    tips_1000_c3 = protocol.load_labware("opentrons_flex_96_tiprack_1000ul", "C3")
    tips_50_a3   = protocol.load_labware("opentrons_flex_96_tiprack_50ul", "A3")
   
    # Loading the temperature module and reagent reservoirs
    temp_mod = protocol.load_module(module_name="temperature module gen2", location="C1")
    reagent_reservoir = temp_mod.load_labware("nest_12_reservoir_22ml")
    KF_plate = protocol.load_labware("thermokingfisherdeepwell_96_wellplate_2000ul", "A1") #May want to change to loading from a labware definition if interest in running remotely

    # 24-well tube rack with 1.5 mL snap cap adapter (Opentrons 999-00030)
    # A1: 1.03 µM protein solution in incubation buffer, A2: magnetic beads, A3: compound pool 1, A4: compound pool 2
    tube_rack = protocol.load_labware("opentrons_24_tuberack_nest_1.5ml_snapcap", "A2")

    trash_bin = protocol.load_trash_bin("D1")

     # Load instruments
    left = protocol.load_instrument("flex_8channel_50",  "left",  tip_racks=[tips_50_a3])
    right = protocol.load_instrument("flex_8channel_1000","right", tip_racks=[tips_1000_c3])

    # Set Temperature Module to 4 °C
    temp_mod.set_temperature(4)

    # Function for dispensing 800 µL from A1 of the reagent reservoir into 3 wells, then another 800 µL into the next 3 wells using one tip
    def dispense_six_wells_from_A1_single_tip(pipette, source_well, plate, first_three, second_three, tip_well, trash_bin):
        pipette.pick_up_tip(tip_well)

        pipette.aspirate(800, source_well.bottom(1), flow_rate=358)
        for well_name in first_three:
            pipette.dispense(242.5, plate[well_name].bottom(1), flow_rate=120)
            pipette.touch_tip(plate[well_name], radius=0.75, v_offset=-1)

        pipette.move_to(trash_bin.top())
        pipette.blow_out()
        protocol.delay(seconds=3)

        pipette.aspirate(800, source_well.bottom(1), flow_rate=358)
        for well_name in second_three:
            pipette.dispense(242.5, plate[well_name].bottom(1), flow_rate=120)
            pipette.touch_tip(plate[well_name], radius=0.75, v_offset=-1)

        pipette.drop_tip()

    # Aspirate from reagent reservoir column of choice (A2 for June 17th) and dispense into A1-A3, then A4-A6 using one tip
    dispense_six_wells_from_A1_single_tip(right, reagent_reservoir['A2'], KF_plate, ['A1', 'A2', 'A3'], ['A4', 'A5', 'A6'], tips_1000_c3['H1'], trash_bin)

    # Function for dispensing 242.5 µL of 1.03 µM protein solution (in A2 and A3 of the tube rack) into each target well
    def dispense_protein_into_A7_to_A12(pipette, source_well, plate, target_well_names, tip_well):
        pipette.pick_up_tip(tip_well)
        pipette.aspirate(740, source_well.bottom(1), flow_rate=120)
        for well_name in target_well_names:
            pipette.dispense(242.5, plate[well_name].bottom(1), flow_rate=120)
            pipette.touch_tip(plate[well_name], radius=0.75, v_offset=-1)
        pipette.drop_tip()

    dispense_protein_into_A7_to_A12(right, tube_rack['A1'], KF_plate, ['A7', 'A8', 'A9'], tips_1000_c3['G1']) #for 15th of June, 1.03uM of protein/target dispense into A7 to A9
    dispense_protein_into_A7_to_A12(right, tube_rack['A2'], KF_plate, ['A10', 'A11', 'A12'], tips_1000_c3['F1']) #for 17th of June, 1.03uM of protein/target dispense into A10 to A12

    #function for dispensing wash buffers into the KingFisher plate
    def dispense_wash_buffer(well_list, source_well):   
        for well in well_list:
            right.aspirate(1000, reagent_reservoir.wells_by_name()[source_well].bottom(1.0), flow_rate = 144)
            protocol.delay(seconds=3)
            right.dispense(1000, KF_plate.wells_by_name()[well].bottom(1), flow_rate = 144)
            right.touch_tip(KF_plate.wells_by_name()[well], radius=0.75, v_offset=-1)

    # Function for aspirating 800 µL and dispensing 120 µL of 80% methanol from column of choice of the reagent reservoir into row E wells using one tip
    def dispense_methanol_into_E_wells(pipette, source_well, plate, first_three, second_three, trash_bin):

        pipette.aspirate(800, source_well.bottom(1)) #aspirates 800 µL to ensure enough volume for dispensing into 6 wells (120 µL x 6 = 720 µL) while accounting for some loss during dispensing and tip retention
        for well_name in first_three:
            pipette.dispense(120, plate[well_name].bottom(1), flow_rate = 358)
            pipette.touch_tip(plate[well_name], radius=0.75, v_offset=-1)

        pipette.move_to(trash_bin.top())
        pipette.blow_out()
        protocol.delay(seconds=3)

        pipette.aspirate(800, source_well.bottom(1))
        for well_name in second_three:
            pipette.dispense(120, plate[well_name].bottom(1), flow_rate = 358)
            pipette.touch_tip(plate[well_name], radius=0.75, v_offset=-1)

        pipette.move_to(trash_bin.top())
        pipette.drop_tip()
    
    #function for mixing the beads slurry solution (A3 of tube rack) and dispensing into row A wells of the KingFisher plate
    def add_beads_to_wells(tip_position, destination_wells):
        left.pick_up_tip(tips_50_a3[tip_position])
        for _ in range(3):
            left.aspirate(30, tube_rack.wells_by_name()["A3"].bottom(0.5))
            left.dispense(30, tube_rack.wells_by_name()["A3"])
        left.aspirate(35, tube_rack.wells_by_name()["A3"].bottom(0.5), flow_rate=10)
        for well in destination_wells:
            left.dispense(5, KF_plate.wells_by_name()[well].bottom(1))
            left.touch_tip(KF_plate.wells_by_name()[well], radius=0.75, v_offset=-1)
        left.drop_tip()

    #function for dispensing compound pool 1 (position A4 of tube rack) into the KingFisher plate
    def add_pool_1_to_wells(tip_position, destination_wells):
        left.pick_up_tip(tips_50_a3[tip_position])
        left.aspirate(20, tube_rack.wells_by_name()["A4"].bottom(0.5))
        for well in destination_wells:
            left.dispense(2.5, KF_plate.wells_by_name()[well].bottom(1))
            left.touch_tip(KF_plate.wells_by_name()[well], radius=0.75, v_offset=-1)
        left.drop_tip()
    #function for dispensing compound pool 2 (position A5 of tube rack) into the KingFisher plate
    def add_pool_2_to_wells(tip_position, destination_wells):
        left.pick_up_tip(tips_50_a3[tip_position])
        left.aspirate(20, tube_rack.wells_by_name()["A5"].bottom(0.5))
        for well in destination_wells:
            left.dispense(2.5, KF_plate.wells_by_name()[well].bottom(1))
            left.touch_tip(KF_plate.wells_by_name()[well], radius=0.75, v_offset=-1)
        left.drop_tip()       


    # picks up two tips from position D1 of the 1000 µL tip rack to dispense wash buffer #1 into rows B and C of the KingFisher plate
    #right.pick_up_tip(tips_1000_c3["D1"])
    # pre-wet the tip once with column A2 before dispensing wash buffer; column A3 uses the same solution so no second pre-wet is needed
    #right.aspirate(1000, reagent_reservoir.wells_by_name()["A2"].bottom(1.0), flow_rate=144)
    #right.dispense(1000, reagent_reservoir.wells_by_name()["A2"].top(), flow_rate=144)
    #dispense_wash_buffer(["B1", "B2", "B3", "B4", "B5", "B6"], "A2")
    #dispense_wash_buffer(["B7", "B8", "B9", "B10", "B11", "B12"], "A3") 
    # dispose of tips
    #right.drop_tip() as of June 9th 2026, temporarily will dispense wash buffer #1 manually due to air bubbles still being formed in tip

    # picks up one tip from position C1 of the 1000 µL tip rack to dispense wash buffer #2 into row D of the KingFisher plate
    right.pick_up_tip(tips_1000_c3["E1"])
    dispense_wash_buffer(["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10", "D11", "D12"], "A3")
    # dispose of tip
    right.drop_tip()

    #Picks up tip, then aspirates and dispenses 1000 µL of buffer from the tenth column of the reagent reservoir (80% methanol) for pre-wetting the tip
    right.pick_up_tip(tips_1000_c3['D1'])
    for _ in range(3):
        right.aspirate(1000, reagent_reservoir.wells_by_name()["A4"].bottom(0.5))
        right.dispense(1000, reagent_reservoir.wells_by_name()["A4"].bottom(0.5))

    # Aspirate 800 µL of buffer from the tenth column of the reagent reservoir (80% Methanol) and adds 120 µL to E1 to E6 of KF plate, then E7 to E12
    dispense_methanol_into_E_wells(right, reagent_reservoir['A4'], KF_plate, ['E1', 'E2', 'E3', 'E4', 'E5', 'E6'], ['E7', 'E8', 'E9', 'E10', 'E11', 'E12'], trash_bin)

    #picks up a 50 µL tip from position H1 of the 50 µL tip rack to call the add_beads_to_wells function and mix the beads slurry and dispense into A1 to A6 of the KingFisher plate
    add_beads_to_wells("H1", ["A1", "A2", "A3", "A4", "A5", "A6"])
    #picks up a 50 µL tip from position G1 of the 50 µL tip rack to call the add_beads_to_wells function and mix the beads slurry and dispense into A7 to A9 of the KingFisher plate
    add_beads_to_wells("G1", ["A7", "A8", "A9","A10", "A11", "A12"])
    #picks up a 50 µL tip from position F1 of the 50 µL tip rack to call the add_beads_to_wells function and mix the beads slurry and dispense into A10 to A12 of the KingFisher plate
   
    #picks up a 50 µL tip from position F1 of the 50 µL tip rack to call the add_pool_1_to_wells function and dispense compound first pool into A1 to A3 of Kingfisher plate
    add_pool_1_to_wells("F1", ["A1", "A2", "A3"]) #For June 15th, this adds WDR91 reference cmpd into A1 to A3
    #picks up a 50 µL tip from position E1 of the 50µL tip rack to call the add_pool_1_to_wells function and dispense compound first pool into A7 to A9 of Kingfisher plate
    add_pool_1_to_wells("E1", ["A7", "A8", "A9"]) #For June 15th, this adds WDR91 reference cmpd into A7 to A9
    #picks up a 50 µL tip from position F1 of the 50 µL tip rack to call the add_pool_2_to_wells function and dispense compound second pool into A4 to A6 of Kingfisher plate
    add_pool_2_to_wells("D1", ["A4", "A5", "A6"]) #For June 15th, this adds WDR5 control cmpds into A4 to A6
    #picks up a 50 µL tip from position E1 of the 50µL tip rack to call the add_pool_2_to_wells function and dispense compound second pool into A10 to A12 of Kingfisher plate
    add_pool_2_to_wells("C1", ["A10", "A11", "A12"]) #For June 15th, this adds WDR5 control cmpds into A10 to A12

    # Deactivate temperature module
    temp_mod.deactivate()
