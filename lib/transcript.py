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

try:
    from .conform_core import (
        to_time_str, frac_to_tc, parse_original_timeline,
        conform_timeline, generate_fcpxml,
    )
except ImportError:
    from conform_core import (
        to_time_str, frac_to_tc, parse_original_timeline,
        conform_timeline, generate_fcpxml,
    )


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


def direct_cut_multitrack(original_xml, transcript_text, selected_indices):
    """Cut a multitrack FCPXML directly from transcript segment selections.

    Handles Resolve's gap-spine export format where the spine contains a gap
    element and all media (cameras, audio) are connected clips on that gap.
    Transcript timecodes are in the gap's timeline space.

    For each selected transcript segment, trims every connected clip to the
    segment's timecode range and outputs them sequentially.

    Args:
        original_xml: Original multitrack FCPXML as string
        transcript_text: Raw transcript .txt content
        selected_indices: List of segment indices in desired playback order

    Returns:
        (result_xml_string, log_lines) tuple
    """
    from copy import deepcopy
    try:
        from .conform_core import parse_time
    except ImportError:
        from conform_core import parse_time

    segments = parse_transcript(transcript_text)
    if not segments:
        raise ValueError("No transcript segments found. Check the transcript format.")

    for idx in selected_indices:
        if idx < 0 or idx >= len(segments):
            raise ValueError(f"Segment index {idx} out of range (0-{len(segments) - 1})")

    orig_tree = ET.ElementTree(ET.fromstring(original_xml))
    orig_root = orig_tree.getroot()

    # Extract sequence info
    sequence = orig_root.find('.//sequence')
    if sequence is None:
        raise ValueError("No <sequence> found in original FCPXML")

    seq_tc_start = parse_time(sequence.get('tcStart'))
    seq_format = sequence.get('format')
    seq_tc_format = sequence.get('tcFormat')

    # Find the spine and its children (gap or asset-clips)
    spine = sequence.find('spine')
    if spine is None:
        raise ValueError("No <spine> found in original FCPXML")

    # Collect all connected clips from spine children (gap or asset-clip)
    # Resolve exports multitrack as: <gap> with connected <asset-clip>s
    connected_clips = []
    spine_child = list(spine)[0]  # The gap or primary clip

    gap_start = parse_time(spine_child.get('start'))
    gap_offset = parse_time(spine_child.get('offset'))

    for child in spine_child:
        if child.tag in ('asset-clip', 'clip', 'ref-clip') and child.get('lane'):
            conn_start = parse_time(child.get('start'))
            conn_offset = parse_time(child.get('offset'))
            conn_duration = parse_time(child.get('duration'))
            connected_clips.append({
                'element': child,
                'ref': child.get('ref'),
                'name': child.get('name', ''),
                'lane': child.get('lane'),
                'start': conn_start,       # source in-point
                'offset': conn_offset,      # position in gap's timeline
                'duration': conn_duration,
                'format': child.get('format'),
                'tcFormat': child.get('tcFormat'),
                'enabled': child.get('enabled', '1'),
                'audioStart': child.get('audioStart'),
                'audioDuration': child.get('audioDuration'),
                'children': [c for c in child if c.tag not in ('asset-clip', 'clip', 'ref-clip')],
            })

    if not connected_clips:
        raise ValueError("No connected clips found on spine element")

    log_lines = []
    log_lines.append(f"Original timeline: {frac_to_tc(gap_offset)} to "
                     f"{frac_to_tc(gap_offset + parse_time(spine_child.get('duration')))}")
    log_lines.append(f"Connected tracks: {len(connected_clips)}")
    for cc in connected_clips:
        log_lines.append(f"  Lane {cc['lane']}: {cc['name']}")

    # The gap's timeline starts at gap_start. Connected clips have offset = gap_start
    # meaning they're aligned to the start of the gap. Transcript timecodes are
    # in absolute timecode space. The gap_offset tells us where that maps to.
    #
    # For a transcript TC, the offset into the connected clip's source is:
    #   tc_as_seconds - gap_offset  (how far into the gap timeline)
    #   + (conn_start - gap_start)   (source offset of this clip relative to gap)
    #
    # But since conn_offset == gap_start for all clips (they're synced),
    # the trim math simplifies to:
    #   new_conn_start = conn_start + (seg_tc_start - gap_start)
    #   new_conn_duration = seg_duration

    # Build output FCPXML
    root = ET.Element('fcpxml', version=orig_root.get('version', '1.13'))

    # Copy resources
    orig_resources = orig_root.find('resources')
    root.append(deepcopy(orig_resources))

    # Build sequence
    library = ET.SubElement(root, 'library')
    orig_event = orig_root.find('.//event')
    event = ET.SubElement(library, 'event',
                          name=orig_event.get('name', 'Script Edit') + ' - Script Edit')
    orig_project = orig_root.find('.//project')
    project = ET.SubElement(event, 'project',
                            name=orig_project.get('name', 'Script Edit') + ' - Script Edit')

    # Calculate total duration
    total_duration = Fraction(0)
    for idx in selected_indices:
        total_duration += segments[idx]['duration']

    new_sequence = ET.SubElement(project, 'sequence',
                                  duration=to_time_str(total_duration),
                                  format=seq_format,
                                  tcFormat=seq_tc_format,
                                  tcStart=to_time_str(seq_tc_start))
    new_spine = ET.SubElement(new_sequence, 'spine')

    # For each selected segment, create a gap with trimmed connected clips
    running_offset = seq_tc_start

    for seg_i, idx in enumerate(selected_indices):
        seg = segments[idx]
        seg_start_tc = seg['start']     # absolute TC (e.g., 01:00:06:09 as Fraction)
        seg_duration = seg['duration']

        log_lines.append(
            f"Segment {seg_i + 1}: [{idx}] {seg['tc_in']}-{seg['tc_out']} "
            f"dur={float(seg_duration):.2f}s — {seg['text'][:50]}..."
        )

        # Create a gap on the spine for this segment
        gap_el = ET.SubElement(new_spine, 'gap',
                                name='Gap',
                                start=to_time_str(gap_start),
                                duration=to_time_str(seg_duration),
                                offset=to_time_str(running_offset))

        # Trim each connected clip to this segment's range
        for cc in connected_clips:
            # How far into the gap timeline does this segment start?
            offset_into_gap = seg_start_tc - gap_start

            # Check if this segment overlaps with the connected clip
            cc_gap_start = cc['offset'] - gap_start  # usually 0
            cc_gap_end = cc_gap_start + cc['duration']

            seg_gap_start = offset_into_gap
            seg_gap_end = offset_into_gap + seg_duration

            overlap_start = max(seg_gap_start, cc_gap_start)
            overlap_end = min(seg_gap_end, cc_gap_end)

            if overlap_start >= overlap_end:
                continue  # No overlap

            overlap_duration = overlap_end - overlap_start
            trim_into_cc = overlap_start - cc_gap_start

            new_cc_start = cc['start'] + trim_into_cc
            # Connected clip offset is relative to the gap's start value
            new_cc_offset = gap_start + (overlap_start - seg_gap_start)

            attribs = {
                'ref': cc['ref'],
                'lane': cc['lane'],
                'name': cc['name'],
                'enabled': cc['enabled'],
                'start': to_time_str(new_cc_start),
                'duration': to_time_str(overlap_duration),
                'offset': to_time_str(new_cc_offset),
            }
            if cc['format']:
                attribs['format'] = cc['format']
            if cc['tcFormat']:
                attribs['tcFormat'] = cc['tcFormat']

            cc_el = ET.SubElement(gap_el, 'asset-clip', **attribs)

            # Copy non-clip children (transforms, etc.)
            for child in cc['children']:
                cc_el.append(deepcopy(child))

        running_offset += seg_duration

    log_lines.append(f"Total duration: {float(total_duration):.2f}s ({len(selected_indices)} segments)")

    # Format output
    tree = ET.ElementTree(root)
    ET.indent(tree, space='    ')

    buf = io.StringIO()
    buf.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    buf.write('<!DOCTYPE fcpxml>\n')
    tree.write(buf, encoding='unicode', xml_declaration=False)

    return buf.getvalue(), log_lines


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
