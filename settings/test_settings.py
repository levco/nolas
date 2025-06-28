import logging
from unittest.mock import Mock


class TestSettings(Mock):
    environment = "test"
    logging = Mock(level=logging.INFO)
