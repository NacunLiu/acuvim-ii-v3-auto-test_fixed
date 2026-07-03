"""Modbus register addresses for the Acuvim II v3 meter.

Central map of the holding-register addresses the test suite reads/writes, so
the magic numbers live in one documented place instead of scattered literals.
Energy-region addresses live with their data in run.ENERGY_TESTS_*.
"""

# Communication / network
DHCP_ENABLE = 258        # 0 = disabled (manual IP), 1 = enabled
IP_ADDRESS = 259         # channel-1 IP register (2 words)
PROTOCOL_CH1 = 4094      # channel-1 protocol (2 = BACnet)
BAUD_CH1 = 4098          # channel-1 (protocol-1) baud rate
BAUD_CH2 = 4143          # channel-2 baud rate
SLAVE_ID = 4145          # device slave id
PROTOCOL_CH2 = 4152      # channel-2 protocol (0=Other, 4=Web2, 5=Profibus)
PROFIBUS_ID = 65280      # Profibus node id

# BACnet (MS/TP) config
BACNET_BAUD = 8449       # BACnet baud rate
BACNET_ID = 8451         # BACnet device id (2 words)

# Energy / control
ENERGY_RESET = 4118      # write 1 to clear all energy
DISPLAY_MODE = 4121      # decimal-point / display mode (0/1/2)

# Energy register regions (start addresses) tested by energyLegitCheck.
ENERGY_TOTAL = 16456           # Total Ep/Eq/Es (imp/exp/total/net)  -> web "Total" block
ENERGY_PHASE = 17952           # per-phase Ep/Eq                     -> web "Phase" block
ENERGY_PHASE_APPARENT = 18688  # per-phase apparent energy Es        -> web "Phase" block (Es)
ENERGY_FOUR_QUADRANT = 18704   # four-quadrant reactive Eq (Q1-Q4)   -> web "Four-Quadrant" block
ENERGY_INDEP_CHANNEL = 9472    # independent input channel energy    -> not shown on the web page

# ABB meters (CS0/CS2 families) use the Energy_1Cycle region instead, R/W:
# 96 energy values starting here. New M4M40 stores them as float64 (4 regs each),
# old Acuvim IIX Class S as float32 (2 regs each). Same start address for both.
ABB_ENERGY_1CYCLE = 50412      # 0xC4EC, 96 energy values (R/W)
ABB_ENERGY_COUNT = 96

# Friendly names for the energy regions (used in log labels).
ENERGY_REGION_NAMES = {
    ENERGY_TOTAL: 'Total energy',
    ENERGY_PHASE: 'Phase Ep/Eq',
    ENERGY_PHASE_APPARENT: 'Phase apparent Es',
    ENERGY_FOUR_QUADRANT: 'Four-quadrant Eq',
    ENERGY_INDEP_CHANNEL: 'Independent channel',
}


def energy_label(address, extra=''):
    """Build a log label like 'Total energy (16456)' or 'Total energy (16456) - max Ep/q/s'."""
    base = '{} ({})'.format(ENERGY_REGION_NAMES.get(address, 'Energy region'), address)
    return '{} - {}'.format(base, extra) if extra else base

# Diagnostics / identity
CUSTOM_REG_DEFAULT = 27136  # custom register, default 0
REBOOT_COUNTER = 38144      # reboot counter (Accuenergy / Eaton / DEIF), 0x9500
LATENCY_REG = 38146         # communication latency register, 0x9502
# ABB families (old ABB Class S + new M4M40) relocate these two registers.
REBOOT_COUNTER_ABB = 54528  # reboot counter (ABB), 0xD500
LATENCY_REG_ABB = 54530     # communication latency register (ABB), 0xD502
MODEL = 61440               # model string (2 words)
SERIAL_NUMBER = 61504       # serial number string (6 words)
METER_TYPE = 61552          # meter type code
MOUNT_TYPE = 61553          # 0 = display (LCD), else non-display
