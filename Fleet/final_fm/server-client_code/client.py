# server-client_code_prajwal/client.py

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import mysql.connector
from mysql.connector import Error
import logging
from datetime import datetime
import os
import sys
import csv
import hashlib
import json
from typing import Optional, Dict, Any
from pathlib import Path
import asyncio
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Setup path for FM_latest imports
BASE_DIR = Path(__file__).resolve().parent.parent
FM_LATEST_DIR = BASE_DIR / "FM_latest"
if str(FM_LATEST_DIR) not in sys.path:
    sys.path.insert(0, str(FM_LATEST_DIR))

try:
    from data_manager.device_data_handler import DeviceDataHandler
    device_data_handler = DeviceDataHandler()
except Exception as e:
    print(f"Warning: Could not initialize DeviceDataHandler: {e}")
    device_data_handler = None

# ------------------- CONFIGURATION -------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host": "localhost",
    "database": "host_db2",
    "user": "myuser",
    "password": "mypassword"
}

def _resolve_csv_base_dir() -> str:
    wms = os.environ.get("WMS_DATA_DIR", "").strip()
    if wms:
        return str(Path(wms) / "device_logs")

    env_val = os.environ.get("CSV_BASE_DIR", "").strip()
    if env_val:
        return env_val

    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
        return str(base / "data" / "device_logs")
    else:
        base = Path(__file__).resolve().parent.parent
        return str(base / "FM_latest" / "data" / "device_logs")

CSV_BASE_DIR            = _resolve_csv_base_dir()
PATH_CSV_DIR            = os.environ.get("PATH_CSV_DIR", CSV_BASE_DIR)
DATA_DIR                = os.path.dirname(CSV_BASE_DIR)
DISPATCHER_PRINTING_DIR = os.path.join(DATA_DIR, "dispatcher_printing_logs")

os.makedirs(CSV_BASE_DIR,            exist_ok=True)
os.makedirs(PATH_CSV_DIR,            exist_ok=True)
os.makedirs(DISPATCHER_PRINTING_DIR, exist_ok=True)

logger.info(f"CSV_BASE_DIR            → {CSV_BASE_DIR}")
logger.info(f"DISPATCHER_PRINTING_DIR → {DISPATCHER_PRINTING_DIR}")

CLIENT_CSV_MAPPING = {
    1: "rob1.csv",
    2: "rob2.csv",
}

# =============================================================================
# Multi-Robot Command Reader
# =============================================================================

