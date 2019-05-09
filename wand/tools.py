from enum import IntEnum
import os
import datetime
import asyncio
import shutil
import logging

from artiq.protocols import pyon
import wand

logger = logging.getLogger(__name__)


class WLMMeasurementStatus(IntEnum):
    OKAY = 0,
    UNDER_EXPOSED = 1,
    OVER_EXPOSED = 2,
    ERROR = 3


class LaserOwnedException(Exception):
    pass


def get_config_path(args, name_suffix=""):
    config_file = "{}{}_config.pyon".format(args.name, name_suffix)
    wand_dir = os.path.dirname(wand.__file__)
    config_path = os.path.join(wand_dir, config_file)
    if os.name == "nt":
        backup_path = os.path.join("z:\\", "wand", config_file)
    else:
        backup_path = os.path.join("~", "steaneShared", "wand", config_file)
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
        next_backup = datetime.datetime.now()
        next_backup = next_backup.replace(
            day=next_backup.day+1, hour=4, minute=0, second=0, microsecond=0)
        await asyncio.sleep(
            (next_backup - datetime.datetime.now()).total_seconds())
        backup_config(args)
