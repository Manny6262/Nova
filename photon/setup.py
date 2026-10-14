"""Provision a Photon project dedicated to Nova Native, without changing Hermes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes")) / "hermes-agent"
sys.path.insert(0, str(HERMES))
from plugins.platforms.photon import auth  # type: ignore

DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "NovaNative"
CONFIG = DATA / "photon.json"
PROJECT_NAME = "Nova Native"


def main() -> int:
    own_number, hermes_line = auth.load_user_numbers()
    if not own_number:
        print("Hermes has no registered Photon phone number. Finish Hermes Photon setup first.", flush=True)
        return 1
    token = auth.load_photon_token()
    try:
        if not token:
            raise auth.PhotonDashboardAuthError("No dashboard session")
        auth.list_projects(token)
    except Exception:
        device = auth.request_device_code()
        print("PHOTON_SIGN_IN_URL=" + (device.verification_uri_complete or device.verification_uri), flush=True)
        print("PHOTON_SIGN_IN_CODE=" + device.user_code, flush=True)
        token = auth.poll_for_token(device)
        auth.validate_photon_token(token)
        print("Photon sign-in complete.", flush=True)

    project = auth.find_project_by_name(token, PROJECT_NAME)
    if not project:
        project = auth.create_project(token, name=PROJECT_NAME)
        print("Created a separate Nova Native project.", flush=True)
    project_id = project["id"]
    # This rotates only Nova Native's project secret; Hermes keeps its own project.
    secret = auth.regenerate_project_secret(token, project_id)
    user, _ = auth.register_user_if_absent(project_id, secret, phone_number=own_number)
    line = auth.user_assigned_line(user)
    if not line:
        print("Photon has not assigned Nova a shared iMessage line yet. Check its dashboard.", flush=True)
        return 2
    if auth._normalize_phone(line) == auth._normalize_phone(hermes_line):
        print("Photon assigned the same number as Hermes; Nova bridge will stay off to prevent mixed replies.", flush=True)
        return 3
    DATA.mkdir(parents=True, exist_ok=True)
    temp = CONFIG.with_suffix(".json.tmp")
    temp.write_text(json.dumps({"projectId": project_id, "projectSecret": secret,
                                "allowedUser": own_number, "assignedLine": line}), encoding="utf-8")
    if os.name == "nt":
        identity = subprocess.check_output(["whoami"], text=True).strip()
        subprocess.run(["icacls.exe", str(temp), "/grant:r", identity + ":(F)",
                        "NT AUTHORITY\\SYSTEM:(F)", "BUILTIN\\Administrators:(F)"],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["icacls.exe", str(temp), "/inheritance:r"],
                       check=True, stdout=subprocess.DEVNULL)
    os.replace(temp, CONFIG)
    print("Nova Photon project ready. Text Nova at " + line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
