# Description: see below
# last updated by Nacun Liu 2024-04-03

"""
KasaSmartPlug Class

Create a Kasa Smart Plug object

Currently support plug power control (power on/off) and power cycle

ALL Changes to plug subject are using awaitable methods (Async), you must await :func:`update()` before sending commands

"""
import kasa
import asyncio
from acuvim_test.hardware.ip_tracker import get_target_ip_map
import time


class KasaSmartPlug():

    def __init__(self, ip):
        self.Ip = ip
        self.dev = kasa.SmartPlug(ip)

    def loading_animation(self, duration):
        time.sleep(duration)
        return

    async def cycle(self):
        """Pure power toggle: off, brief pause, on. No store/boot waits.

        All FeRAM-store and boot waits are owned by run.reboot_meter so the
        timing lives in one place and switch/manual modes stay consistent.
        """
        await self.dev.update()
        print('Power cycling plug {} >>>>'.format(self.Ip))
        await self.dev.turn_off()
        await asyncio.sleep(3)
        await self.dev.turn_on()

    async def powerCycle(self, time=20):
        await self.dev.update()
        print('Power cycling plug {}, please wait >>>>'.format(self.Ip))
        await asyncio.sleep(2)
        await self.dev.turn_off()
        await asyncio.sleep(3)
        await self.dev.turn_on()
        self.loading_animation(time)

    async def powerCycleNormal(self):
        await self.dev.update()
        print('Power cycling the meter, please wait >>>>')
        await asyncio.sleep(2)
        await self.dev.turn_off()
        await asyncio.sleep(3)
        await self.dev.turn_on()
        # await asyncio.sleep(30)
        self.loading_animation(30)

    async def powerCycleSlow(self):
        await self.dev.update()
        print('Power cycling the meter, please wait >>>>')
        await asyncio.sleep(2)
        await self.dev.turn_off()
        await asyncio.sleep(2)
        await self.dev.turn_on()
        time.sleep(90)

    async def powerCycleSuperSlow(self):
        await self.dev.update()
        print('Store readings into FeRAM, please wait for 130 seconds >>>>')
        self.loading_animation(130)
        await self.dev.turn_off()
        await asyncio.sleep(3)
        await self.dev.turn_on()
        print('Powering up meter >>>>')
        self.loading_animation(50)

    async def powerCycleEnergy(self):
        await self.dev.update()
        print('Power cycling the meter, please wait >>>>')
        self.loading_animation(90)
        await self.dev.turn_off()
        await asyncio.sleep(2)
        await self.dev.turn_on()
        self.loading_animation(40)

    async def powerOff(self):
        await self.dev.update()
        print('Powering off the meter, please wait >>>>')
        await self.dev.turn_off()

    async def powerOn(self, sleep=None):
        await self.dev.update()
        print('Powering on the meter, please wait >>>>')
        await self.dev.turn_on()
        if (sleep):
            time.sleep(30)

    async def powerOnSlow(self):
        await self.dev.update()
        print('Powering on the meter, please wait >>>>')
        await self.dev.turn_on()
        self.loading_animation(70)


if __name__ == '__main__':
    PlugIp = get_target_ip_map()
    for ip in PlugIp:
        plug = KasaSmartPlug(PlugIp[ip][1])
        asyncio.run(plug.powerCycle())

    # plug = KasaSmartPlug(PlugIp[0])
    # asyncio.run(plug.powerCycleSlow())
