"""
Logging level initialization for TWCManager.

Defines custom logging levels used throughout the application.
"""

import logging


def initialize_logging_levels():
    """Initialize custom logging levels."""
    logging.addLevelName(19, "INFO2")
    logging.addLevelName(18, "INFO3")
    logging.addLevelName(17, "INFO4")
    logging.addLevelName(16, "INFO5")
    logging.addLevelName(15, "INFO6")
    logging.addLevelName(14, "INFO7")
    logging.addLevelName(13, "INFO8")
    logging.addLevelName(12, "INFO9")
    logging.addLevelName(9, "DEBUG2")
    logging.INFO2 = 19
    logging.INFO3 = 18
    logging.INFO4 = 17
    logging.INFO5 = 16
    logging.INFO6 = 15
    logging.INFO7 = 14
    logging.INFO8 = 13
    logging.INFO9 = 12
    logging.DEBUG2 = 9
