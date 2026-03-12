# SPDX-FileCopyrightText: Copyright (c) 2025-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Shared trace utilities.

Writes CSV trace events compatible with the Chrome Trace Event async format.
Log format: timestamp, worker, event_name, phase, event_id, [message]
"""

import asyncio
import atexit
import os
import signal
import sys
import threading
import time

import aiohttp


async def _get_nats_time_offset(
    nats_server_ip: str, monitor_port: int = 8222, timeout: float = 0.05
) -> float:
    """Get time offset between local system and NATS server.

    Returns server_time - local_time in seconds.
    """
    monitor_url = f"http://{nats_server_ip}:{monitor_port}/varz"

    try:
        t1 = time.time()
        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_obj) as session:
            async with session.get(monitor_url) as response:
                t2 = time.time()
                server_info = await response.json()

                server_time_str = server_info.get("now", "")
                if server_time_str:
                    from dateutil import parser as dateutil_parser

                    server_time = dateutil_parser.parse(server_time_str).timestamp()
                else:
                    print("+-+ NATS time sync failed: 'now' field not found in response")
                    return 0.0

                rtt = t2 - t1
                estimated_server_time = server_time + (rtt / 2)
                local_time_mid = t1 + (rtt / 2)
                offset = estimated_server_time - local_time_mid

                print(f"+-+ rtt={rtt:.6f} offset = {offset:.6f}")
                return offset

    except Exception as exc:
        print(f"+-+ NATS time sync failed: {exc}")
        return 0.0


def _get_nats_time_offset_sync(nats_server_ip: str, monitor_port: int = 8222) -> float:
    """Synchronous wrapper for async time sync."""
    return asyncio.run(_get_nats_time_offset(nats_server_ip, monitor_port))


_nats_server = os.environ.get("MMD_TRACE_NATS_SRV_ADDR")
if _nats_server is None:
    print("+-+ Error: Environment variable MMD_TRACE_NATS_SRV_ADDR is not defined")
    _time_offset = 0.0
else:
    print(f"+-+ Environment variable MMD_TRACE_NATS_SRV_ADDR ={_nats_server}")
    _time_offset = _get_nats_time_offset_sync(_nats_server)
    print(f"+-+ _time_offset ={_time_offset}")

_trace_base_name = os.environ.get("MMD_TRACE_FILE_NAME")
if _trace_base_name is None:
    print("+-+ Warning: MMD_TRACE_FILE_NAME is not set, tracing disabled")

_pid = os.getpid()
_trace_buffers: dict[str, list[str]] = {}


def _trace_file_path(worker_name: str) -> str:
    return f"{_trace_base_name}_{worker_name}_{_pid}_trace.txt"


def _flush_traces() -> None:
    for worker_name, lines in _trace_buffers.items():
        if not lines:
            continue
        try:
            with open(_trace_file_path(worker_name), "a", encoding="utf-8") as handle:
                handle.writelines(lines)
        except Exception as exc:
            print(f"+-+ Failed to flush traces for {worker_name}: {exc}")
    _trace_buffers.clear()


atexit.register(_flush_traces)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))


def _periodic_flush_worker(interval: float = 1.0) -> None:
    while True:
        time.sleep(interval)
        _flush_traces()


if _trace_base_name is not None:
    _flush_thread = threading.Thread(target=_periodic_flush_worker, daemon=True)
    _flush_thread.start()


def write_trace(
    worker_name: str,
    event_name: str,
    event_phase: str,
    event_id: str = "",
    log_message: str = "",
    timestamp: float | None = None,
) -> None:
    """Write a trace event in Chrome Trace Event async format."""
    if _trace_base_name is None:
        return
    if timestamp is None:
        timestamp = time.time()
    timestamp += _time_offset
    trace_msg = (
        f"{timestamp:.6f}, {worker_name}, {event_name}, "
        f"{event_phase}, {event_id}, {log_message}\n"
    )
    _trace_buffers.setdefault(worker_name, []).append(trace_msg)
