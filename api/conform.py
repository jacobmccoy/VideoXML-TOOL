"""
Vercel Serverless Function: FCPXML Conform
Accepts two FCPXML files (base64-encoded in JSON body), returns conformed FCPXML.
"""

from http.server import BaseHTTPRequestHandler
import base64
import importlib.util
import json
import os
import traceback

# Load conform_core via importlib to avoid sys.path issues on Vercel
_core_path = os.path.join(os.path.dirname(__file__), "..", "lib", "conform_core.py")
_spec = importlib.util.spec_from_file_location("conform_core", _core_path)
_conform_core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_conform_core)
conform_from_strings = _conform_core.conform_from_strings


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

            if 'original' not in data or 'edit' not in data:
                _json_response(self, 400, {
                    'error': 'Missing required fields: "original" and "edit" (base64-encoded FCPXML)'
                })
                return

            original_xml = base64.b64decode(data['original']).decode('utf-8')
            edit_xml = base64.b64decode(data['edit']).decode('utf-8')

            result_xml, log_lines = conform_from_strings(original_xml, edit_xml)

            _json_response(self, 200, {
                'xml': result_xml,
                'log': log_lines,
            })

        except json.JSONDecodeError as e:
            _json_response(self, 400, {'error': f'Invalid JSON: {str(e)}'})

        except (base64.binascii.Error, UnicodeDecodeError) as e:
            _json_response(self, 400, {'error': f'Invalid base64 encoding: {str(e)}'})

        except ValueError as e:
            _json_response(self, 422, {'error': f'FCPXML processing error: {str(e)}'})

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
