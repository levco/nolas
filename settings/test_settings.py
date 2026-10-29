import logging
from unittest.mock import Mock


class TestSettings(Mock):
    environment = "test"
    use_sentinel = False
    logging = Mock(level=logging.INFO)
