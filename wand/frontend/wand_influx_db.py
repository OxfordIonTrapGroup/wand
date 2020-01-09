""" InfluxDB logger for WAnD servers """
import logging
import argparse
import time

import influxdb

from sipyco.pc_rpc import Client as RPCClient

# verbosity_args() was renamed to add_common_args() in ARTIQ 5.0
from sipyco.common_args import verbosity_args, init_logger_from_args

from wand.tools import WLMMeasurementStatus

logger = logging.getLogger(__name__)


def get_argparser():
    parser = argparse.ArgumentParser(description="WAnD laser locker")
    verbosity_args(parser)
    parser.add_argument("-s", "--server",
                        action="append",
                        help="Add a WAnD server by IP address")
    parser.add_argument("-poll", "--poll-time",
                        help="time between log updates (s) (default: "
                             "'%(default)s')",
                        type=int,
                        default=300)
    parser.add_argument("-db", "--database",
                        help="influxdb database to log to '%(default)s')",
                        default="lasers")
    return parser


def main():

    args = get_argparser().parse_args()
    init_logger_from_args(args)

    servers = {
        idx: {
            "host": ip,
            "notify": 3250,
            "control": 3251
        }
        for idx, ip in enumerate(args.server)}

    while True:

        measurements = []

        for _, server in servers.items():
            try:
                client = RPCClient(server["host"], server["control"])
                lasers = client.get_laser_db()
                for laser in lasers:
                    meas = client.get_freq(laser,
                                           age=args.poll_time,
                                           priority=3,
                                           get_osa_trace=False,
                                           blocking=True,
                                           mute=False,
                                           offset_mode=False)
                    status, freq, _ = meas

                    if status != WLMMeasurementStatus.OKAY:
                        logger.info("{}: measurement error")
                        continue

                    f_ref = lasers[laser]["f_ref"]
                    delta = freq - lasers[laser]["f_ref"]
                    measurements.append({
                        "measurement": laser,
                        "fields": {
                            "freq": freq,
                            "f_ref": f_ref,
                            "detuning": delta
                        }
                    })
                    logger.info("{}: freq {} THz, f_ref {} THz, "
                                "detuning {} MHz".format(laser,
                                                         freq,
                                                         f_ref,
                                                         delta))
            except OSError:
                logger.warning("Error querying server {}".format(server))
            finally:
                client.close_rpc()

        if measurements == []:
            time.sleep(args.poll_time)
            continue

        try:
            influx = influxdb.InfluxDBClient(
                host="10.255.6.4",
                database=args.database,
                username="admin",
                password="admin")

            influx.write_points(measurements)
        finally:
            influx.close()
        time.sleep(args.poll_time)


if __name__ == "__main__":
    main()
