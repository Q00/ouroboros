"""
Data models for the microservice.
"""

from dataclasses import dataclass


@dataclass
class User:
    """User model representing a system user."""

    id: int
    username: str
    email: str
