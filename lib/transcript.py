"""
Transcript-to-FCPXML Script Editor
-----------------------------------
Parse interview transcripts, format them for LLM-based editorial selection,
and generate reference FCPXML files that feed into the existing conform pipeline.
"""

import io
import json
import re
import xml.etree.ElementTree as ET
from fractions import Fraction

from .conform_core import to_time_str, frac_to_tc


# ---------------------------------------------------------------------------
# Timecode conversion
# ---------------------------------------------------------------------------

def tc_to_fraction(tc_str, fps=Fraction(24000, 1001)):
    """Convert HH:MM:SS:FF timecode string to Fraction seconds.

    Each frame is 1/fps seconds. At 23.976fps (24000/1001), one frame
    is 1001/24000 seconds, and 01:00:00:00 = 86400 frames = 18018/5 seconds.
    """
    parts = tc_str.strip().split(':')
    hh, mm, ss, ff = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
    total_frames = hh * 86400 + mm * 1440 + ss * 24 + ff
    return Fraction(total_frames, 1) / fps


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

def parse_transcript(text, fps=Fraction(24000, 1001)):
    """Parse a timestamped transcript into a list of segment dicts.

    Expected format per block:
        [HH:MM:SS:FF - HH:MM:SS:FF]
        Speaker Name
         Transcript text here...

    Returns list of dicts with keys:
        index, tc_in, tc_out, speaker, text, start, end, duration
    """
    pattern = re.compile(
        r'\[(\d{2}:\d{2}:\d{2}:\d{2})\s*-\s*(\d{2}:\d{2}:\d{2}:\d{2})\]\s*\n'
        r'(.*?)\n'
        r'(.*?)(?=\n\[|\Z)',
        re.DOTALL
    )
    segments = []
    for i, match in enumerate(pattern.finditer(text)):
        tc_in, tc_out, speaker, body = match.groups()
        start = tc_to_fraction(tc_in, fps)
        end = tc_to_fraction(tc_out, fps)
        segments.append({
            'index': i,
            'tc_in': tc_in,
            'tc_out': tc_out,
            'speaker': speaker.strip(),
            'text': body.strip(),
            'start': start,
            'end': end,
            'duration': end - start,
        })
    return segments


# ---------------------------------------------------------------------------
# LLM prompt formatting
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a documentary editor. You will receive a transcript with numbered segments.
Each segment has a timecode range, speaker label, and transcript text.

Your task: select segments and arrange them to create a compelling edit based on the
editorial guidance provided.

Return ONLY valid JSON in this exact format:
{"segments": [0, 5, 3, ...], "reasoning": "..."}

