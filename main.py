"""
Grok Usage - StreamController plugin

Shows your Grok Build CLI subscription usage on a Stream Deck key: the
current billing period's credit-usage percentage (read straight from the
CLI's own local log, same number Grok Build itself uses), plus the time
remaining until that period resets. Optionally shows the last turn's token
count or approximate cost instead, from the CLI's session logs.
"""

from src.backend.PluginManager.PluginBase import PluginBase
from src.backend.PluginManager.ActionHolder import ActionHolder
from src.backend.DeckManagement.InputIdentifier import Input
from src.backend.PluginManager.ActionInputSupport import ActionInputSupport

# StreamController imports every plugin's main.py as `plugins.<folder>.main`,
# so a relative import resolves within that package regardless of what the
# plugin's folder is named. An absolute `import actions...` after manually
# appending this directory to sys.path is fragile: it registers a top-level
# "actions" module/package in sys.modules that can collide with any other
# installed plugin that also ships an "actions" folder (e.g. the official OS
# plugin), silently breaking the import - and with it, the whole plugin.
from .actions.GrokUsage.GrokUsage import GrokUsage


class GrokUsagePlugin(PluginBase):
    def __init__(self):
        super().__init__()

        self.lm = self.locale_manager
        self.lm.set_to_os_default()
        self.lm.set_fallback_language("en_US")

        self.grok_usage_holder = ActionHolder(
            plugin_base=self,
            action_base=GrokUsage,
            action_id_suffix="GrokUsage",
            action_name=self.lm.get("actions.grok-usage.name"),
            action_support={
                Input.Key: ActionInputSupport.SUPPORTED,
                Input.Dial: ActionInputSupport.UNTESTED,
                Input.Touchscreen: ActionInputSupport.UNTESTED,
            },
        )
        self.add_action_holder(self.grok_usage_holder)

        self.register(
            plugin_name=self.lm.get("plugin.name"),
            github_repo="https://github.com/ENjxzlt/Grok-Usage-Streamcontroller",
            plugin_version="1.0.1",
            app_version="1.5.0-beta",
        )
