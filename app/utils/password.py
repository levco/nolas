from cryptography.fernet import Fernet

from settings import settings

cipher_suite = Fernet(settings.password_encryption_key.encode())


class PasswordUtils:
    """Utility class for password operations."""

    @staticmethod
    def encrypt_password(password: str) -> str:
        """Encrypt a password"""
        return cipher_suite.encrypt(password.encode()).decode()

    @staticmethod
    def decrypt_password(password: str) -> str:
        """Decrypt apassword"""
        return cipher_suite.decrypt(password.encode()).decode()
