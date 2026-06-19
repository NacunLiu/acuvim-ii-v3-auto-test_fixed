import serial

def serial_ports():
    """ Lists serial port names

        :raises EnvironmentError:
            On unsupported or unknown platforms
        :returns:
            A list of the serial ports available on the system
    """
    ports = ['COM%s' % (i + 1) for i in range(20)]
    result = []
    for port in ports:
        try:
            s = serial.Serial(port)
            s.close()
            result.append(port)
        except (OSError, serial.SerialException):
            pass
    return result


def can_open(port):
    """Return True if the serial port can be opened right now.

    False covers both 'does not exist' and 'exists but is busy / access denied'.
    """
    try:
        s = serial.Serial(port)
        s.close()
        return True
    except (OSError, serial.SerialException):
        return False


if __name__ == '__main__':
    print(serial_ports())
        
                