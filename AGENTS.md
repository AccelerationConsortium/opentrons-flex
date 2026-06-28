General, High-Level Feature Design and Grouping
Seed input:
Use an abstraction level suitable for lab staff with no/little technical expertise.
Grouping multiple device models and types from the same vendor that use the same transport protocol into a single SiLA Server is recommended over developing individual SiLA Servers for each of them. Relying on dynamic checking of device type / feature-set and subsequent feature registration on startup
Moving everything into a single feature is bad practice. Group endpoints according to functional groups (See Section 7: Separation of Concerns)
Aim for os-independent implementations wherever possible
Keep descriptions of Server and Feature in a vendor neutral tone
When implementing observable commands, make use of intermediate responses and execution updates (time remaining, progress, execution status)

DO NOT USE OPTIONAL PARAMETERS WITH UNITECDK

Unobservable
Observable
Property
A single value to read

Best for:
Unchanging data sources, e.g.
Serial Number
Server Settings / Configuration
Use with Client-side subscription / polling mechanisms
A stream of values to read

Best For:
Values where Client should be notified on change, e.g.
Sensor read
Status

Command
An action with at most one response

Best For:
Parameterized data accession
Changing settings / configuration
An action which can have status updates and intermediate responses before its final response

Best For:

- Long-running actions
- Robotic movements
- Sensitive Response Data

Error Handling
Seed input:
Never use a boolean response "Success" or similar to indicate the success/failure of a method. This was flagged as a recurring mistake in AI-generated features.
Always create Defined Execution Errors for expected failure modes. Defined Execution Errors should contain as much information as is necessary to enable users to recover from error states.
Any un-annotated exceptions that are thrown will be Undefined Execution Errors and represent unexpected failures for the user.
Surface error codes from the Hardware and don't obfuscate information that could be useful for e.g. the vendor support.
Error messages should be understandable by an operator and be explicit. If possible/applicable, they should contain resolution strategies or suggestions as to what the error could be.
If you adhere to using observable commands for robotic movement, allow cancellation of these movements

Constraints
Seed input:
Every parameter that has a bounded range or a known set of valid values should have constraints that ensure that the user cannot send invalid data to the device.
Exception: In cases of dynamic ranges, i.e. if supporting multiple device models where each model may have a different range of valid inputs, bounds cannot be applied dynamically, and thus should be documented rather than programmatically enforced via constraints.
Enforcement of dynamic constraints should be done through active raising of errors with clear guidance as to the device-specific value ranges.
For numerical input values consider using the Unit constraint to provide context to the user and enable conversions. (Scientists maxim: numbers without units are meaningless)
Use constraints as annotations to add context, not just to enforce value ranges, i.e. by annotating response values.
Use the Set constraint to describe the possible values of a property, e.g. instrument status.
Use the Content Type constraint "image/png" when sending PNG images, or "application/pdf" when sending PDF files as binary data.
Use enums and Set constraints. Don't use strings and raise non Validation error exceptions

Data Types
Seed input:
Use strings (or enums) instead of integers to model instrument status, so features are self-contained and don't require additional context for common interactions.
Integers don't contain context about the state. Context would have to be provided in an additional way, e.g. via parameter description
String contains both (can also be easier understood by AI ;) )
Use dedicated inline structures or custom datatypes for structured responses.
Data sent over a cable/connection should generally be Binary, not String. Using String forces a hardcoded encoding assumption (probably ASCII) that may not match the device.
Try to avoid using unannotated string types for sending xml or json payloads. Take the time to create an inline structure or custom datatype. Passing an xml or json inside a string defeats the purpose of introspection and presents a black box to the user.

Units and Identifiers
Seed input:
Do not embed units in command/property identifiers (e.g. "TimeoutMs"). Use a unit constraint instead and keep the identifier unit-free.
Follow the standard's conventions for converting from Identifier to Display Name.
Consider the user context when naming and try to avoid redundancy. For a feature named TemperatureController prefer start_ramp as method name over start_temperature_ramp, or get_current over get_current_temperature.

