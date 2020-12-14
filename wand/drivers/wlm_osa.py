""" Use the WLM as an OSA """

import numpy as np


class WlmOSA:

    def __init__(self, wlm):
        self._wlm = wlm

    def get_trace(self, osa, timeout=10):
        """ Captures and returns an OSA trace.

        This function is synchronous (blocking) and does not return until the
        measurement is complete.

        The trace is downsampled (decimated) to reduce noise and to decrease
        the amount of memory/bandwidth consumed by storing/broadcasting traces.

        :param osa: the osa to capture
        :param timeout: data acquisition timeout (default: 10)
        :returns: the trace as an array of numpy int16s
        """

        if self._wlm.simulation:
            num_samples = 1024
            x = np.arange(num_samples) + np.random.uniform(-0.05, +0.05) * num_samples
            x -= num_samples / 2
            trace = np.random.normal(loc=0, scale=0.05, size=num_samples)
            trace += 900. / ((x) ** 2 + 1000)

            trace -= min(trace)
            trace /= max(trace)
            trace = np.round(trace * 32767).astype(np.int16)

        else:
            trace = self._wlm.get_pattern().astype(np.int16)

        return trace
