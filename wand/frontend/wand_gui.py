""" Wavelength Analysis 'Nd Display GUI """

import argparse
import logging
import pkg_resources
import sys
import traceback
import asyncio
import atexit
import time
import numpy as np

from artiq.tools import init_logger, atexit_register_coroutine
from artiq.protocols.sync_struct import Subscriber
from artiq.protocols.pc_rpc import AsyncioClient as RPCClient

import pyqtgraph as pg
import pyqtgraph.dockarea as dock
from PyQt5 import QtGui, QtWidgets
from quamash import QEventLoop

from wand.tools import load_config, get_laser_db, WLMMeasurementStatus

# verbosity_args() was renamed to add_common_args() in ARTIQ 5.0; support both.
try:
    from artiq.tools import add_common_args
except ImportError:
    from artiq.tools import verbosity_args as add_common_args


logger = logging.getLogger(__name__)


def get_argparser():
    parser = argparse.ArgumentParser(description="WAnD GUI")
    parser.add_argument("-poll", "--poll-time",
                        default=300,
                        type=float,
                        help="Min time between (s) updates when not in 'fast "
                        "mode' (default: '%(default)s')")
    parser.add_argument("-fpoll", "--poll-time-fast",
                        default=0.01,
                        type=float,
                        help="Min time (s) between updates when in 'fast mode'"
                             " (default: '%(default)s')")
    parser.add_argument("-n", "--name",
                        default="test",
                        help="server name, used to locate configuration file")
    parser.add_argument("-f", "--log-to-file",
                        action="store_true",
                        help="Save log output to file")
    add_common_args(parser)

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
        init_logger(args)

        if args.log_to_file:
            log_file = pkg_resources.resource_filename("wand", "log.txt")
            fh = logging.FileHandler(log_file, mode="wt")
            fh.setLevel(logger.getEffectiveLevel())
            logger.addHandler(fh)
            logging.getLogger("quamash").addHandler(fh)
            sys.excepthook = lambda exc_type, exc_value, exc_traceback: \
                logger.exception("".join(
                    traceback.format_exception(exc_type,
                                               exc_value,
                                               exc_traceback)))

        self.config = load_config(args, "_gui")
        self.laser_db, self.laser_sup_db = get_laser_db(self.config["servers"])
        self.freq_db = {}
        self.osa_db = {}

        self.qapp = QtWidgets.QApplication(["WAnD"])
        self.loop = QEventLoop(self.qapp)
        asyncio.set_event_loop(self.loop)
        atexit.register(self.loop.close)

        # set icon
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

        for laser in self.laser_displays.keys():
            self.laser_sup_db[laser]["measurement_loop_running"] = \
                asyncio.Event()
        self.regular_update_priority = 3
        self.fast_mode_priority = 2

    def notifier_cb(self, mod, db):
        """ Called whenever we get new data from a server "notifier" interface.

        NB sync_struct takes care of updating the relevant db for us, so all we
        do here is call the relevant GUI update function.
        """
        lasers = self.laser_displays.keys()
        if any([laser not in self.freq_db.keys() for laser in lasers]) \
           or any([laser not in self.osa_db.keys() for laser in lasers]):
            return

        if mod["action"] == "init":
            if db == "freq_db" or db == "osa_db":
                for laser, display in self.laser_displays.items():
                    self.laser_sup_db[laser]["measurement_loop_running"].set()
                    display.update_exposure()
                    display.update_reference()
                    display.update_osa_trace()
                    display.update_fast_mode()
                    display.update_auto_exposure()
            return

        elif mod["action"] == "setitem":
            if mod["path"] == []:
                laser = mod["key"]
            else:
                laser = mod["path"][0]

            if laser not in lasers:
                return

            if db == "freq_db":
                self.laser_displays[laser].update_freq()
                self.laser_sup_db[laser]["measurement_loop_running"].set()
            elif db == "osa_db":
                self.laser_displays[laser].update_osa_trace()
                self.laser_sup_db[laser]["measurement_loop_running"].set()
            elif db == "laser_db":
                if mod["key"] == "f_ref":
                    self.laser_displays[laser].update_reference()
                if len(mod["path"]) > 1 and mod["path"][1] == "exposure":
                    self.laser_displays[laser].update_exposure()
                if mod["key"] == "fast_mode":
                    self.laser_displays[laser].update_fast_mode()
                if mod["key"] == "auto_exposure":
                    self.laser_displays[laser].update_auto_exposure()

        else:
            raise ValueError("Unexpected 'notifier' modification")

    async def measurement_loop(self, laser):
        """
        Ask the server for fresh frequency and OSA data whenever we need it
        """

        # connect to server "control" (RPC) interface
        server = self.laser_sup_db[laser]["server"]
        client = RPCClient()
        await client.connect_rpc(self.config["servers"][server]["host"],
                                 self.config["servers"][server]["control"],
                                 target_name="control")
        atexit.register(client.close_rpc)

        display = self.laser_displays[laser]

        loop_running = self.laser_sup_db[laser]["measurement_loop_running"]
        await loop_running.wait()

        while not self.win.exit_request.is_set():

            if display.fast_mode.isChecked():
                poll_time = self.args.poll_time_fast
                priority = self.fast_mode_priority
            else:
                poll_time = self.args.poll_time
                priority = self.regular_update_priority

            data_timestamp = min(self.freq_db[laser]["timestamp"],
                                 self.osa_db[laser]["timestamp"])

            data_expiry = data_timestamp + poll_time
            next_measurement_in = data_expiry - time.time()

            if next_measurement_in >= 0:
                loop_running.clear()
                self.loop.call_later(next_measurement_in, loop_running.set)
                await loop_running.wait()
                continue

            try:
                await client.get_freq(laser=laser,
                                      age=poll_time,
                                      priority=priority,
                                      get_osa_trace=True,
                                      blocking=True,
                                      mute=True)
            except SyntaxError:
                break

            # we'll get new timestamps via the notifier interface eventually,
            # but this gives us something to work with for now...
            self.freq_db[laser]["timestamp"] = time.time()
            self.osa_db[laser]["timestamp"] = time.time()

        client.close_rpc()

    def start(self):

        def connection_lost_cb(server):
            if self.win.exit_request.is_set():
                return
            logger.error("Connection to server '{}' lost, closing".format(
                server))
            self.win.exit_request.set()

        # request frequency/osa updates for our lasers
        for laser in self.laser_displays.keys():
            server = self.laser_sup_db[laser]["server"]
            fut = asyncio.ensure_future(self.measurement_loop(laser))
            fut.add_done_callback(lambda fut: connection_lost_cb(server))
            self.laser_sup_db[laser]["measurement_loop_fut"] = fut

        # connect to server notifier and control interfaces
        def init_cb(db, mod):
            db.update(mod)
            return db

        for server, server_cfg in self.config["servers"].items():
            # ask the servers to keep us updated with changes to laser settings
            # (exposures, references, etc)
            subscriber = Subscriber(
                "laser_db",
                lambda mod: init_cb(self.laser_db, mod),
                lambda mod: self.notifier_cb(mod, "laser_db"),
                disconnect_cb=lambda: connection_lost_cb(server))
            self.loop.run_until_complete(subscriber.connect(
                server_cfg["host"], server_cfg["notify"]))
            atexit_register_coroutine(subscriber.close)

            # ask the servers to keep us updated with the latest frequency data
            subscriber = Subscriber(
                "freq_db",
                lambda mod: init_cb(self.freq_db, mod),
                lambda mod: self.notifier_cb(mod, "freq_db"),
                disconnect_cb=lambda: connection_lost_cb(server))
            self.loop.run_until_complete(subscriber.connect(
                server_cfg["host"], server_cfg["notify"]))
            atexit_register_coroutine(subscriber.close)

        # ask the servers to keep us updated with the latest osa traces
            subscriber = Subscriber(
                "osa_db",
                lambda mod: init_cb(self.osa_db, mod),
                lambda mod: self.notifier_cb(mod, "osa_db"),
                disconnect_cb=lambda: connection_lost_cb(server))
            self.loop.run_until_complete(subscriber.connect(
                server_cfg["host"], server_cfg["notify"]))
            atexit_register_coroutine(subscriber.close)

        # close down all asyncio tasks gracefully
        for laser, display in self.laser_displays.items():
            async def close_display():
                display.cbs_waiting.set()
                await display.cb_fut

            async def close_measurement_loop():
                self.laser_sup_db[laser]["measurement_loop_running"].set()
                await self.laser_sup_db[laser]["measurement_loop_fut"]

            atexit_register_coroutine(close_display)
            atexit_register_coroutine(close_measurement_loop)

        self.win.showMaximized()
        atexit.register(self.win.exit_request.set)
        self.loop.run_until_complete(self.win.exit_request.wait())


