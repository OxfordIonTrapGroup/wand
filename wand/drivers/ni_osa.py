""" Interface to NI DAQ card(s) that sample OSAs """

import numpy as np
from ctypes import byref, c_int32
from scipy.signal import decimate

try:
    import PyDAQmx
    from PyDAQmx.DAQmxFunctions import DAQError
except ImportError:
    pass


class OSAException(Exception):
    """ Raised on errors involving the OSA DAQ card(s) """
    pass


class NiOSA:
    """ Interface to one or more Optical Spectrum Analyser """

    def __init__(self, osas, simulation=False):
        """
        :param osas: dictionary containing OSA configuration info
        :param simulation: if True we operate in simulation mode
        """
        self.simulation = simulation
        self.osas = dict(osas)
        self.handles = {}

        if self.simulation:
            self.f0 = {}
            for osa_name, osa in self.osas.items():
                self.f0[osa_name] = np.random.uniform(0, 1) * osa["num_samples"]
            return

        try:
            # Reset all DAQ cards
            devices = set([osa["device"] for _, osa in self.osas.items()])
            for device in devices:
                PyDAQmx.DAQmxResetDevice(device)

            for name, osa in self.osas.items():
                self.handles[name] = task_handle = PyDAQmx.TaskHandle(0)
                PyDAQmx.DAQmxCreateTask("osa_" + name, byref(task_handle))

                PyDAQmx.DAQmxCreateAIVoltageChan(
                    task_handle,
                    "/{}/{}".format(osa["device"], osa["input_channel"]),
                    "Voltage",
                    PyDAQmx.DAQmx_Val_NRSE,
                    -osa["v_span"] / 2, osa["v_span"] / 2,
                    PyDAQmx.DAQmx_Val_Volts,
                    None)

                PyDAQmx.DAQmxCfgSampClkTiming(
                    task_handle,
                    None,
                    osa["sample_rate"],
                    PyDAQmx.DAQmx_Val_Rising,
                    PyDAQmx.DAQmx_Val_FiniteSamps,
                    osa["num_samples"])

                PyDAQmx.DAQmxCfgDigEdgeStartTrig(
                    task_handle,
                    "/{}/{}".format(osa["device"], osa["trigger_channel"]),
                    PyDAQmx.DAQmx_Val_Falling)

        except DAQError as err:
            self.clear()
            raise OSAException(err)

    def clear(self):
        """ Stops and clears all NI DAQ tasks we have created """
        for _, osa in self.osas.items():
            task_handle = self.handles.get(osa)
            if task_handle is not None:
                try:
                    PyDAQmx.DAQmxStopTask(task_handle)
                finally:
                    PyDAQmx.DAQmxClearTask(task_handle)
                    self.handles[osa] = None

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
        num_samples = self.osas[osa]["num_samples"]
        read = c_int32()

        if self.simulation:
            x = np.arange(num_samples) - self.f0[osa] \
                + np.random.uniform(-0.05, +0.05) * num_samples
            trace = np.random.normal(loc=0, scale=0.05, size=num_samples)
            trace += 900. / ((x) ** 2 + 1000)
            trace *= self.osas[osa]["v_span"] / 2

        else:
            task_handle = self.handles[osa]
            trace = np.zeros((num_samples,),
                             dtype=np.float64)
            try:
                PyDAQmx.DAQmxReadAnalogF64(
                    task_handle,
                    -1,
                    timeout,
                    PyDAQmx.DAQmx_Val_GroupByChannel,
                    trace,
                    num_samples,
                    byref(read),
                    None)

            except DAQError as err:
                self.clear()
                raise OSAException(err)

            if read.value != num_samples:
                self.clear()
                raise OSAException("Error acquiring OSA trace")

        if self.osas[osa]["downsample"] > 1:
            trace = decimate(trace, self.osas[osa]["downsample"])

        trace /= self.osas[osa]["v_span"] / 2
        trace = np.round(trace * 32767).astype(np.int16)

        return trace
