# Pinned AS-MS custom labware

These Opentrons schema-v2 definitions are physical safety inputs for the AS-MS
workflow. They were supplied with the workflow and are uploaded together with
`../asms_single_point_wash_and_elute.py`.

| Definition | Brand identifier | Capacity | Canonical JSON SHA-256 |
| --- | --- | ---: | --- |
| `azenta_96_wellplate_200ul_pcr.json` | `4ti-0740`, `4ti-0741` | 200 µL/well | `43506d5482e3dfebff377e56b709150c81415473efc9cf2c6362dc4b68a1e20f` |
| `thermokingfisherdeepwell_96_wellplate_2000ul.json` | `95040450` | 2,000 µL/well | `2ea9c15468816ace3970fe497cef7e1dc22d5f9ab033656bf9472a62396dfb47` |

The hashes use UTF-8 canonical JSON with sorted keys and compact separators, so
whitespace-only changes do not alter the identity. Tests also pin schema version,
namespace, load name, brand identifier, 96-well ordering, and per-well capacity.
Any intentional geometry update must be reviewed against the physical labware
and update the corresponding test expectation.