class LaserDisplay:
    """ Diagnostics for one laser """

    def __init__(self, display_name, gui):
        self._gui = gui
        self.display_name = display_name
        self.laser = gui.config["display_names"][display_name]

        if gui.laser_db[self.laser]["osa"] == "blue":
            self.colour = "5555ff"
        elif gui.laser_db[self.laser]["osa"] == "red":
            self.colour = "ff5555"
        else:
            self.colour = "7c7c7c"

        self.dock = dock.Dock(self.display_name, autoOrientation=False)
        self.layout = pg.GraphicsLayoutWidget(border=(80, 80, 80))

        # create widgets
        self.detuning = pg.LabelItem("")
        self.detuning.setText("-", color=self.colour, size="64pt")

        self.frequency = pg.LabelItem("")
        self.frequency.setText("-", color="ffffff", size="12pt")

        self.name = pg.LabelItem("")
        self.name.setText(display_name, color=self.colour, size="32pt")

        self.osa = pg.PlotItem()
        self.osa.hideAxis('bottom')
        self.osa.showGrid(y=True)
        self.osa_curve = self.osa.plot(pen='y', color=self.colour)

        self.fast_mode = QtGui.QCheckBox("Fast mode")
        self.auto_exposure = QtGui.QCheckBox("Auto expose")

        self.exposure = [QtGui.QSpinBox() for _ in range(2)]
        for idx in range(2):

            self.exposure[idx].setSuffix(" ms")
            self.exposure[idx].setRange(
                self._gui.laser_sup_db[self.laser]["exp_min"],
                self._gui.laser_sup_db[self.laser]["exp_max"])

        self.f_ref = QtGui.QDoubleSpinBox()
        self.f_ref.setSuffix(" THz")
        self.f_ref.setDecimals(7)
        self.f_ref.setSingleStep(1e-6)
        self.f_ref.setRange(0., 1000.)

        # Zero frequency menu
        self.menu = QtGui.QMenu()
        self.zeroAction = QtGui.QAction("Set reference to current", self.dock)
        self.menu.addAction(self.zeroAction)

        for label in [self.detuning, self.name, self.frequency]:
            label.contextMenuEvent = lambda ev: self.menu.popup(
                QtGui.QCursor.pos())
            label.mouseReleaseEvent = lambda ev: None

        # layout GUI
        self.layout.addItem(self.osa, colspan=2)
        self.layout.nextRow()
        self.layout.addItem(self.detuning, colspan=2)
        self.layout.nextRow()
        self.layout.addItem(self.name)
        self.layout.addItem(self.frequency)

        self.dock.addWidget(self.layout, colspan=6)

        self.dock.addWidget(self.fast_mode, row=1, col=1)
        self.dock.addWidget(self.auto_exposure, row=2, col=1)

        self.dock.addWidget(QtGui.QLabel("Reference"), row=1, col=2)
        self.dock.addWidget(QtGui.QLabel("Exposure 0 (ms)"), row=1, col=3)
        self.dock.addWidget(QtGui.QLabel("Exposure 1 (ms)"), row=1, col=4)

        self.dock.addWidget(self.f_ref, row=2, col=2)
        self.dock.addWidget(self.exposure[0], row=2, col=3)
        self.dock.addWidget(self.exposure[1], row=2, col=4)

        # Sort the layout to make the most of available space
        self.layout.ci.setSpacing(4)
        self.layout.ci.setContentsMargins(2, 2, 2, 2)
        self.dock.layout.setContentsMargins(0, 0, 0, 4)
        for i in [0, 5]:
            self.dock.layout.setColumnMinimumWidth(i, 4)
        for i in [2, 3, 4]:
            self.dock.layout.setColumnStretch(i, 1)

        # connect callbacks
        def connection_lost_cb(future):
            if self._gui.win.exit_request.is_set():
                return
            server = self._gui.laser_sup_db[self.laser]["server"]
            logger.error("Connection to server '{}' lost, closing".format(
                server))
            self._gui.win.exit_request.set()

        self.cb_queue = []
        self.cbs_waiting = asyncio.Event()
        self.cb_fut = asyncio.ensure_future(self.cb_loop())
        self.cb_fut.add_done_callback(connection_lost_cb)

        def add_async_cb(data):
            self.cb_queue.append(data)
            self.cbs_waiting.set()

        def cb_gen(data):
            # fix scoping around lambda generation
            return lambda: add_async_cb(data)

        self.zeroAction.triggered.connect(self.zero_cb)
        self.fast_mode.clicked.connect(cb_gen(("fast_mode",)))
        self.auto_exposure.clicked.connect(cb_gen(("auto_expose",)))
        self.f_ref.valueChanged.connect(cb_gen(("f_ref",)))
        for ccd, exp in enumerate(self.exposure):
            exp.valueChanged.connect(cb_gen(("exposure", ccd)))

        server = self._gui.laser_sup_db[self.laser]["server"]
        server_cfg = self._gui.config["servers"][server]

        self.client = RPCClient()
        self._gui.loop.run_until_complete(self.client.connect_rpc(
                server_cfg["host"],
                server_cfg["control"],
                target_name="control"))
        atexit.register(self.client.close_rpc)

    async def cb_loop(self):
        while not self._gui.win.exit_request.is_set():
            await self.cbs_waiting.wait()

            if self.cb_queue:
                next_cb = self.cb_queue[0]
                del self.cb_queue[0]
                self.cb_queue = list(set(self.cb_queue))

                if next_cb[0] == "fast_mode":
                    await self.fast_mode_cb()
                if next_cb[0] == "auto_expose":
                    await self.auto_expose_cb()
                elif next_cb[0] == "f_ref":
                    await self.f_ref_cb()
                elif next_cb[0] == "exposure":
                    await self.exposure_cb(next_cb[1])

            if not self.cb_queue:
                self.cbs_waiting.clear()

            await asyncio.sleep(0)
        self.client.close_rpc()

    async def fast_mode_cb(self):
        laser = self.laser
        server_fast_mode = self._gui.laser_db[laser]["fast_mode"]

        if server_fast_mode != self.fast_mode.isChecked():
            await self.client.set_fast_mode(laser, self.fast_mode.isChecked())
            self._gui.laser_db[laser]["fast_mode"] = self.fast_mode.isChecked()

        if self.fast_mode.isChecked():
            self._gui.laser_sup_db[laser]["measurement_loop_running"].set()

    async def auto_expose_cb(self):
        laser = self.laser
        server_auto_exp = self._gui.laser_db[laser]["auto_exposure"]
        gui_auto_exp = self.auto_exposure.isChecked()

        if server_auto_exp != gui_auto_exp:
            await self.client.set_auto_exposure(laser, gui_auto_exp)
            self._gui.laser_db[laser]["auto_exposure"] = gui_auto_exp

    def zero_cb(self):
        """Set the current value as reference (zeros the detuning) """
        freq = self._gui.freq_db[self.laser]["freq"]
        if freq < 0:
            return
        self.f_ref.setValue(freq/1e12)

    async def f_ref_cb(self):

        f_ref = self._gui.laser_db[self.laser]["f_ref"]

        if self.f_ref.value()*1e12 == f_ref:
            return

        self._gui.laser_db[self.laser]["f_ref"] = self.f_ref.value()*1e12
        await self.client.set_reference_freq(self.laser,
                                             self.f_ref.value()*1e12)

    async def exposure_cb(self, ccd):

        exp_gui = self.exposure[ccd].value()
        exp_db = self._gui.laser_db[self.laser]["exposure"][ccd]

        if exp_db == exp_gui:
            return

        self._gui.laser_db[self.laser]["exposure"][ccd] = exp_gui
        await self.client.set_exposure(self.laser, self.exposure[ccd].value(),
                                       ccd)

    def update_fast_mode(self):
        server_fast_mode = self._gui.laser_db[self.laser]["fast_mode"]
        self.fast_mode.setChecked(server_fast_mode)

    def update_auto_exposure(self):
        server_auto_exposure = self._gui.laser_db[self.laser]["auto_exposure"]
        self.auto_exposure.setChecked(server_auto_exposure)

    def update_exposure(self):
        for ccd, exp in enumerate(self._gui.laser_db[self.laser]["exposure"]):
            self.exposure[ccd].setValue(exp)

    def update_reference(self):
        self.f_ref.setValue(self._gui.laser_db[self.laser]["f_ref"]/1e12)
        self.update_freq()

    def update_osa_trace(self):
        trace = np.array(self._gui.osa_db[self.laser]["trace"])/32767
        self.osa_curve.setData(trace)

    def update_freq(self):

        freq = self._gui.freq_db[self.laser]["freq"]
        status = self._gui.freq_db[self.laser]["status"]

        if status == WLMMeasurementStatus.OKAY:
            colour = self.colour
            f_ref = self._gui.laser_db[self.laser]["f_ref"]
            if abs(freq - f_ref) > 100e9:
                detuning = "-"
            else:
                detuning = "{:.1f}".format((freq-f_ref)/1e6)
            freq = "{:.7f} THz".format(freq/1e12)
        elif status == WLMMeasurementStatus.UNDER_EXPOSED:
            freq = "-"
            detuning = "Low"
            colour = "ff9900"
        elif status == WLMMeasurementStatus.OVER_EXPOSED:
            freq = "-"
            detuning = "High"
            colour = "ff9900"
        else:
            freq = "-"
            detuning = "Error"
            colour = "ff9900"

        self.frequency.setText(freq)
        self.detuning.setText(detuning, color=colour)


def main():
    gui = WandGUI()
    gui.start()


if __name__ == "__main__":
    main()
