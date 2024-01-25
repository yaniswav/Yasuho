import configparser
import os

class ConfigLoader:
    def __init__(self, *file_names):
        self.config = configparser.ConfigParser()
        for file_name in file_names:
            file_path = os.path.join(os.path.dirname(__file__), '..', 'config', file_name)
            if os.path.exists(file_path):
                self.config.read(file_path)

    def get(self, section, key, fallback=None):
        return self.config.get(section, key, fallback=fallback)

# Initialization with configuration file names
config_loader = ConfigLoader('bot.ini', 'tokens.ini')
