"""
公司项目管理看板 - FastAPI 后端
项目分售前 / 售中 / 售后三个阶段，以时间线维度管理项目。
商务属性 + 会议记录(含文件) + 售中计划骨架 + 后向供应商 + 风险点。
"""
import os
import re
import json
import uuid
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from contextlib import contextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "dashboard.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")

app = FastAPI(title="公司项目看板 API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

STAGES = ("presale", "sale", "aftersale")
STAGE_LABELS = {"presale": "售前", "sale": "售中（PM主责）", "aftersale": "售后"}
CATEGORIES = ("platform", "hybrid", "hardware")
CATEGORY_LABELS = {"platform": "平台类", "hybrid": "硬件平台混合", "hardware": "硬件类"}
PROCUREMENT_METHODS = ("公开询比", "直接采购", "公开招标", "原子能力下单")
SEVERITIES = ("high", "medium", "low")

EVENT_TYPES = {
    "presale": ["线索", "方案", "投标", "谈判", "签约"],
    "sale": ["项目采购", "后向合同签订", "制定实施计划", "硬件到货", "项目实施", "项目验收", "项目交维"],
    "aftersale": ["回款", "运维", "续签", "关闭"],
}
MEETING_TYPES = {
    "tech_review": ("技术评审会", "tech_review_at", "tech_review_file"),
    "project_review": ("项目评审会", "project_review_at", "project_review_file"),
    "kickoff": ("交底会", "kickoff_at", "kickoff_file"),
}
HARDWARE_STEP = "硬件到货"


# ==================== DB ====================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_context():
    conn = get_db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(conn, table, column, ddl):
    if column not in _cols(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def sale_steps(category):
    steps = ["项目采购", "后向合同签订", "制定实施计划"]
    if category in ("hybrid", "hardware"):
        steps.append(HARDWARE_STEP)
    steps += ["项目实施", "项目验收", "项目交维"]
    return steps


def ensure_sale_skeleton(conn, project_id, category, force=False):
    """为售中项目补齐标准计划骨架（event_type 为标准步骤名、is_sale_step=1）。"""
    existing = {
        r["event_type"]
        for r in conn.execute(
            "SELECT event_type FROM milestones WHERE project_id=? AND is_sale_step=1",
            (project_id,),
        ).fetchall()
    }
    steps = sale_steps(category)
    order = 0
    for step in steps:
        if step not in existing:
            conn.execute(
                """INSERT INTO milestones
                   (project_id, time, text, stage, event_type, description, is_done, is_sale_step, sort_order)
                   VALUES (?, '', ?, 'sale', ?, '', 0, 1, ?)""",
                (project_id, step, step, order),
            )
        order += 1
    if force:
        keep = set(steps)
        # 硬件步骤被移除：其下待办迁移为未分配
        for row in conn.execute(
            "SELECT id FROM milestones WHERE project_id=? AND is_sale_step=1 AND event_type=?",
            (project_id, HARDWARE_STEP),
        ).fetchall():
            if HARDWARE_STEP not in keep:
                conn.execute("UPDATE todos SET milestone_id=NULL WHERE milestone_id=?", (row["id"],))
                conn.execute("DELETE FROM milestones WHERE id=?", (row["id"],))
        conn.execute(
            "UPDATE milestones SET sort_order = CASE event_type "
            + " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(steps))
            + " END WHERE project_id=? AND is_sale_step=1",
            (project_id,),
        )


def init_db():
    with db_context() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                customer TEXT DEFAULT '',
                pm TEXT DEFAULT '',
                stage TEXT DEFAULT 'sale',
                status TEXT DEFAULT 'progress',
                status_text TEXT DEFAULT '进行中',
                color TEXT DEFAULT '#00d4ff',
                created_at TEXT,
                progress INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                text TEXT NOT NULL,
                completed INTEGER DEFAULT 0,
                progress INTEGER DEFAULT 0,
                priority TEXT DEFAULT 'medium',
                completed_at TEXT,
                completion_desc TEXT,
                due_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS progress_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                todo_id INTEGER NOT NULL,
                time TEXT NOT NULL,
                progress_date TEXT,
                text TEXT NOT NULL,
                FOREIGN KEY (todo_id) REFERENCES todos(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                time TEXT NOT NULL,
                text TEXT NOT NULL,
                stage TEXT DEFAULT 'sale',
                event_type TEXT DEFAULT '节点',
                description TEXT DEFAULT '',
                is_done INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                procurement_method TEXT DEFAULT '其他',
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS risks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                description TEXT NOT NULL,
                severity TEXT DEFAULT 'medium',
                resolved INTEGER DEFAULT 0,
                resolution_note TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_todos_project ON todos(project_id);
            CREATE INDEX IF NOT EXISTS idx_logs_todo ON progress_logs(todo_id);
            CREATE INDEX IF NOT EXISTS idx_milestones_project ON milestones(project_id);
            CREATE INDEX IF NOT EXISTS idx_suppliers_project ON suppliers(project_id);
            CREATE INDEX IF NOT EXISTS idx_risks_project ON risks(project_id);
            """
        )

        # projects 兼容列
        for col, ddl in [
            ("category", "category TEXT DEFAULT 'platform'"),
            ("description", "description TEXT DEFAULT ''"),
            ("amount", "amount REAL DEFAULT 0"),
            ("ops_team", "ops_team TEXT DEFAULT ''"),
            ("profit_margin", "profit_margin REAL DEFAULT 0"),
            ("tech_review_at", "tech_review_at TEXT DEFAULT ''"),
            ("tech_review_file", "tech_review_file TEXT DEFAULT ''"),
            ("project_review_at", "project_review_at TEXT DEFAULT ''"),
            ("project_review_file", "project_review_file TEXT DEFAULT ''"),
            ("kickoff_at", "kickoff_at TEXT DEFAULT ''"),
            ("kickoff_file", "kickoff_file TEXT DEFAULT ''"),
            ("early_implementation", "early_implementation INTEGER DEFAULT 0"),
            ("completed", "completed INTEGER DEFAULT 0"),
            ("completed_at", "completed_at TEXT DEFAULT ''"),
        ]:
            _add_column(conn, "projects", col, ddl)
        # milestones 兼容列
        for col, ddl in [
            ("is_sale_step", "is_sale_step INTEGER DEFAULT 0"),
            ("sort_order", "sort_order INTEGER DEFAULT 0"),
        ]:
            _add_column(conn, "milestones", col, ddl)
        _add_column(conn, "todos", "milestone_id", "milestone_id INTEGER")
        _add_column(conn, "suppliers", "supply_content", "supply_content TEXT DEFAULT ''")

        # 为所有售中项目补齐骨架（幂等）
        rows = conn.execute(
            "SELECT id, category FROM projects WHERE deleted=0 AND stage='sale'"
        ).fetchall()
        for r in rows:
            ensure_sale_skeleton(conn, r["id"], r["category"] or "platform")


# ==================== 模型 ====================

class ProjectIn(BaseModel):
    name: str
    customer: str = ""
    pm: str = ""
    stage: str = "sale"
    status: str = "progress"
    status_text: str = "进行中"
    category: str = "platform"
    description: str = ""
    amount: float = 0
    ops_team: str = ""
    profit_margin: float = 0
    early_implementation: int = 0
    completed: int = 0


class ProjectPatch(BaseModel):
    name: Optional[str] = None
    customer: Optional[str] = None
    pm: Optional[str] = None
    stage: Optional[str] = None
    status: Optional[str] = None
    status_text: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    amount: Optional[float] = None
    ops_team: Optional[str] = None
    profit_margin: Optional[float] = None
    early_implementation: Optional[int] = None
    completed: Optional[int] = None


class TodoIn(BaseModel):
    project_id: Optional[str] = None
    text: str
    priority: str = "medium"
    due_date: Optional[str] = None
    milestone_id: Optional[int] = None


class SupplierIn(BaseModel):
    name: str
    procurement_method: str = "公开询比"
    supply_content: str = ""


class SupplierPatch(BaseModel):
    name: str | None = None
    procurement_method: str | None = None
    supply_content: str | None = None


class RiskIn(BaseModel):
    description: str
    severity: str = "medium"


class RiskPatch(BaseModel):
    description: Optional[str] = None
    severity: Optional[str] = None
    resolved: Optional[int] = None
    resolution_note: Optional[str] = None


class TimelineIn(BaseModel):
    project_id: str
    text: str
    time: str = ""
    stage: str = "sale"
    event_type: str = "节点"
    description: str = ""
    is_done: int = 0


class TimelinePatch(BaseModel):
    text: Optional[str] = None
    time: Optional[str] = None
    stage: Optional[str] = None
    event_type: Optional[str] = None
    description: Optional[str] = None
    is_done: Optional[int] = None


class MeetingsIn(BaseModel):
    tech_review_at: Optional[str] = ""
    project_review_at: Optional[str] = ""
    kickoff_at: Optional[str] = ""


# ==================== 序列化 ====================

def _serialize_project(conn, r, include_todos=True):
    p = dict(r)
    cat = p.get("category") or "platform"
    p["category_label"] = CATEGORY_LABELS.get(cat, cat)
    p["stage_label"] = STAGE_LABELS.get(p.get("stage"), p.get("stage"))
    p["suppliers"] = [dict(x) for x in conn.execute(
        "SELECT * FROM suppliers WHERE project_id=? ORDER BY id", (p["id"],)).fetchall()]
    p["risks"] = [dict(x) for x in conn.execute(
        "SELECT * FROM risks WHERE project_id=? ORDER BY resolved ASC, id DESC", (p["id"],)).fetchall()]

    # 时间线：骨架步骤优先按 sort_order，其余按时间/创建排序
    events = conn.execute(
        "SELECT * FROM milestones WHERE project_id=? ORDER BY is_sale_step DESC, sort_order ASC, time ASC, id ASC",
        (p["id"],),
    ).fetchall()
    timeline = [dict(e) for e in events]
    p["timeline"] = timeline
    p["current_step"] = next(
        (e["event_type"] for e in timeline if e.get("is_sale_step") and not e["is_done"]),
        None,
    )

    if include_todos:
        todos = [dict(t) for t in conn.execute(
            "SELECT * FROM todos WHERE project_id=? ORDER BY completed ASC, created_at DESC",
            (p["id"],)).fetchall()]
        for t in todos:
            t["progress_logs"] = [dict(l) for l in conn.execute(
                "SELECT * FROM progress_logs WHERE todo_id=? ORDER BY time DESC",
                (t["id"],)).fetchall()]
        p["todos"] = todos
        p["progress"] = int(round(sum(t["progress"] for t in todos) / len(todos))) if todos else 0
    else:
        p["todos"] = []

    # 会议信息
    p["meetings"] = {
        mtype: {"date": p.get(date_col) or "", "file": p.get(file_col) or "", "label": label}
        for mtype, (label, date_col, file_col) in MEETING_TYPES.items()
    }
    return p


# ==================== 启动 ====================

@app.on_event("startup")
async def startup():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    init_db()
    static_path = os.path.join(BASE_DIR, "static")
    if os.path.exists(static_path):
        app.mount("/static", StaticFiles(directory=static_path), name="static")
        app.mount("/", StaticFiles(directory=static_path, html=True), name="root")


# ==================== 项目 ====================

@app.get("/api/projects")
def get_projects(stage: Optional[str] = None):
    with db_context() as conn:
        sql = "SELECT * FROM projects WHERE deleted=0"
        params = []
        if stage in STAGES:
            sql += " AND stage=?"; params.append(stage)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
        return [_serialize_project(conn, r) for r in rows]


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    with db_context() as conn:
        r = conn.execute("SELECT * FROM projects WHERE id=? AND deleted=0", (project_id,)).fetchone()
        if not r:
            raise HTTPException(404, "project not found")
        return _serialize_project(conn, r)


@app.post("/api/projects")
def create_project(data: ProjectIn):
    stage = data.stage if data.stage in STAGES else "sale"
    category = data.category if data.category in CATEGORIES else "platform"
    pid = f"proj_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    with db_context() as conn:
        conn.execute(
            """INSERT INTO projects
               (id,name,customer,pm,stage,status,status_text,color,created_at,
                category,description,amount,ops_team,profit_margin,
                early_implementation,completed,completed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid, data.name, data.customer, data.pm, stage, data.status, data.status_text,
             "#00d4ff", datetime.now().isoformat(), category, data.description,
             data.amount, data.ops_team, data.profit_margin,
             1 if data.early_implementation else 0,
             1 if data.completed else 0,
             datetime.now().isoformat() if data.completed else ""),
        )
        if stage == "sale":
            ensure_sale_skeleton(conn, pid, category)
    return {"id": pid, "stage": stage, "category": category}


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, data: ProjectPatch):
    fields, params = [], []
    simple = ("name", "customer", "pm", "status", "status_text",
              "description", "ops_team")
    for key in ("early_implementation", "completed"):
        v = getattr(data, key)
        if v is not None:
            fields.append(f"{key}=?"); params.append(1 if v else 0)
            if key == "completed":
                fields.append("completed_at=?")
                params.append(datetime.now().isoformat() if v else "")
    for key in simple:
        v = getattr(data, key)
        if v is not None:
            fields.append(f"{key}=?"); params.append(v)
    if data.amount is not None:
        fields.append("amount=?"); params.append(data.amount)
    if data.profit_margin is not None:
        fields.append("profit_margin=?"); params.append(data.profit_margin)

    new_category = None
    if data.category is not None and data.category in CATEGORIES:
        fields.append("category=?"); params.append(data.category)
        new_category = data.category

    with db_context() as conn:
        row = conn.execute("SELECT stage FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(404, "project not found")

        if data.stage is not None and data.stage in STAGES:
            fields.append("stage=?"); params.append(data.stage)

        if fields:
            params.append(project_id)
            conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id=?", params)

        # 进入售中 -> 生成骨架；在售中改分类 -> 同步硬件步骤
        target_stage = data.stage or row["stage"]
        if target_stage == "sale":
            cat = new_category or (conn.execute(
                "SELECT category FROM projects WHERE id=?", (project_id,)
            ).fetchone()["category"] or "platform")
            ensure_sale_skeleton(conn, project_id, cat, force=bool(new_category))
    return {"ok": True}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    with db_context() as conn:
        conn.execute("UPDATE projects SET deleted=1 WHERE id=?", (project_id,))
    return {"ok": True}


# ==================== 待办 ====================

@app.post("/api/projects/{project_id}/todos")
def create_todo(project_id: str, data: TodoIn):
    due = data.due_date or datetime.now().isoformat()
    with db_context() as conn:
        cur = conn.execute(
            "INSERT INTO todos (project_id,text,priority,due_date,milestone_id) VALUES (?,?,?,?,?)",
            (project_id, data.text, data.priority, due, data.milestone_id),
        )
        return {"id": cur.lastrowid}


@app.put("/api/todos/{todo_id}")
def update_todo(todo_id: int, data: dict):
    with db_context() as conn:
        fields, params = [], []
        for key in ("text", "priority", "due_date", "milestone_id", "completion_desc"):
            if key in data:
                fields.append(f"{key}=?"); params.append(data[key])
        if "progress" in data:
            fields.append("progress=?"); params.append(data["progress"])
        if "completed" in data:
            if data["completed"]:
                completed_at = data.get("completed_at") or datetime.now().isoformat()
                fields += ["completed=?", "progress=?", "completed_at=?"]
                params += [1, 100, completed_at]
            else:
                fields += ["completed=?", "completed_at=?"]
                params += [0, None]
        if fields:
            params.append(todo_id)
            conn.execute(f"UPDATE todos SET {', '.join(fields)} WHERE id=?", params)
    return {"ok": True}


@app.delete("/api/todos/{todo_id}")
def delete_todo(todo_id: int):
    with db_context() as conn:
        conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
    return {"ok": True}


@app.post("/api/todos/{todo_id}/progress")
def add_progress_log(todo_id: int, data: dict):
    with db_context() as conn:
        cur = conn.execute(
            "INSERT INTO progress_logs (todo_id,time,progress_date,text) VALUES (?,?,?,?)",
            (todo_id, datetime.now().isoformat(), data.get("progress_date"), data.get("text", "")),
        )
        return {"ok": True, "log_id": cur.lastrowid}


@app.delete("/api/progress-logs/{log_id}")
def delete_progress_log(log_id: int):
    with db_context() as conn:
        conn.execute("DELETE FROM progress_logs WHERE id=?", (log_id,))
    return {"ok": True}


# ==================== 时间线 ====================

@app.post("/api/timeline")
def create_timeline(data: TimelineIn):
    stage = data.stage if data.stage in STAGES else "sale"
    with db_context() as conn:
        cur = conn.execute(
            """INSERT INTO milestones
               (project_id,time,text,stage,event_type,description,is_done,is_sale_step)
               VALUES (?,?,?,?,?,?,?,0)""",
            (data.project_id, data.time, data.text, stage,
             data.event_type, data.description, 1 if data.is_done else 0),
        )
        return {"ok": True, "id": cur.lastrowid}


@app.put("/api/timeline/{event_id}")
def update_timeline(event_id: int, data: TimelinePatch):
    fields, params = [], []
    for key in ("text", "time", "event_type", "description"):
        v = getattr(data, key)
        if v is not None:
            fields.append(f"{key}=?"); params.append(v)
    if data.stage is not None and data.stage in STAGES:
        fields.append("stage=?"); params.append(data.stage)
    if data.is_done is not None:
        fields.append("is_done=?"); params.append(1 if data.is_done else 0)
    if fields:
        params.append(event_id)
        with db_context() as conn:
            conn.execute(f"UPDATE milestones SET {', '.join(fields)} WHERE id=?", params)
    return {"ok": True}


@app.delete("/api/timeline/{event_id}")
def delete_timeline(event_id: int):
    with db_context() as conn:
        row = conn.execute("SELECT is_sale_step FROM milestones WHERE id=?", (event_id,)).fetchone()
        if row and row["is_sale_step"]:
            raise HTTPException(400, "售中计划骨架步骤不可删除，可标记完成")
        conn.execute("UPDATE todos SET milestone_id=NULL WHERE milestone_id=?", (event_id,))
        conn.execute("DELETE FROM milestones WHERE id=?", (event_id,))
    return {"ok": True}


# ==================== 供应商 ====================

@app.post("/api/projects/{project_id}/suppliers")
def add_supplier(project_id: str, data: SupplierIn):
    method = data.procurement_method if data.procurement_method in PROCUREMENT_METHODS else "公开询比"
    with db_context() as conn:
        cur = conn.execute(
            "INSERT INTO suppliers (project_id,name,procurement_method,supply_content) VALUES (?,?,?,?)",
            (project_id, data.name, method, (data.supply_content or "").strip()),
        )
        return {"id": cur.lastrowid}


@app.patch("/api/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, data: SupplierPatch):
    fields, params = [], []
    if data.name is not None and data.name.strip():
        fields.append("name=?"); params.append(data.name.strip())
    if data.procurement_method is not None:
        method = data.procurement_method if data.procurement_method in PROCUREMENT_METHODS else "公开询比"
        fields.append("procurement_method=?"); params.append(method)
    if data.supply_content is not None:
        fields.append("supply_content=?"); params.append(data.supply_content.strip())
    if not fields:
        return {"ok": True}
    params.append(supplier_id)
    with db_context() as conn:
        conn.execute(f"UPDATE suppliers SET {','.join(fields)} WHERE id=?", params)
    return {"ok": True}


@app.delete("/api/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int):
    with db_context() as conn:
        conn.execute("DELETE FROM suppliers WHERE id=?", (supplier_id,))
    return {"ok": True}


# ==================== 风险 ====================

@app.post("/api/projects/{project_id}/risks")
def add_risk(project_id: str, data: RiskIn):
    sev = data.severity if data.severity in SEVERITIES else "medium"
    with db_context() as conn:
        cur = conn.execute(
            "INSERT INTO risks (project_id,description,severity) VALUES (?,?,?)",
            (project_id, data.description, sev),
        )
        return {"id": cur.lastrowid}


@app.put("/api/risks/{risk_id}")
def update_risk(risk_id: int, data: RiskPatch):
    fields, params = [], []
    if data.description is not None:
        fields.append("description=?"); params.append(data.description)
    if data.severity is not None and data.severity in SEVERITIES:
        fields.append("severity=?"); params.append(data.severity)
    if data.resolution_note is not None:
        fields.append("resolution_note=?"); params.append(data.resolution_note)
    if data.resolved is not None:
        fields.append("resolved=?"); params.append(1 if data.resolved else 0)
        fields.append("resolved_at=?")
        params.append(datetime.now().isoformat() if data.resolved else None)
    if fields:
        params.append(risk_id)
        with db_context() as conn:
            conn.execute(f"UPDATE risks SET {', '.join(fields)} WHERE id=?", params)
    return {"ok": True}


@app.delete("/api/risks/{risk_id}")
def delete_risk(risk_id: int):
    with db_context() as conn:
        conn.execute("DELETE FROM risks WHERE id=?", (risk_id,))
    return {"ok": True}


# ==================== 会议 ====================

@app.put("/api/projects/{project_id}/meetings")
def update_meetings(project_id: str, data: MeetingsIn):
    fields, params = [], []
    for key in ("tech_review_at", "project_review_at", "kickoff_at"):
        v = getattr(data, key)
        if v is not None:
            fields.append(f"{key}=?"); params.append(v)
    if fields:
        params.append(project_id)
        with db_context() as conn:
            conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id=?", params)
    return {"ok": True}


@app.post("/api/projects/{project_id}/meetings/{mtype}/file")
async def upload_meeting_file(project_id: str, mtype: str, file: UploadFile = File(...)):
    if mtype not in MEETING_TYPES:
        raise HTTPException(400, "unknown meeting type")
    _, _, file_col = MEETING_TYPES[mtype]
    suffix = os.path.splitext(file.filename or "")[1]
    safe_name = f"{project_id}_{mtype}_{uuid.uuid4().hex}{suffix}"
    rel_path = f"uploads/{safe_name}"
    abs_path = os.path.join(UPLOAD_DIR, safe_name)
    with open(abs_path, "wb") as f:
        f.write(await file.read())
    with db_context() as conn:
        # 删除旧文件
        old = conn.execute(
            f"SELECT {file_col} FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if old and old[0]:
            old_abs = os.path.join(BASE_DIR, "static", old[0])
            if old_abs.startswith(UPLOAD_DIR) and os.path.exists(old_abs):
                try:
                    os.remove(old_abs)
                except OSError:
                    pass
        conn.execute(
            f"UPDATE projects SET {file_col}=? WHERE id=?", (rel_path, project_id)
        )
    return {"ok": True, "path": f"/static/{rel_path}", "name": file.filename}


@app.delete("/api/projects/{project_id}/meetings/{mtype}/file")
def delete_meeting_file(project_id: str, mtype: str):
    if mtype not in MEETING_TYPES:
        raise HTTPException(400, "unknown meeting type")
    _, _, file_col = MEETING_TYPES[mtype]
    with db_context() as conn:
        row = conn.execute(
            f"SELECT {file_col} FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if row and row[0]:
            abs_path = os.path.join(BASE_DIR, "static", row[0])
            if abs_path.startswith(UPLOAD_DIR) and os.path.exists(abs_path):
                try:
                    os.remove(abs_path)
                except OSError:
                    pass
        conn.execute(f"UPDATE projects SET {file_col}='' WHERE id=?", (project_id,))
    return {"ok": True}



# ==================== AI 解析评审材料 ====================

PARSE_PROMPT = """你是一名项目管理助手。请阅读这份项目评审会材料，并从中提取以下信息，**只输出一个 JSON 对象**，不要输出任何解释、Markdown 或代码块。

JSON 结构：
{
  "project": {
    "name": "项目名称，无法判断留空字符串",
    "customer": "前向客户/甲方名称",
    "pm": "项目经理姓名",
    "category": "platform 或 hybrid 或 hardware（平台类/硬件平台混合/硬件类）",
    "amount": 项目金额，单位为人民币元，数字；无法判断填 0,
    "profit_margin": 利润率，数字（如25表示25%）；无法判断填 0,
    "ops_team": "运营运维承接组名称",
    "early_implementation": true或false,
    "completed": true或false,
    "description": "项目内容简介，简短一段话"
  },
  "suppliers": [ {"name": "后向供应商名称", "procurement_method": "公开询比 或 直接采购 或 公开招标 或 原子能力下单", "supply_content": "向该供应商采购的具体内容/供货范围，如服务器设备、网络设备、软件平台、实施服务等，简短描述，未知留空"} ],
  "meetings": {
    "tech_review_at": "技术评审会日期 YYYY-MM-DD，未知留空",
    "project_review_at": "项目评审会日期 YYYY-MM-DD，未知留空",
    "kickoff_at": "交底会日期 YYYY-MM-DD，未知留空"
  },
  "risks": [ {"description": "风险点描述", "severity": "high 或 medium 或 low"} ],
  "todos": [ {"text": "待办事项", "priority": "high或medium或low", "milestone": "所属售中步骤名，如项目采购/后向合同签订/制定实施计划/硬件到货/项目实施/项目验收/项目交维，无法判断留空", "due_date": "YYYY-MM-DD或空"} ]
}

规则：金额若材料写“万元”，请换算成元（乘以10000）。没有的项：数组留空 []，字符串留空 ""，数字填 0。只输出 JSON。"""


def _normalize_category(v):
    if not v:
        return ""
    v = str(v).strip().lower()
    if v in CATEGORIES:
        return v
    mapping = {"平台": "platform", "平台类": "platform", "软件": "platform",
               "混合": "hybrid", "软硬一体": "hybrid", "硬件平台混合": "hybrid",
               "硬件": "hardware", "硬件类": "hardware"}
    return mapping.get(v, "")


def _normalize_method(v):
    if not v:
        return ""
    v = str(v).strip()
    for m in PROCUREMENT_METHODS:
        if m in v:
            return m
    if "原子" in v or "能力下单" in v:
        return "原子能力下单"
    if "询比" in v or "询" in v:
        return "公开询比"
    if "直接" in v:
        return "直接采购"
    if "招标" in v:
        return "公开招标"
    return ""


def _normalize_severity(v):
    v = str(v or "").strip().lower()
    if v in SEVERITIES:
        return v
    if "高" in v:
        return "high"
    if "低" in v:
        return "low"
    return "medium" if v else ""


def _normalize_date(v):
    if not v:
        return ""
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", str(v))
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _prepare_asset(abs_path, suffix, workdir):
    """把输入文件转换成 arkcli 支持的 PDF/图片，返回可发送的绝对路径。"""
    ext = suffix.lower()
    if ext in (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"):
        return abs_path
    if ext in (".doc", ".docx", ".rtf", ".txt", ".html", ".htm"):
        txt_path = os.path.join(workdir, "source.txt")
        try:
            subprocess.run(["textutil", "-convert", "txt", abs_path,
                            "-output", txt_path], check=True,
                           capture_output=True, timeout=30)
        except Exception:
            if ext == ".txt":
                shutil.copy(abs_path, txt_path)
            else:
                raise HTTPException(400, f"无法转换文件类型 {ext} 为文本")
        pdf_path = os.path.join(workdir, "source.pdf")
        r = subprocess.run(["cupsfilter", txt_path], capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
            with open(pdf_path, "wb") as f:
                f.write(r.stdout)
        if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
            raise HTTPException(400, "无法把文档转换为 PDF")
        return pdf_path
    raise HTTPException(400, f"不支持的文件类型：{ext}")


def _run_arkcli(asset_path):
    cmd = [
        "arkcli", "+chat", PARSE_PROMPT,
        "--input", f"@{asset_path}",
        "--text-format", "json_object",
        "--no-progress",
        "--format", "json",
        "--max-output-tokens", "4000",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "AI 解析超时，请稍后重试")
    except FileNotFoundError:
        raise HTTPException(500, "未找到 arkcli，请确认已安装并登录")
    out = (proc.stdout or "").strip()
    if not out:
        raise HTTPException(502, f"AI 无返回：{(proc.stderr or '').strip()[:300]}")
    try:
        data = json.loads(out)
    except Exception:
        raise HTTPException(502, f"AI 返回不是有效 JSON：{out[:300]}")
    if data.get("ok") is False:
        msg = (data.get("error") or {}).get("message", "arkcli 调用失败")
        if "not logged" in msg.lower() or "auth" in msg.lower():
            raise HTTPException(401, "arkcli 未登录，请在服务器上执行 arkcli auth login")
        raise HTTPException(502, f"AI 调用失败：{msg[:300]}")
    content = data.get("content") or ""
    try:
        return json.loads(content)
    except Exception:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        raise HTTPException(502, f"无法解析 AI 返回内容：{content[:300]}")


@app.post("/api/projects/{project_id}/parse-review")
async def parse_review(project_id: str, file: UploadFile = File(None)):
    with db_context() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id=? AND deleted=0", (project_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "项目不存在")
        stored_file = row["project_review_file"] or ""

    tmp_upload = None
    if file is not None and file.filename:
        suffix = os.path.splitext(file.filename)[1]
        fd, tmp_upload = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(await file.read())
        source_path = tmp_upload
        source_suffix = suffix
    elif stored_file:
        source_path = os.path.join(BASE_DIR, "static", stored_file)
        source_suffix = os.path.splitext(stored_file)[1]
        if not os.path.exists(source_path):
            raise HTTPException(400, "已保存的评审材料文件不存在")
    else:
        raise HTTPException(400, "请先上传项目评审会材料")

    workdir = tempfile.mkdtemp(prefix="parse_")
    try:
        asset = _prepare_asset(source_path, source_suffix, workdir)
        parsed = _run_arkcli(asset)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if tmp_upload and os.path.exists(tmp_upload):
            os.remove(tmp_upload)

    # 归一化 + 匹配 milestone
    p = parsed.get("project") or {}
    category = _normalize_category(p.get("category"))
    proj = {
        "name": (str(p.get("name") or "")).strip(),
        "customer": (str(p.get("customer") or "")).strip(),
        "pm": (str(p.get("pm") or "")).strip(),
        "category": category,
        "amount": p.get("amount") or 0,
        "profit_margin": p.get("profit_margin") or 0,
        "ops_team": (str(p.get("ops_team") or "")).strip(),
        "early_implementation": 1 if p.get("early_implementation") else 0,
        "completed": 1 if p.get("completed") else 0,
        "description": (str(p.get("description") or "")).strip(),
    }
    try:
        proj["amount"] = float(proj["amount"])
    except (TypeError, ValueError):
        proj["amount"] = 0
    try:
        proj["profit_margin"] = float(proj["profit_margin"])
    except (TypeError, ValueError):
        proj["profit_margin"] = 0

    suppliers = []
    for s in (parsed.get("suppliers") or []):
        if not isinstance(s, dict):
            continue
        name = (str(s.get("name") or "")).strip()
        if not name:
            continue
        method = _normalize_method(s.get("procurement_method"))
        content = (str(s.get("supply_content") or s.get("content") or "")).strip()
        suppliers.append({"name": name, "procurement_method": method or "公开询比", "supply_content": content})

    m = parsed.get("meetings") or {}
    meetings = {
        "tech_review_at": _normalize_date(m.get("tech_review_at")),
        "project_review_at": _normalize_date(m.get("project_review_at")),
        "kickoff_at": _normalize_date(m.get("kickoff_at")),
    }

    risks = []
    for r in (parsed.get("risks") or []):
        if not isinstance(r, dict):
            continue
        desc = (str(r.get("description") or "")).strip()
        if not desc:
            continue
        sev = _normalize_severity(r.get("severity")) or "medium"
        risks.append({"description": desc, "severity": sev})

    # milestone 名称 -> id
    with db_context() as conn:
        steps = {
            row["event_type"]: row["id"]
            for row in conn.execute(
                "SELECT id, event_type FROM milestones WHERE project_id=? AND is_sale_step=1",
                (project_id,),
            ).fetchall()
        }
    todos = []
    for t in (parsed.get("todos") or []):
        if not isinstance(t, dict):
            continue
        text = (str(t.get("text") or "")).strip()
        if not text:
            continue
        ms_name = (str(t.get("milestone") or "")).strip()
        mid = steps.get(ms_name)
        pri = (str(t.get("priority") or "medium")).strip().lower()
        if pri not in SEVERITIES:
            pri = "medium"
        todos.append({
            "text": text,
            "priority": pri,
            "milestone": ms_name,
            "milestone_id": mid,
            "due_date": _normalize_date(t.get("due_date")),
        })

    return {
        "ok": True,
        "project": proj,
        "suppliers": suppliers,
        "meetings": meetings,
        "risks": risks,
        "todos": todos,
    }


# ==================== 统计/元数据 ====================

@app.get("/api/stats")
def get_stats():
    with db_context() as conn:
        stage_counts = {
            s: conn.execute(
                "SELECT COUNT(*) FROM projects WHERE deleted=0 AND stage=?", (s,)).fetchone()[0]
            for s in STAGES
        }
        total = conn.execute("SELECT COUNT(*) FROM projects WHERE deleted=0").fetchone()[0]
        active_projects = conn.execute("SELECT COUNT(*) FROM projects WHERE deleted=0 AND completed=0").fetchone()[0]
        completed_projects = conn.execute("SELECT COUNT(*) FROM projects WHERE deleted=0 AND completed=1").fetchone()[0]
        total_todos = conn.execute("SELECT COUNT(*) FROM todos t JOIN projects p ON p.id=t.project_id WHERE p.deleted=0").fetchone()[0]
        done_todos = conn.execute("SELECT COUNT(*) FROM todos t JOIN projects p ON p.id=t.project_id WHERE p.deleted=0 AND t.completed=1").fetchone()[0]
        open_risks = conn.execute("SELECT COUNT(*) FROM risks r JOIN projects p ON p.id=r.project_id WHERE p.deleted=0 AND r.resolved=0").fetchone()[0]
        overdue = conn.execute(
            """SELECT COUNT(*) FROM milestones
               WHERE is_sale_step=1 AND is_done=0 AND time<>'' AND time<?""",
            (datetime.now().date().isoformat(),),
        ).fetchone()[0]
        upcoming = conn.execute(
            """SELECT m.*, p.name AS project_name FROM milestones m
               JOIN projects p ON p.id=m.project_id
               WHERE p.deleted=0 AND m.is_sale_step=1 AND m.is_done=0 AND m.time<>''
               ORDER BY m.time ASC LIMIT 8"""
        ).fetchall()
        return {
            "total_projects": total,
            "active_projects": active_projects,
            "completed_projects": completed_projects,
            "stage_counts": stage_counts,
            "total_todos": total_todos,
            "completed_todos": done_todos,
            "pending_todos": total_todos - done_todos,
            "risk_count": open_risks,
            "overdue_count": overdue,
            "upcoming_events": [dict(e) for e in upcoming],
        }


@app.get("/api/meta")
def get_meta():
    return {
        "stages": [{"key": k, "label": STAGE_LABELS[k]} for k in STAGES],
        "categories": [{"key": k, "label": CATEGORY_LABELS[k]} for k in CATEGORIES],
        "procurement_methods": list(PROCUREMENT_METHODS),
        "severities": list(SEVERITIES),
        "event_types": EVENT_TYPES,
        "meetings": {k: v[0] for k, v in MEETING_TYPES.items()},
        "sale_steps": {c: sale_steps(c) for c in CATEGORIES},
    }
