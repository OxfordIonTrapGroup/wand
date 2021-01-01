from enum import IntEnum
import os
import datetime
import asyncio
import shutil
import logging
import sys
from pathlib import Path

from sipyco import pyon

logger = logging.getLogger(__name__)


class WLMMeasurementStatus(IntEnum):
    OKAY = 0,
    UNDER_EXPOSED = 1,
    OVER_EXPOSED = 2,
    ERROR = 3


class LaserOwnedException(Exception):
    pass


class LockException(Exception):
    pass


def get_config_path(args, name_suffix=""):
    config_file = "{}{}_config.pyon".format(args.name, name_suffix)

    home = str(Path.home())
    if sys.platform == "win32":
        data_dir = os.path.join(home, "AppData", "Local", "wand")
    elif sys.platform == "linux":
        data_dir = os.path.join(home, ".local", "share", "wand")
    elif sys.platform == "darwin":
        data_dir = os.path.join(home, "Library", "Preferences", "wand")
    else:
        raise Exception("Unsupported platform")

    if not os.path.exists(data_dir):
        logger.info("Data directory does not exist, creating at {}".format(
            data_dir))
        os.makedirs(data_dir)
    config_path = os.path.join(data_dir, config_file)

    if args.backup_dir == "":
        backup_path = ""
    else:
        backup_path = os.path.join(args.backup_dir, config_file)
        backup_path = os.path.expanduser(backup_path)
    return config_path, backup_path


def load_config(args, name_suffix=""):
    """ load configuration file, restoring from backup if necessary cf
    config_args
    """
    config_path, backup_path = get_config_path(args, name_suffix)
    try:
        config = pyon.load_file(config_path)
    except FileNotFoundError:
        logger.warning("Unable to find server configuration file, "
                       "restoring from backup")
        shutil.copyfile(backup_path, config_path)
        config = pyon.load_file(config_path)
    return config


def backup_config(args, name_suffix=""):
    """" Backup server configuration file cf config_args"""
    config_path, backup_path = get_config_path(args, name_suffix)

    try:
        shutil.copyfile(config_path, backup_path)
    except IOError:
        logger.warning("Unable to backup configuration file")
    else:
        logger.info("Config backed up")


async def regular_config_backup(args, name_suffix=""):
    """ asyncio task to backup the server configuration file every day at
    4 am cf backup_config
    """
    while True:
        next_backup = datetime.datetime.now() + datetime.timedelta(days=1)
        next_backup = next_backup.replace(
            day=next_backup.day, hour=4, minute=0, second=0, microsecond=0)
        await asyncio.sleep(
            (next_backup - datetime.datetime.now()).total_seconds())
        backup_config(args)
