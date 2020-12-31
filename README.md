# WAnD Super-Duper Laser Diagnostics

Wavelength Analysis and Display laser diagnostics suite.

![WAnD GUI](docs/wand_gui.png)

A WAnD server (an instance of `wand_server`) controls: a wavelength meter; a fibre switch; optionally, a set of optical spectrum analysers (OSAs); and, optionally, lasers (for frequency locks). It provides network interfaces that clients can connect to in order to control the server (e.g. schedule wavelength readings, change exposure times, etc) and to receive updates (parameter changes, new frequency data).

WAnD clients, such as the GUI or an ARTIQ experiment, can connect to multiple servers (and, each server can support connections from multiple clients).

![WAnD servers and clients](docs/servers_and_clients.svg)

To allow multiple clients to simultaneously request frequency data, potentially from different lasers (using a fibre switch) the server works on a priority queue basis. Frequency requests are queued and dealt with in order of the requested priority and the request time.

Since many applications can tolerate data that is slightly old, the server stores the most recent measurement for each laser. Each frequency request specifies a maximum data age (how long ago the data was taken). The server completes a measurement request from a client as soon as it has data that is younger than the requested age. This also means, for example, that a low-priority measurement can be completed early if a high-priority measurement request for the same laser comes along first.

## Installation

From inside Oxford, this is installed as part of the `artiq_env` Conda package.

From outside Oxford, use Pip to install into an [ARTIQ](https://github.com/m-labs/artiq) Conda environment. You may also need to Pip install a couple of extra packages like `pydaqmx`.

## WAnD servers

To start a server run `wand_server -n <name> -b <backup_dir>` where `<name>` is the name of the server and `<backup_dir>` is the location of a directory that contains backups of the configuration data.

The name parameter is used to locate the configuration file for the server (named `<name>_server_config.pyon`). The first time a server runs on a given machine this file is copied from the backup directory into a local directory. Subsequently, `wand_server` will periodically attempt to backup the local configuration file in the backup directory. You can find an example configuration file in the [examples](wand/examples).

The server can be run in "simulation" mode without hardware access by using the `--simulation` command line argument (use `--help` for a complete list of arguments).

If you get `connection refused` errors when a client tries to connect to the server remember to make sure to add a suitable bind argument (e.g. `--bind=*`).

The default server port is `3251`. To control the server (e.g. to lock a laser or configure a lock gain), or list the methods provided by the server, use `artiq_rpctool`.

## OSAs

WAnD currently supports two forms of OSA: the interferometer pattern from wavelength meter; or, one or mote external etalons connected to a NI DAQ card. If no OSA is defined in the configuration file, we use the wlm interferometer data.

## WAnD GUI

To start a GUI client, run `wand_gui -n <name> -b <backup_dir>` where `<name>` is the name of the client. The client configuration file specifies the layout of the GUI and which servers to connect to.

## Notes

- The wavemeter peak height on CCD 2 (the coarse grating) is affected by the exposure time on CCD 1 (the fine grating)
- If you see the wavemeter reading jumping between underexposed and overexposed then your laser power is probably too high. What's happening is that turning the CCD 1 exposure time up enough to get a good reading is causing the coarse CCD 2 to overexpose. NB the peak heights are strongly non-linear in the exposure times, so it can be non-obvious when this is happening...
