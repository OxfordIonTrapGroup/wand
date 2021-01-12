""" Driver for Leoni eol/mol 1xn fibre switches """

import socket


class DoubleLeoniSwitch:
    def __init__(self, ip_addr, simulation=False):

        self.simulation = simulation
        if simulation:
            self._num_channels = 16
            return
        
        self.sock1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock1.connect((ip_addr, 10001))
        self.sock2.connect((ip_addr, 10002))

        self._num_channels = None
        self.get_num_channels()

    def get_num_channels(self):
        """ Returns the number of channels on the switch """
        if self.simulation:
            return 16

        if self._num_channels is None:
            self.sock1.send("type?\r\n".encode())
            with self.sock1.makefile() as stream1:
                resp1 = stream1.readline().strip()  # "eol 1xn"
            assert resp1.startswith("eol 1x") or resp1.startswith("mol 1x")
            
            self.sock2.send("type?\r\n".encode())
            with self.sock2.makefile() as stream2:
                resp2 = stream2.readline().strip()  # "eol 1xn"
            assert resp2.startswith("eol 1x") or resp2.startswith("mol 1x")
            
            self._num_channels = int(resp1[6:])+int(resp2[6:])
        return self._num_channels

    def set_active_channel(self, channel):
        """ Sets the active channel.

        :param channel: the channel number to select, not zero-indexed
        """
        if channel < 1 or channel > self._num_channels:
            raise ValueError('Channel out of bounds')
        if self.simulation:
            return
        if channel < self._num_channels/2:
            self.sock1.send("ch{}\r\n".format(channel).encode())
            self.sock2.send("ch{}\r\n".format(8).encode())
        else:
            self.sock2.send("ch{}\r\n".format(channel-8).encode())
            self.sock1.send("ch{}\r\n".format(8).encode())

    def get_active_channel(self):
        """ Returns the active channel number
        :return: the active channel, not zero-indexed
        """
        if self.simulation:
            return 1

        self.sock1.send("ch?\r\n".encode())
        self.sock2.send("ch?\r\n".encode())
        with self.sock1.makefile() as stream1, self.sock2.makefile() as stream2:
            return [int(stream1.readline().strip()),int(stream2.readline().strip())]

    def get_firmware_rev(self):
        """ Returns a firmware revision string, such as 'v8.09' """
        if self.simulation:
            return "Leoni fibre switch simulator"

        self.sock1.send("firmware?\r\n".encode())
        self.sock2.send("firmware?\r\n".encode())
        with self.sock1.makefile() as stream1, self.sock2.makefile() as stream2:
          
            return [stream1.readline().strip(),stream2.readline().strip()]

    def ping(self):
        if self.simulation:
            return True
        return bool(self.get_num_channels())

    def close(self):
        if self.simulation:
            return
        self.sock1.close()
        self.sock2.close()
