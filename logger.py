import logging
import inspect
from enum import IntEnum


class LogLevel(IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40


class Logger:
    def __init__(self, name: str = "app", level: LogLevel = LogLevel.INFO):
        self.name = name
        self.level = level
        self._logger = logging.getLogger(name)
        self._logger.setLevel(level)
        
        if not self._logger.handlers:
            # Console handler
            console_handler = logging.StreamHandler()
            console_handler.setLevel(level)
            # File handler
            file_handler = logging.FileHandler("discord-bot.log", encoding="utf-8")
            file_handler.setLevel(level)
            # Formatter
            formatter = logging.Formatter(
                "%(levelname)s | %(name)s | %(filename)s:%(lineno)d | %(message)s"
            )
            console_handler.setFormatter(formatter)
            file_handler.setFormatter(formatter)
            self._logger.addHandler(console_handler)
            self._logger.addHandler(file_handler)

    def _get_caller_info(self) -> tuple[str, int]:
        frame = inspect.currentframe()
        if frame:
            caller_frame = frame.f_back
            if caller_frame:
                caller_frame = caller_frame.f_back  # Skip _log frame
                if caller_frame:
                    caller_frame = caller_frame.f_back  # Skip debug/info frame
                    if caller_frame:
                        filename = caller_frame.f_code.co_filename
                        lineno = caller_frame.f_lineno
                        return filename, lineno
        return "", 0

    def _log(self, level: LogLevel, message: str, *args, **kwargs):
        if level >= self.level:
            filename, lineno = self._get_caller_info()
            filename = filename.split("/")[-1]
            formatted = message.format(*args, **kwargs)
            self._logger.log(level, f"[{filename}:{lineno}] {formatted}")

    def debug(self, message: str, *args, **kwargs):
        self._log(LogLevel.DEBUG, message, *args, **kwargs)

    def info(self, message: str, *args, **kwargs):
        self._log(LogLevel.INFO, message, *args, **kwargs)

    def warning(self, message: str, *args, **kwargs):
        self._log(LogLevel.WARNING, message, *args, **kwargs)

    def error(self, message: str, *args, **kwargs):
        self._log(LogLevel.ERROR, message, *args, **kwargs)


log = Logger("discord-bot", LogLevel.DEBUG)
