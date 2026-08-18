#!/usr/bin/env python3
"""
TWCManager main entry point.

This module is executed when running: python -m TWCManager
"""

import os
import grp
import pwd
import sys

# If we are being run as root, drop privileges to twcmanager user
# This avoids any potential permissions issues if it is run as root and settings.json is created as root
if os.getuid() == 0:
    user = "twcmanager"
    groups = [g.gr_gid for g in grp.getgrall() if user in g.gr_mem]

    _, _, uid, gid, gecos, root, shell = pwd.getpwnam(user)
    groups.append(gid)
    os.setgroups(groups)
    os.setgid(gid)
    os.setuid(uid)

# Import and run the main TWCManager module
from TWCManager.TWCManager import *  # noqa: F401, F403