Rules:
- "segments" is an array of segment index numbers in your desired playback order.
- You may reorder segments freely to build narrative structure.
- You may include the same segment index more than once if repetition serves the edit.
- You may NOT invent new segments or modify timecodes.
- Include a brief "reasoning" explaining your editorial choices."""


def format_for_llm(segments):
    """Format parsed transcript segments as a numbered list for LLM consumption.

    Returns (system_prompt, user_prompt) tuple. The user copies these into
    their LLM of choice along with editorial guidance.
    """
    lines = []
    for seg in segments:
        preview = seg['text'][:200]
        if len(seg['text']) > 200:
            preview += '...'
        lines.append(
            f"[{seg['index']}] [{seg['tc_in']} - {seg['tc_out']}] {seg['speaker']}\n"
            f"{preview}\n"
        )
    user_prompt = '\n'.join(lines)
    return SYSTEM_PROMPT, user_prompt


# ---------------------------------------------------------------------------
# LLM output parsing
# ---------------------------------------------------------------------------

def parse_llm_response(response_text, num_segments):
    """Extract segment selections from LLM JSON response.

    Handles markdown fences, commentary before/after JSON, etc.
    Returns dict with 'segments' (list of ints) and optional 'reasoning'.
    Raises ValueError on parse errors or invalid indices.
    """
    # Strip markdown code fences
    cleaned = re.sub(r'```(?:json)?\s*', '', response_text)
    cleaned = re.sub(r'```', '', cleaned)

    # Find the JSON object
    match = re.search(r'\{[^{}]*"segments"\s*:\s*\[[^\]]*\][^{}]*\}', cleaned, re.DOTALL)
    if not match:
        raise ValueError(
            "Could not find valid JSON with 'segments' array in LLM response. "
            "Expected format: {\"segments\": [0, 5, 3], \"reasoning\": \"...\"}"
        )

    data = json.loads(match.group())

    if 'segments' not in data:
        raise ValueError("JSON response missing 'segments' key")

    indices = data['segments']
    if not isinstance(indices, list):
        raise ValueError("'segments' must be an array of integers")

    for idx in indices:
        if not isinstance(idx, int) or idx < 0 or idx >= num_segments:
            raise ValueError(
                f"Segment index {idx} out of range (valid: 0-{num_segments - 1})"
            )

    if not indices:
        raise ValueError("No segments selected — 'segments' array is empty")

    return data


# ---------------------------------------------------------------------------
# Reference FCPXML generation
# ---------------------------------------------------------------------------

DEFAULT_FORMAT = {
    'width': '3840',
    'height': '2160',
    'name': 'FFVideoFormat3840x2160p2398',
    'frameDuration': '1001/24000s',
}


def generate_reference_fcpxml(transcript_segments, selected_indices,
                               format_info=None, tc_start=None,
                               source_name='transcript-source',
                               source_path='file:///transcript-source'):
    """Generate a minimal reference FCPXML from selected transcript segments.

    The output FCPXML has one asset-clip per selected segment in a spine,
    structured identically to what the conform tool expects as its
    'edited reference' input.

    Args:
        transcript_segments: list from parse_transcript()
        selected_indices: list of int indices in desired playback order
        format_info: dict with width/height/name/frameDuration (optional)
        tc_start: Fraction for sequence tcStart (default: 18018/5 = 01:00:00:00)
        source_name: name for the asset reference
        source_path: file path for the media-rep element

    Returns:
        (xml_string, summary_lines) tuple
    """
    fmt = format_info or DEFAULT_FORMAT
    if tc_start is None:
        tc_start = Fraction(18018, 5)  # 01:00:00:00 at 23.976fps

    # Build selected segment list
    selected = []
    for idx in selected_indices:
        seg = transcript_segments[idx]
        selected.append(seg)

    # Calculate total duration
    total_duration = sum(seg['duration'] for seg in selected)

    # Build XML
    root = ET.Element('fcpxml', version='1.13')

    # Resources
    resources_el = ET.SubElement(root, 'resources')
    ET.SubElement(resources_el, 'format',
                  id='r0',
                  name=fmt.get('name', DEFAULT_FORMAT['name']),
                  width=fmt.get('width', DEFAULT_FORMAT['width']),
                  height=fmt.get('height', DEFAULT_FORMAT['height']),
                  frameDuration=fmt.get('frameDuration', DEFAULT_FORMAT['frameDuration']))

    # One asset per clip (matches existing reference FCPXML pattern)
    for i in range(len(selected)):
        asset_id = f'r{i + 1}'
        # Asset start/duration span the full source range
        asset_el = ET.SubElement(resources_el, 'asset',
                                  id=asset_id,
                                  name=source_name,
                                  start=to_time_str(tc_start),
                                  duration=to_time_str(total_duration + tc_start),
                                  hasVideo='1',
                                  hasAudio='1',
                                  format='r0',
                                  audioChannels='2',
                                  audioSources='1')
        ET.SubElement(asset_el, 'media-rep', src=source_path, kind='original-media')

    # Library > Event > Project > Sequence > Spine
    library = ET.SubElement(root, 'library')
    event = ET.SubElement(library, 'event', name='Script Edit')
    project = ET.SubElement(event, 'project', name='Script Edit')
    sequence = ET.SubElement(project, 'sequence',
                              duration=to_time_str(total_duration),
                              format='r0',
                              tcFormat='NDF',
                              tcStart=to_time_str(tc_start))
    spine = ET.SubElement(sequence, 'spine')

    # One asset-clip per selected segment
    running_offset = tc_start
    summary_lines = []

    for i, seg in enumerate(selected):
        asset_ref = f'r{i + 1}'
        clip_el = ET.SubElement(spine, 'asset-clip',
                                 ref=asset_ref,
                                 start=to_time_str(seg['start']),
                                 duration=to_time_str(seg['duration']),
                                 offset=to_time_str(running_offset),
                                 enabled='1',
                                 format='r0',
                                 name=source_name,
                                 tcFormat='NDF')

        summary_lines.append(
            f"Clip {i + 1}: [{seg['index']}] {seg['tc_in']}-{seg['tc_out']} "
            f"({seg['speaker']}) dur={float(seg['duration']):.2f}s — "
            f"{seg['text'][:60]}..."
        )

        running_offset += seg['duration']

    summary_lines.append(f"Total duration: {float(total_duration):.2f}s ({len(selected)} segments)")

    # Format XML output
    tree = ET.ElementTree(root)
    ET.indent(tree, space='    ')

    buf = io.StringIO()
    buf.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    buf.write('<!DOCTYPE fcpxml>\n')
    tree.write(buf, encoding='unicode', xml_declaration=False)

    return buf.getvalue(), summary_lines


# ---------------------------------------------------------------------------
# High-level entry points
# ---------------------------------------------------------------------------

def script_edit_to_fcpxml(transcript_text, selected_indices,
                           format_info=None, source_name='transcript-source',
                           source_path='file:///transcript-source'):
    """Parse transcript and generate reference FCPXML from selected indices.

    Returns (fcpxml_string, summary_lines).
    Raises ValueError on parse or validation errors.
    """
    segments = parse_transcript(transcript_text)
    if not segments:
        raise ValueError("No transcript segments found. Check the transcript format.")

    for idx in selected_indices:
        if idx < 0 or idx >= len(segments):
            raise ValueError(f"Segment index {idx} out of range (0-{len(segments) - 1})")

    return generate_reference_fcpxml(
        segments, selected_indices, format_info,
        source_name=source_name, source_path=source_path
    )


def prepare_for_llm(transcript_text):
    """Parse transcript and return formatted prompts for LLM interaction.

    Returns dict with:
        segments: list of parsed segment dicts (with Fraction values as floats)
        system_prompt: the system prompt for the LLM
        user_prompt: the formatted transcript to send to the LLM
        num_segments: total number of segments
    """
    segments = parse_transcript(transcript_text)
    if not segments:
        raise ValueError("No transcript segments found. Check the transcript format.")

    system_prompt, user_prompt = format_for_llm(segments)

    # Convert Fraction values to floats for JSON serialization
    serializable_segments = []
    for seg in segments:
        serializable_segments.append({
            'index': seg['index'],
            'tc_in': seg['tc_in'],
            'tc_out': seg['tc_out'],
            'speaker': seg['speaker'],
            'text': seg['text'],
            'start_seconds': float(seg['start']),
            'end_seconds': float(seg['end']),
            'duration_seconds': float(seg['duration']),
        })

    return {
        'segments': serializable_segments,
        'system_prompt': system_prompt,
        'user_prompt': user_prompt,
        'num_segments': len(segments),
    }
