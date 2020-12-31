""" Wavelength Analysis 'Nd Display GUI """
import argparse
import logging
import pkg_resources
import sys
import traceback
import asyncio
import atexit
import functools

from sipyco.common_args import init_logger_from_args, verbosity_args
from sipyco.asyncio_tools import atexit_register_coroutine
from sipyco.sync_struct import Subscriber

from qasync import QEventLoop
from PyQt5 import QtWidgets, QtGui
import pyqtgraph.dockarea as dock

from wand.gui import LaserDisplay
from wand.tools import load_config


logger = logging.getLogger(__name__)


def get_argparser():
    parser = argparse.ArgumentParser(description="WAnD GUI")
    parser.add_argument("-n", "--name",
                        default="test",
                        help="server name, used to locate configuration file")
    parser.add_argument("-f", "--log-to-file",
                        action="store_true",
                        help="Save log output to file")
    parser.add_argument("-b", "--backup-dir",
                        default="",
                        type=str,
                        help="directory containing backup copies of "
                             "configuration files")
    verbosity_args(parser)

    return parser


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        QtWidgets.QMainWindow.__init__(self)
        self.exit_request = asyncio.Event()

    def closeEvent(self, event):
        event.ignore()
        self.exit_request.set()


class WandGUI():
    def __init__(self):

        self.args = args = get_argparser().parse_args()
        init_logger_from_args(args)

        if args.log_to_file:
            log_file = pkg_resources.resource_filename("wand", "log.txt")
            fh = logging.FileHandler(log_file, mode="wt")
            fh.setLevel(logger.getEffectiveLevel())
            logger.addHandler(fh)
            logging.getLogger("qasync").addHandler(fh)
            sys.excepthook = lambda exc_type, exc_value, exc_traceback: \
                logger.exception("".join(
                    traceback.format_exception(exc_type,
                                               exc_value,
                                               exc_traceback)))

        self.config = load_config(args, "_gui")

        self.laser_db = {}
        self.freq_db = {}
        self.osa_db = {}
        self.subscribers = {}

        self.qapp = QtWidgets.QApplication(["WAnD"])
        self.loop = QEventLoop(self.qapp)
        asyncio.set_event_loop(self.loop)
        atexit.register(self.loop.close)

        # set program icon
        icon = QtGui.QIcon()
        icon.addFile(pkg_resources.resource_filename("wand", "wand.svg"))
        self.qapp.setWindowIcon(icon)

        # create main window
        self.win = MainWindow()
        self.area = dock.DockArea()
        self.win.setCentralWidget(self.area)
        self.win.setWindowTitle("Super-duper Python Wavemeter Viewer!")

        # populate GUI
        self.laser_displays = {}
        for row in self.config["layout"]:
            prev = None
            pos = 'bottom'
            for display_name in row:
                display = LaserDisplay(display_name, self)
                self.laser_displays.update({display.laser: display})
                self.area.addDock(display.dock, position=pos, relativeTo=prev)
                pos = 'right'
                prev = display.dock

    def notifier_cb(self, db, server, mod):
        """ Called whenever we get new data from a server "notifier" interface.

        NB sync_struct takes care of updating the relevant db for us, so all we
        do here is call the relevant GUI update function.
        """
        if mod["action"] == "init":
            self.subscribers[server][db]["connected"] = True

        # check we're fully connected to the server before processing updates
        if (
            not self.subscribers[server]["laser_db"]["connected"] or
            not self.subscribers[server]["freq_db"]["connected"] or
            not self.subscribers[server]["osa_db"]["connected"]
        ):
            return

        if mod["action"] == "init":
            # called when we first connect to a Notifier
            # we only activate the GUI channel for a laser once we have initial
            # data from all three Notifier interfaces (laser, freq, osa)
            displays = self.laser_displays

            for laser in mod["struct"].keys():
                if laser not in self.laser_displays.keys():
                    continue

                if displays[laser].server not in [server, ""]:
                    logger.error("laser '{}' found on multiple servers")
                    displays.server = ""
                else:
                    displays[laser].server = server

                displays[laser].wake_loop.set()

        elif mod["action"] == "setitem":
            if mod["path"] == []:
                laser = mod["key"]
            else:
                laser = mod["path"][0]

            if laser not in self.laser_displays.keys():
                return

            if db == "freq_db":
                self.laser_displays[laser].update_freq()
            elif db == "osa_db":
                self.laser_displays[laser].update_osa_trace()
            elif db == "laser_db":
                if mod["key"] == "f_ref":
                    self.laser_displays[laser].update_reference()
                elif (mod["key"] == "exposure" or
                      (len(mod["path"]) > 1 and mod["path"][1] == "exposure")):
                    self.laser_displays[laser].update_exposure()
                elif mod["key"] == "fast_mode":
                    self.laser_displays[laser].update_fast_mode()
                elif mod["key"] == "auto_exposure":
                    self.laser_displays[laser].update_auto_exposure()
                elif mod["key"] in ["locked", "lock_owner"]:
                    self.laser_displays[laser].update_laser_status()
            else:
                raise ValueError("Unexpected notifier interface")
        else:
            raise ValueError("Unexpected 'notifier' modification: {}"
                             .format(mod))

    def start(self):
        """ Connect to the WaND servers """

        def init_cb(db, mod):
            db.update(mod)
            return db

        async def subscriber_reconnect(self, server, db):
            logger.info("No connection to server '{}'".format(server))

            for _, display in self.laser_displays.items():
                if display.server == server:
                    display.server = ""
                    display.wake_loop.set()

            server_cfg = self.config["servers"][server]
            subscriber = self.subscribers[server][db]["subscriber"]

            if self.win.exit_request.is_set():
                return

            def make_fut(self, server, db):
                fut = asyncio.ensure_future(
                    subscriber_reconnect(self, server, db))
                self.subscribers[server][db]["connected"] = False
                self.subscribers[server][db]["future"] = fut

            subscriber.disconnect_cb = functools.partial(
                make_fut, self, server, db)

            while not self.win.exit_request.is_set():
                try:
                    await subscriber.connect(server_cfg["host"],
                                             server_cfg["notify"])

                    logger.info("Reconnected to server '{}'".format(server))
                    break
                except OSError:
                    logger.info("could not connect to '{}' retry in 10s..."
                                .format(server))
                    await asyncio.sleep(10)

        for server, server_cfg in self.config["servers"].items():
            self.subscribers[server] = {}

            # ask the servers to keep us updated with changes to laser settings
            # (exposures, references, etc)
            subscriber = Subscriber(
                "laser_db",
                functools.partial(init_cb, self.laser_db),
                functools.partial(self.notifier_cb, "laser_db", server))
            fut = asyncio.ensure_future(
                subscriber_reconnect(self, server, "laser_db"))
            self.subscribers[server]["laser_db"] = {
                "subscriber": subscriber,
                "connected": False,
                "future": fut
            }

            # ask the servers to keep us updated with the latest frequency data
            subscriber = Subscriber(
                "freq_db",
                functools.partial(init_cb, self.freq_db),
                functools.partial(self.notifier_cb, "freq_db", server))
            fut = asyncio.ensure_future(
                subscriber_reconnect(self, server, "freq_db"))
            self.subscribers[server]["freq_db"] = {
                "subscriber": subscriber,
                "connected": False,
                "future": fut
            }

            # ask the servers to keep us updated with the latest osa traces
            subscriber = Subscriber(
                "osa_db",
                functools.partial(init_cb, self.osa_db),
                functools.partial(self.notifier_cb, "osa_db", server))
            fut = asyncio.ensure_future(
                subscriber_reconnect(self, server, "osa_db"))
            self.subscribers[server]["osa_db"] = {
                "subscriber": subscriber,
                "connected": False,
                "future": fut
            }

        atexit_register_coroutine(self.shutdown)

        self.win.showMaximized()
        atexit.register(self.win.exit_request.set)
        self.loop.run_until_complete(self.win.exit_request.wait())

    async def shutdown(self):
        self.win.exit_request.set()

        for _, server in self.subscribers.items():
            for _, subs_dict in server.items():
                subs = subs_dict["subscriber"]
                fut = subs_dict["future"]
                try:
                    await subs.close()
                except Exception:
                    pass
                if fut is not None and not fut.done():
                    fut.cancel()
        for _, display in self.laser_displays.items():
            display.fut.cancel()


def main():
    gui = WandGUI()
    gui.start()


if __name__ == "__main__":
    main()