Naming Conventions
Seed input:
How much to generalize? E.g. "PlateReaderFeature" or "AnalysisFeature"? "ReadPlate", "ScanPlate", or just "Read" (depending on the feature name)?
“Feature” should not be in the Feature name
Always adhere to Part A best practices using “Controller”, “Provider”, “Service” suffix in feature names
What are default names for common actions?
Examples
Type
Example
Command
SetX, StartX, StopX, VerbAdjectiveNoun
Property
CurrentX, TargetX, AdjectiveNoun
…

No "Get” in unobservable property identifiers. It is implicit.
No "Subscribe” in observable property identifiers. It is implicit.
No Abbreviations
Check SiLA Git repository for existing features and endpoint names <https://gitlab.com/SiLA2/sila_base/-/tree/master/feature_definitions?ref_type=heads>
The SiLA standard (Part A) gives guidance on naming in the Feature Definition sections
SiLA Server Category Naming:
LiquidHandler vs. LiquidHandling? Instrument category or action category
SiLA Feature Category Naming:
Pipettor or Pipetting? Module category or action?

Separation of Concerns
Seed input:
Create one feature per domain/concern of a system. Example: two features "Climate Controller" and "Door Controller" for an incubator, not one combined feature.
Model cross-cutting concerns like locking or authorization via Client Metadata in a separate feature. Example: define a Client Metadata field "Access Token" in an auth-specific feature instead of adding an "Access Token" parameter to all commands of another feature. See also the core AuthorizationService feature.
Separate the implementation of the transport protocol (serial, TCP/IP, … protocol) from the feature implementation.
Wire-format logic (byte framing, checksums, encoding) should not be exposed on feature layer
SiLA types and user-facing structures can be isolated to the feature space

Statelessness
Seed input:
A SiLA Server should expose a stateless surface wherever possible. Internal state (connections, caches, device mode) should be managed automatically by the server.
Requiring a user to first establish a connection (e.g. "connect to serial port") before issuing commands introduces a state machine that makes the server harder to use and reason about.
A SiLA Client or the instance that uses many SiLA Clients must/should (?) be aware of state…

AI-Generated Features: Common Pitfalls
AI tools produce syntactically valid features that miss design best practices.
Common issues: boolean success responses, missing constraints, units embedded in identifiers, String where Binary is appropriate, unnecessary statefulness.
Suggestion: converting best practices into an AGENTS.md file for AI agents to follow.

Project Structure
Add recommended project structure for UniteLabs CDK, SiLA Tecan, SiLA C#, … here
Python
Add comprehensive README.md to your SiLA Server implementations
device-and-server-name/
├── CHANGELOG.md
├── README.md
├── config.json/.yaml/.md/.txt/.csv
├── pyproject.toml
├── uv.lock
├── other files (gitignore, etc.)
├── scripts/
│   ├── script-1.py
│   └── script-2.py
│
├── src/
│   └── company-or-author-name/
│       └── device-name/
│           ├── __init__.py
│           ├── __main__.py
│           │
│           ├── features/                     # SiLA 2 feature implementations
│           │   ├── __init__.py
│           │   ├── device_service.py         # Custom Feature 1
│           │   ├── sealing_controller.py     # Custom Feature 2
│           │   ├── simulation_controller.py  # Core Feature
│           │   ├── stage_controller.py       # Custom Feature 3
│           │   └── another_provider.py       # Feature n
│           │
│           └── io/                           # Hardware I/O layer
│               ├── __init__.py
│               ├── errors.py                 # Custom exception types
│               ├── device_protocol.py        # Communication protocol (e.g. UDP)
│               ├── simulation.py             # Hardware simulation backend
│               ├── units.py                  # Unit conversion helpers
│               └── interfaces/               # Shared data contracts
│                   ├── __init__.py
│                   ├── commands.py           # Command definitions
│                   ├── enums.py              # Device state enums
│                   ├── protocol.py           # Protocol interface/ABC
│                   └── sila_data_types.py    # Data type mappings (Cust. Datatypes or Shorthands)
│
└── tests/
   ├── __init__.py
   ├── conftest.py
   ├── test_version.py
   ├── features/
   │   ├── test_feature_1.py
   │   └── test_feature_n.py
   ├── io/
   │   ├── test_protocol.py
   │   └── test_n.py
   └── simulation/
       └── test_simulation_protocol.py
