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

from sipyco import pyon
from sipyco.pc_rpc import Server as RPCServer
from sipyco.sync_struct import Publisher, Notifier
from sipyco.common_args import (simple_network_args, bind_address_from_args,
                                init_logger_from_args, verbosity_args)
from sipyco.asyncio_tools import atexit_register_coroutine

# v3.0.1 is the most recent Toptica DLC pro firmware version at the time of writing;
# however, the piezo controller set-point interface has been the same all the way back
# to much older versions.
from toptica.lasersdk.asyncio.dlcpro.v3_0_1 import DecopError, DLCpro, NetworkConnection

from wand.drivers.leoni_switch import LeoniSwitch
from wand.drivers.high_finesse import WLM, WLMException
from wand.drivers.ni_osa import NiOSA
from wand.tools import (load_config, backup_config, regular_config_backup,
                        get_config_path, WLMMeasurementStatus)
from wand.server import ControlInterface


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
    verbosity_args(parser)
    parser.add_argument("-n", "--name",
                        default="test",
                        help="server name, used to locate configuration file")
    parser.add_argument("--simulation",
                        action='store_true',
                        help="run in simulation mode")
    parser.add_argument("--fast-mode-timeout",
                        default=1800,
                        type=int,
                        help="fast mode timeout (s) (default: '%(default)s')")
    parser.add_argument("-b", "--backup-dir",
                        default="",
                        type=str,
                        help="directory containing backup copies of "
                             "configuration files")
    return parser


