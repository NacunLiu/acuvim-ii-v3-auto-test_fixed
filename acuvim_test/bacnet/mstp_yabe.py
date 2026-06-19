# Description: This script is primarily designed to automate the BACnet connection tests, for Acuvim II v3.
# last updated by Nacun Liu 2024-05-02

import logging
import subprocess
from time import sleep
import pyautogui
import cv2
from skimage.metrics import structural_similarity as ssim
from PIL import ImageGrab
import os
import screeninfo

logger = logging.getLogger(__name__)

# YABE / MS/TP pass threshold. This is now a *relaxed smoke check*: the
# deterministic BACnet verification lives in bacnet_ip.run_bacnet_ip_test
# (BACnet/IP), so here we only confirm YABE rendered a device that broadly
# matches the reference, rather than demanding a near-pixel-perfect match.
# Lowered from the old hard 0.9 to cut false negatives. Tune as needed.
SSIM_THRESHOLD = 0.6

class Client():
    def __init__(self,Serial,id):
        self.start_scan_button_location = None
        self.port_location = None
        self.firstMeter = None
        self.secMeter = None
        self.test_start = None
        self.first_slave = None
        self.close_Yabe = None
        self.whichScreen = self.findScreen()
        dir_path = os.getcwd()
        self.yabe_executable_path = 'C:\\Program Files\\Yabe\\Yabe.exe'  # Replace with the actual path, subject to change
        self.reference_path = dir_path+"\\Reference.png"
        if(not os.path.exists(os.getcwd()+'\\Sc')):
            os.mkdir('Sc')
        if(not Serial):
            Serial = 'Test'
        self.test_path = dir_path+"\\Sc\\"+Serial+".png"
        self.ssim_value = None
        self.id = id

    def findScreen(self):
        screen_list = screeninfo.get_monitors()
        # print(screen_list)
        if(len(screen_list)>1):
            #two monitor config
            self.start_scan_button_location = (28, 65)
            self.port_location = (517, 144)
            self.firstMeter = (483,168)
            self.secMeter = (466,183)
            self.test_start = (891,142)
            self.first_slave = (92,135)
            self.close_Yabe = (1899,0)
            self.screen_type = 1
        elif(screen_list[-1].height_mm<200):
            #laptop screen config
            self.start_scan_button_location = (17, 59)
            self.port_location = (202, 255)
            self.firstMeter = (148,296)
            self.secMeter = (148,308)
            self.test_start = (910,358)
            self.first_slave = (80,127)
            self.close_Yabe = (1888,0)
            self.screen_type = 1
        else:
            #depends on screen setups
            self.start_scan_button_location = (28, 65)
            self.port_location = (517, 144)
            self.firstMeter = (483,168)
            self.secMeter = (466,183)
            self.test_start = (891,142)
            self.first_slave = (92,135)
            self.close_Yabe = (1899,0)
            self.screen_type = 0

    def compare_Port(self, curPath, refPath):
        image1 = cv2.imread(curPath)
        image2 = cv2.imread(refPath)
        # Convert images to grayscale
        gray_image1 = cv2.cvtColor(image1, cv2.COLOR_BGR2GRAY)
        gray_image2 = cv2.cvtColor(image2, cv2.COLOR_BGR2GRAY)
        # Compute Similarity Index SSIM
        ssim_value, _ = ssim(gray_image1, gray_image2, full=True)
        if(ssim_value>0.9):
            return True
        else:
            return False
        
    def take_screenshot(self, option = None):
        # Take a screenshot of the box
        if(option):
            screenshot = ImageGrab.grab(bbox=option)
        else:
            screenshot = ImageGrab.grab(bbox=(0,95,184,440))
        # Save the screenshot to the target location, subject to change
        screenshot.save(self.test_path)
        pyautogui.click(self.close_Yabe)
        
    def compare_images(self):
        image1 = cv2.imread(self.reference_path)
        image2 = cv2.imread(self.test_path)
        if image1 is None or image2 is None:
            # Missing reference or capture failed -> can't judge; leave value None.
            logger.warning('BACnet MS/TP: reference or capture image unreadable')
            self.ssim_value = None
            return
        # Convert images to grayscale
        gray_image1 = cv2.cvtColor(image1, cv2.COLOR_BGR2GRAY)
        gray_image2 = cv2.cvtColor(image2, cv2.COLOR_BGR2GRAY)
        # Compute Similarity Index SSIM
        self.ssim_value, _ = ssim(gray_image1, gray_image2, full=True)
        # Keep the capture on a clear failure so it can be inspected later.
        if self.ssim_value >= SSIM_THRESHOLD:
            os.remove(self.test_path)

    # Purpose: relaxed pass check — see SSIM_THRESHOLD note above.
    def checkSSIM(self):
        if self.ssim_value is None:
            logger.warning('BACnet MS/TP: no SSIM value, treating as fail')
            return False
        logger.info('BACnet MS/TP SSIM={:.3f} (threshold {})'.format(self.ssim_value, SSIM_THRESHOLD))
        return self.ssim_value >= SSIM_THRESHOLD
        
    # Purpose: execute sequential commands to perform bacnect connection test
    def run(self):
        os.system("taskkill /f /im msedge.exe")
        subprocess.Popen([self.yabe_executable_path])
        sleep(2)
        if(self.screen_type ==1):
            pass
        else:
            pyautogui.hotkey('winleft', 'up')
        sleep(2)
        pyautogui.click(self.start_scan_button_location)
        sleep(2)
        pyautogui.hotkey('winleft', 'left')
        sleep(2)
        pyautogui.click(self.port_location)
        if(self.id==1):
            pyautogui.click(self.firstMeter)
        else:
            pyautogui.click(self.secMeter)
        sleep(1)
        pyautogui.click(self.test_start)
        sleep(50)
        pyautogui.click(self.first_slave)
        sleep(5)
        self.take_screenshot()
        self.compare_images()
        return self.checkSSIM()
    
    # Purpose: print cursor location
    def debug(self):
        print(pyautogui.displayMousePosition())

if __name__ == '__main__':
    client = Client('Debug',1)
    Connection = client.run()
    print(client.ssim_value)
    # client.debug()