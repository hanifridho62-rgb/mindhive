# SPDX-License-Identifier: Apache-2.0
# build/app_entry.py
"""PyInstaller entrypoint: launch the MIND HIVE web dashboard."""
from mindhive import dashboard

if __name__ == "__main__":
    dashboard.serve()
