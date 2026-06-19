# Description: see below

"""
Extension Class for KasaPlug

ALL Changes to plug subject are using awaitable methods (Async), you must await :func:`update()` before sending commands

Add-on fe

"""
from acuvim_test.hardware.kasa_plug import KasaSmartPlug
from acuvim_test.hardware.ip_tracker import get_target_ip_map
import asyncio
class miniKasa(KasaSmartPlug):
    
    def __init__(self,Ip):
        super().__init__(Ip)
        
    async def recurring_switch(self):
        while(True):
            await self.powerCycle(50)
            
if __name__ == '__main__':
    PlugIp = get_target_ip_map()
    for ip in PlugIp:
        KasaPlug = miniKasa(PlugIp[ip][1])
        asyncio.run(KasaPlug.recurring_switch())

    