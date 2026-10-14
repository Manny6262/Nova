"""Run Nova Native's private engine without a browser window."""
import os
import sys
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=5100)
args = parser.parse_args()
os.environ["PORT"] = str(args.port)
os.environ["NOVA_DATA_DIR"] = os.path.realpath(os.path.dirname(__file__))

NOVA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine")
if not os.path.isfile(os.path.join(NOVA_DIR, "app.py")):
    raise SystemExit(f"Nova Native engine is missing: {NOVA_DIR}")
os.chdir(NOVA_DIR)
sys.path.insert(0, NOVA_DIR)

from app import app, FastRequestHandler
from flask import jsonify


@app.get("/native-host")
def native_host():
    return jsonify({"native": True, "engine_path": os.path.normcase(NOVA_DIR)})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=args.port, threaded=True,
            use_reloader=False, request_handler=FastRequestHandler)
