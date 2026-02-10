"""Tests for memory detection and pressure handling."""

import pytest

from efspurge.purger import get_memory_usage_mb


def test_get_memory_usage_mb_returns_positive():
    """Test that memory detection returns a positive value."""
    memory_mb = get_memory_usage_mb()
    assert memory_mb > 0, "Memory detection should return a positive value"
    assert memory_mb < 100000, "Memory detection should return a reasonable value (< 100GB)"
    print(f"Detected memory usage: {memory_mb:.1f} MB")


def test_get_memory_usage_mb_multiple_calls_similar():
    """Test that multiple calls return similar values."""
    mem1 = get_memory_usage_mb()
    mem2 = get_memory_usage_mb()

    # Values should be within 10% of each other for consecutive calls
    assert mem1 > 0 and mem2 > 0
    diff_percent = abs(mem2 - mem1) / mem1 * 100
    assert diff_percent < 10, f"Memory readings varied by {diff_percent:.1f}%"
    print(f"Memory reading 1: {mem1:.1f} MB")
    print(f"Memory reading 2: {mem2:.1f} MB")


def test_memory_detection_method():
    """Test which memory detection method is being used."""
    # Check cgroup v2
    try:
        with open("/sys/fs/cgroup/memory.current", "r") as f:
            _ = int(f.read().strip())
        print("Using cgroup v2 memory detection")
        return
    except (FileNotFoundError, PermissionError, ValueError):
        pass

    # Check cgroup v1
    try:
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r") as f:
            _ = int(f.read().strip())
        print("Using cgroup v1 memory detection")
        return
    except (FileNotFoundError, PermissionError, ValueError):
        pass

    # Check psutil
    try:
        import psutil  # noqa: F401

        print("Using psutil memory detection")
        return
    except ImportError:
        pass

    # Check resource
    try:
        import resource  # noqa: F401

        print("Using resource module memory detection")
    except Exception:
        print("No memory detection method available!")
        pytest.fail("No memory detection method available")


if __name__ == "__main__":
    test_get_memory_usage_mb_returns_positive()
    test_get_memory_usage_mb_multiple_calls_similar()
    test_memory_detection_method()
