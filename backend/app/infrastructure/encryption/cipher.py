"""
密码加解密工具 - AES加密
用于安全存储Facebook账号密码
"""
import base64
import hashlib

from cryptography.fernet import Fernet

from app.infrastructure.config.settings import get_settings


def _get_fernet() -> Fernet:
    """根据配置密钥生成Fernet实例"""
    key = get_settings().encryption_key.encode("utf-8")
    # 将任意长度密钥转为32字节的Fernet兼容密钥
    hashed = hashlib.sha256(key).digest()
    fernet_key = base64.urlsafe_b64encode(hashed)
    return Fernet(fernet_key)


def encrypt_password(plain_text: str) -> str:
    """加密密码"""
    fernet = _get_fernet()
    encrypted = fernet.encrypt(plain_text.encode("utf-8"))
    return encrypted.decode("utf-8")


def decrypt_password(encrypted_text: str) -> str:
    """解密密码"""
    fernet = _get_fernet()
    decrypted = fernet.decrypt(encrypted_text.encode("utf-8"))
    return decrypted.decode("utf-8")