class WandServer:
    def __init__(self):

        self.args = args = get_argparser().parse_args()
        init_logger_from_args(args)

        self.config = load_config(args, "_server")
        self.lasers = self.config["lasers"].keys()

        for laser in self.lasers:
            self.config["lasers"][laser]["lock_ready"] = False

        # connect to hardware
        self.wlm = WLM(args.simulation)

        if self.config.get("osas", "wlm") != "wlm":
            self.osas = NiOSA(self.config["osas"], args.simulation)

        self.exp_min = self.wlm.get_exposure_min()
        self.exp_max = self.wlm.get_exposure_max()
        self.num_ccds = self.wlm.get_num_ccds()

        if self.config["switch"]["type"] == "internal":
            self.switch = self.wlm.get_switch()
        elif self.config["switch"]["type"] == "leoni":
            self.switch = LeoniSwitch(
                self.config["switch"]["ip"], args.simulation)
        elif self.config["switch"]["type"] == None:
            self.switch = None
        else:
            raise ValueError("Unrecognised switch type: {}".format(
                self.config["switch"]["type"]))

        # measurement queue, processed by self.measurement_task
        self.measurement_ids = task_id_generator()
        self.measurements_queued = asyncio.Event()
        self.queue = []

        self.wake_locks = {laser: asyncio.Event() for laser in self.lasers}

        # schedule initial frequency/osa readings all lasers
        self.measurements_queued.set()
        for laser in self.lasers:
            self.queue.append({
                "laser": laser,
                "priority": 0,
                "expiry": time.time(),
                "id": next(self.measurement_ids),
                "get_osa_trace": True,
                "done": asyncio.Event()
            })

        # "notify" interface
        self.laser_db = Notifier(self.config["lasers"])
        self.freq_db = Notifier({laser: {
            "freq": None,
            "status": int(WLMMeasurementStatus.ERROR),
            "timestamp": 0
        } for laser in self.lasers})
        self.osa_db = Notifier({laser: {
            "trace": None,
            "timestamp": 0
        } for laser in self.lasers})

        self.server_notify = Publisher({
            "laser_db": self.laser_db,  # laser settings
            "freq_db": self.freq_db,  # most recent frequency measurements
            "osa_db": self.osa_db  # most recent osa traces
        })

        # "control" interface
        self.control_interface = ControlInterface(self)
        self.server_control = RPCServer({"control": self.control_interface},
                                        allow_parallel=True)

        self.running = False

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

        asyncio.ensure_future(self.measurement_task())

        for laser in self.lasers:
            asyncio.ensure_future(self.lock_task(laser))

        # backup of configuration file
        backup_config(self.args, "_server")
        asyncio.ensure_future(regular_config_backup(self.args, "_server"))
        atexit.register(backup_config, self.args, "_server")

        logger.info("server started")
        self.running = True
        loop.run_forever()

    async def lock_task(self, laser):
        conf = self.laser_db.raw_view[laser]

        # only try to lock lasers with a controller specified
        if not conf.get("host") or self.args.simulation:
            return

        while self.running:
            conf["lock_ready"] = False

            try:
                dlcpro = DLCpro(NetworkConnection(conf["host"]))
                await dlcpro.open()
                iface = getattr(dlcpro, conf.get("target", "laser1"))
            except (DecopError, OSError):
                logger.warning(
                    "could not connect to laser '{}', retrying in 60s (lock unavailable)"
                    .format(laser))
                if conf["locked"]:
                    self.control_interface.unlock(laser, conf["lock_owner"])
                try:
                    await dlcpro.close()
                except:
                    logger.warning(f"failed to clean up connection to '{laser}'", exc_info=True)
                    pass
                await asyncio.sleep(60)
                continue

            self.wake_locks[laser].set()
            conf["lock_ready"] = True

            while self.running:

                if not conf["locked"]:
                    await self.wake_locks[laser].wait()
                    self.wake_locks[laser].clear()
                    continue

                poll_time = conf["lock_poll_time"]
                locked_at = conf["locked_at"]
                timeout = conf["lock_timeout"]
                set_point = conf["lock_set_point"]
                gain = conf["lock_gain"] * poll_time
                capture_range = conf["lock_capture_range"]
                v_pzt_max = conf.get("v_pzt_max", 100)
                v_pzt_min = conf.get("v_pzt_min", 25)

                await asyncio.wait({self.wake_locks[laser].wait()},
                                   timeout=poll_time)
                self.wake_locks[laser].clear()

                if timeout is not None and time.time() > (locked_at + timeout):
                    logger.info("'{}' lock timed out".format(laser))
                    self.control_interface.unlock(laser, conf["lock_owner"])
                    await asyncio.sleep(0)
                    continue

                status, delta, _ = await self.control_interface.get_freq(
                    laser, age=0, priority=5, get_osa_trace=False,
                    blocking=True, mute=False, offset_mode=True)

                if status != WLMMeasurementStatus.OKAY:
                    continue

                f_error = delta - set_point
                V_error = f_error * gain

                if abs(f_error) > capture_range:
                    logger.warning("'{}' outside capture range".format(laser))
                    self.control_interface.unlock(laser, conf["lock_owner"])
                    await asyncio.sleep(0)
                    continue

                # don't drive the PZT too far in a single step
                V_error = min(V_error, 0.25)
                V_error = max(V_error, -0.25)

                try:
                    v_pzt = await iface.dl.pc.voltage_set.get()
                    v_pzt -= V_error

                    if v_pzt > v_pzt_max or v_pzt < v_pzt_min:
                        logger.warning(
                            f"'{laser}' lock railed, piezo voltage: {v_pzt:.2f}V " +
                            f"outside range {v_pzt_min} - {v_pzt_max}V"
                        )
                        self.control_interface.unlock(laser,
                                                      conf["lock_owner"])
                        await asyncio.sleep(0)
                        continue

                    await iface.dl.pc.voltage_set.set(v_pzt)

                except OSError:
                    logger.warning("Connection to laser '{}' lost"
                                   .format(laser))
                    self.control_interface.unlock(laser, conf["lock_owner"])
                    await asyncio.sleep(0)
                    break

        try:
            await dlcpro.close()
        except Exception:
            pass
        finally:
            conf["lock_ready"] = False

    async def measurement_task(self):
        """ Process queued measurements """
        active_laser = ""

        while True:

            if self.queue == []:
                self.measurements_queued.clear()
            await self.measurements_queued.wait()

            # process in order of priority, followed by submission time
            priorities = [meas["priority"] for meas in self.queue]
            meas = self.queue[priorities.index(max(priorities))]

            laser = meas["laser"]
            laser_conf = self.laser_db.raw_view[laser]

            if laser != active_laser and self.switch is not None:
                self.switch.set_active_channel(laser_conf["channel"])

                # Switching is slow so we might as well take an OSA trace!
                meas["get_osa_trace"] = True

                # We only need to change the range after switching channels
                try:
                    range_ = laser_conf.get("wavelength_range")
                    self.wlm.set_wavelength_range(range_)
                except WLMException:
                    pass

                await asyncio.sleep(self.config["switch"]["dead_time"])
            active_laser = meas["laser"]

            exposure = laser_conf["exposure"]
            for ccd, exp in enumerate(exposure):
                self.wlm.set_exposure(exposure[ccd], ccd)

            if laser_conf.get("osa", "wlm") == "wlm":
                freq_osa_measurement = self.loop.run_in_executor(
                    self.executor,
                    self.take_freq_osa_measurement,
                    laser,
                    laser_conf["f_ref"],
                    meas["get_osa_trace"])
                wlm_data, osa = await freq_osa_measurement

            else:
                freq_measurement = self.loop.run_in_executor(
                    self.executor,
                    self.take_freq_measurement,
                    laser,
                    laser_conf["f_ref"])

                osa_measurement = self.loop.run_in_executor(
                    self.executor,
                    self.take_osa_measurement,
                    laser,
                    laser_conf.get("osa"),
                    meas["get_osa_trace"])

                wlm_data, osa = (await asyncio.gather(freq_measurement,
                                                      osa_measurement))[:]

            freq, peaks = wlm_data
            self.freq_db[laser] = freq

            if meas["get_osa_trace"]:
                self.osa_db[laser] = osa

            # fast mode timeout
            if laser_conf["fast_mode"]:
                t_en = laser_conf["fast_mode_set_at"]
                if time.time() > (t_en + self.args.fast_mode_timeout):
                    self.laser_db[laser]["fast_mode"] = False
                    self.save_config_file()
                    logger.info("{} fast mode timeout".format(laser))

            # auto-exposure
            if laser_conf["auto_exposure"]:
                new_exp = laser_conf["exposure"]
                for ccd, peak in enumerate(peaks):

                    # don't try to find a suitable exposure for lasers that
                    # aren't on!
                    if peak < 0.05:
                        break

                    if not (0.4 < peak < 0.6):
                        exp = laser_conf["exposure"][ccd]
                        new_exp[ccd] = exp + 1 if peak < 0.4 else exp - 1
                        new_exp[ccd] = min(new_exp[ccd], self.exp_max[ccd])
                        new_exp[ccd] = max(new_exp[ccd], self.exp_min[ccd])

                if new_exp != exp:
                    self.laser_db[laser]["exposure"] = new_exp
                    self.save_config_file()

            # check which other measurements wanted this data
            for task in self.queue:
                if task["laser"] == laser \
                   and (meas["get_osa_trace"] or not task["get_osa_trace"]):
                    task["done"].set()
                    self.queue.remove(task)
                    logger.info("task {} complete".format(task["id"]))

    def take_freq_measurement(self, laser, f0):
        """ Preform a single frequency measurement """
        logger.info("Taking new frequency measurement for {}".format(laser))

        status, freq = self.wlm.get_frequency()
        freq = {
            "freq": freq,
            "status": int(status),
            "timestamp": time.time()
        }

        # make simulation data more interesting!
        if self.args.simulation:
            freq["freq"] = f0 + np.random.normal(loc=0, scale=10e6)

        peaks = [self.wlm.get_fringe_peak(ccd) for ccd in range(self.num_ccds)]

        return freq, peaks

    def take_osa_measurement(self, laser, osa, get_osa_trace):
        """ Capture an osa trace """
        if not get_osa_trace:
            return {
                "trace": None,
                "timestamp": time.time()
            }

        osa = {"trace": self.osas.get_trace(osa).tolist(),
               "timestamp": time.time()
               }
        return osa

    def take_freq_osa_measurement(self, laser, f0, get_osa_trace):
        """ Get frequency and spectral data from the wlm """
        logger.info("Taking new frequency + spectral measurement for {}"
                    .format(laser))

        status, freq = self.wlm.get_frequency()
        freq = {
            "freq": freq,
            "status": int(status),
            "timestamp": time.time()
        }

        # make simulation data more interesting!
        if self.args.simulation:
            freq["freq"] = f0 + np.random.normal(loc=0, scale=10e6)

        peaks = [self.wlm.get_fringe_peak(ccd) for ccd in range(self.num_ccds)]

        if not get_osa_trace:
            osa = {
                "trace": None,
                "timestamp": time.time()
            }
        else:
            osa = {"trace": self.wlm.get_pattern().tolist(),
                   "timestamp": time.time()
                   }

        return (freq, peaks), osa

    def save_config_file(self):
        try:
            self.config["lasers"] = self.laser_db.raw_view
            config_path, _ = get_config_path(self.args, "_server")
            pyon.store_file(config_path, self.config)
        except Exception:
            logger.warning("error when trying to save config data")


def main():
    server = WandServer()
    server.start()


if __name__ == "__main__":
    main()
