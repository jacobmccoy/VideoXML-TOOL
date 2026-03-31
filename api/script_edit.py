"""
Vercel Serverless Function: Script Editor
Parses transcripts for LLM editing and generates reference FCPXML files.
"""

from http.server import BaseHTTPRequestHandler
import base64
import importlib.util
import json
import os
import traceback

# Load modules via importlib to avoid sys.path issues on Vercel.
# conform_core must be loaded first and registered in sys.modules
# so transcript.py can import it.
import sys

_lib_dir = os.path.join(os.path.dirname(__file__), "..", "lib")

_core_path = os.path.join(_lib_dir, "conform_core.py")
_core_spec = importlib.util.spec_from_file_location("conform_core", _core_path)
_conform_core = importlib.util.module_from_spec(_core_spec)
_core_spec.loader.exec_module(_conform_core)
sys.modules['conform_core'] = _conform_core

_transcript_path = os.path.join(_lib_dir, "transcript.py")
_transcript_spec = importlib.util.spec_from_file_location("transcript", _transcript_path)
_transcript = importlib.util.module_from_spec(_transcript_spec)
_transcript_spec.loader.exec_module(_transcript)

prepare_for_llm = _transcript.prepare_for_llm
parse_llm_response = _transcript.parse_llm_response
script_edit_to_fcpxml = _transcript.script_edit_to_fcpxml
parse_transcript = _transcript.parse_transcript
direct_cut_multitrack = _transcript.direct_cut_multitrack
conform_from_strings = _conform_core.conform_from_strings


def _extract_text(raw_bytes):
    """Extract text content from raw bytes (direct or zipped)."""
    if raw_bytes[:2] == b'PK':
        import zipfile, io
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            for name in zf.namelist():
                if name.endswith('.txt'):
                    return zf.read(name).decode('utf-8')
            raise ValueError("No .txt file found inside the zip.")
    return raw_bytes.decode('utf-8')


def _extract_fcpxml(raw_bytes):
    """Extract FCPXML content from raw bytes (direct or zipped bundle)."""
    if raw_bytes[:2] == b'PK':
        import zipfile, io
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            for name in zf.namelist():
                if name.endswith('Info.fcpxml'):
                    return zf.read(name).decode('utf-8')
            for name in zf.namelist():
                if name.endswith('.fcpxml'):
                    return zf.read(name).decode('utf-8')
            raise ValueError("No .fcpxml file found inside the zip.")
    return raw_bytes.decode('utf-8')


def _json_response(handler, status, body):
    """Send a JSON response with CORS headers."""
    payload = json.dumps(body).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Content-Length', str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)

            action = data.get('action', '')

            if action == 'parse':
                # Parse transcript and return numbered segments + LLM prompt
                if 'transcript' not in data:
                    _json_response(self, 400, {
                        'error': 'Missing "transcript" field (base64-encoded .txt)'
                    })
                    return

                transcript_text = _extract_text(base64.b64decode(data['transcript']))
                result = prepare_for_llm(transcript_text)
                _json_response(self, 200, result)

            elif action == 'generate':
                # Generate reference FCPXML from transcript + selections
                if 'transcript' not in data or 'selections' not in data:
                    _json_response(self, 400, {
                        'error': 'Missing "transcript" and/or "selections" fields'
                    })
                    return

                transcript_text = _extract_text(base64.b64decode(data['transcript']))
                segments = parse_transcript(transcript_text)

                # Parse selections (can be JSON string or direct array)
                selections = data['selections']
                if isinstance(selections, str):
                    selection_data = parse_llm_response(selections, len(segments))
                    selected_indices = selection_data['segments']
                elif isinstance(selections, list):
                    selected_indices = selections
                else:
                    _json_response(self, 400, {
                        'error': '"selections" must be a JSON string or array of indices'
                    })
                    return

                ref_xml, summary = script_edit_to_fcpxml(
                    transcript_text, selected_indices)

                _json_response(self, 200, {
                    'xml': ref_xml,
                    'summary': summary,
                })

            elif action == 'direct_cut':
                # Direct multitrack cut from transcript segments
                required = ['transcript', 'selections', 'original']
                missing = [f for f in required if f not in data]
                if missing:
                    _json_response(self, 400, {
                        'error': f'Missing fields: {", ".join(missing)}'
                    })
                    return

                transcript_text = _extract_text(base64.b64decode(data['transcript']))
                segments = parse_transcript(transcript_text)

                selections = data['selections']
                if isinstance(selections, str):
                    selection_data = parse_llm_response(selections, len(segments))
                    selected_indices = selection_data['segments']
                elif isinstance(selections, list):
                    selected_indices = selections
                else:
                    _json_response(self, 400, {
                        'error': '"selections" must be a JSON string or array of indices'
                    })
                    return

                original_xml = _extract_fcpxml(base64.b64decode(data['original']))
                result_xml, log_lines = direct_cut_multitrack(
                    original_xml, transcript_text, selected_indices)

                _json_response(self, 200, {
                    'xml': result_xml,
                    'log': log_lines,
                })

            else:
                _json_response(self, 400, {
                    'error': f'Unknown action: "{action}". Use "parse", "generate", or "direct_cut".'
                })

        except json.JSONDecodeError as e:
            _json_response(self, 400, {'error': f'Invalid JSON: {str(e)}'})

        except (base64.binascii.Error, UnicodeDecodeError) as e:
            _json_response(self, 400, {'error': f'Invalid base64 encoding: {str(e)}'})

        except ValueError as e:
            _json_response(self, 422, {'error': f'Processing error: {str(e)}'})

        except Exception as e:
            _json_response(self, 500, {
                'error': f'Internal error: {str(e)}',
                'trace': traceback.format_exc(),
            })

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
