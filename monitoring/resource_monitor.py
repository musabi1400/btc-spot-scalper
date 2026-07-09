"""
monitoring/resource_monitor.py
==============================
ResourceMonitor — tracks CPU, memory, and disk usage over time.
Records samples for trend analysis and alerts on spikes.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import psutil

logger = logging.getLogger("monitoring.resources")


@dataclass
class ResourceSample:
    """A single resource usage sample."""
    timestamp: str
    cpu_pct: float
    memory_mb: float
    memory_pct: float
    disk_pct: float
    net_bytes_sent: int
    net_bytes_recv: int

    def to_dict(self) -> dict:
        return asdict(self)


class ResourceMonitor:
    """
    Monitors system resources (CPU, memory, disk, network).
    Keeps a rolling window of samples for trend analysis.
    """

    def __init__(self, history_size: int = 1440):
        """
        Args:
            history_size: Number of samples to keep (default: 1440 = 24h at 1-min intervals).
        """
        self._samples: deque[ResourceSample] = deque(maxlen=history_size)
        self._last_net_io: Optional[tuple[int, int]] = None

    def sample(self) -> ResourceSample:
        """Take a resource usage sample and store it."""
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.Process().memory_info()
        mem_mb = mem.rss / 1024 / 1024
        mem_pct = (mem.rss / psutil.virtual_memory().total) * 100
        disk = psutil.disk_usage(".")
        disk_pct = (disk.used / disk.total) * 100
        net = psutil.net_io_counters()
        sent = net.bytes_sent
        recv = net.bytes_recv

        sample = ResourceSample(
            timestamp=datetime.now(timezone.utc).isoformat(),
            cpu_pct=round(cpu, 1),
            memory_mb=round(mem_mb, 1),
            memory_pct=round(mem_pct, 1),
            disk_pct=round(disk_pct, 1),
            net_bytes_sent=sent,
            net_bytes_recv=recv,
        )
        self._samples.append(sample)
        return sample

    def get_samples(self, limit: int = 100) -> list[dict]:
        """Get the most recent samples."""
        return [s.to_dict() for s in list(self._samples)[-limit:]]

    def get_summary(self) -> dict:
        """Get a summary of current resource usage."""
        if not self._samples:
            return {"error": "No samples yet"}
        latest = self._samples[-1]
        avg_cpu = sum(s.cpu_pct for s in self._samples) / len(self._samples)
        max_cpu = max(s.cpu_pct for s in self._samples)
        max_mem = max(s.memory_mb for s in self._samples)

        return {
            "current": latest.to_dict(),
            "avg_cpu_pct": round(avg_cpu, 1),
            "max_cpu_pct": round(max_cpu, 1),
            "max_memory_mb": round(max_mem, 1),
            "sample_count": len(self._samples),
        }

    def check_alerts(self, thresholds: Optional[dict] = None) -> list[dict]:
        """Check if any resource exceeds thresholds."""
        if not self._samples:
            return []
        t = thresholds or {"cpu_pct": 80, "memory_mb": 500, "disk_pct": 90}
        latest = self._samples[-1]
        alerts = []

        if latest.cpu_pct > t.get("cpu_pct", 80):
            alerts.append({
                "level": "WARN", "metric": "cpu",
                "value": latest.cpu_pct, "threshold": t.get("cpu_pct", 80),
            })
        if latest.memory_mb > t.get("memory_mb", 500):
            alerts.append({
                "level": "WARN", "metric": "memory",
                "value": latest.memory_mb, "threshold": t.get("memory_mb", 500),
            })
        if latest.disk_pct > t.get("disk_pct", 90):
            alerts.append({
                "level": "ERROR", "metric": "disk",
                "value": latest.disk_pct, "threshold": t.get("disk_pct", 90),
            })
        return alerts