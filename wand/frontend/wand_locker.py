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
from wand.tools import get_laser_db


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
                "locked": False,
                "set_point": 0,
                "owner": None,
                "timeout": None,
                "lock_task": None,
                "detuning": None,
                "Vpzt": None,
                "freq_timestamp": None,
                "capture_range": 2e9
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
                disconnect_cb=lambda: self.loop.create_task(
                    reconnect_cb(server)))

            self.loop.run_until_complete(subscriber.connect(
                server_cfg["host"], server_cfg["notify"]))
            atexit_register_coroutine(subscriber.close)
            self.subscribers[server] = subscriber

    def lock(self, laser, set_point=None, enabled=True, name=None,
             claim_ownership=False, timeout=300, capture_range=None):
        """ lock/unlock a laser

        See also :meth release:

        :param laser: the name of the laser to lock
        :param set_point: the frequency offset (Hz) to lock to. If None, we
          keep the current set-point. NB set-points are not stored by the
          server and must be reprogrammed whenever the locker is restarted
          otherwise they revert to 0 (default: None)
        :param enabled: if True, the lock is active (default: True)
        :param name: your name -- required to claim ownership of a laser
          (default: None)
        :param claim_ownership: if True, you will "own" the laser upon
          successful completion of this call. Attempts to alter the lock
          parameters using a different name will raise a LaserOwnedException.
          Ownership can be lost/given away by: calling this method again with
          claim_ownership=False; timeout expiring; calling :meth steal:. For
          shared lasers, where possible, please consider embedding calls to
          this function within a try ... finally lock(claim_ownership=False)
          block.
        :param timeout: duration (s) to lock the laser for. After this
          time, the laser is unlocked and any ownership is released. If None:
          (default: 300), the laser is locked indefinitely.
        :capture_range: lock capture range (Hz). If the frequency error is
          larger than this then we unlock the laser and release ownership.
          If None, we keep the current value. NB whenever the locker server is
          restarted, all capture ranges default to 2GHz.
        """
        if laser not in self.laser_db.keys():
            raise ValueError("Unrecognised laser {}".format(laser))

        if self.laser_db[laser].get("lock_gain") is None:
            raise ValueError("No lock gain defined for {}".format(laser))
        if self.laser_db[laser].get("lock_poll_time") is None:
            raise ValueError("No lock poll time defined for {}".format(laser))

        current_owner = self.lock_db[laser]["owner"]
        if current_owner and (current_owner != name):
            raise LaserOwnedException("Laser currently owned by {}!".format(
                current_owner))

        if claim_ownership and not name:
            raise ValueError("Grrrr...one cannot claim ownership anonymously! "
                             "Plant a flag!!")

        if set_point is None:
            set_point = self.lock_db[laser].get("set_point")

        if not isinstance(set_point, int) \
           and not isinstance(set_point, float):
            raise ValueError("set_point must be an int or a float")

        if capture_range is None:
            capture_range = self.lock_db[laser].get("capture_range")

        if not isinstance(capture_range, int) \
           and not isinstance(capture_range, float):
            raise ValueError("Capture range must be an int or a float")

        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a bool")

        if enabled and not self.lock_db[laser]["locked"]:
            self.lock_db[laser]["locked_at"] = time.time()

        if claim_ownership and (name != self.lock_db[laser]["owner"]):
            self.lock_db[laser]["ownership_claimed_at"] = time.time()

        self.lock_db[laser]["set_point"] = set_point
        self.lock_db[laser]["capture_range"] = capture_range
        self.lock_db[laser]["locked"] = enabled
        self.lock_db[laser]["timeout"] = None if timeout is None \
            else time.time() + timeout

        if claim_ownership:
            self.lock_db[laser]["owner"] = name

        if self.lock_db[laser]["lock_task"] is None:
            self.lock_db[laser]["lock_task"] = self.loop.create_task(
                lock_task(laser, self))

    def unlock(self, laser, name):
        """ Unlock and release ownership of a laser """
        if laser not in self.laser_db.keys():
            raise ValueError("Unrecognised laser {}".format(laser))

        current_owner = self.lock_db[laser]["owner"]
        if current_owner and (current_owner != name):
            raise LaserOwnedException("Laser currently owned by {}!".format(
                current_owner))

        self.lock_db[laser]["locked"] = False
        self.lock_db[laser]["owner"] = None

    def set_lock_params(self, laser, gain, poll_time):
        """ Sets the feedback parameters used by wand_lock

        :param gain: the feedback gain to use (V/Hz)
        :param poll_time: time (s) between lock updates
        """
        server = self.laser_sup_db[laser]["server"]
        server_cfg = self.servers[server]

        # This blocks the event loop, but should return promptly and should
        # only be called rarely
        client = SyncRPCClient(server_cfg["host"],
                               server_cfg["control"],
                               timeout=1)
        try:
            client.set_lock_params(laser, gain, poll_time)
        finally:
            client.close_rpc()

    def get_owner(self, laser):
        """ Returns the current owner of a laser, or None if no one currently
         claims ownership of it """
        return self.lock_db[laser]["owner"]

    def get_lock_status(self, laser):
        """ Returns a dictionary of information about a laser lock """
        return {key: value for key, value in self.lock_db[laser].items()
                if key != "lock_task"}

    def steal(self, laser):
        """
        Sets the current owner of laser to None, without changing its lock
        status.
        """
        self.lock_db[laser]["owner"] = None

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
            laser_iface = DLPro(locker.laser_db[laser]["host"],
                target=locker.laser_db[laser].get("target", "laser1"))
            locker.laser_sup_db[laser]["iface"] = laser_iface
            logger.info("Connected to {} laser".format(laser))

        locker.lock_db[laser]["last_timestep"] = time.time()

        logger.info("{} locked".format(laser))

        while locker.lock_db[laser]["locked"]:

            if locker.lock_db[laser]["timeout"] is not None \
               and (time.time() > locker.lock_db[laser]["timeout"]):
                logger.info("{} lock timed out".format(laser))
                break

            try:
                delta = await rpc_client.get_freq(laser,
                                                  age=0,
                                                  priority=5,
                                                  get_osa_trace=False,
                                                  blocking=True,
                                                  mute=False,
                                                  offset_mode=True)
                locker.lock_db[laser]["detuning"] = delta
                locker.lock_db[laser]["freq_timestamp"] = time.time()
            except Exception as e:
                locker.lock_db[laser]["lock_task"].cancel()
                locker.lock_db[laser]["lock_task"] = None
                locker.lock_db[laser]["locked"] = False
                rpc_client.close_rpc()

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

            # NB this picks up WLM errors so long as f_ref > capture_range
            if abs(f_error) > locker.lock_db[laser]["capture_range"]:
                logger.warning("{} outside capture range".format(laser))
                continue

            if v_pzt > 100 or v_pzt < 25:
                logger.warning("{} lock railed".format(laser))

            laser_iface.set_pzt_voltage(v_pzt)

            await asyncio.sleep(locker.laser_db[laser]["lock_poll_time"])

    except Exception:
        laser_iface.close()
        locker.laser_sup_db[laser]["iface"] = None
    finally:
        locker.lock_db[laser]["locked"] = False
        locker.lock_db[laser]["owner"] = None
        locker.lock_db[laser]["lock_task"] = None
        rpc_client.close_rpc()

    logger.info("{} lock task complete".format(laser))


def main():
    locker = WandLocker()
    logger.info("locker ready")
    locker.loop.run_forever()


if __name__ == "__main__":
    main()
