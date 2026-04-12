# LoggerFactory module. Provides centralized logger creation with configurable formatting.
import logging


class LoggerFactory:
    """
    Centralized factory for creating loggers with configurable icon/category prefixes.
    Supports both emoji icons (default) and text category labels based on configuration.
    """

    master = None

    # Icon mappings for different module categories
    ICON_MAP = {
        "EMS": "⚡",
        "Status": "📊",
        "Vehicle": "🚗",
        "Control": "🎮",
        "Interface": "🔌",
        "Policy": "⛽",
        "Master": "⛽",
        "Manager": "⛽",
        "Slave": "⛽",
        "Protocol": "📡",
    }

    # Text category mappings for when icons are disabled
    CATEGORY_MAP = {
        "EMS": "[EMS]",
        "Status": "[Status]",
        "Vehicle": "[Vehicle]",
        "Control": "[Control]",
        "Interface": "[Interface]",
        "Policy": "[Policy]",
        "Master": "[Master]",
        "Manager": "[Manager]",
        "Slave": "[Slave]",
        "Protocol": "[Protocol]",
    }

    @staticmethod
    def set_master(master):
        """Initialize the factory with the master configuration object."""
        LoggerFactory.master = master

    @staticmethod
    def get_logger(module_name, category):
        """
        Get a logger with the specified module name and category.

        Args:
            module_name: The name of the module (e.g., "HASS", "TeslaBLE")
            category: The category of the module (e.g., "EMS", "Status", "Vehicle")

        Returns:
            A configured logger instance
        """
        if LoggerFactory.master is None:
            # Fallback if master not initialized - use icons by default
            use_icons = True
        else:
            use_icons = (
                LoggerFactory.master.config.get("logging", {}).get("use_icons", True)
            )

        # Get the appropriate prefix based on configuration
        if use_icons:
            prefix = LoggerFactory.ICON_MAP.get(category, f"[{category}]")
        else:
            prefix = LoggerFactory.CATEGORY_MAP.get(category, f"[{category}]")

        logger_name = f"{prefix} {module_name}"
        return logging.getLogger(logger_name)
