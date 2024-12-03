from argon2 import PasswordHasher as _PasswordHasher
from argon2.exceptions import VerificationError


class Argon2PasswordHasher:
    def __init__(self) -> None:
        self.hasher = _PasswordHasher()

    def hash_password(self, password: str) -> str:
        return self.hasher.hash(password)

    def verify_password(self, password_hash: str, password: str) -> bool:
        try:
            return self.hasher.verify(password_hash, password)
        except VerificationError:
            return False
