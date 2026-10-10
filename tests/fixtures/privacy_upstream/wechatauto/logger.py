"""Model the pinned upstream's lazy file logger and console handler."""
import logging
from pathlib import Path
from .param import WxParam


class SyntheticLogger:
    def __init__(self):
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.handlers.clear()
        self.console_handler = logging.StreamHandler()
        root.addHandler(self.console_handler)
        self.logger = logging.getLogger('wechatauto')
        self.file_handler = None

    def info(self, message):
        if WxParam.ENABLE_FILE_LOGGER and self.file_handler is None:
            directory = Path('wechatauto_logs')
            directory.mkdir()
            self.file_handler = logging.FileHandler(directory / 'synthetic.log')
            logging.getLogger().addHandler(self.file_handler)
        self.logger.info(message)


wxlog = SyntheticLogger()
