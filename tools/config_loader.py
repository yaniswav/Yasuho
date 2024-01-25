import configparser
import os

class ConfigLoader(configparser.ConfigParser):
    def __init__(self, *file_names):
        super().__init__()
        for file_name in file_names:
            file_path = os.path.join(os.path.dirname(__file__), '..', 'config', file_name)
            if os.path.exists(file_path):
                self.read(file_path)

    def getlist(self, section, option):
        value = self.get(section, option)
        return list(filter(None, (x.strip() for x in value.splitlines())))

    def getlistint(self, section, option):
        return [int(x) for x in self.getlist(section, option)]

# Initialisation avec les noms des fichiers de configuration
config_loader = ConfigLoader('bot.ini', 'tokens.ini')
