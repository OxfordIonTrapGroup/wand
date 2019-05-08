# WaND Super-Duper Laser Diagnostics

See the [documentation](wand\introduction.md) for an overview of WaND.

## Installation

From inside Oxford, this is installed as part of the `artiq_env` Conda package.

From outside Oxford, use Pip to install this into an [ARTIQ](https://github.com/m-labs/artiq) Conda environment. You may also need to Pip install a couple of extra packages like `pydaqmx`.

## Starting a server

To start a server run `wand_server -n <name>` where `<name>` is the name of the server.

The name parameter is used to locate the configuration file for the server (named `<name>_server_config.pyon`), which should reside in the root WaND directory. If the configuration file isn't found there, `wand_server` will attempt to copy it from the Oxford shared area (to do: make this less Oxford-specific!). If you're running WaND outside Oxford, you need to manually copy the config file to the root wand directory. You can find an example configuration file in the [examples](wand\examples).

The server can be run in "simulation" mode without hardware access by using the `--simulation` command line argument (use `--help` for a complete list of arguments).

If you get `connection refused` errors when a client tries to connect to the server remember to make sure to add a suitable bind argument (e.g. `--bind=*`).


## Starting a GUI client

To start a GUI client, run `wand_gui -n <name>` where `<name>` is the name of the client. The client configuration file specifies the layout of the GUI and which servers to connect to.
