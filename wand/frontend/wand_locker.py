""" Locks one or more lasers to their wavemeters """

import asyncio
import logging
import argparse
import time

from artiq.tools import (simple_network_args, init_logger,
                         atexit_register_coroutine, bind_address_from_args)
from artiq.protocols.pc_rpc import Server as RPCServer
from artiq.protocols.pc_rpc import AsyncioClient as RPCClient
from artiq.protocols.pc_rpc import Client as SyncRPCClient
from artiq.protocols.sync_struct import Subscriber

from wand.drivers.dl_pro import DLPro
from wand.tools import get_laser_db, WLMMeasurementStatus


# verbosity_args() was renamed to add_common_args() in ARTIQ 5.0; support both.
try:
    from artiq.tools import add_common_args
except ImportError:
    from artiq.tools import verbosity_args as add_common_args


logger = logging.getLogger(__name__)


def get_argparser():
    parser = argparse.ArgumentParser(description="WAnD laser locker")
    add_common_args(parser)
    parser.add_argument("-s", "--server",
                        action="append",
                        help="Add a WAnD server by IP address")
    simple_network_args(parser, [("lock", "laser lock server", 3252)])
    return parser


class LaserOwnedException(Exception):
    pass


class WandLocker:
    def __init__(self):
        args = get_argparser().parse_args()
        init_logger(args)

        if args.server == []:
            raise ValueError("No servers specified")

        self.servers = {
            idx: {
                "host": ip,
                "notify": 3250,
                "control": 3251
            }
            for idx, ip in enumerate(args.server)}

        self.laser_db, self.laser_sup_db = get_laser_db(self.servers)
        self.lock_db = {
            laser: {
                "set_point": 0,
                "timeout": None,
                "lock_task": None,
                "detuning": None,
                "Vpzt": None,
                "freq_timestamp": None,
                # immediately run an iteration of the loop when params change
                "wake_loop": asyncio.Event()
            }
            for laser in self.laser_db.keys()}

        self.loop = asyncio.get_event_loop()

        # start RPC interface
        self.control = RPCServer({"control": self}, allow_parallel=True)
        bind = bind_address_from_args(args)
        self.loop.run_until_complete(self.control.start(bind, args.port_lock))
        atexit_register_coroutine(self.control.stop)

        # connect to server notifier interfaces
        def init_cb(db, mod):
            db.update(mod)
            return db

        self.subscribers = {}
        for server, server_cfg in self.servers.items():

            async def reconnect_cb(server):
                logger.info(
                    "Connection to server '{}' lost, reconnecting...".format(
                        server))
                server_cfg = self.servers[server]
                subscriber = self.subscribers[server]

                try:
                    await subscriber.close()
                except Exception:
                    pass

                subscriber.disconnect_cb = lambda: self.loop.create_task(
                    reconnect_cb(server))

                while True:
                    try:
                        await subscriber.connect(server_cfg["host"],
                                                 server_cfg["notify"])
                        logger.info(
                            "Reconnected to server '{}'".format(server))
                        break
                    except Exception as err:
                        logger.info(
                            "could not connect to '{}' retry in 10s...".format(
                                server))
                        await asyncio.sleep(10)

            # ask the servers to keep us updated with changes to laser settings
            # like lock gain

            subscriber = Subscriber(
                "laser_db",
                lambda mod: init_cb(self.laser_db, mod),
                lambda mod: self._laser_db_cb(mod),
                disconnect_cb=lambda: self.loop.create_task(
                    reconnect_cb(server)))

            self.loop.run_until_complete(subscriber.connect(
                server_cfg["host"], server_cfg["notify"]))
            atexit_register_coroutine(subscriber.close)
            self.subscribers[server] = subscriber

    def _laser_db_cb(self, mod):
        if mod["action"] == "init":
            for laser in mod["struct"].keys():
                if self.lock_db[laser]["lock_task"]:
                    self.lock_db[laser]["wake_loop"].set()
                    continue
                elif mod["struct"][laser].get("locked"):
                    self.lock_db[laser]["lock_task"] = self.loop.create_task(
                        lock_task(laser, self))

        if mod["action"] != "setitem" or not isinstance(mod.get("key"), str):
            return

        if mod["key"].startswith("lock") or mod["key"] == "f_ref":
            laser = mod["path"][0]
            if self.lock_db[laser]["lock_task"]:
                self.lock_db[laser]["wake_loop"].set()
            elif mod["key"] == "locked" and mod["value"]:
                self.lock_db[laser]["lock_task"] = self.loop.create_task(
                    lock_task(laser, self))

    def lock(self, laser, set_point=None, name="", timeout=300):
        """ Locks a laser

        See also :meth unlock:

        :param laser: the name of the laser to lock
        :param set_point: the frequency offset (Hz) to lock to. If None, we
          keep the current set-point. NB set-points are not stored by the
          server and must be reprogrammed whenever the locker is restarted
          otherwise they revert to 0 (default: None)
        :param name: your name. If not "" then you take ownership of the laser,
          preventing anyone else from locking it. Ownership is released when
          the laser unlocks. c.f. :meth unlock:
        :param timeout: duration (s) to lock the laser for. After this
          time, the laser is unlocked and any ownership is released. If None:
          (default: 300), the laser is locked indefinitely.
        """
        if laser not in self.laser_db.keys():
            raise ValueError("Unrecognised laser {}".format(laser))

        current_owner = self.laser_db[laser]["lock_owner"]
        if current_owner and (current_owner != name):
            raise LaserOwnedException("Laser currently owned by {}!".format(
                current_owner))

        if set_point is None:
            set_point = self.lock_db[laser].get("set_point")

        if not isinstance(set_point, int) \
           and not isinstance(set_point, float):
            raise ValueError("set_point must be an int or a float")

        self.lock_db[laser]["set_point"] = set_point
        self.lock_db[laser]["timeout"] = timeout

        server = self.laser_sup_db[laser]["server"]
        server_cfg = self.servers[server]
        client = SyncRPCClient(server_cfg["host"],
                               server_cfg["control"],
                               timeout=1)
        try:
            client.set_lock_status(laser, True, name)
        finally:
            client.close_rpc()

        # force early loop update
        if self.lock_db[laser]["lock_task"]:
            self.lock_db[laser]["wake_loop"].set()

    def unlock(self, laser, name=""):
        """ Unlock and release ownership of a laser """
        if laser not in self.laser_db.keys():
            raise ValueError("Unrecognised laser {}".format(laser))

        current_owner = self.laser_db[laser]["lock_owner"]
        if current_owner and (current_owner != name):
            raise LaserOwnedException("Lock currently owned by {}!".format(
                current_owner))

        server = self.laser_sup_db[laser]["server"]
        server_cfg = self.servers[server]
        client = SyncRPCClient(server_cfg["host"],
                               server_cfg["control"],
                               timeout=1)
        try:
            client.set_lock_status(laser, False, "")
        finally:
            client.close_rpc()

    def set_lock_params(self, laser, gain, poll_time, capture_range):
        """ Sets the feedback parameters used by wand_lock

        :param gain: the feedback gain to use (V/Hz)
        :param poll_time: time (s) between lock updates
        :param capture_range: lock capture range (Hz). If the frequency error
          is larger than this then we unlock the laser and release ownership.
        """
        server = self.laser_sup_db[laser]["server"]
        server_cfg = self.servers[server]

        # This blocks the event loop, but should return promptly and should
        # only be called rarely
        client = SyncRPCClient(server_cfg["host"],
                               server_cfg["control"],
                               timeout=1)
        try:
            client.set_lock_params(laser, gain, poll_time, capture_range)
        finally:
            client.close_rpc()

    def get_owner(self, laser):
        """ Returns the current owner of a laser, or None if no one currently
         claims ownership of it """
        return self.laser_db[laser]["lock_owner"]

    def get_lock_status(self, laser):
        """ Returns a dictionary of information about a laser lock """
        return {key: value for key, value in self.laser_db[laser].items()
                if key.startswith("lock")}

    def steal(self, laser):
        """
        Sets the current owner of laser to None, without changing its lock
        status.
        """
        server = self.laser_sup_db[laser]["server"]
        server_cfg = self.servers[server]
        client = SyncRPCClient(server_cfg["host"],
                               server_cfg["control"],
                               timeout=1)
        try:
            client.set_lock_status(laser, self.laser_db[laser]["locked"], "")
        finally:
            client.close_rpc()

    def get_laser_dbs(self):
        """ Returns a database of laser parameters stored by the WAnD servers
        we are connected to """
        return self.laser_db


