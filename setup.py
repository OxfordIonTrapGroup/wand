#!/usr/bin/env python3
from setuptools import setup, find_packages


console_scripts = [
    "wand_server = wand.frontend.wand_server:main",
    "wand_gui = wand.frontend.wand_gui:main",
    "wand_influx_db = wand.frontend.wand_influx_db:main",
]

requirements = [
    # Explicitly depend on setuptools to bring back the deprecated pkg_resources
    # before it is removed entirely (at which point we need a new solution).
    "setuptools",
]

setup(
    name="wand",
    version="0.1",
    description="Wavelength Analysis 'Nd Display",
    packages=find_packages(),
    include_package_data=True,
    install_requires=requirements,
    entry_points={
        "console_scripts": console_scripts,
    }
)
