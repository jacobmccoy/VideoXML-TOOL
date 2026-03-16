#!/usr/bin/env python3
"""
FCPXML Conform Tool
-------------------
Takes an original multitrack FCPXML and an edited single-track FCPXML
(e.g., from Descript or a mock edit), and produces a new FCPXML that
applies the edit decisions across all tracks in the original.

Usage:
    python conform.py --original original.fcpxml --edit edited.fcpxml --output conformed.fcpxml
"""

import argparse
import os
import sys
import xml.etree.ElementTree as ET

from lib.conform_core import (
    extract_edit_segments,
    frac_to_tc,
    generate_fcpxml,
    parse_original_timeline,
    conform_timeline,
)


def resolve_fcpxml_path(path):
    """Resolve a path that may be a .fcpxmld bundle directory or a direct .fcpxml file."""
    if os.path.isdir(path):
        info_path = os.path.join(path, 'Info.fcpxml')
        if os.path.isfile(info_path):
            return info_path
        raise FileNotFoundError(f"No Info.fcpxml found inside bundle: {path}")
    return path


def main():
    parser = argparse.ArgumentParser(
        description='Conform a multitrack FCPXML timeline to match edits from a single-track edit XML.')
    parser.add_argument('--original', required=True, help='Path to original multitrack FCPXML')
    parser.add_argument('--edit', required=True, help='Path to edited reference FCPXML (e.g., from Descript)')
    parser.add_argument('--output', required=True, help='Path for output conformed FCPXML')
    parser.add_argument('--verbose', action='store_true', help='Print detailed debug info')
    args = parser.parse_args()

    print("=" * 60)
    print("FCPXML Conform Tool")
    print("=" * 60)

    # Parse both XMLs (handle .fcpxmld bundles)
    orig_path = resolve_fcpxml_path(args.original)
    edit_path = resolve_fcpxml_path(args.edit)

    print(f"\nParsing original: {orig_path}")
    orig_tree = ET.parse(orig_path)
    seq_info, spine_data, asset_map, resources, orig_root = parse_original_timeline(orig_tree)

    print(f"  Sequence duration: {float(seq_info['duration']):.3f}s")
    print(f"  Sequence tcStart:  {frac_to_tc(seq_info['tcStart'])}")
    print(f"  Spine clips: {len(spine_data)}")
    for i, sc in enumerate(spine_data):
        print(f"    Spine clip {i+1}: {sc['name']} (ref={sc['ref']}, "
              f"src_in={frac_to_tc(sc['start'])}, dur={float(sc['duration']):.3f}s)")
        print(f"      Connected clips: {len(sc['connected'])}")
        for cc in sc['connected']:
            print(f"        Lane {cc['lane']}: {cc['name']} (ref={cc['ref']}, "
                  f"src_in={frac_to_tc(cc['start'])}, dur={float(cc['duration']):.3f}s)")

    print(f"\nParsing edit: {edit_path}")
    edit_tree = ET.parse(edit_path)
    edit_segments = extract_edit_segments(edit_tree)

    print(f"  Edit segments: {len(edit_segments)}")
    for i, seg in enumerate(edit_segments):
        if seg.get('is_gap'):
            print(f"    Segment {i+1}: GAP ({float(seg['duration']):.3f}s)")
        else:
            print(f"    Segment {i+1}: {seg['name']} "
                  f"src_in={frac_to_tc(seg['source_start'])} dur={float(seg['duration']):.3f}s")

    # Get reference print source start from original spine
    if not spine_data:
        print("ERROR: No spine clips found in original timeline")
        sys.exit(1)

    ref_source_start = spine_data[0]['start']
    print(f"\nReference print source start: {frac_to_tc(ref_source_start)}")

    # Conform
    print("\nConforming timeline...")
    conformed_clips, total_duration, log_lines = conform_timeline(
        seq_info, spine_data, edit_segments, ref_source_start)

    for line in log_lines:
        print(f"  {line}")

    print(f"\nConformed timeline:")
    print(f"  Total clips: {len(conformed_clips)}")
    print(f"  Total duration: {float(total_duration):.3f}s ({frac_to_tc(total_duration + seq_info['tcStart'])})")

    for i, clip in enumerate(conformed_clips):
        if clip['is_gap']:
            print(f"  Clip {i+1}: GAP ({float(clip['duration']):.3f}s)")
        else:
            print(f"  Clip {i+1}: {clip.get('name', '?')} "
                  f"src_in={frac_to_tc(clip['start'])} dur={float(clip['duration']):.3f}s "
                  f"offset={frac_to_tc(clip['offset'])}")
            for cc in clip['connected']:
                print(f"    Lane {cc['lane']}: {cc['name']} "
                      f"src_in={frac_to_tc(cc['start'])} dur={float(cc['duration']):.3f}s")

    # Generate output
    print(f"\nGenerating output FCPXML...")
    result_xml = generate_fcpxml(orig_tree, seq_info, conformed_clips, total_duration)

    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(result_xml)

    print(f"Output written to: {args.output}")
    print("\nDone!")
    print("=" * 60)


if __name__ == '__main__':
    main()