async def lock_task(laser, locker):
    """ Locking a single laser """

    server = locker.laser_sup_db[laser]["server"]
    server_cfg = locker.servers[server]

    try:
        rpc_client = RPCClient()
        await rpc_client.connect_rpc(server_cfg["host"],
                                     server_cfg["control"],
                                     target_name="control")
        # don't reconnect to the laser each time we lock as they make an
        # infuriating sound!
        laser_iface = locker.laser_sup_db[laser].get("iface")
        if laser_iface is None:
            laser_iface = DLPro(
                locker.laser_db[laser]["host"],
                target=locker.laser_db[laser].get("target", "laser1"))
            locker.laser_sup_db[laser]["iface"] = laser_iface
            logger.info("Connected to {} laser".format(laser))

        locker.lock_db[laser]["last_timestep"] = time.time()

        logger.info("{} locked".format(laser))

        while locker.laser_db[laser]["locked"]:

            locker.lock_db[laser]["wake_loop"].clear()

            timeout = locker.lock_db[laser]["timeout"]
            locked_at = locker.laser_db[laser]["locked_at"]
            if timeout is not None and time.time() > (locked_at + timeout):
                    logger.info("{} lock timed out".format(laser))
                    break

            try:
                status, delta = await rpc_client.get_freq(laser,
                                                          age=0,
                                                          priority=5,
                                                          get_osa_trace=False,
                                                          blocking=True,
                                                          mute=False,
                                                          offset_mode=True)
                locker.lock_db[laser]["detuning"] = delta
                locker.lock_db[laser]["freq_timestamp"] = time.time()
            except Exception:
                locker.lock_db[laser]["lock_task"].cancel()
                assert 0  # should never be reached...

            if status != WLMMeasurementStatus.OKAY:
                await asyncio.wait(
                    {locker.lock_db[laser]["wake_loop"].wait()},
                    timeout=locker.laser_db[laser]["lock_poll_time"])
                continue

            detuning = locker.lock_db[laser]["detuning"]
            set_point = locker.lock_db[laser]["set_point"]
            gain = locker.laser_db[laser]["lock_gain"]

            f_error = detuning - set_point
            V_error = f_error * gain

            # don't drive the PZT too far in a single step
            V_error = min(V_error, 0.25)
            V_error = max(V_error, -0.25)

            v_pzt = laser_iface.get_pzt_voltage()
            v_pzt -= V_error

            logger.info("{}: f_error {} MHz, Vpzt {} V".format(
                laser, f_error/1e6, v_pzt))

            if abs(f_error) > locker.laser_db[laser]["lock_capture_range"]:
                logger.warning("{} outside capture range".format(laser))
                continue

            if v_pzt > 100 or v_pzt < 25:
                logger.warning("{} lock railed".format(laser))

            laser_iface.set_pzt_voltage(v_pzt)

            await asyncio.wait(
                {locker.lock_db[laser]["wake_loop"].wait()},
                timeout=locker.laser_db[laser]["lock_poll_time"])

    except Exception:
        laser_iface.close()
        locker.laser_sup_db[laser]["iface"] = None
    finally:
        await rpc_client.set_lock_status(laser, False, "")
        locker.lock_db[laser]["lock_task"] = None
        rpc_client.close_rpc()
        logger.info("{} lock task complete".format(laser))


def main():
    locker = WandLocker()
    logger.info("locker ready")
    locker.loop.run_forever()


if __name__ == "__main__":
    main()
