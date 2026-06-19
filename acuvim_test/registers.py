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

# Diagnostics / identity
CUSTOM_REG_DEFAULT = 27136  # custom register, default 0
REBOOT_COUNTER = 38144      # reboot counter
LATENCY_REG = 38146         # communication latency register
MODEL = 61440               # model string (2 words)
SERIAL_NUMBER = 61504       # serial number string (6 words)
METER_TYPE = 61552          # meter type code
MOUNT_TYPE = 61553          # 0 = display (LCD), else non-display
