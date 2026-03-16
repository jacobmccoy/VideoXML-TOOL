"""
Vercel Serverless Function: FCPXML Conform
Accepts two FCPXML files (base64-encoded in JSON body), returns conformed FCPXML.
"""

import base64
import json
import os
import sys
from http.server import BaseHTTPRequestHandler

# Add project root to path for lib imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.conform_core import conform_from_strings


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)

            original_xml = base64.b64decode(data['original']).decode('utf-8')
            edit_xml = base64.b64decode(data['edit']).decode('utf-8')

            result_xml, log_lines = conform_from_strings(original_xml, edit_xml)

            response = json.dumps({
                'xml': result_xml,
                'log': log_lines,
            })

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(response.encode('utf-8'))

        except (KeyError, json.JSONDecodeError) as e:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': f'Invalid request: {str(e)}'
            }).encode('utf-8'))

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'error': str(e)
            }).encode('utf-8'))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
