"""HP 7475A protocol constants — the single source of truth.

Every raw byte sequence used to talk to the plotter lives here. Nothing else
in the codebase may embed escape sequences or HP-GL string literals.

All facts below are verified against:
- HP 7475A Interfacing and Programming Manual (Ch. 1-3, 7, 10)
- HP 7475A Operation and Interconnection Manual
Cross-checked with vpype 1.15 ``vpype_config.toml`` device ``hp7475a``.
See docs/hardware-notes.md for full citations.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Physical constants (Prog. Manual §7-2; vpype hp7475a.plotter_unit_length)
# ---------------------------------------------------------------------------

PLOTTER_UNIT_MM = 0.02488  #: millimetres per plotter unit
PLOTTER_UNIT_IN = 0.00098  #: inches per plotter unit
PEN_COUNT = 6              #: physical pen carousel positions
INPUT_BUFFER_BYTES = 1024  #: plotter input buffer size (Prog. Manual §10-14)

#: Minimum/maximum pen velocity in cm/s; steps of 0.38 cm/s (Prog. Manual §3-3)
VELOCITY_MIN_CM_S = 0.38
VELOCITY_MAX_CM_S = 38.1
VELOCITY_STEP_CM_S = 0.38

# ---------------------------------------------------------------------------
# HP-GL instructions used by this application
# Each constant carries its manual reference. Output instructions reply with
# a line terminated by OUTPUT_TERMINATOR (default CR on RS-232-C).
# ---------------------------------------------------------------------------

#: Initialize plotter state; clears errors, does not move pen.
#: Prog. Manual §1-13 "The Initialize Instruction, IN"
HPGL_INIT = "IN;"

#: Default plotter conditions (like IN, plus resets P1/P2 to hard-clip corners).
#: Prog. Manual §1-13
HPGL_DEFAULTS = "DF;"

#: Select pen n (0 = store current pen / pen away). Prog. Manual §3-2.
HPGL_SELECT_PEN_FMT = "SP{pen};"

#: Pen up, optionally moving to absolute x,y first. Prog. Manual §3-2.
HPGL_PEN_UP_FMT = "PU{x},{y};"
HPGL_PEN_UP = "PU;"

#: Pen down, optionally moving first. Prog. Manual §3-2.
HPGL_PEN_DOWN_FMT = "PD{x},{y};"
HPGL_PEN_DOWN = "PD;"

#: Plot absolute: move/draw through the listed points. Prog. Manual §3-4.
HPGL_PLOT_ABSOLUTE_FMT = "PA{x0},{y0},...;"

#: Velocity select in cm/s (0.38-38.1, rounded to 0.38 steps by plotter).
#: Prog. Manual §3-3
HPGL_VELOCITY_FMT = "VS{velocity};"

# --- Output (query) instructions: reply "in parse order", i.e. only after  ---
# --- previously buffered instructions have executed (Prog. Manual Ch.7/10). ---

#: Output Identification — always replies "7475A". Prog. Manual §7-6.
HPGL_OUTPUT_IDENTIFICATION = "OI;"

#: Output Actual Position & pen status — replies "X,Y,P". Prog. Manual §7-2.
HPGL_OUTPUT_ACTUAL_POSITION = "OA;"

#: Output Commanded position — replies "X,Y,P". Prog. Manual §7-3.
HPGL_OUTPUT_COMMANDED_POSITION = "OC;"

#: Output Error (HP-GL error number 0-8). Prog. Manual §7 (OE).
HPGL_OUTPUT_ERROR = "OE;"

#: Output Options — replies e.g. "0,1,0,0,1,0,0,0". Prog. Manual §7-6.
HPGL_OUTPUT_OPTIONS = "OO;"

#: Output Status byte (decimal 0-255). Prog. Manual §7-6.
HPGL_OUTPUT_STATUS = "OS;"

#: Output P1/P2. Prog. Manual Ch.2.
HPGL_OUTPUT_P1_P2 = "OP;"

# ---------------------------------------------------------------------------
# RS-232-C device-control (escape) instructions
# Syntax: ESC . <letter> [params] : — parameterized forms end with ':'.
# Prog. Manual Ch.10.
# ---------------------------------------------------------------------------

ESC = "\x1b"

#: Output Buffer Space — replies with decimal 0..1024 free bytes.
#: Prog. Manual §10-28. Core of the software-checking handshake.
ESC_OUTPUT_BUFFER_SPACE = ESC + ".B;"

#: Output Extended Error — RS-232 I/O error 0 or 10-16 (16 = buffer overflow).
#: Prog. Manual §10-29.
ESC_OUTPUT_EXTENDED_ERROR = ESC + ".E;"

#: Device-reset of comm config: default buffer (1024), hardwire handshake
#: enabled, monitor off, transmission mode unchanged. Prog. Manual §10-27.
#: The default form ``ESC .@:`` leaves Data Transmission Mode unchanged.
ESC_CONFIGURE_DEFAULT = ESC + ".@:"

#: Configure buffer/handshake. Second parameter bits (Prog. Manual §10-27):
#:   bit0 (1)  = enable hardwire (DTR pin 20) handshake
#:   bit2 (2)  = monitor mode type (0=on parse, 1=on receive)
#:   bit3 (8)  = enable monitor mode
#:   bit4 (16) = block mode (0/absent = normal mode)
#: Example: ESC .@;19: -> buffer default 1024, hardwire OFF (bit0=0 is
#: achieved with value 18; 19 = 16+2+1 = block+monitor-type1+hardwire) —
#: callers must compose bits explicitly via configure_frame().
ESC_CONFIGURE_FMT = ESC + ".@{p1};{p2}:"

#: Handshake mode 1 (ENQ/ACK): block size; enquiry char; ack string.
#: Prog. Manual §10-32.
ESC_HANDSHAKE_MODE_1_FMT = ESC + ".H{block};{enq};{ack}:"

#: Handshake mode 2 (XON/XOFF or ENQ/ACK without full ESC .M framing).
#: Params: block size; enquiry char (NUL=0 selects XON/XOFF thresholds);
#: ack string. Prog. Manual §10-33.
ESC_HANDSHAKE_MODE_2_FMT = ESC + ".I{block};{enq};{ack}:"

#: Output framing: turnaround delay; output trigger; echo terminate;
#: output terminator; output initiator. Prog. Manual §10-24.
ESC_OUTPUT_FORMAT_FMT = ESC + ".M{t_delay};{trigger};{echo_term};{term};{initiator}:"

#: Intercharacter delay + immediate response string. Prog. Manual §10-26.
ESC_DELAY_FMT = ESC + ".N{delay};{response}:"

# ---------------------------------------------------------------------------
# Framing constants
# ---------------------------------------------------------------------------

#: Default plotter output terminator on RS-232-C (Prog. Manual Ch.7).
OUTPUT_TERMINATOR = "\r"

#: Termination character for HP-GL instruction sequences we generate.
INSTRUCTION_TERMINATOR = ";"

# ---------------------------------------------------------------------------
# Status byte bit masks (Prog. Manual §7-7)
# ---------------------------------------------------------------------------

STATUS_PEN_DOWN = 0x01        # bit 0: pen down
STATUS_P1P2_CHANGED = 0x02    # bit 1
STATUS_DIGITIZE_READY = 0x04  # bit 2
STATUS_INITIALIZED = 0x08     # bit 3 (power-up; cleared by reading OS)
STATUS_READY = 0x10           # bit 4: ready for data (pinch wheels down)
STATUS_ERROR = 0x20           # bit 5: error (cleared by OE / IN)
STATUS_RSV = 0x40             # bit 6

#: Expected power-up status: 8 (initialized) + 16 (ready) = 24.
STATUS_POWER_UP = 24

#: Identification string the 7475A returns for OI; (Prog. Manual §7-6).
IDENTIFICATION = "7475A"

#: Options string the 7475A returns for OO; (Prog. Manual §7-6).
#: 1s at position 2 (pen select) and 5 (arcs/circles).
OPTIONS_PEN_SELECT = 2
OPTIONS_ARCS_CIRCLES = 5

# ---------------------------------------------------------------------------
# HP-GL error numbers returned by OE; (Prog. Manual Ch.7)
# ---------------------------------------------------------------------------

HPGL_ERRORS: dict[int, str] = {
    0: "No error",
    1: "Instruction not recognized",
    2: "Wrong number of parameters",
    3: "Out-of-range parameter",
    4: "Not used",
    5: "Unknown character set",
    6: "Position overflow",
    7: "Not used",
    8: "Vector received while pinch wheels raised",
}

# ---------------------------------------------------------------------------
# RS-232-C extended error numbers returned by ESC .E; (Prog. Manual §10-29)
# ---------------------------------------------------------------------------

RS232_ERRORS: dict[int, str] = {
    0: "No I/O error",
    10: "Output instruction received while another output instruction executing",
    11: "Invalid byte after first two characters of device-control instruction",
    12: "Invalid byte while parsing device-control instruction",
    13: "Parameter out of range",
    14: "Too many parameters",
    15: "Framing, parity, or overrun error",
    16: "Input buffer overflow — data lost",
}

# ---------------------------------------------------------------------------
# Safe HP-GL subset (validator allowlist). All verified for HP 7475A.
# LB/DT text plotting deliberately included for pen-test labels; see
# docs/hpgl-safety.md. Anything outside this set is rejected/flagged.
# ---------------------------------------------------------------------------

SAFE_HPGL_COMMANDS: frozenset[str] = frozenset(
    {
        "IN",  # initialize
        "DF",  # defaults
        "SP",  # select pen
        "PU",  # pen up (with optional coords)
        "PD",  # pen down (with optional coords)
        "PA",  # plot absolute
        "PR",  # plot relative
        "VS",  # velocity select
        "AS",  # acceleration select (documented alongside VS)
        "FS",  # force select — documented in manual but ONLY for models with
        # pen force hardware; 7475A manual lists it as ignored — validator
        # flags it as "accepted but ineffective" (warning, not error).
        "OA",  # output actual position
        "OC",  # output commanded position
        "OD",  # output digitized point
        "OE",  # output error
        "OF",  # output factors
        "OH",  # output hard-clip limits
        "OI",  # output identification
        "OO",  # output options
        "OP",  # output P1/P2
        "OS",  # output status
        "OW",  # output window
        "IM",  # input masking (error handling)
        "LT",  # line type
        "PW",  # pen width (ignored on fixed-pen plotters — flagged)
        "CT",  # chord tolerance (affects AA/AR rendering)
        "AA",  # arc absolute
        "AR",  # arc relative
        "CI",  # circle
        "LB",  # label (text)
        "DT",  # label terminator (required for LB)
        "DR",  # relative direction (label)
        "SI",  # absolute character size (label)
        "SR",  # relative character size (label)
        "CP",  # character plot (label)
        "IW",  # input window
        "SC",  # scale
        "IP",  # input P1/P2
        "RO",  # rotate
        "TL",  # tick length
        "XT",  # x tick
        "YT",  # y tick
        "PT",  # pen thickness
        "SM",  # symbolic mode (marker at plotted points)
        "HM",  # home move (hard-clip lower-left corner, pen up)
    }
)

#: Commands accepted but flagged "ineffective on 7475A" by the validator.
INEFFECTIVE_HPGL_COMMANDS: frozenset[str] = frozenset({"FS", "PW"})

#: Pen select range (0..6). OE error 3 / validator reject otherwise.
MIN_PEN = 0
MAX_PEN = PEN_COUNT
