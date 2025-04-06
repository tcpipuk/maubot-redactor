"""Manages the plugin's configuration structure and update mechanism.

Defines the settings available via `base-config.yaml` and the Maubot UI,
separated into 'redaction' and 'reporting' sections. Uses Maubot's
BaseProxyConfig for handling settings.
"""

from __future__ import annotations

from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper


class Config(BaseProxyConfig):
    """Configuration definition for the Redactor plugin.

    See the module docstring for details on available settings under the
    `redaction` and `reporting` keys.
    """

    def do_update(self, helper: ConfigUpdateHelper) -> None:
        """Apply updates from the Maubot UI configuration field.

        Called by Maubot when the user saves configuration changes. It copies
        the 'redaction' and 'reporting' sections from the user-provided data
        into the plugin's active configuration.

        Args:
            helper: The Maubot ConfigUpdateHelper providing access to new data.
        """
        helper.copy("redaction")
        helper.copy("reporting")
