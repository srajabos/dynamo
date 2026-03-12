#!/usr/bin/env python3
"""
Parse custom log file and convert to Chrome trace format.
Log format: timestamp, worker, event_name, phase, event_id, [optional_message]

event_id correlates matching begin/end pairs for the same async invocation.
Chrome async phases 'b'/'e' with an 'id' field are used so that overlapping
spans from concurrent async calls are rendered as separate swimlanes.
"""

import argparse
import glob
import json
import os
import sys


def _extract_pid_from_filename(log_file):
    """Extract numeric PID from trace filename.

    Expected pattern: {base}_{worker}_{pid}_trace.txt
    Returns PID as a string, or None if extraction fails.
    """
    basename = os.path.basename(log_file)
    if basename.endswith("_trace.txt"):
        stem = basename[: -len("_trace.txt")]
        parts = stem.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return parts[1]
    return None


def parse_log_to_chrome_trace(log_file, pid_map):
    """Parse log file and convert to Chrome trace events."""
    events = []
    errors = []
    line_num = 0
    file_pid = _extract_pid_from_filename(log_file)

    try:
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                line_num += 1
                line = line.strip()

                # Skip empty lines
                if not line:
                    continue

                # Parse comma-separated fields
                fields = [field.strip() for field in line.split(",")]

                # Need at least 5 fields: timestamp, worker, event_name, phase, event_id
                if len(fields) < 5:
                    errors.append(
                        f"Line {line_num}: Not enough fields (need at least 5): {line}"
                    )
                    continue

                try:
                    timestamp_str = fields[0]
                    pid = fields[1]
                    event_name = fields[2]
                    phase = fields[3]
                    event_id = fields[4]

                    # Skip info events
                    if event_name == "info":
                        continue

                    # Parse timestamp (seconds to microseconds)
                    try:
                        timestamp_sec = float(timestamp_str)
                        timestamp_us = int(timestamp_sec * 1000000)
                    except ValueError:
                        errors.append(
                            f"Line {line_num}: Invalid timestamp '{timestamp_str}': {line}"
                        )
                        continue

                    # Map phase to Chrome trace async phase.
                    phase_map = {
                        "begin": "b",
                        "end": "e",
                    }

                    chrome_phase = phase_map.get(phase.lower())
                    if chrome_phase is None:
                        errors.append(
                            f"Line {line_num}: Unknown phase '{phase}' (expected 'begin' or 'end'): {line}"
                        )
                        continue

                    # Map string pid to a unique number.
                    # Perfetto needs integer pid values.
                    if pid not in pid_map:
                        pid_map[pid] = 10 + len(pid_map)

                    # Build Chrome trace event
                    event = {
                        "ts": timestamp_us,
                        "pid": pid_map[pid],
                        "tid": 0,
                        "name": event_name,
                        "ph": chrome_phase,
                        "id": event_id,  # Correlates matching b/e pairs
                    }

                    # Store event ID as an argument for easier filtering in Perfetto.
                    if chrome_phase == "b":
                        event["args"] = {"event_id": event_id}
                        if file_pid is not None:
                            event["args"]["pid"] = file_pid

                    # Remaining fields (field 5+) form the optional log message
                    if len(fields) > 5:
                        message = ",".join(fields[5:]).strip()
                        if message:
                            if "args" not in event:
                                event["args"] = {}
                            event["args"]["msg"] = message

                    events.append(event)

                except Exception as e:
                    errors.append(f"Line {line_num}: Error processing line: {e}: {line}")
                    continue

    except FileNotFoundError:
        print(f"Error: Log file '{log_file}' not found!")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading log file: {e}")
        sys.exit(1)

    return events, errors


