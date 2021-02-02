import importlib.machinery
from sipyco.sync_struct import Notifier, update_from_dict
import logging

logger = logging.getLogger(__name__)

class driver_importer:
    
    def __init__(self, config):  
        self.config = config
        
    def get_driver(self, device_name, simulation):
        device_mgr = DeviceManager(DeviceDB(self.config), simulation)
        device =  get_device(device_name, device_mgr)
        return device
   
class DeviceManager:
    """Handles creation and destruction of local device drivers and controller
    RPC clients."""
    def __init__(self, ddb, simulation):
        self.ddb = ddb
        self.active_devices = []
        self.simulation = simulation

    def get_device_db(self):
        """Returns the full contents of the device database."""
        return self.ddb.get_device_db()

    def get_desc(self, name):
        return self.ddb.get(name, resolve_alias=True)

    def get(self, name):
        """Get the device driver or controller client corresponding to a
        device database entry."""

        try:
            desc = self.get_desc(name)
        except Exception as e:
            raise DeviceError("Failed to get description of device '{}'"
                              .format(name)) from e

        for existing_desc, existing_dev in self.active_devices:
            if desc == existing_desc:
                return existing_dev

        try:
            dev = _create_device(desc, self, self.simulation)
        except Exception as e:
            raise DeviceError("Failed to create device '{}'"
                              .format(name)) from e
        self.active_devices.append((desc, dev))
        return dev

    def close_devices(self):
        """Closes all active devices, in the opposite order as they were
        requested."""
        for _desc, dev in reversed(self.active_devices):
            try:
                 if hasattr(dev, "close"):
                     dev.close()
            except Exception as e:
                logger.warning("Exception %r when closing device %r", e, dev)
        self.active_devices.clear()

class DeviceDB:
    def __init__(self, backing_file):
        self.backing_file = backing_file
        self.data = Notifier(self.backing_file)

    def scan(self):
        update_from_dict(self.data,
            self.backing_file)

    def get_device_db(self):
        return self.data.raw_view

    def get(self, key, resolve_alias=False):
        desc = self.data.raw_view[key]
        if resolve_alias:
            while isinstance(desc, str):
                desc = self.data.raw_view[desc]
        return desc

class DeviceError(Exception):
    pass

def _create_device(desc, device_mgr, simulation):
    arg = desc.get("arguments",{})
    if simulation:
        arg["simulation"] = simulation

    module = importlib.import_module(desc["module"])
    device_class = getattr(module, desc["class"])
    return device_class(**arg)

def get_device(key, __device_mgr):
        """Creates and returns a device driver."""
        return __device_mgr.get(key)
