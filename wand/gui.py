import asyncio
import logging
import numpy as np
import time
import functools

import pyqtgraph as pg
import pyqtgraph.dockarea as dock
from PyQt5 import QtWidgets

from sipyco.pc_rpc import AsyncioClient as RPCClient

from wand.tools import WLMMeasurementStatus


logger = logging.getLogger(__name__)

REGULAR_UPDATE_PRIORITY = 3
FAST_MODE_PRIORITY = 2


class LaserDisplay:
    """ Diagnostics for one laser """

    def __init__(self, display_name, gui):
        self.connected = False
        self._gui = gui
        self.display_name = display_name
        self.laser = gui.config["display_names"][display_name]
        self.server = ""

        self.wake_loop = asyncio.Event()

        self.dock = dock.Dock(self.display_name, autoOrientation=False)
        self.layout = pg.GraphicsLayoutWidget(border=(80, 80, 80))

        # create widgets
        self.colour = "#FFFFFF"  # will be set later depending on laser colour

        self.detuning = pg.LabelItem("")
        self.detuning.setText("-", color="#FFFFFF", size="64pt")

        self.frequency = pg.LabelItem("")
        self.frequency.setText("-", color="#FFFFFF", size="12pt")

        self.name = pg.LabelItem("")
        self.name.setText(display_name, color="#FFFFFF", size="32pt")

        self.osa = pg.PlotItem()
        self.osa.hideAxis('bottom')
        self.osa.showGrid(y=True)
        self.osa_curve = self.osa.plot(pen='y', color=self.colour)

        self.fast_mode = QtWidgets.QCheckBox("Fast mode")
        self.auto_exposure = QtWidgets.QCheckBox("Auto expose")

        self.exposure = [QtWidgets.QSpinBox() for _ in range(2)]
        for idx in range(2):
            self.exposure[idx].setSuffix(" ms")
            self.exposure[idx].setRange(0, 0)

        self.laser_status = QtWidgets.QLineEdit()
        self.laser_status.setReadOnly(True)

        self.f_ref = QtWidgets.QDoubleSpinBox()
        self.f_ref.setSuffix(" THz")
        self.f_ref.setDecimals(7)
        self.f_ref.setSingleStep(1e-6)
        self.f_ref.setRange(0., 1000.)

        # context menu
        self.menu = QtWidgets.QMenu()
        self.ref_editable = QtWidgets.QAction("Enable reference changes",
                                          self.dock)
        self.ref_editable.setCheckable(True)
        self.menu.addAction(self.ref_editable)

        for label in [self.detuning, self.name, self.frequency, self.f_ref]:
            label.contextMenuEvent = lambda ev: self.menu.popup(
                QtWidgets.QCursor.pos())
            label.mouseReleaseEvent = lambda ev: None

        # layout GUI
        self.layout.addItem(self.osa, colspan=2)
        self.layout.nextRow()
        self.layout.addItem(self.detuning, colspan=2)
        self.layout.nextRow()
        self.layout.addItem(self.name)
        self.layout.addItem(self.frequency)

        self.dock.addWidget(self.layout, colspan=7)

        self.dock.addWidget(self.fast_mode, row=1, col=1)
        self.dock.addWidget(self.auto_exposure, row=2, col=1)

        self.dock.addWidget(QtWidgets.QLabel("Reference"), row=1, col=2)
        self.dock.addWidget(QtWidgets.QLabel("Exp 0"), row=1, col=3)
        self.dock.addWidget(QtWidgets.QLabel("Exp 1"), row=1, col=4)
        self.dock.addWidget(QtWidgets.QLabel("Status"), row=1, col=5)

        self.dock.addWidget(self.f_ref, row=2, col=2)
        self.dock.addWidget(self.exposure[0], row=2, col=3)
        self.dock.addWidget(self.exposure[1], row=2, col=4)
        self.dock.addWidget(self.laser_status, row=2, col=5)

        # Sort the layout to make the most of available space
        self.layout.ci.setSpacing(4)
        self.layout.ci.setContentsMargins(2, 2, 2, 2)
        self.dock.layout.setContentsMargins(0, 0, 0, 4)
        for i in [0, 6]:
            self.dock.layout.setColumnMinimumWidth(i, 4)
        for i in [2, 5]:
            self.dock.layout.setColumnStretch(i, 2)
        for i in [1, 3, 4]:
            self.dock.layout.setColumnStretch(i, 1)

        self.cb_queue = []

        def add_async_cb(data):
            self.cb_queue.append(data)
            self.wake_loop.set()

        self.ref_editable.triggered.connect(self.ref_editable_cb)
        self.fast_mode.clicked.connect(functools.partial(add_async_cb,
                                                         ("fast_mode",)))
        self.auto_exposure.clicked.connect(functools.partial(add_async_cb,
                                                             ("auto_expose",)))
        self.f_ref.valueChanged.connect(functools.partial(add_async_cb,
                                                          ("f_ref",)))
        for ccd, exp in enumerate(self.exposure):
            exp.valueChanged.connect(functools.partial(add_async_cb,
                                                       ("exposure", ccd)))

        self.fut = asyncio.ensure_future(self.loop())
        self._gui.loop.run_until_complete(self.setConnected(False))

    async def setConnected(self, connected):
        """ Enable/disable controls on the gui dependent on whether or not
        the server is connected.
        """
        if not connected:
            self.ref_editable.blockSignals(True)
            self.fast_mode.blockSignals(True)
            self.auto_exposure.blockSignals(True)
            self.f_ref.blockSignals(True)

            self.ref_editable.setEnabled(False)
            self.fast_mode.setEnabled(False)
            self.auto_exposure.setEnabled(False)
            self.f_ref.setEnabled(False)

            for exp in self.exposure:
                exp.blockSignals(True)
                exp.setEnabled(False)

            self.laser_status.setText("no connection")
            self.laser_status.setStyleSheet("color: red")

            self.server = ""
            self.client = None
            self.wake_loop.set()

        else:
            self.colour = self._gui.laser_db[self.laser].get("display_colour",
                                                             "#7C7C7C")
            if self.colour == "red":
                self.colour = "#FF5555"
            elif self.colour == "blue":
                self.colour = "#5555FF"

            self.name.setText(self.display_name,
                              color=self.colour,
                              size="32pt")

            try:
                exp_min = await self.client.get_min_exposures()
                exp_max = await self.client.get_max_exposures()
                num_ccds = await self.client.get_num_wlm_ccds()
                polls = await self.client.get_poll_times()
                (self.poll_time, self.fast_poll_time) = polls
            except (OSError, AttributeError):
                self.setConnected(False)
                return

            for ccd, exposure in enumerate(self.exposure[:num_ccds]):
                exposure.setRange(exp_min[ccd], exp_max[ccd])
                exposure.setValue(
                    self._gui.laser_db[self.laser]["exposure"][ccd])

            # sync GUI with server
            self.update_fast_mode()
            self.update_auto_exposure()
            self.update_reference()
            self.update_exposure()
            self.update_laser_status()
            self.update_osa_trace()

            # re-enable GUI controls
            self.ref_editable.setEnabled(True)
            self.fast_mode.setEnabled(True)
            self.auto_exposure.setEnabled(True)
            self.ref_editable_cb()

            self.ref_editable.blockSignals(False)
            self.fast_mode.blockSignals(False)
            self.auto_exposure.blockSignals(False)
            self.f_ref.blockSignals(False)

            for exp in self.exposure[:num_ccds]:
                exp.setEnabled(True)
                exp.blockSignals(False)

        self.connected = connected

    async def loop(self):
        """ Update task for this laser display.

        Runs as long as the parent window's exit request is not set.

        The loop sleeps until it has something to do. It wakes up when (a) the
        wake_loop event is set (b) a measurement is due.
        """
        laser = self.laser

        while not self._gui.win.exit_request.is_set():
            self.wake_loop.clear()

            try:
                self.client.close_rpc()
            except Exception:
                pass

            if not self.server:
                await self.setConnected(False)
                self.wake_loop.clear()
                await self.wake_loop.wait()
                continue

            try:
                server_cfg = self._gui.config["servers"][self.server]
                self.client = RPCClient()
                await self.client.connect_rpc(server_cfg["host"],
                                              server_cfg["control"],
                                              target_name="control")
                await self.setConnected(True)
            # to do: catch specific exceptions
            except Exception:
                logger.exception("Error connecting to server '{}'".format(self.server))
                continue

            while not self._gui.win.exit_request.is_set() and self.server:
                self.wake_loop.clear()

                try:
                    # process any callbacks
                    while self.cb_queue:
                        next_cb = self.cb_queue[0]
                        if next_cb[0] == "fast_mode":
                            await self.fast_mode_cb()
                        if next_cb[0] == "auto_expose":
                            await self.auto_expose_cb()
                        elif next_cb[0] == "f_ref":
                            await self.f_ref_cb()
                        elif next_cb[0] == "exposure":
                            await self.exposure_cb(next_cb[1])
                        del self.cb_queue[0]

                    # ask for new data
                    if self.fast_mode.isChecked():
                        poll_time = self.fast_poll_time
                        priority = FAST_MODE_PRIORITY
                    else:
                        poll_time = self.poll_time
                        priority = REGULAR_UPDATE_PRIORITY

                    data_timestamp = min(self._gui.freq_db[laser]["timestamp"],
                                         self._gui.osa_db[laser]["timestamp"])

                    data_expiry = data_timestamp + poll_time
                    next_measurement_in = data_expiry - time.time()
                    if next_measurement_in <= 0:
                        try:
                            await self.client.get_freq(
                                laser=laser, age=poll_time, priority=priority,
                                get_osa_trace=True, blocking=True, mute=True)
                            next_measurement_in = poll_time
                        except (OSError, AttributeError):
                            self.server = ""
                            continue

                except Exception:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    await asyncio.wait_for(self.wake_loop.wait(),
                                           next_measurement_in)
                except asyncio.TimeoutError:
                    pass

    async def fast_mode_cb(self):
        try:
            await self.client.set_fast_mode(self.laser,
                                            self.fast_mode.isChecked())
        except (OSError, AttributeError):
            self.setConnected(False)

    async def auto_expose_cb(self):
        try:
            await self.client.set_auto_exposure(self.laser,
                                                self.auto_exposure.isChecked())
        except (OSError, AttributeError):
            self.setConnected(False)

    def ref_editable_cb(self):
        """ Enable/disable editing of the frequency reference """
        if not self.ref_editable.isChecked():
            self.f_ref.setEnabled(False)
        else:
            self.f_ref.setEnabled(True)

    async def f_ref_cb(self):
        try:
            await self.client.set_reference_freq(self.laser,
                                                 self.f_ref.value() * 1e12)
        except (OSError, AttributeError):
            self.setConnected(False)

    async def exposure_cb(self, ccd):
        try:
            await self.client.set_exposure(self.laser,
                                           self.exposure[ccd].value(),
                                           ccd)
        except (OSError, AttributeError):
            self.setConnected(False)

    def update_fast_mode(self):
        server_fast_mode = self._gui.laser_db[self.laser]["fast_mode"]
        self.fast_mode.blockSignals(True)
        self.fast_mode.setChecked(server_fast_mode)
        if self.connected:
            self.fast_mode.blockSignals(False)

    def update_auto_exposure(self):
        server_auto_exposure = self._gui.laser_db[self.laser]["auto_exposure"]
        self.auto_exposure.blockSignals(True)
        self.auto_exposure.setChecked(server_auto_exposure)
        if self.connected:
            self.auto_exposure.blockSignals(False)

    def update_exposure(self):
        for ccd, exp in enumerate(self._gui.laser_db[self.laser]["exposure"]):
            self.exposure[ccd].blockSignals(True)
            self.exposure[ccd].setValue(exp)
            if self.connected:
                self.exposure[ccd].blockSignals(False)

    def update_reference(self):
        f_ref = self._gui.laser_db[self.laser]["f_ref"] / 1e12
        self.f_ref.blockSignals(True)
        self.f_ref.setValue(f_ref)
        if self.connected:
            self.f_ref.blockSignals(False)
        self.update_freq()

    def update_osa_trace(self):
        self.wake_loop.set()  # recalculate when next measurement due

        if self._gui.osa_db[self.laser].get("trace") is None:
            return

        trace = np.array(self._gui.osa_db[self.laser]["trace"]) / 32767
        self.osa_curve.setData(trace)

    def update_freq(self):

        freq = self._gui.freq_db[self.laser]["freq"]
        status = self._gui.freq_db[self.laser]["status"]

        if status == WLMMeasurementStatus.OKAY:
            colour = self.colour
            f_ref = self._gui.laser_db[self.laser]["f_ref"]

            # this happens if the server hasn't taken a measurement yet
            if freq is None:
                return

            if abs(freq - f_ref) > 100e9:
                detuning = "-"
            else:
                detuning = "{:.1f}".format((freq - f_ref) / 1e6)
            freq = "{:.7f} THz".format(freq / 1e12)
        elif status == WLMMeasurementStatus.UNDER_EXPOSED:
            freq = "-"
            detuning = "Low"
            colour = "#FF9900"
        elif status == WLMMeasurementStatus.OVER_EXPOSED:
            freq = "-"
            detuning = "High"
            colour = "#FF9900"
        else:
            freq = "-"
            detuning = "Error"
            colour = "#FF9900"

        self.frequency.setText(freq, color=colour)
        self.detuning.setText(detuning, color=colour)

        self.wake_loop.set()  # recalculate when next measurement due

    def update_laser_status(self):
        locked = self._gui.laser_db[self.laser].get("locked", False)
        owner = self._gui.laser_db[self.laser].get("lock_owner", "")

        if not locked:
            self.laser_status.setText("unlocked")
            self.laser_status.setStyleSheet("color: grey")
        elif owner:
            self.laser_status.setText("locked: {}".format(owner))
            self.laser_status.setStyleSheet("color: red")
        else:
            self.laser_status.setText("locked")
            self.laser_status.setStyleSheet("color: orange")
