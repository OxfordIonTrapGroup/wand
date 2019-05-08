""" Wavelength Analysis 'nd Display server

The server exposes two interfaces to clients:
 1. The "control" interface: an RPC interface for tasks such as:
    frequency/OSA measurements; querying server parameters, such as the laser
    database; setting WLM exposure times; etc
2. The "notify" interface: an asynchronous "sync_struct" interface that keeps
   clients notified about new measurement data, changes to laser parameters,
   etc.
"""

import argparse
import asyncio
import atexit
import time
import logging
import numpy as np
from concurrent.futures import ThreadPoolExecutor

from artiq.protocols import pyon
from artiq.protocols.pc_rpc import Server as RPCServer
from artiq.protocols.sync_struct import Publisher, Notifier
from artiq.tools import (simple_network_args, atexit_register_coroutine,
                         bind_address_from_args, init_logger)

from wand.drivers.leoni_switch import LeoniSwitch
from wand.drivers.high_finesse import WLM
from wand.drivers.osa import OSAs
from wand.tools import (load_config, backup_config, regular_config_backup,
                        get_config_path)

# verbosity_args() was renamed to add_common_args() in ARTIQ 5.0; support both.
try:
    from artiq.tools import add_common_args
except ImportError:
    from artiq.tools import verbosity_args as add_common_args

logger = logging.getLogger(__name__)


def task_id_generator():
    """ Yields unique, sequential task ids beginning with 0 """
    task_id = 0
    while True:
        yield task_id
        task_id += 1


def get_argparser():
    parser = argparse.ArgumentParser(description="WAnD server")

    simple_network_args(parser, [
        ("notify", "notifications", 3250),
        ("control", "control", 3251),
    ])
    add_common_args(parser)
    parser.add_argument("-n", "--name",
                        default="test",
                        help="server name, used to locate configuration file")
    parser.add_argument("--simulation",
                        action='store_true',
                        help="run in simulation mode")
    parser.add_argument("-dt", "--switch-dead-time",
                        default=0.075,
                        type=float,
                        help="dead time (s) after changing channels on the "
                             "fibre switch (default: '%(default)s')")
    parser.add_argument("--fast-mode-timeout",
                        default=1800,
                        type=int,
                        help="fast mode timeout (s) (default: '%(default)s')")
    return parser