Server Configuration
Leave technical configuration of a SiLA Server like RS232 ports to the server implementation and configuration (e.g. startup parameters), don't model it in the feature. This makes the feature more generically applicable across vendors and connection mechanisms. (See also: Section 8: Statelessness)
NEVER store secrets in code repositories; always enable external provision of secrets through environment variables or CLI args.
Enable passing
of arguments via command line arguments or environment variables
configuration using a json, yaml, or similar configuration file type

Logging
Suggested default logging behaviour:
FileHandler
Path: ~user/.company/…
Type: Rotating file handler
Size of log file: 50 MB
…

Testing
Always add a simulation mode, even if it just returns mocked data. The simulation implementation should be implemented on the transport protocol level, not in the feature implementation
Add unit tests for every endpoint in the transport protocol implementation (simulated)
Add unit tests for every SiLA endpoint

Flex Deployment and ARM Wheel Builds
The Flex host is aarch64 (ARM64) with a modern glibc — unlike the OT-2 (armv7l,
glibc 2.25), which had to compile grpcio + OpenSSL from source on Debian Stretch.
For the Flex, PyPI's standard manylinux_2_17_aarch64 wheels (grpcio, numpy, rpds-py,
etc.) install directly. Build them with `Dockerfile.build` (`--platform linux/arm64`)
and ship via `deploy.sh`. Do NOT carry over the OT-2 from-source grpcio/OpenSSL
build — it is armv7-specific and unnecessary here.

pip.conf pitfall (still applies): the Opentrons system image ships `/etc/pip.conf`
with `root = /var/user-packages`, which silently redirects every `pip install` —
including inside a venv — away from the venv's own site-packages, so imports fail.
Fix: always pass `--root /` to pip when installing into a venv on the robot (this
overrides the config-file root on the command line). `--isolated` does NOT fix it
(it only suppresses user-level config, not site-level /etc/pip.conf). Call the
venv's pip binary directly (`"$VENV_PATH/bin/pip" install --root / ...`) and do NOT
use `#!/bin/bash` in deployed scripts (the robot's install shell is `sh`).

Hardware Driver Rules
Never use or introduce silent fallback-to-simulation patterns for hardware. If the
OT3API hardware controller fails to initialise (CAN bus, instruments, etc.), raise
an error immediately — do not catch it and substitute a simulator.
Simulation is explicit and opt-in via `use_simulator=True` (which builds
`OT3API.build_hardware_simulator()`) only — never automatic on error.
If you see `except (ImportError, OSError): return SimulatingFoo()` anywhere, remove it.

Flex (OT-3) Axis Reference
The Flex is driven through OT3API over CAN — no Smoothie, no single serial port.
Axis names differ from the OT-2 (which used X/Y/Z/A/B/C):

| Axis | Description |
|------|-------------|
| X | Gantry left/right |
| Y | Gantry front/back |
| Z_L | Left mount up/down |
| Z_R | Right mount up/down |
| P_L | Left pipette plunger |
| P_R | Right pipette plunger |
| Z_G | Gripper mount up/down |
| G | Gripper jaw open/close |
| Q | 96-channel tip-pickup axis |

Mounts (`OT3Mount`): LEFT, RIGHT, GRIPPER. The SiLA MotionControl feature exposes
motion per *mount* in deck coordinates (x, y, z mm), not by raw axis, matching how
OT3API models the Flex.

CI Monitoring
When watching a GitHub Actions run, use: `gh run watch <run-id> --repo <owner/repo> --interval 30` in the background, then `tail -f` its output file. Do NOT write custom Python or shell polling loops that parse the GitHub API — they break on shell variable conflicts and other edge cases. Simple and reliable.

Maintainability and Versioning
Use semantic versioning
Document the models of the instrument the sila server was tested with
Adhere to best practice of the programming language you are using, e.g. PEP8 for Python
Enforce with automated linting and code reformatting e.g. via pre-commit hooks
Add a Changelog.md to your repository to track changes
