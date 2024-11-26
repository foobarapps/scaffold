"""
A UUID 7 implementation that allows to parametrize the timestamp
(other implementations I found did not allow that and instead always used the current time).
"""

import datetime
import secrets
import uuid


# TODO write tests
def uuid7(timestamp: float | None = None) -> uuid.UUID:
    """
    Generate a UUID version 7.

    Args:
        timestamp (float, optional): A timestamp in seconds. Uses current time if not provided.

    Returns:
        uuid.UUID: A UUID version 7.
    """
    if timestamp is None:
        timestamp = datetime.datetime.utcnow().timestamp()

    # Convert timestamp to milliseconds
    timestamp_ms = int(timestamp * 1000)

    # Create a 60-bit timestamp
    time_low = timestamp_ms & 0xFFFFFFFF
    time_mid = (timestamp_ms >> 32) & 0xFFFF
    time_high_and_version = ((timestamp_ms >> 48) & 0x0FFF) | (7 << 12)  # Version 7

    # Generate random bits using secrets.randbits
    clock_seq = secrets.randbits(14)  # 14 bits for clock sequence
    node = secrets.randbits(48)  # 48 bits for node

    # Combine parts to create a UUID
    uuid_fields = (
        time_low,
        time_mid,
        time_high_and_version,
        clock_seq >> 8,
        clock_seq & 0xFF,
        node,
    )

    return uuid.UUID(fields=uuid_fields)
