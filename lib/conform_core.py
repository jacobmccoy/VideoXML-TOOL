"""
FCPXML Conform Core Logic
-------------------------
Pure functions for conforming a multitrack FCPXML timeline to match edits
made to a single reference video. No file I/O — works entirely on in-memory
XML strings and ElementTree objects.
"""

import copy
import io
import xml.etree.ElementTree as ET
from fractions import Fraction


# ---------------------------------------------------------------------------
# Utility: FCPXML rational time parsing
# ---------------------------------------------------------------------------

def parse_time(time_str):
    """Parse FCPXML rational time string (e.g., '18018/5s') to Fraction seconds."""
    if time_str is None:
        return None
    time_str = time_str.strip().rstrip('s')
    if '/' in time_str:
        num, den = time_str.split('/')
        return Fraction(int(num), int(den))
    else:
        return Fraction(time_str)


def to_time_str(frac):
    """Convert Fraction seconds back to FCPXML rational time string."""
    f = Fraction(frac)
    return f"{f.numerator}/{f.denominator}s"


def frac_to_tc(secs, fps=Fraction(24000, 1001)):
    """Convert Fraction seconds to HH:MM:SS:FF for debug output."""
    total_frames = int(secs * fps)
    ff = total_frames % 24
    ss = (total_frames // 24) % 60
    mm = (total_frames // (24 * 60)) % 60
    hh = total_frames // (24 * 60 * 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


# ---------------------------------------------------------------------------
# Step 1: Parse the edited XML to extract edit segments
# ---------------------------------------------------------------------------

def extract_edit_segments(edit_tree):
    """
    Parse the edited FCPXML and return a list of segments:
    Each segment is (source_start, duration) in absolute source timecode.
    These represent the 'keep regions' from the reference print.
    """
    root = edit_tree.getroot()
    spine = root.find('.//spine')
    if spine is None:
        raise ValueError("No <spine> found in edited FCPXML")

    segments = []
    for clip in spine:
        tag = clip.tag
        if tag in ('asset-clip', 'clip', 'ref-clip'):
            src_start = parse_time(clip.get('start'))
            duration = parse_time(clip.get('duration'))
            offset = parse_time(clip.get('offset'))
            name = clip.get('name', '(unnamed)')
            segments.append({
                'source_start': src_start,
                'duration': duration,
                'offset': offset,
                'name': name,
            })
        elif tag == 'gap':
            duration = parse_time(clip.get('duration'))
            offset = parse_time(clip.get('offset'))
            segments.append({
                'source_start': None,
                'duration': duration,
                'offset': offset,
                'name': '(gap)',
                'is_gap': True,
            })

    return segments


# ---------------------------------------------------------------------------
# Step 2: Parse original XML to get spine + connected clips
# ---------------------------------------------------------------------------

def parse_original_timeline(orig_tree):
    """
    Parse the original FCPXML. Returns:
    - sequence metadata (format, tcFormat, tcStart, duration)
    - spine clip info (the reference print)
    - list of connected clips (cameras, audio) with their lane/offset/start/duration
    - resource map
    """
    root = orig_tree.getroot()

    resources = {}
    for res in root.iter('format'):
        resources[res.get('id')] = res
    asset_map = {}
    for asset in root.iter('asset'):
        asset_map[asset.get('id')] = asset

    sequence = root.find('.//sequence')
    if sequence is None:
        raise ValueError("No <sequence> found in original FCPXML")

    seq_info = {
        'duration': parse_time(sequence.get('duration')),
        'format': sequence.get('format'),
        'tcFormat': sequence.get('tcFormat'),
        'tcStart': parse_time(sequence.get('tcStart')),
    }

    spine = sequence.find('spine')
    spine_clips = list(spine)

    spine_data = []
    for spine_clip in spine_clips:
        clip_info = {
            'element': spine_clip,
            'tag': spine_clip.tag,
            'ref': spine_clip.get('ref'),
            'start': parse_time(spine_clip.get('start')),
            'duration': parse_time(spine_clip.get('duration')),
            'offset': parse_time(spine_clip.get('offset')),
            'name': spine_clip.get('name', ''),
            'format': spine_clip.get('format'),
            'tcFormat': spine_clip.get('tcFormat'),
            'enabled': spine_clip.get('enabled'),
            'connected': [],
        }

        for child in spine_clip:
            if child.tag in ('asset-clip', 'clip', 'ref-clip') and child.get('lane'):
                connected = {
                    'element': child,
                    'tag': child.tag,
                    'ref': child.get('ref'),
                    'start': parse_time(child.get('start')),
                    'duration': parse_time(child.get('duration')),
                    'offset': parse_time(child.get('offset')),
                    'name': child.get('name', ''),
                    'lane': child.get('lane'),
                    'format': child.get('format'),
                    'tcFormat': child.get('tcFormat'),
                    'enabled': child.get('enabled'),
                    'sub_elements': [se for se in child if se.tag != 'asset-clip'],
                }
                connected['non_clip_children'] = [
                    se for se in child if se.tag not in ('asset-clip', 'clip', 'ref-clip')
                ]
                clip_info['connected'].append(connected)

        clip_info['non_clip_children'] = [
            se for se in spine_clip
            if not (se.tag in ('asset-clip', 'clip', 'ref-clip') and se.get('lane'))
            and se.tag not in ('asset-clip', 'clip', 'ref-clip')
        ]

        spine_data.append(clip_info)

    return seq_info, spine_data, asset_map, resources, root


# ---------------------------------------------------------------------------
# Step 3: Conform - apply edit segments to all tracks
# ---------------------------------------------------------------------------

def conform_timeline(seq_info, spine_data, edit_segments, ref_source_start):
    """
    For each edit segment, calculate where it falls in the original timeline,
    then create new spine clips with appropriately trimmed connected clips.
    """
    conformed_spine_clips = []
    log_lines = []
    current_output_offset = seq_info['tcStart']

    for seg_idx, segment in enumerate(edit_segments):
        if segment.get('is_gap'):
            conformed_spine_clips.append({
                'is_gap': True,
                'duration': segment['duration'],
                'offset': current_output_offset,
            })
            current_output_offset += segment['duration']
            continue

        seg_src_start = segment['source_start']
        seg_duration = segment['duration']
        seg_src_end = seg_src_start + seg_duration

        seg_rel_start = seg_src_start - ref_source_start
        seg_rel_end = seg_rel_start + seg_duration

        log_lines.append(
            f"Segment {seg_idx + 1}: ref src {frac_to_tc(seg_src_start)} - {frac_to_tc(seg_src_end)} "
            f"(rel {float(seg_rel_start):.3f}s - {float(seg_rel_end):.3f}s, dur {float(seg_duration):.3f}s)"
        )

        for orig_clip in spine_data:
            orig_offset = orig_clip['offset']
            orig_duration = orig_clip['duration']
            orig_start = orig_clip['start']

            orig_timeline_start = orig_offset
            orig_timeline_end = orig_timeline_start + orig_duration

            seg_timeline_start = orig_offset + seg_rel_start
            seg_timeline_end = seg_timeline_start + seg_duration

            overlap_start = max(seg_timeline_start, orig_timeline_start)
            overlap_end = min(seg_timeline_end, orig_timeline_end)

            if overlap_start >= overlap_end:
                continue

            overlap_duration = overlap_end - overlap_start

            offset_into_clip = overlap_start - orig_timeline_start
            new_spine_src_start = orig_start + offset_into_clip

            conformed_clip = {
                'is_gap': False,
                'ref': orig_clip['ref'],
                'start': new_spine_src_start,
                'duration': overlap_duration,
                'offset': current_output_offset,
                'name': orig_clip['name'],
                'format': orig_clip['format'],
                'tcFormat': orig_clip['tcFormat'],
                'enabled': orig_clip['enabled'],
                'non_clip_children': orig_clip['non_clip_children'],
                'connected': [],
            }

            for conn in orig_clip['connected']:
                conn_offset = conn['offset']
                conn_duration = conn['duration']
                conn_start = conn['start']

                conn_timeline_start = conn_offset
                conn_timeline_end = conn_offset + conn_duration

                conn_overlap_start = max(seg_timeline_start, conn_timeline_start)
                conn_overlap_end = min(seg_timeline_end, conn_timeline_end)

                if conn_overlap_start >= conn_overlap_end:
                    continue

                conn_overlap_duration = conn_overlap_end - conn_overlap_start

                conn_offset_into = conn_overlap_start - conn_timeline_start
                new_conn_src_start = conn_start + conn_offset_into

                # In FCPXML, connected clip offset is in the PARENT's local timeline,
                # which starts at the parent's 'start' value (source in-point).
                conn_rel_to_spine = conn_overlap_start - overlap_start
                new_conn_offset = new_spine_src_start + conn_rel_to_spine

                conformed_conn = {
                    'ref': conn['ref'],
                    'start': new_conn_src_start,
                    'duration': conn_overlap_duration,
                    'offset': new_conn_offset,
                    'name': conn['name'],
                    'lane': conn['lane'],
                    'format': conn['format'],
                    'tcFormat': conn['tcFormat'],
                    'enabled': conn['enabled'],
                    'non_clip_children': conn['non_clip_children'],
                }
                conformed_clip['connected'].append(conformed_conn)

            conformed_spine_clips.append(conformed_clip)

        current_output_offset += seg_duration

    total_duration = current_output_offset - seq_info['tcStart']
    return conformed_spine_clips, total_duration, log_lines


# ---------------------------------------------------------------------------
# Step 4: Generate output FCPXML
# ---------------------------------------------------------------------------

def generate_fcpxml(orig_tree, seq_info, conformed_clips, total_duration):
    """
    Build a new FCPXML document with the conformed edit.
    Returns the XML as a string.
    """
    orig_root = orig_tree.getroot()

    root = ET.Element('fcpxml', version=orig_root.get('version'))

    orig_resources = orig_root.find('resources')
    root.append(copy.deepcopy(orig_resources))

    orig_library = orig_root.find('library')
    orig_event = orig_library.find('event')
    orig_project = orig_event.find('project')

    library = ET.SubElement(root, 'library')
    event = ET.SubElement(library, 'event', name=orig_event.get('name') + ' - Conformed')
    project = ET.SubElement(event, 'project', name=orig_project.get('name') + ' - Conformed')

    sequence = ET.SubElement(project, 'sequence',
                             duration=to_time_str(total_duration),
                             format=seq_info['format'],
                             tcFormat=seq_info['tcFormat'],
                             tcStart=to_time_str(seq_info['tcStart']))

    spine = ET.SubElement(sequence, 'spine')

    for clip_data in conformed_clips:
        if clip_data['is_gap']:
            ET.SubElement(spine, 'gap',
                          offset=to_time_str(clip_data['offset']),
                          duration=to_time_str(clip_data['duration']),
                          name='Gap')
            continue

        clip_attribs = {
            'start': to_time_str(clip_data['start']),
            'enabled': clip_data['enabled'] or '1',
            'duration': to_time_str(clip_data['duration']),
            'ref': clip_data['ref'],
            'offset': to_time_str(clip_data['offset']),
        }
        if clip_data.get('format'):
            clip_attribs['format'] = clip_data['format']
        if clip_data.get('name'):
            clip_attribs['name'] = clip_data['name']
        if clip_data.get('tcFormat'):
            clip_attribs['tcFormat'] = clip_data['tcFormat']

        clip_el = ET.SubElement(spine, 'asset-clip', **clip_attribs)

        for child in clip_data.get('non_clip_children', []):
            clip_el.append(copy.deepcopy(child))

        for conn in clip_data['connected']:
            conn_attribs = {
                'start': to_time_str(conn['start']),
                'enabled': conn['enabled'] or '1',
                'duration': to_time_str(conn['duration']),
                'ref': conn['ref'],
                'offset': to_time_str(conn['offset']),
                'lane': conn['lane'],
            }
            if conn.get('format'):
                conn_attribs['format'] = conn['format']
            if conn.get('name'):
                conn_attribs['name'] = conn['name']
            if conn.get('tcFormat'):
                conn_attribs['tcFormat'] = conn['tcFormat']

            conn_el = ET.SubElement(clip_el, 'asset-clip', **conn_attribs)

            for child in conn.get('non_clip_children', []):
                conn_el.append(copy.deepcopy(child))

    tree = ET.ElementTree(root)
    ET.indent(tree, space='    ')

    buf = io.StringIO()
    buf.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    buf.write('<!DOCTYPE fcpxml>\n')
    tree.write(buf, encoding='unicode', xml_declaration=False)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# High-level entry point for web/API use
# ---------------------------------------------------------------------------

def conform_from_strings(original_xml, edit_xml):
    """
    Accept two XML strings (original multitrack + edited reference),
    run the full conform pipeline, return (result_xml_string, log_lines).
    Raises ValueError on parse/logic errors.
    """
    orig_tree = ET.ElementTree(ET.fromstring(original_xml))
    edit_tree = ET.ElementTree(ET.fromstring(edit_xml))

    seq_info, spine_data, asset_map, resources, orig_root = parse_original_timeline(orig_tree)
    edit_segments = extract_edit_segments(edit_tree)

    if not spine_data:
        raise ValueError("No spine clips found in original timeline")

    ref_source_start = spine_data[0]['start']

    conformed_clips, total_duration, log_lines = conform_timeline(
        seq_info, spine_data, edit_segments, ref_source_start)

    # Add summary to log
    log_lines.append(f"Total clips: {len(conformed_clips)}")
    log_lines.append(f"Total duration: {float(total_duration):.3f}s")
    for i, clip in enumerate(conformed_clips):
        if clip['is_gap']:
            log_lines.append(f"  Clip {i+1}: GAP ({float(clip['duration']):.3f}s)")
        else:
            log_lines.append(
                f"  Clip {i+1}: {clip.get('name', '?')} "
                f"src_in={frac_to_tc(clip['start'])} dur={float(clip['duration']):.3f}s "
                f"offset={frac_to_tc(clip['offset'])}"
            )
            for cc in clip['connected']:
                log_lines.append(
                    f"    Lane {cc['lane']}: {cc['name']} "
                    f"src_in={frac_to_tc(cc['start'])} dur={float(cc['duration']):.3f}s"
                )

    result_xml = generate_fcpxml(orig_tree, seq_info, conformed_clips, total_duration)
    return result_xml, log_lines
