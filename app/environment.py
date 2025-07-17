from enum import Enum


class EnvironmentName(Enum):
    TESTING = "test"
    UNIT_TESTING = "unit_test"
    DEVELOPMENT = "development"
    STAGING = "staging"
    QA = "qa"
    PRODUCTION = "production"
