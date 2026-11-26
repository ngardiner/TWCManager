import io
import urllib.parse

from TWCManager.Control.HTTPControl import CreateHTTPHandlerClass


def callAPI(master, path):
    # Run one API GET through the real handler without a socket.
    # Returns (status, headers dict, body bytes).
    cls = CreateHTTPHandlerClass(master)
    handler = cls.__new__(cls)  # bypass __init__, which would serve a request
    handler.path = path
    handler.url = urllib.parse.urlparse(path)
    handler.client_address = ("127.0.0.1", 0)
    handler.wfile = io.BytesIO()
    status = []
    headers = {}
    handler.send_response = lambda code, message=None: status.append(code)
    handler.send_header = lambda name, value: headers.__setitem__(name, value)
    handler.end_headers = lambda: None
    handler.do_API_GET()
    return (status[-1] if status else None), headers, handler.wfile.getvalue()
