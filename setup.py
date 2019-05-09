#!/usr/bin/env python3
from setuptools import setup, find_packages


console_scripts = [
    "wand_server = wand.frontend.wand_server:main",
    "wand_gui = wand.frontend.wand_gui:main",
    "wand_locker = wand.frontend.wand_locker:main",
    "wand_influx_db = wand.frontend.wand_influx_db:main",
]

setup(
    name="wand",
    version="0.1",
    description="Wavelength Analysis 'Nd Display",
    packages=find_packages(),
    include_package_data=True,
    entry_points={
        "console_scripts": console_scripts,
    }
)
