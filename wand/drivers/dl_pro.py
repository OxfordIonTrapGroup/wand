"""
Simple driver for DL PRO laser controllers, providing the minimal
functionality required by wand.frontend.wand_locker.
"""

import telnetlib


class DLProError(Exception):
    pass


class DLPro:
    _prompt = b'> '

    def __init__(self, host, port=1998, target="laser1"):
        self.host = host
        self.port = port
        self.target = target

        self.tel = telnetlib.Telnet(self.host, self.port)
        self.tel.read_until(self._prompt)  # read handshake - to do: validate!

    def close(self):
        self.tel.close()

    def _execute(self, command, blocking=True):

        self.tel.write(command.encode() + b'\r\n')

        if blocking:
            response = self.tel.read_until(self._prompt)
            lines = response.split(b'\r\n')

            if lines[0] != command.encode():
                raise DLProError(
                    "Incorrect echo response from server: \"{}\", expected "
                    "\"{}\"".format(lines[0].decode(), command))

            if lines[-1] != self._prompt:
                raise DLProError(
                    "Incorrect response from server: \"{}\", expected "
                    "\"{}\"".format(lines[-1].decode(), self._prompt.decode()))

            if len(lines) > 2:
                return lines[-2].decode()
            else:
                return None

    def _get(self, parameter):

        command = "(param-ref '{})".format(parameter)
        value = self._execute(command, blocking=True)

        if value is None:
            raise DLProError(
                "No value returned in response to get command: {}".format(
                    command))

        return value

    def _set(self, parameter, value):

        if type(value) is bool:
            value = '#t' if value else '#f'
        else:
            value = str(value)

        command = "(param-set! '{} {})".format(parameter, value)
        response = self._execute(command, blocking=True)

        if response == '#t':
            response_code = True
        elif response == '#f':
            response_code = False
        else:
            try:
                response_code = int(response)
            except ValueError:
                raise DLProError(
                    "Non-numeric value returned by set command: "
                    "{}, {}".format(command, response))

            if response_code < 0:
                raise DLProError(
                    "Negative response code from set command, "
                    "indicating failure: {}, {}".format(
                        command, response_code))

        return response

    def get_pzt_voltage(self):
        voltage_str = self._get(self.target + ":dl:pc:voltage-set")
        try:
            voltage = float(voltage_str)
        except ValueError:
            raise DLProError("Could not convert string to float: {}".format(
                voltage_str))

        return voltage

    def set_pzt_voltage(self, value):
        return self._set(self.target + ":dl:pc:voltage-set", value)