class CommandReader:
    def __init__(self, robot_id: str):
        self.robot_id = robot_id
        self.csv_path = os.path.join(CSV_BASE_DIR, f"{robot_id}_command.csv")
        self.last_timestamp = None
        self._ensure_csv_exists()

    def _ensure_csv_exists(self):
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow(['command', 'params', 'timestamp'])

    def read_command(self) -> Optional[Dict]:
        try:
            if not os.path.exists(self.csv_path):
                return None
            with open(self.csv_path, 'r', newline='', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
            if not rows:
                return None
            row = rows[0]
            command    = row.get('command',   '').strip()
            params_str = row.get('params',    '{}')
            timestamp  = row.get('timestamp', '')
            if not command:
                return None
            if timestamp == self.last_timestamp:
                return None
            try:
                params = json.loads(params_str) if params_str else {}
            except json.JSONDecodeError:
                params = {}
            self.last_timestamp = timestamp
            logger.info(f"[ROBOT {self.robot_id}] Read command: {command} at {timestamp}")
            return {"action": command, "params": params, "timestamp": timestamp}
        except Exception as e:
            logger.error(f"[ROBOT {self.robot_id}] Error reading CSV: {e}")
            return None

    def clear_command(self):
        try:
            with open(self.csv_path, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow(['command', 'params', 'timestamp'])
        except Exception as e:
            logger.error(f"[ROBOT {self.robot_id}] Error clearing CSV: {e}")


class RobotStateManager:
    def __init__(self):
        self.robots: Dict[str, Dict[str, Any]] = {}

    def get_robot_state(self, robot_id: str) -> Dict[str, Any]:
        if robot_id not in self.robots:
            self.robots[robot_id] = {
                "current_mode": "MANUAL", "connected": False,
                "last_heartbeat": None, "status": {},
                "program_content": None, "program_filename": None
            }
        return self.robots[robot_id]

    def update_robot_status(self, robot_id: str, status: Dict):
        state = self.get_robot_state(robot_id)
        state["status"] = status
        state["last_heartbeat"] = datetime.now()
        state["connected"] = True

    def is_robot_connected(self, robot_id: str) -> bool:
        state = self.get_robot_state(robot_id)
        if state["last_heartbeat"] is None:
            return False
        elapsed = (datetime.now() - state["last_heartbeat"]).total_seconds()
        state["connected"] = elapsed < 5
        return state["connected"]

    def get_all_robots_status(self) -> Dict[str, Dict]:
        result = {}
        for robot_id in self.robots:
            result[robot_id] = {
                "connected":      self.is_robot_connected(robot_id),
                "mode":           self.robots[robot_id]["current_mode"],
                "last_heartbeat": self.robots[robot_id]["last_heartbeat"].isoformat() if self.robots[robot_id]["last_heartbeat"] else None,
                "status":         self.robots[robot_id]["status"]
            }
        return result


robot_state_manager = RobotStateManager()
command_readers: Dict[str, CommandReader] = {}

def get_command_reader(robot_id: str) -> CommandReader:
    if robot_id not in command_readers:
        command_readers[robot_id] = CommandReader(robot_id)
    return command_readers[robot_id]

# =============================================================================
# FastAPI App
# =============================================================================

app = FastAPI(
    title="Enmac Host Server",
    description="MySQL + Path Sync + Task Sync + Multi-Robot Control"
)

class URLNormalizationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        original_path = path

        print(f"[DEBUG MW] Request Path: {path}")
        if path.startswith("//"):
            while "//" in path:
                path = path.replace("//", "/")
        print(f"[DEBUG MW] Final Path: {path}")

        if path != original_path:
            logger.debug(f"Normalizing URL path: {original_path} -> {path}")
            request.scope["path"] = path

        response = await call_next(request)
        return response

app.add_middleware(URLNormalizationMiddleware)

# =============================================================================
# Pydantic Models
# =============================================================================

class MotorData(BaseModel):
    client_id: int
    RIGHT_MOTOR_ENCODER_VAL: float | None = None
    LEFT_MOTOR_ENCODER_VAL:  float | None = None
    RIGHT_TURN_ENCODER_VAL:  float | None = None
    LEFT_TURN_ENCODER_VAL:   float | None = None
    current_location: str | None = None
    facing_direction: str | None = None

class CommandUpdate(BaseModel):
    command: str

class ObstacleData(BaseModel):
    robot_id:  str
    obstacle:  int | None = None
    timestamp: str | None = None

class BatteryData(BaseModel):
    robot_id:           str
    battery_percentage: float | None = None
    timestamp:          str | None = None

class AlarmData(BaseModel):
    robot_id:  str
    alarmRM:   int | None = None
    alarmLM:   int | None = None
    timestamp: str | None = None

class ChargingStatusData(BaseModel):
    robot_id:        str
    charging_status: int | None = None
    timestamp:       str | None = None

class SwitchStatusData(BaseModel):
    robot_id:      str
    switch_status: int | None = None
    timestamp:     str | None = None

class RobotCommAck(BaseModel):
    acknowledgement: str

class FleetResponseBody(BaseModel):
    fleet_response: str

class AcknowledgeData(BaseModel):
    robot_id:        str
    acknowledgement: str | None = None
    fleet_response:  str | None = None
    timestamp:       str | None = None

# =============================================================================
# Database Helper
# =============================================================================

def get_db_connection():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        if conn.is_connected():
            return conn
        logger.error("Failed to connect to MySQL")
        return None
    except Error as e:
        logger.error(f"Database connection error: {e}")
        return None

# =============================================================================
# CSV Helpers — Motor Data
# =============================================================================

def get_csv_path(client_id: int) -> str:
    filename = CLIENT_CSV_MAPPING.get(client_id, f"client_{client_id}.csv")
    return os.path.join(CSV_BASE_DIR, filename)

def append_to_csv(client_id: int, data: MotorData):
    csv_path = get_csv_path(client_id)
    file_exists = os.path.exists(csv_path) and os.path.getsize(csv_path) > 0
    header = ["timestamp","right_drive","left_drive","right_motor","left_motor","current_location", "facing_direction"]

    with open(csv_path, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(header)

        direction = data.facing_direction
        if direction is None and device_data_handler:
            try:
                device_id = CLIENT_CSV_MAPPING.get(client_id, f"rob{client_id}").replace('.csv', '')
                device_info = device_data_handler.get_latest_device_data(device_id)
                direction = device_info.get('facing_direction')
            except Exception:
                pass

        writer.writerow([
            datetime.now().isoformat(),
            data.RIGHT_MOTOR_ENCODER_VAL, data.LEFT_MOTOR_ENCODER_VAL,
            data.RIGHT_TURN_ENCODER_VAL,  data.LEFT_TURN_ENCODER_VAL,
            data.current_location,
            direction
        ])

# =============================================================================
# CSV Helpers — Task Sync
# =============================================================================

def read_last_task_status(client_id: int):
    filepath = os.path.join(CSV_BASE_DIR, f"rob{client_id}_task.csv")
    if not os.path.exists(filepath):
        return None, "idle"
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    if len(lines) <= 1:
        return None, "idle"
    last_line = lines[-1].strip()
    if not last_line:
        return None, "idle"
    parts = last_line.split(',')
    return (parts[0], parts[1]) if len(parts) >= 2 else (None, "idle")

def update_last_task_status(client_id: int, new_status: str):
    logger.info(f"Client {client_id} reported new status: '{new_status}'")
    filepath = os.path.join(CSV_BASE_DIR, f"rob{client_id}_task.csv")
    if not os.path.exists(filepath):
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(["task_id", "task_status"])
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    last_task_id = None
    for line in reversed(lines):
        if line.strip() and not line.startswith("task_id"):
            parts = line.strip().split(',')
            if parts:
                last_task_id = parts[0]
                break
    if not last_task_id:
        last_task_id = "TASK0001"
    with open(filepath, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow([last_task_id, new_status])

# =============================================================================
# CSV Helpers — Device Logs
# =============================================================================

def _ensure_device_log_csv(filename: str, headers: list) -> str:
    filepath = os.path.join(CSV_BASE_DIR, filename)
    if not os.path.exists(filepath):
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(headers)
    return filepath

def write_obstacle_to_csv(robot_id, obstacle, timestamp):
    fp = _ensure_device_log_csv(f"{robot_id}_obstacle.csv", ["obstacle","timestamp"])
    with open(fp,'w',newline='',encoding='utf-8') as f:
        w=csv.writer(f); w.writerow(["obstacle","timestamp"]); w.writerow([obstacle, timestamp or datetime.now().isoformat()])

def write_battery_to_csv(robot_id, battery_percentage, timestamp):
    fp = _ensure_device_log_csv(f"{robot_id}_Battery_status.csv", ["battery_percentage","timestamp"])
    with open(fp,'w',newline='',encoding='utf-8') as f:
        w=csv.writer(f); w.writerow(["battery_percentage","timestamp"]); w.writerow([battery_percentage, timestamp or datetime.now().isoformat()])

def write_alarm_to_csv(robot_id, alarmRM, alarmLM, timestamp):
    fp = _ensure_device_log_csv(f"{robot_id}_Alarm_status.csv", ["alarmRM","alarmLM","timestamp"])
    with open(fp,'w',newline='',encoding='utf-8') as f:
        w=csv.writer(f); w.writerow(["alarmRM","alarmLM","timestamp"]); w.writerow([alarmRM, alarmLM, timestamp or datetime.now().isoformat()])

def write_charging_status_to_csv(robot_id, charging_status, timestamp):
    fp = _ensure_device_log_csv(f"{robot_id}_Charging_Status.csv", ["charging_status","timestamp"])
    with open(fp,'w',newline='',encoding='utf-8') as f:
        w=csv.writer(f); w.writerow(["charging_status","timestamp"]); w.writerow([charging_status, timestamp or datetime.now().isoformat()])

def write_switch_status_to_csv(robot_id, switch_status, timestamp):
    fp = _ensure_device_log_csv(f"{robot_id}_emergency_status.csv", ["switch_status","timestamp"])
    with open(fp,'w',newline='',encoding='utf-8') as f:
        w=csv.writer(f); w.writerow(["switch_status","timestamp"]); w.writerow([switch_status, timestamp or datetime.now().isoformat()])

# =============================================================================
# CSV Helpers — Communication
# =============================================================================

COMM_HEADER = ["timestamp", "acknowledgement", "fleet_response"]

def _comm_path(robot_id: str)     -> str: return os.path.join(CSV_BASE_DIR,            f"{robot_id}_communication.csv")
def _disp_comm_path(disp_id: str) -> str: return os.path.join(DISPATCHER_PRINTING_DIR, f"{disp_id}.csv")
def _print_comm_path(print_id: str) -> str: return os.path.join(DISPATCHER_PRINTING_DIR, f"{print_id}.csv")

def read_communication_row(filepath: str) -> Dict[str, str]:
    out = {"timestamp": "", "acknowledgement": "", "fleet_response": ""}
    if not os.path.exists(filepath):
        return out
    try:
        with open(filepath, 'r', newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
            if rows:
                last = rows[-1]
                out["timestamp"]       = last.get("timestamp",       "")
                out["acknowledgement"] = last.get("acknowledgement", "")
                out["fleet_response"]  = last.get("fleet_response",  "")
    except Exception as e:
        logger.warning(f"read_communication_row {filepath}: {e}")
    return out

def write_communication_row(filepath: str, timestamp: str = "", acknowledgement: str = "", fleet_response: str = ""):
    now      = datetime.now().isoformat()
    existing = read_communication_row(filepath) if os.path.exists(filepath) else {}
    ts   = timestamp       if timestamp       else existing.get("timestamp",       now)
    ack  = acknowledgement if acknowledgement is not None else existing.get("acknowledgement", "")
    resp = fleet_response  if fleet_response  is not None else existing.get("fleet_response",  "")
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(COMM_HEADER)
        w.writerow([ts, ack, resp])

# =============================================================================
# Endpoints — Motor Data
# =============================================================================

@app.post("/update/", status_code=200)
def update_motor_data(data: MotorData):
    if data.client_id < 1:
        raise HTTPException(status_code=400, detail="Invalid client_id")
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Could not connect to database")
    try:
        cursor = conn.cursor()
        sql = """
        INSERT INTO enmac (id, RIGHT_MOTOR_ENCODER_VAL, LEFT_MOTOR_ENCODER_VAL,
            RIGHT_TURN_ENCODER_VAL, LEFT_TURN_ENCODER_VAL, current_location)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            RIGHT_MOTOR_ENCODER_VAL = VALUES(RIGHT_MOTOR_ENCODER_VAL),
            LEFT_MOTOR_ENCODER_VAL  = VALUES(LEFT_MOTOR_ENCODER_VAL),
            RIGHT_TURN_ENCODER_VAL  = VALUES(RIGHT_TURN_ENCODER_VAL),
            LEFT_TURN_ENCODER_VAL   = VALUES(LEFT_TURN_ENCODER_VAL),
            current_location        = VALUES(current_location)
        """
        cursor.execute(sql, (
            data.client_id,
            data.RIGHT_MOTOR_ENCODER_VAL, data.LEFT_MOTOR_ENCODER_VAL,
            data.RIGHT_TURN_ENCODER_VAL,  data.LEFT_TURN_ENCODER_VAL,
            data.current_location
        ))
        conn.commit(); cursor.close(); conn.close()
        append_to_csv(data.client_id, data)
        return {"status": "success", "updated_id": data.client_id}
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update and log data")

@app.get("/download/{client_id}")
def download_csv(client_id: int):
    csv_path = get_csv_path(client_id)
    if not os.path.exists(csv_path):
        return JSONResponse({"error": "CSV not found"}, status_code=404)
    return FileResponse(csv_path, media_type="text/csv", filename=os.path.basename(csv_path))

@app.get("/path/{client_id}")
def get_path_plan(client_id: int):
    filename  = f"path_rob{client_id}.csv"
    file_path = os.path.join(PATH_CSV_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"Path file {filename} not found on server")
    with open(file_path, "rb") as f:
        file_hash = hashlib.md5(f.read()).hexdigest()
    return FileResponse(file_path, media_type="text/csv", filename=filename, headers={"X-File-Hash": file_hash})

@app.get("/command/{client_id}")
def get_client_command(client_id: int):
    _, task_status = read_last_task_status(client_id)
    return {"command": task_status}

@app.post("/command/{client_id}")
def report_client_status(client_id: int, update: CommandUpdate):
    update_last_task_status(client_id, update.command)
    return {"status": "updated"}

# =============================================================================
# Endpoints — Multi-Robot Control
# =============================================================================

@app.get("/api/robot/{robot_id}/poll")
async def robot_poll(robot_id: str):
    cmd_reader = get_command_reader(robot_id)
    state      = robot_state_manager.get_robot_state(robot_id)
    command    = cmd_reader.read_command()
    if command and command.get("action") == "UPLOAD_PROGRAM":
        params = command.get("params", {})
        state["program_content"]  = params.get("content")
        state["program_filename"] = params.get("filename")
    if command and command.get("action") == "SET_MODE":
        state["current_mode"] = command.get("params", {}).get("mode", "MANUAL")
    return {
        "command": command,
        "mode":    state["current_mode"],
        "program": {"filename": state["program_filename"], "content": state["program_content"]} if state["program_content"] else None
    }

@app.post("/api/robot/{robot_id}/heartbeat")
async def robot_heartbeat(robot_id: str, status: Dict[str, Any] = {}):
    robot_state_manager.update_robot_status(robot_id, status)
    return {"status": "ok", "received": True}

# =============================================================================
# Endpoints — Device Log Sync  (static routes BEFORE wildcard /{robot_id}/…)
# =============================================================================

@app.post("/api/robot/sync/obstacle")
async def sync_obstacle(data: ObstacleData):
    write_obstacle_to_csv(data.robot_id, data.obstacle, data.timestamp)
    return {"status": "ok", "robot_id": data.robot_id}

@app.post("/api/robot/sync/battery")
async def sync_battery(data: BatteryData):
    write_battery_to_csv(data.robot_id, data.battery_percentage, data.timestamp)
    return {"status": "ok", "robot_id": data.robot_id}

@app.post("/api/robot/sync/alarm")
async def sync_alarm(data: AlarmData):
    write_alarm_to_csv(data.robot_id, data.alarmRM, data.alarmLM, data.timestamp)
    return {"status": "ok", "robot_id": data.robot_id}

@app.post("/api/robot/sync/charging_status")
async def sync_charging_status(data: ChargingStatusData):
    write_charging_status_to_csv(data.robot_id, data.charging_status, data.timestamp)
    return {"status": "ok", "robot_id": data.robot_id}

@app.post("/api/robot/sync/switch_status")
async def sync_switch_status(data: SwitchStatusData):
    write_switch_status_to_csv(data.robot_id, data.switch_status, data.timestamp)
    return {"status": "ok", "robot_id": data.robot_id}

@app.post("/api/robot/acknowledge")
async def sync_robot_acknowledge(data: AcknowledgeData):
    path = _comm_path(data.robot_id)
    write_communication_row(path,
        timestamp=data.timestamp or "",
        acknowledgement=data.acknowledgement or "",
        fleet_response=data.fleet_response or ""
    )
    return {"status": "ok", "robot_id": data.robot_id}

# =============================================================================
# Endpoints — Fleet Task Control
# =============================================================================

@app.post("/api/fleet/robot/{robot_id}/run_task")
async def fleet_trigger_run_task(robot_id: str):
    client_id = int(robot_id.replace("rob", ""))
    filepath  = os.path.join(CSV_BASE_DIR, f"rob{client_id}_task.csv")
    if not os.path.exists(filepath):
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(["task_id", "task_status"])
    with open(filepath, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow(["TASK0001", "run_task"])
    return {"status": "ok", "robot_id": robot_id, "command": "run_task"}

@app.post("/api/fleet/robot/{robot_id}/cancel_task")
async def fleet_trigger_cancel_task(robot_id: str):
    """
    Fleet triggers task cancellation for a robot.
    Writes 'task_cancelled' into rob{id}_task.csv so the robot's
    GET /command/{id} poll picks it up and forwards it to execution.csv.
    """
    client_id = int(robot_id.replace("rob", ""))
    filepath  = os.path.join(CSV_BASE_DIR, f"rob{client_id}_task.csv")
    if not os.path.exists(filepath):
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(["task_id", "task_status"])
    with open(filepath, 'a', newline='', encoding='utf-8') as f:
        csv.writer(f).writerow(["TASK0001", "task_cancelled"])
    logger.info(f"Fleet triggered task_cancelled for {robot_id}")
    return {"status": "ok", "robot_id": robot_id, "command": "task_cancelled"}

# =============================================================================
# Endpoints — Communication  (wildcard /{robot_id}/… routes LAST)
# =============================================================================

POLL_TIMEOUT_SEC = 60
POLL_SLEEP_SEC   = 0.5

@app.get("/api/robot/{robot_id}/communication")
async def get_robot_communication(robot_id: str, long_poll: bool = True):
    path = _comm_path(robot_id)
    if long_poll:
        initial  = read_communication_row(path)
        last_key = (initial.get("timestamp"), initial.get("acknowledgement"), initial.get("fleet_response"))
        for _ in range(int(POLL_TIMEOUT_SEC / POLL_SLEEP_SEC)):
            await asyncio.sleep(POLL_SLEEP_SEC)
            row = read_communication_row(path)
            key = (row.get("timestamp"), row.get("acknowledgement"), row.get("fleet_response"))
            if key != last_key:
                return row
    return read_communication_row(path)

@app.post("/api/robot/{robot_id}/communication")
async def robot_write_acknowledgement(robot_id: str, body: RobotCommAck):
    path = _comm_path(robot_id)
    ack  = (body.acknowledgement or "").strip()
    if not ack:
        write_communication_row(path, acknowledgement="", fleet_response="")
    else:
        write_communication_row(path, acknowledgement=ack)
    return {"status": "ok", "robot_id": robot_id}

@app.post("/api/fleet/robot/{robot_id}/communication")
async def fleet_write_robot_fleet_response(robot_id: str, body: FleetResponseBody):
    write_communication_row(_comm_path(robot_id), fleet_response=body.fleet_response)
    return {"status": "ok", "robot_id": robot_id}

@app.get("/api/dispatcher/{disp_id}/communication")
async def get_dispatcher_communication(disp_id: str, long_poll: bool = True):
    path = _disp_comm_path(disp_id)
    if long_poll:
        initial  = read_communication_row(path)
        last_key = (initial.get("timestamp"), initial.get("acknowledgement"), initial.get("fleet_response"))
        for _ in range(int(POLL_TIMEOUT_SEC / POLL_SLEEP_SEC)):
            await asyncio.sleep(POLL_SLEEP_SEC)
            row = read_communication_row(path)
            key = (row.get("timestamp"), row.get("acknowledgement"), row.get("fleet_response"))
            if key != last_key:
                return row
    return read_communication_row(path)

@app.post("/api/fleet/dispatcher/{disp_id}/communication")
async def fleet_write_dispatcher_fleet_response(disp_id: str, body: FleetResponseBody):
    write_communication_row(_disp_comm_path(disp_id), fleet_response=body.fleet_response)
    return {"status": "ok", "disp_id": disp_id}

@app.get("/api/printing/{print_id}/communication")
async def get_printing_communication(print_id: str, long_poll: bool = True):
    path = _print_comm_path(print_id)
    if long_poll:
        initial  = read_communication_row(path)
        last_key = (initial.get("timestamp"), initial.get("acknowledgement"), initial.get("fleet_response"))
        for _ in range(int(POLL_TIMEOUT_SEC / POLL_SLEEP_SEC)):
            await asyncio.sleep(POLL_SLEEP_SEC)
            row = read_communication_row(path)
            key = (row.get("timestamp"), row.get("acknowledgement"), row.get("fleet_response"))
            if key != last_key:
                return row
    return read_communication_row(path)

@app.post("/api/fleet/printing/{print_id}/communication")
async def fleet_write_printing_fleet_response(print_id: str, body: FleetResponseBody):
    write_communication_row(_print_comm_path(print_id), fleet_response=body.fleet_response)
    return {"status": "ok", "print_id": print_id}

@app.post("/api/robot/{robot_id}/status")
async def robot_status_update(robot_id: str, status: Dict[str, Any]):
    robot_state_manager.update_robot_status(robot_id, status)
    return {"status": "ok"}

@app.get("/api/robots/status")
async def get_all_robots_status():
    return {"robots": robot_state_manager.get_all_robots_status(), "total_robots": len(robot_state_manager.robots)}

@app.get("/api/robot/{robot_id}/status")
async def get_robot_status(robot_id: str):
    state = robot_state_manager.get_robot_state(robot_id)
    return {
        "robot_id":       robot_id,
        "connected":      robot_state_manager.is_robot_connected(robot_id),
        "mode":           state["current_mode"],
        "last_heartbeat": state["last_heartbeat"].isoformat() if state["last_heartbeat"] else None,
        "status":         state["status"],
        "program_loaded": state["program_filename"],
        "command_csv":    os.path.join(CSV_BASE_DIR, f"{robot_id}_command.csv")
    }

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

@app.get("/")
async def root():
    return {
        "service":          "Enmac Host Server - Multi-Robot Control",
        "csv_base_dir":     CSV_BASE_DIR,
        "connected_robots": list(robot_state_manager.robots.keys()),
    }