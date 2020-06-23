""" InfluxDB logger for WAnD servers """
import logging
import argparse
import time

import influxdb
from sipyco.pc_rpc import Client as RPCClient
from sipyco.common_args import verbosity_args, init_logger_from_args

from wand.tools import WLMMeasurementStatus

logger = logging.getLogger(__name__)


def get_argparser():
    parser = argparse.ArgumentParser(description="WAnD InfluxDB logger")
    verbosity_args(parser)
    parser.add_argument("-s",
                        "--server",
                        action="append",
                        help="Add a WAnD server by IP address")
    parser.add_argument("-poll",
                        "--poll-time",
                        help="time between log updates (s) (default: '%(default)s')",
                        type=int,
                        default=300)
    influx = parser.add_argument_group("InfluxDB")
    influx.add_argument("-db",
                        "--database",
                        help="influxdb database to log to (default: '%(default)s')",
                        default="lasers")
    influx.add_argument("--host-db",
                        help="InfluxDB host name (default: '%(default)s')",
                        default="10.255.6.4")
    influx.add_argument("--user-db",
                        help="InfluxDB username (default: '%(default)s')",
                        default="admin")
    influx.add_argument("--password-db",
                        help="InfluxDB password (default: '%(default)s')",
                        default="admin")
    parser.add_argument("--timeout",
                        help=("timeout for RPC connection to servers, in seconds " +
                              "(default: %(default)s s)"),
                        type=float,
                        default=5.0)
    return parser


def main():
    args = get_argparser().parse_args()
    init_logger_from_args(args)

    next_poll_time = time.monotonic()
    while True:
        time.sleep(max(0, next_poll_time - time.monotonic()))
        next_poll_time += args.poll_time

        measurements = []
        for server in args.server:
            try:
                client = RPCClient(server, 3251, timeout=args.timeout)
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
                                "detuning {} MHz".format(laser, freq, f_ref, delta))
            except OSError:
                logger.warning("Error querying server {}".format(server))
            finally:
                client.close_rpc()
        if not measurements:
            continue

        try:
            influx = influxdb.InfluxDBClient(host=args.host_db,
                                             database=args.database,
                                             username=args.user_db,
                                             password=args.password_db)

            influx.write_points(measurements)
        finally:
            influx.close()


if __name__ == "__main__":
    main()
