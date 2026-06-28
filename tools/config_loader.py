import configparser
import os


class ConfigLoader(configparser.ConfigParser):
    """ConfigParser that loads project config files and unquotes values."""

    def __init__(self, *file_names):
        super().__init__(interpolation=None)
        for file_name in file_names:
            file_path = os.path.join(os.path.dirname(__file__), '..', 'config', file_name)
            if os.path.exists(file_path):
                self.read(file_path, encoding='utf-8')

    @staticmethod
    def _unquote(value):
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            return value[1:-1]
        return value

    def getstr(self, section, option):
        """Like get(), but strips one pair of surrounding quotes if present."""
        return self._unquote(self.get(section, option))

    def getlist(self, section, option):
        value = self.get(section, option)
        return [self._unquote(x) for x in value.splitlines() if x.strip()]

    def getlistint(self, section, option):
        return [int(x) for x in self.getlist(section, option)]


# Initialise with the configuration file names
config_loader = ConfigLoader('bot.ini', 'tokens.ini', 'emojis.ini', 'games.ini')
