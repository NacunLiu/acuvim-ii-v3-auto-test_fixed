# Description: see below
#last updated by Nacun Liu 2024-04-07
"""
Display test result
"""
import pyfiglet
from termcolor import colored
testFail = colored(pyfiglet.figlet_format("Test Failed",font='banner3-D',width=150),'red',attrs=['bold'])
p1Fail = colored(pyfiglet.figlet_format("P1 Failed",font='banner3-D',width=100),'light_yellow',attrs=['bold'])
p2Fail = colored(pyfiglet.figlet_format("P2 Failed",font='banner3-D',width=100),'light_yellow',attrs=['bold'])
allPassed = colored(pyfiglet.figlet_format("Tests Completed",font='banner3-D',width=150),'green',attrs=['bold'])
p1Passed = colored(pyfiglet.figlet_format("P1 Passed",font='banner3-D',width=100),'light_cyan',attrs=['bold'])
p2Passed = colored(pyfiglet.figlet_format("P2 Passed",font='banner3-D',width=100),'light_cyan')
# Create the PyFiglet text
text = pyfiglet.figlet_format("AcuTest GO", font="slant",width=200)

# Define a list of rainbow colors
rainbow_colors = ['light_grey','light_grey','cyan','cyan', 'blue','blue','light_blue','light_blue']

# Initialize an empty string to store the rainbow-colored text
starter = ''

# Iterate through each character in the text and apply a different color
for i, char in enumerate(text):
    # Use modulo to cycle through rainbow_colors
    color = rainbow_colors[i % len(rainbow_colors)]
    starter += colored(char, color)
    
if(__name__ == '__main__'):
    print(starter)
    print(testFail)
    print(allPassed)
    print(p1Fail)
    print(p2Fail)
    print(p1Passed)
    print(p2Passed)
