"""
Shared configuration module for the microservice.
All modules should import configuration values from this single source.
"""


class Config:
    """Application configuration settings."""

    APP_NAME: str = "microservice"
    DEBUG: bool = False
    VERSION: str = "0.1.0"
