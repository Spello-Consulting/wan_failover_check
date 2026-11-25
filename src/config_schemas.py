"""Configuration schemas for use with the SCConfigManager class."""

from typing import Any


class ConfigSchema:
    """Cerberus validation schema for the app-specific configuration sections.

    The standard Files, Email, HeartbeatMonitor and Location sections are validated by
    sc-foundation's built-in schema, which SCConfigManager merges with this one.
    """

    def __init__(self) -> None:
        """Build the validation schema."""
        self.validation: dict[str, Any] = {
            "General": {
                "type": "dict",
                "required": False,
                "nullable": True,
                "schema": {
                    "AppName": {"type": "string", "required": False, "nullable": True},
                    "CheckInterval": {"type": "number", "required": False, "nullable": True, "min": 5, "max": 3600},
                },
            },
            "WAN": {
                "type": "dict",
                "required": True,
                "schema": {
                    "PrimaryMatch": {
                        "type": "list",
                        "required": True,
                        "minlength": 1,
                        "schema": {"type": "string", "empty": False},
                    },
                    "PrimaryLabel": {"type": "string", "required": False, "nullable": True},
                    "BackupLabel": {"type": "string", "required": False, "nullable": True},
                    "StatusFile": {"type": "string", "required": False, "nullable": True},
                    "FailoverHistoryFile": {"type": "string", "required": False, "nullable": True},
                    "DNSTimeout": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 60},
                },
            },
            "StatusAPI": {
                "type": "dict",
                "required": False,
                "nullable": True,
                "schema": {
                    "Enable": {"type": "boolean", "required": False, "nullable": True},
                    "Host": {"type": "string", "required": False, "nullable": True},
                    "Port": {"type": "number", "required": False, "nullable": True, "min": 1, "max": 65535},
                },
            },
        }
