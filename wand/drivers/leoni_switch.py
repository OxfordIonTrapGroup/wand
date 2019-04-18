""" Driver for Leoni eol/mol 1xn fibre switches """

import socket


class LeoniSwitch:
    def __init__(self, ip_addr, simulation=False):

        self.simulation = simulation
        if simulation:
            self._num_channels = 16
            return

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((ip_addr, 10001))

        self._num_channels = None
        self.get_num_channels()

    def get_num_channels(self):
        """ Returns the number of channels on the switch """
        if self.simulation:
            return 16

        if self._num_channels is None:
            self.sock.send("type?\r\n".encode())
            with self.sock.makefile() as stream:
                resp = stream.readline().strip()  # "eol 1xn"
            assert resp.startswith("eol 1x") or resp.startswith("mol 1x")
            self._num_channels = int(resp[6:])
        return self._num_channels

    def set_active_channel(self, channel):
        """ Sets the active channel.

        :param channel: the channel number to select, not zero-indexed
        """
        if channel < 1 or channel > self._num_channels:
            raise ValueError('Channel out of bounds')
        if self.simulation:
            return
        self.sock.send("ch{}\r\n".format(channel).encode())

    def get_active_channel(self):
        """ Returns the active channel number
        :return: the active channel, not zero-indexed
        """
        if self.simulation:
            return 1

        self.sock.send("ch?\r\n".encode())
        with self.sock.makefile() as stream:
            return int(stream.readline().strip())

    def get_firmware_rev(self):
        """ Returns a firmware revision string, such as 'v8.09' """
        if self.simulation:
            return "Leoni fibre switch simulator"

        self.sock.send("firmware?\r\n".encode())
        with self.sock.makefile() as stream:
            return stream.readline().strip()

    def ping(self):
        if self.simulation:
            return True
        return bool(self.get_num_channels())

    def close(self):
        if self.simulation:
            return
        self.sock.close()