def fix_event_arguments(all_events):
    """
    Fix Perfetto showing arguments only from 'b' (begin) events by merging
    arguments from matching 'e' (end) events into their 'b' counterpart, then
    clearing args from the end event.

    Events are matched by (pid, tid, id, name). If two begin events with the
    same key appear without an intervening end event the first begin is treated
    as unmatched (ill-formed trace) and counted; processing continues.
    """
    all_events.sort(key=lambda e: e["ts"])

    open_begins = {}  # (pid, tid, id, name) -> begin event
    unmatched_begins = 0

    for event in all_events:
        ph = event.get("ph")
        key = (
            event.get("pid"),
            event.get("tid"),
            event.get("id"),
            event.get("name"),
        )

        if ph == "b":
            if key in open_begins:
                # Two begins without a matching end: discard the earlier one.
                unmatched_begins += 1
                del open_begins[key]
            open_begins[key] = event

        elif ph == "e":
            if key in open_begins:
                begin_event = open_begins.pop(key)
                end_args = event.get("args", {})
                if end_args:
                    begin_args = begin_event.setdefault("args", {})
                    for k, v in end_args.items():
                        # Avoid overwriting existing begin args.
                        dest_key = f"end_{k}" if k in begin_args else k
                        begin_args[dest_key] = v
                    event["args"] = {}

    # Any begin events still open had no matching end.
    unmatched_begins += len(open_begins)

    print(f"  Merged end-event arguments into begin events for {len(all_events)} events.")
    if unmatched_begins:
        print(f"  Ignored {unmatched_begins} begin event(s) with no matching end event.")


def write_chrome_trace(events, output_file):
    """Write events in Chrome trace format."""
    trace_data = {
        "traceEvents": events,
        "displayTimeUnit": "ms",
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(trace_data, f, indent=2)

    print(f"Chrome trace written to: {output_file}")
    print(f"Total events: {len(events)}")


def main():
    parser = argparse.ArgumentParser(
        description="Parse custom log files and convert to Chrome trace format"
    )
    parser.add_argument(
        "--in",
        dest="input_files",
        nargs="+",
        help="Input log file(s) to process",
    )
    parser.add_argument(
        "--in_pat",
        dest="input_pattern",
        help="Prefix pattern to match trace files: finds all files matching <prefix>*trace.txt",
    )
    args = parser.parse_args()

    input_files = args.input_files or []

    # Expand --in_pat pattern to matching files.
    if args.input_pattern:
        prefix = args.input_pattern
        pattern = f"{prefix}*trace.txt"
        matched = sorted(glob.glob(pattern))
        if not matched:
            print(f"Error: No files matching '{pattern}' found!")
            sys.exit(1)
        input_files.extend(matched)

    if not input_files:
        parser.error("At least one of --in or --in_pat is required")

    # Generate output filename from first input file.
    first_file = input_files[0]
    output_file = os.path.splitext(first_file)[0] + ".json"

    print(f"Processing {len(input_files)} input file(s):")
    for input_file in input_files:
        print(f"  - {input_file}")
    print(f"Output file: {output_file}")
    print("-" * 60)

    all_events = []
    all_errors = []
    pid_map = {}

    # Process each input file.
    for log_file in input_files:
        print(f"\nParsing: {log_file}")
        events, errors = parse_log_to_chrome_trace(log_file, pid_map)
        all_events.extend(events)
        all_errors.extend(errors)
        print(f"  Found {len(events)} events")

    # Fix Perfetto argument display: merge end-event args into begin events.
    print("\nFixing event arguments...")
    fix_event_arguments(all_events)

    # Report any errors.
    if all_errors:
        print(f"\nParsing errors encountered ({len(all_errors)}):")
        for error in all_errors:
            print(f"  {error}")
        print()

    if all_events:
        write_chrome_trace(all_events, output_file)
        print(
            f"\nSuccessfully processed {len(all_events)} total events from {len(input_files)} file(s)"
        )
        print("\nTo view the trace:")
        print("  1. Open Chrome and navigate to: chrome://tracing")
        print(f"  2. Click 'Load' and select: {output_file}")
    else:
        print("No valid events found in log files.")
        sys.exit(1)


if __name__ == "__main__":
    main()
