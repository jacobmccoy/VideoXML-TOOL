#!/usr/bin/env python3
"""
Script Editor CLI
-----------------
LLM-powered transcript editing that generates reference FCPXML files
for the existing conform pipeline.

Usage:
    # Step 1: Prepare numbered transcript + LLM prompt
    python script_edit.py --transcript interview.txt --prepare

    # Step 2: Generate reference FCPXML from LLM selections
    python script_edit.py --transcript interview.txt \\
        --selections '{"segments": [3, 7, 12, 1, 15]}' \\
        --output reference_edit.fcpxml

    # Full pipeline: transcript + selections + original → conformed output
    python script_edit.py --transcript interview.txt \\
        --selections '{"segments": [3, 7, 12, 1, 15]}' \\
        --original original_timeline.fcpxml \\
        --output conformed.fcpxml
"""

import argparse
import os
import sys

from lib.transcript import (
    parse_transcript,
    format_for_llm,
    parse_llm_response,
    generate_reference_fcpxml,
)
from lib.conform_core import conform_from_strings


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
        description='LLM-powered transcript editing → reference FCPXML generation')
    parser.add_argument('--transcript', required=True,
                        help='Path to transcript .txt file')
    parser.add_argument('--prepare', action='store_true',
                        help='Output numbered transcript + LLM prompt for copy-paste')
    parser.add_argument('--selections', type=str,
                        help='LLM response JSON (inline or @filepath)')
    parser.add_argument('--original', type=str,
                        help='Path to original multitrack FCPXML (for full pipeline)')
    parser.add_argument('--output', type=str,
                        help='Output file path')
    args = parser.parse_args()

    # Read transcript
    with open(args.transcript, 'r', encoding='utf-8') as f:
        transcript_text = f.read()

    segments = parse_transcript(transcript_text)
    if not segments:
        print("ERROR: No transcript segments found. Check the transcript format.", file=sys.stderr)
        sys.exit(1)

    print(f"Parsed {len(segments)} transcript segments")

    # Mode: Prepare LLM prompt
    if args.prepare:
        system_prompt, user_prompt = format_for_llm(segments)

        print("\n" + "=" * 60)
        print("SYSTEM PROMPT (paste this as system/instructions):")
        print("=" * 60)
        print(system_prompt)
        print("\n" + "=" * 60)
        print("USER PROMPT (paste this along with your editorial guidance):")
        print("=" * 60)
        print(user_prompt)
        print("\n" + "=" * 60)
        print(f"Total segments: {len(segments)}")
        print("Copy the above into your LLM, add editorial guidance, and")
        print("paste the JSON response back with --selections")
        print("=" * 60)
        return

    # Mode: Generate reference FCPXML (requires --selections)
    if not args.selections:
        print("ERROR: --selections required (unless using --prepare)", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    # Read selections (inline JSON or @filepath)
    selections_text = args.selections
    if selections_text.startswith('@'):
        filepath = selections_text[1:]
        with open(filepath, 'r', encoding='utf-8') as f:
            selections_text = f.read()

    try:
        selection_data = parse_llm_response(selections_text, len(segments))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    selected_indices = selection_data['segments']
    reasoning = selection_data.get('reasoning', '')

    print(f"Selected {len(selected_indices)} segments: {selected_indices}")
    if reasoning:
        print(f"Reasoning: {reasoning}")

    # Generate reference FCPXML
    ref_xml, summary_lines = generate_reference_fcpxml(segments, selected_indices)

    print("\nGenerated reference FCPXML:")
    for line in summary_lines:
        print(f"  {line}")

    # Full pipeline mode (with --original)
    if args.original:
        orig_path = resolve_fcpxml_path(args.original)
        print(f"\nRunning conform pipeline with original: {orig_path}")

        with open(orig_path, 'r', encoding='utf-8') as f:
            original_xml = f.read()

        result_xml, log_lines = conform_from_strings(original_xml, ref_xml)

        print("\nConform log:")
        for line in log_lines:
            print(f"  {line}")

        output_path = args.output or 'conformed_script_edit.fcpxml'
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(result_xml)

        print(f"\nConformed output written to: {output_path}")

    else:
        # Just output the reference FCPXML
        output_path = args.output or 'reference_script_edit.fcpxml'
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ref_xml)

        print(f"\nReference FCPXML written to: {output_path}")
        print("Feed this into the conform tool as the --edit file.")

    print("\nDone!")


if __name__ == '__main__':
    main()
