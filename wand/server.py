import numpy as np
import time
import asyncio
import logging

from wand.tools import LaserOwnedException, LockException

logger = logging.getLogger(__name__)


def _validate_numeric(field, name):
    try:
        return float(field)
    except ValueError:
        raise ValueError("{} must be a numeric type".format(name))


def _validate_int(field, name):
    try:
        field = float(field)
        if field != np.rint(field):
            raise ValueError
        return int(np.rint(field))
    except ValueError:
        raise ValueError("{} must be an integer".format(name))


def _validate_bool(field, name):
    if not isinstance(field, bool):
        raise ValueError("{} must be a bool".format(name))


class ControlInterface:
    """ RPC interface to the WAnD server """

    def __init__(self, wand_server):
        self._server = wand_server

    def _validate_laser(self, laser):
        if laser not in self._server.laser_db.raw_view.keys():
            raise ValueError("unrecognised laser name '{}'".format(laser))

    def _check_owner(self, laser, name):
        try:
            name = str(name)
        except ValueError:
            raise ValueError("name must be a string")

        current_owner = self._server.laser_db.raw_view[laser]["lock_owner"]
        if current_owner and (current_owner != name):
            raise LaserOwnedException("Laser currently owned by '{}' "
                                      "(you are '{}')!"
                                      .format(current_owner, name))

        if not self._server.laser_db.raw_view[laser]["host"]:
            raise ValueError("No controller found for '{}'".format(laser))

    async def get_freq(self, laser, age=0, priority=3, get_osa_trace=False,
                       blocking=True, mute=False, offset_mode=False):
        """ Schedule a frequency measurement and, optionally, capture an osa
        trace for a single laser.

        :param laser: name of the laser to interrogate
        :param age: how old the measurement may be (s, default: 0). age <= 0
          implies that a new measurement must be taken. ages > 0 imply that we
          can use cached data from a previous measurement.
        :param priority: integer giving the priority of the measurement
          (default: 3). Measurements are processed first in priority order
          (higher priorities first), and then in chronological order (FIFO).
        :param get_osa_trace: if True, we also return an OSA trace (default:
          False)
        :param blocking: if False we immediately return a task id for the
          measurement, rather than waiting for it to complete (default: True).
          Note that setting blocking to False overrides the setting of :param
          mute:. See also :meth get_measurement_queue:
        :param mute: if True we immediately return a task id for the
          measurement, rather than waiting for it to complete (default: False).
          This may be useful, for example, in clients which subscribe to the
          notifier interface. Overridden by :oaram blocking:
        :param offset_mode: if True, frequencies are returned as detunings
          from the reference frequency (default: False)

        :return: returns either a task if for the newly scheduled measurement
          or the tuple (status, freq, osa_trace). Status is a
          wand.tools.WLMMeasurementStatus, frequency is in Hz. If get_osa_trace
          is False, osa_trace is an empty array.
        """
        self._validate_laser(laser)

        age = _validate_numeric(age, "age")
        priority = _validate_int(priority, "priority")
        _validate_bool(get_osa_trace, "get_osa_trace")
        _validate_bool(blocking, "blocking")
        _validate_bool(mute, "mute")
        _validate_bool(offset_mode, "offset_mode")

        expiry = time.time() - max(0, age)
        freq_ts = self._server.freq_db.raw_view[laser]["timestamp"]
        osa_ts = self._server.osa_db.raw_view[laser]["timestamp"]

        # do we need to take a new measurement?
        if (freq_ts > expiry) and (osa_ts > expiry or not get_osa_trace):
            if mute or not blocking:
                return next(self._server.measurement_ids)  # fake task id

            freq = self._server.freq_db.raw_view[laser]["freq"]
            status = self._server.freq_db.raw_view[laser]["status"]
            osa = self._server.osa_db.raw_view[laser]["trace"]

        else:
            measurement = {
                "laser": laser,
                "priority": priority,
                "expiry": expiry,
                "id": next(self._server.measurement_ids),
                "get_osa_trace": get_osa_trace,
                "done": asyncio.Event()}
            self._server.queue.append(measurement)
            self._server.measurements_queued.set()

            logger.info("measurement task scheduled with id {}".format(
                measurement["id"]))

            if not blocking:
                return measurement["id"]

            await measurement["done"].wait()
            logger.info("measurement task with id {} completed".format(
                measurement["id"]))

            if mute:
                return measurement["id"]

            freq = self._server.freq_db.raw_view[laser]["freq"]
            status = self._server.freq_db.raw_view[laser]["status"]
            osa = self._server.osa_db.raw_view[laser]["trace"]

        if offset_mode:
            f_ref = self._server.laser_db.raw_view[laser]["f_ref"]
            freq = freq - f_ref

        osa = osa if get_osa_trace else np.zeros(0)
        return (status, freq, osa)

    def get_measurement_queue(self):
        """ Returns a list of queued measurements """
        queue = [meas.copy() for meas in self._server.queue]
        for meas in queue:
            del meas["done"]  # can't serialise asyncio Events
        return queue

    def set_exposure(self, laser, exposure, ccd):
        """ Sets the exposure time for a laser

        :param laser: name of the laser
        :param exposure: exposure time (ms)
        :param ccd: the CCD number. Must lie in range(get_num_wlm_ccds())
        """
        self._validate_laser(laser)
        exposure = _validate_int(exposure, "exposure")
        ccd = _validate_int(ccd, "ccd")

        if exposure < self._server.exp_min[ccd] or \
           exposure > self._server.exp_max[ccd]:
            raise ValueError("invalid exposure")
        if ccd not in range(self._server.num_ccds):
            raise ValueError("invalid WLM CCD number")

        self._server.laser_db[laser]["exposure"][ccd] = exposure
        self._server.save_config_file()

    def set_auto_exposure(self, laser, enabled=True):
        """ Enable or disable auto-exposure for a given laser """
        self._validate_laser(laser)
        _validate_bool(enabled, "enabled")
        self._server.laser_db[laser]["auto_exposure"] = enabled
        self._server.save_config_file()

    def get_min_exposures(self):
        """
        Returns the WaveLength Meter (WLM) minimum exposure times (ms)
        """
        return self._server.exp_min

    def get_max_exposures(self):
        """
        Returns the WaveLength Meter (WLM) maximum exposure times (ms)
        """
        return self._server.exp_max

    def get_num_wlm_ccds(self):
        """ Returns the number of CCDs on the WaveLength meter (WLM) """
        return self._server.num_ccds

    def get_laser_db(self):
        """ Returns the laser configuration database """
        return self._server.laser_db.raw_view

    def set_reference_freq(self, laser, f_ref):
        """ Sets the reference frequency for a laser (Hz) """
        self._validate_laser(laser)
        f_ref = _validate_numeric(f_ref, "f_ref")
        self._server.laser_db[laser]["f_ref"] = f_ref
        self._server.save_config_file()

    def set_fast_mode(self, laser, enabled=True):
        """ Enables or disables fast data acquisition mode for a laser.

        This parameter is used by clients to decide how quickly to poll
        the server for data.

        :param enabled: if True, we enable fast mode, if False it is
        disabled (default: True)
        """
        self._validate_laser(laser)
        _validate_bool(enabled, "enabled")
        self._server.laser_db[laser]["fast_mode"] = enabled
        self._server.laser_db[laser]["fast_mode_set_at"] = time.time()
        self._server.save_config_file()

    def get_poll_times(self):
        """ Returns the tuple (poll_time, fast_poll_time).

        These poll times are used by clients, such as the GUI, to determine
        how often to request new data.
        """
        config = self._server.config
        return (config["poll_time"], config["fast_poll_time"])

    def lock(self, laser, set_point=None, name="", timeout=300):
        """ Locks a laser to the wavemeter.

        :param laser: the laser name
        :param set_point: the lock set point (Hz) relative to the laser's
          reference frequency (default: None). If None we use the current
          value.
        :param name: if not "" you take ownership of the laser lock,
          preventing anyone else from modifying the lock. See :meth unlock:
        :param timeout: the lock timeout (s) if None the laser is locked
          indefinitely (default: 300). Use with caution!
        """
        self._validate_laser(laser)
        self._check_owner(laser, name)
        set_point = _validate_numeric(set_point, "set_point")
        timeout = None if timeout is None else _validate_numeric(timeout,
                                                                 "timeout")

        if not self._server.laser_db.raw_view[laser]["lock_ready"]:
            raise LockException(
                "Laser lock not ready. Problem trying to connect to laser "
                "controller? See the server log for details...")

        if set_point is None:
            set_point = self._server.laser_db.raw_view[laser]["lock_set_point"]

        laser_conf = self._server.laser_db[laser]
        laser_conf["locked"] = True
        laser_conf["lock_owner"] = name
        laser_conf["locked_at"] = time.time()
        laser_conf["lock_timeout"] = timeout
        laser_conf["lock_set_point"] = set_point

        self._server.save_config_file()
        self._server.wake_locks[laser].set()

    def unlock(self, laser, name):
        """ Unlock and release ownership of a laser """
        self._validate_laser(laser)
        self._check_owner(laser, name)

        laser_conf = self._server.laser_db[laser]
        laser_conf["locked"] = False
        laser_conf["lock_owner"] = ""

        self._server.save_config_file()
        self._server.wake_locks[laser].set()

    def steal(self, laser):
        """ Releases ownership of a laser without changing its lock status.

        Use with caution!
        """
        self._validate_laser(laser)

        laser_conf = self._server.laser_db[laser]
        laser_conf["lock_owner"] = ""

        self._server.save_config_file()
        self._server.wake_locks[laser].set()

    def set_lock_params(self, laser, gain, poll_time, capture_range=3e9,
                        name=""):
        """ Sets the lock parameters for a laser.

        :param gain: the feedback gain to use (V/(Hz*s))
        :param poll_time: time (s) between lock updates
        :param capture_range: lock capture range (Hz) (default: 3e9). If the
          laser frequency is measured outside the capture range, the laser is
          unlocked.
        :param name: string name (default: ""). This function does not take
          ownership of the laser, but it will not allow you to modify the
          settings for a lock owned by someone else.
        """
        self._validate_laser(laser)
        self._check_owner(laser, name)
        gain = _validate_numeric(gain, "gain")
        poll_time = _validate_numeric(poll_time, "poll_time")
        capture_range = _validate_numeric(capture_range, "capture_range")

        laser_db = self._server.laser_db
        laser_db[laser]["lock_gain"] = abs(gain)
        laser_db[laser]["lock_poll_time"] = abs(poll_time)
        laser_db[laser]["lock_capture_range"] = abs(capture_range)

        self._server.save_config_file()
        self._server.wake_locks[laser].set()