class WandServer:
    def __init__(self):

        self.args = args = get_argparser().parse_args()
        init_logger(args)

        self.config = load_config(args, "_server")

        # connect to hardware
        self.wlm = WLM(args.simulation)
        self.osas = OSAs(self.config["osas"], args.simulation)
        if self.config["switch"]["type"] == "internal":
            self.switch = self.wlm.get_switch()
        elif self.config["switch"]["type"] == "leoni":
            self.switch = LeoniSwitch(
                self.config["switch"]["ip"], args.simulation)
        else:
            raise ValueError("Unrecognised switch type: {}".format(
                self.config["switch"]["type"]))

        # task queue, processed by self.process_tasks
        self.task_ids = task_id_generator()
        self.tasks_queued = asyncio.Event()
        self.queue = []

        # schedule initial frequency/osa readings all lasers
        self.tasks_queued.set()
        for laser in self.config["lasers"].keys():
            self.queue.append({
                "type": "measure",
                "laser": laser,
                "priority": 0,
                "expiry": time.time(),
                "id": next(self.task_ids),
                "get_osa_trace": True,
                "done": asyncio.Event()
            })

        # "notify" interface
        self.laser_db = Notifier(self.config["lasers"])
        self.freq_db = Notifier({name: {
            "freq": None,
            "timestamp": 0
        } for (name, _) in self.config["lasers"].items()})
        self.osa_db = Notifier({name: {
            "trace": None,
            "timestamp": 0
        } for (name, _) in self.config["lasers"].items()})

        self.server_notify = Publisher({
            "laser_db": self.laser_db,  # laser settings
            "freq_db": self.freq_db,  # most recent frequency measurements
            "osa_db": self.osa_db  # most recent osa traces
        })

        # "control" interface
        self.control_interface = self.ControlInterface(self)
        self.server_control = RPCServer({"control": self.control_interface},
                                        allow_parallel=True)

    def start(self):
        """ Start the server """

        self.executor = ThreadPoolExecutor(max_workers=2)
        self.loop = loop = asyncio.get_event_loop()

        atexit.register(loop.close)

        # start control server
        bind = bind_address_from_args(self.args)
        loop.run_until_complete(
            self.server_control.start(bind, self.args.port_control))
        atexit_register_coroutine(self.server_control.stop)

        # start notify server
        loop.run_until_complete(
            self.server_notify.start(bind, self.args.port_notify))
        atexit_register_coroutine(self.server_notify.stop)

        # start task processing loop
        loop.create_task(self.process_tasks())

        # backup of configuration file
        backup_config(self.args, "_server")
        loop.create_task(regular_config_backup(self.args, "_server"))
        atexit.register(backup_config, self.args, "_server")

        logger.info("server started")
        loop.run_forever()

    async def process_tasks(self):
        """ Process scheduled tasks

        Where possible, we bunch tasks together to avoid redundant measurements
        or notifier broadcasts.
        """
        active_laser = ""

        while True:
            if self.queue == []:
                self.tasks_queued.clear()
            await self.tasks_queued.wait()

            save_laser_db = False

            # task: set auto exposure
            auto_exposure_tasks = [task for task in self.queue
                                   if task["type"] == "set_auto_exposure"]
            while auto_exposure_tasks:
                laser = auto_exposure_tasks[0]["laser"]
                laser_tasks = [task for task in auto_exposure_tasks
                               if task["laser"] == laser]
                task = laser_tasks[-1]

                enabled = task["enabled"]
                if self.laser_db.raw_view[laser]["auto_exposure"] != enabled:
                    self.laser_db[laser]["auto_exposure"] = enabled
                    save_laser_db = True

                for task in laser_tasks:
                    task["done"].set()
                    self.queue.remove(task)
                    auto_exposure_tasks.remove(task)

            # task: set laser reference frequency
            f_ref_tasks = [task for task in self.queue
                           if task["type"] == "set_f_ref"]
            while f_ref_tasks:

                # get the most recent f_ref for this laser in the queue
                laser = f_ref_tasks[0]["laser"]
                laser_tasks = [task for task in f_ref_tasks
                               if task["laser"] == laser]
                task = laser_tasks[-1]
                f_ref = task["f_ref"]

                if self.laser_db.raw_view[laser]["f_ref"] != f_ref:
                    self.laser_db[laser]["f_ref"] = f_ref
                    save_laser_db = True

                for task in laser_tasks:
                    task["done"].set()
                    self.queue.remove(task)
                    f_ref_tasks.remove(task)

            # task: set laser exposure time
            exposure_tasks = [task for task in self.queue
                              if task["type"] == "set_exposure"]
            while exposure_tasks:

                # only execute the most recent queued update for a given
                # laser ccd
                laser = exposure_tasks[0]["laser"]
                laser_tasks = [task for task in exposure_tasks
                               if task["laser"] == laser]

                curr_exposures = self.laser_db.raw_view[laser]["exposure"]
                for ccd, curr_exp in enumerate(curr_exposures):
                    ccd_tasks = [task for task in laser_tasks
                                 if task["ccd"] == ccd]

                    if not ccd_tasks:
                        continue

                    task = ccd_tasks[-1]
                    new_exp = task["exposure"]

                    if new_exp == curr_exp:
                        continue  # nothing to do here

                    self.laser_db[laser]["exposure"][ccd] = new_exp
                    save_laser_db = True

                    if active_laser == task["laser"]:
                        self.wlm.set_exposure(new_exp, ccd)

                for task in laser_tasks:
                    task["done"].set()
                    self.queue.remove(task)
                    exposure_tasks.remove(task)

            if save_laser_db:
                self.save_config_file()

            # task: frequency/osa measurement
            # processed in order of priority, followed by submission time
            measure_tasks = [task for task in self.queue
                             if task["type"] == "measure"]
            if measure_tasks == []:
                continue

            priorities = [task["priority"] for task in measure_tasks]
            next_task = measure_tasks[priorities.index(max(priorities))]
            laser_settings = self.laser_db.raw_view[next_task["laser"]]

            if next_task["laser"] != active_laser:
                self.switch.set_active_channel(laser_settings["channel"])
                exposure = laser_settings["exposure"]
                for ccd, exp in enumerate(exposure):
                    self.wlm.set_exposure(exposure[ccd], ccd)

                # Switching is slow so we might as well take an OSA trace while
                # we're at it
                next_task["get_osa_trace"] = True
                active_laser = next_task["laser"]

                await asyncio.sleep(self.args.switch_dead_time)

            freq_measurement = self.loop.run_in_executor(
                self.executor,
                self.take_freq_measurement,
                active_laser)
            osa_measurement = self.loop.run_in_executor(
                self.executor,
                self.take_osa_measurement,
                active_laser,
                next_task["get_osa_trace"])

            wlm_data, osa = (await asyncio.gather(freq_measurement,
                                                  osa_measurement))[:]

            freq, peaks = wlm_data
            self.freq_db[active_laser] = freq
            if next_task["get_osa_trace"]:
                self.osa_db[active_laser] = osa

            # fast mode timeout
            if self.laser_db.raw_view[active_laser]["fast_mode"]:
                t_en = self.laser_db.raw_view[active_laser]["fast_mode_set_at"]
                if time.time() > (t_en + self.args.fast_mode_timeout):
                    self.laser_db[active_laser]["fast_mode"] = False
                    save_laser_db = True
                    logger.debug("{} fast mode timeout".format(active_laser))

            # auto-exposure
            for ccd, peak in enumerate(peaks):
                if not (0.3 < peak < 0.7):
                    exp = self.laser_db.raw_view[active_laser]["exposure"][ccd]
                    new_exp = exp + 1 if peak < 0.3 else exp - 1
                    new_exp = min(new_exp, self.wlm.get_exposure_max())
                    new_exp = max(new_exp, self.wlm.get_exposure_min())
                    self.laser_db[active_laser]["exposure"][ccd] = new_exp
                    save_laser_db = True
                    self.wlm.set_exposure(new_exp, ccd)

            # check which other tasks wanted this data
            for task in measure_tasks:
                if task["laser"] == active_laser \
                   and freq["timestamp"] > task["expiry"] \
                   and (osa.get("timestamp", 0) > task["expiry"]
                        or not task["get_osa_trace"]):
                    task["done"].set()
                    self.queue.remove(task)
                    logger.info("task {} complete".format(task["id"]))

            await asyncio.sleep(0)

    def take_freq_measurement(self, laser):
        """ Preform a frequency measurement and, if necessary, adjust the
        wlm exposure time. """
        logger.info("Taking new frequency measurement for {}".format(laser))

        status, freq = self.wlm.get_frequency()
        freq = {
            "freq": freq,
            "status": int(status),
            "timestamp": time.time()
        }

        # make simulation data more interesting!
        if self.args.simulation:
            freq["freq"] = self.laser_db.raw_view[laser]["f_ref"] \
                           + np.random.normal(loc=0, scale=10e6)

        logger.debug("New frequency data available for {} at {}: {} Hz".format(
            laser,
            self.freq_db.raw_view[laser]["timestamp"],
            self.freq_db.raw_view[laser]["freq"]))

        # auto-exposure
        if self.laser_db.raw_view[laser]["auto_exposure"]:
            peaks = [0]*self.wlm.get_num_ccds()
            for ccd in range(len(peaks)):
                peaks[ccd] = self.wlm.get_fringe_peak(ccd)
        else:
            peaks = []

        return freq, peaks

    def take_osa_measurement(self, laser, get_osa_trace):
        """ Capture an osa trace """
        if not get_osa_trace:
            return {}

        osa = {"trace": self.osas.get_trace(
                    self.laser_db.raw_view[laser]["osa"]),
               "timestamp": time.time()
               }
        osa["trace"] = osa["trace"].tolist()  # work around numpy bug
        logger.info("New osa trace available for {} at {}".format(
            laser, self.osa_db.raw_view[laser]["timestamp"]))
        return osa

    def save_config_file(self):
        self.config["lasers"] = self.laser_db.raw_view
        config_path, _ = get_config_path(self.args, "_server")
        pyon.store_file(config_path, self.config)

    class ControlInterface:
        """ RPC interface to the WAnD server """

        def __init__(self, wand_server):
            self.server = wand_server

        async def get_freq(self, laser, age=0, priority=3, get_osa_trace=False,
                           blocking=True, mute=False, offset_mode=False):
            """ Returns the frequency of a laser and, optionally, an osa trace.

            :param laser: name of the laser to interrogate
            :param age: how old the measurement may be (s). age <= 0 implies
              that a new measurement must be taken. ages > 0 imply that we can
              use cached data from a previous measurement, potentially allowing
              this method to return faster
            :param priority: integer giving the priority of the measurement.
              Measurements are processed first in priority order (higher
              higher priorities first), and then in chronological order (FIFO).
              Default (3)
            :param get_osa_trace: if True, we also return an OSA trace
            :param blocking: if False this function returns a task id
              immediately upon scheduling the measurement. Otherwise, it waits
              for the measurement to complete. Note that setting blocking to
              False overrides the setting of :param mute:
            :param mute: if True, we return a task id instead of the
              measurement data. This may be useful, for example, in clients
              which are using the notify interface to listen for measurements
              and don't want to receive the data sent twice
            :param offset_mode: if True, frequencies are returned as detunings
              from the reference frequency (default: False)
            :return: if mute is True, returns None, otherwise it returns either
              status, freq or (status, freq, osa_trace) depending on whether
              get_osa_trace is True. Status is a
              wand.tools.WLMMeasurementStatus (cast to an int to seralize).
              freq is in Hz
            """
            if laser not in self.server.laser_db.raw_view.keys():
                raise ValueError("unrecognised laser name '{}'".format(laser))
            if not isinstance(age, int) and not isinstance(age, float):
                raise ValueError("age must be an integer or float")
            if not isinstance(priority, int):
                raise ValueError("priority must be an integer")
            if not isinstance(get_osa_trace, bool):
                raise ValueError("get_osa_trace must be a bool")
            if not isinstance(blocking, bool):
                raise ValueError("blocking must be a bool")
            if not isinstance(mute, bool):
                raise ValueError("mute must be a bool")

            expiry = time.time() - max(0, age)
            freq_ts = self.server.freq_db.raw_view[laser]["timestamp"]
            osa_ts = self.server.osa_db.raw_view[laser]["timestamp"]

            # do we need to take a new measurement?
            if (freq_ts > expiry) and (osa_ts > expiry or not get_osa_trace):
                if mute or not blocking:
                    return next(self.server.task_ids)  # fake task id

                freq = self.server.freq_db.raw_view[laser]["freq"]
                osa = self.server.osa_db.raw_view[laser]["trace"]

                if offset_mode:
                    f_ref = self.server.laser_db.raw_view[laser]["f_ref"]
                    freq = freq - f_ref

                if not get_osa_trace:
                    return freq
                else:
                    return freq, osa

            task = {
                "type": "measure",
                "laser": laser,
                "priority": priority,
                "expiry": expiry,
                "id": next(self.server.task_ids),
                "get_osa_trace": get_osa_trace,
                "done": asyncio.Event()}
            self.server.queue.append(task)
            self.server.tasks_queued.set()

            logger.info("measurement task scheduled with id {}".format(
                task["id"]))

            if not blocking:
                return task["id"]

            await task["done"].wait()
            logger.info("measurement task with id {} completed".format(
                task["id"]))

            if mute:
                return task["id"]

            freq = self.server.freq_db.raw_view[laser]["freq"]
            status = self.server.freq_db.raw_view[laser]["status"]
            if offset_mode:
                f_ref = self.server.laser_db.raw_view[laser]["f_ref"]
                freq = freq - f_ref

            if not get_osa_trace:
                return status, freq

            osa = self.server.osa_db.raw_view[laser]["trace"]
            return (status, freq, osa)

        def get_task_queue(self):
            """
            Returns a list of queued tasks in chronological (execution) order
            """
            queue = [task.copy() for task in self.server.queue]
            for task in queue:
                del task["done"]
            return queue

        async def set_exposure(self, laser, exposure, ccd, blocking=False):
            """ Sets the exposure time for a laser

            :param laser: name of the laser
            :param exposure: exposure time (ms)
            :param ccd: the CCD number. Must lie in range(get_num_wlm_ccds())
            :param blocking: if True, this function blocks until the WLM
              exposure update has completed
            """
            if laser not in self.server.laser_db.raw_view.keys():
                raise ValueError("unrecognised laser name '{}'".format(laser))
            if exposure < self.server.wlm.get_exposure_min() or \
               exposure > self.server.wlm.get_exposure_max():
                raise ValueError("invalid exposure")
            if ccd not in range(self.get_num_wlm_ccds()):
                raise ValueError("invalid WLM CCD number")
            if not isinstance(blocking, bool):
                raise ValueError("blocking must be a bool")

            task = {
                "type": "set_exposure",
                "laser": laser,
                "exposure": exposure,
                "ccd": ccd,
                "done": asyncio.Event(),
                "id": next(self.server.task_ids)
            }

            self.server.queue.append(task)
            self.server.tasks_queued.set()
            logger.info("set_exposure task scheduled with id {}".format(
                task["id"]))

            if not blocking:
                return

            await task["done"].wait()
            logger.info("set_exposure task with id {} completed".format(
                task["id"]))

        def set_auto_exposure(self, laser, enabled):
            """ Enable or disable autoexposure for a given laser """
            if laser not in self.server.laser_db.raw_view.keys():
                raise ValueError("unrecognised laser name '{}'".format(laser))

            task = {
                "type": "set_auto_exposure",
                "laser": laser,
                "enabled": enabled,
                "done": asyncio.Event(),
                "id": next(self.server.task_ids)
            }

            self.server.queue.append(task)
            self.server.tasks_queued.set()

        def get_min_exposure(self):
            """
            Returns the WaveLength Meter (WLM) minimum exposure time (ms)
            """
            return self.server.wlm.get_exposure_min()

        def get_max_exposure(self):
            """
            Returns the WaveLength Meter (WLM) maximum exposure time (ms)
            """
            return self.server.wlm.get_exposure_max()

        def get_num_wlm_ccds(self):
            """ Returns the number of CCDs on the WaveLength meter (WLM) """
            return self.server.wlm.get_num_ccds()

        def get_laser_db(self):
            """ Returns the laser configuration database """
            return self.server.laser_db.raw_view

        async def set_reference_freq(self, laser, f_ref, blocking=False):
            """ Sets the reference frequency for a laser (Hz)
            :param blocking: if True, this function blocks until the reference
              update request has been processed
            """
            if laser not in self.server.laser_db.raw_view.keys():
                raise ValueError("unrecognised laser name '{}'".format(laser))
            if not isinstance(f_ref, int) and not isinstance(f_ref, float):
                raise ValueError(
                    "reference frequency must be an integer or float")
            if not isinstance(blocking, bool):
                raise ValueError("blocking must be a bool")

            task = {
                "type": "set_f_ref",
                "laser": laser,
                "f_ref": f_ref,
                "done": asyncio.Event(),
                "id": next(self.server.task_ids)
            }

            self.server.queue.append(task)
            self.server.tasks_queued.set()
            logger.info("set_f_ref task scheduled with id {}".format(
                task["id"]))

            if not blocking:
                return

            await task["done"].wait()
            logger.info("set_f_ref task with id {} completed".format(
                task["id"]))

        def set_fast_mode(self, laser, fast_mode):
            """ Enables or disables fast data acquisition mode for a laser.

            This parameter is used by clients to decide how quickly to poll
            the server for data.

            :param fast_mode: if True, we enable fast mode, if false it is
            disabled
            """
            if laser not in self.server.laser_db.raw_view.keys():
                raise ValueError("unrecognised laser name '{}'".format(laser))
            if not isinstance(fast_mode, bool):
                raise ValueError("fast_mode must be a bool")

            self.server.laser_db[laser]["fast_mode"] = fast_mode
            self.server.laser_db[laser]["fast_mode_set_at"] = time.time()
            self.server.save_config_file()

        def set_lock_params(self, laser, gain, poll_time, capture_range):
            """ Sets the feedback parameters used by wand_lock

            :param gain: the feedback gain to use (V/Hz)
            :param poll_time: time (s) between lock updates
            :param capture_range: lock capture range (Hz)
            """
            if laser not in self.server.laser_db.raw_view.keys():
                raise ValueError("unrecognised laser name '{}'".format(laser))
            if not isinstance(gain, int) and not isinstance(gain, float):
                raise ValueError(
                    "lock gain must be an integer or float")
            if not isinstance(poll_time, int) \
               and not isinstance(poll_time, float):
                raise ValueError(
                    "lock poll time must be an integer or float")
            if not isinstance(capture_range, int) \
               and not isinstance(capture_range, float):
                raise ValueError(
                    "lock capture range must be an integer or float")

            laser_db = self.server.laser_db
            laser_db[laser]["lock_gain"] = abs(gain)
            laser_db[laser]["lock_poll_time"] = abs(poll_time)
            laser_db[laser]["lock_capture_range"] = abs(capture_range)
            self.server.save_config_file()

        def set_lock_status(self, laser, locked, owner):
            """ Sets the lock status for a laser """
            if laser not in self.server.laser_db.raw_view.keys():
                raise ValueError("unrecognised laser name '{}'".format(laser))
            if not isinstance(locked, bool):
                raise ValueError("lock status must be a bool")
            if not isinstance(owner, str):
                raise ValueError("lock owner must be a string")

            self.server.laser_db[laser]["locked"] = locked
            self.server.laser_db[laser]["lock_owner"] = owner
            self.server.laser_db[laser]["locked_at"] = time.time()

            self.server.save_config_file()


def main():
    server = WandServer()
    server.start()


if __name__ == "__main__":
    main()
