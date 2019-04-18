import os
import datetime
import asyncio
import shutil
import logging
from socket import timeout as TimeoutError

from artiq.protocols.pc_rpc import Client as RPCClient
from artiq.protocols import pyon
import wand

logger = logging.getLogger(__name__)


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


def get_laser_db(server_db):
    """ Queries all servers in server_db for their laser_db, returning the
    result as a single, unified laser_db. Additionally, returns laser_sup_db,
    containing useful meta-data, such as mappings between lasers and the
    server they reside on.

    :param server_db: dictionary of servers
    :returns: (laser_db, laser_sup_db)
    """
    laser_db = {}
    laser_sup_db = {}

    for server_name, server_cfg in server_db.items():
        try:
            client = RPCClient(server_cfg["host"],
                               server_cfg["control"],
                               timeout=1)
            server_lasers = client.get_laser_db()
            exp_min = client.get_min_exposure()
            exp_max = client.get_max_exposure()
            client.close_rpc()
        except TimeoutError:
            raise Exception("Unable to connect to server '{}'".format(
                server_name))

        for laser_name, laser in server_lasers.items():
            if laser_name in laser_db.keys():
                raise ValueError("Duplicate definitions of laser '{}'"
                                 " found".format(laser_name))
            laser_sup_db.update({laser_name: {
                "server": server_name,
                "exp_min": exp_min,
                "exp_max": exp_max
                }})
        laser_db.update(server_lasers)

    return laser_db, laser_sup_db
