import os
import uuid
import sqlite3
import json
import io
import base64
from datetime import datetime
from pathlib import Path
from typing import Optional

import qrcode
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

BASE_DIR = Path(__file__).parent
app = FastAPI(title="Retro Tool")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# ─── Database ────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(BASE_DIR / "retro.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            phase TEXT DEFAULT 'lobby',
            created_at TEXT,
            dot_budget INTEGER DEFAULT 5,
            timer_seconds INTEGER DEFAULT 0,
            timer_started_at TEXT,
            timer_running INTEGER DEFAULT 0,
            show_live_feed INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS participants (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            joined_at TEXT
        );
        CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            participant_id TEXT,
            category TEXT,
            text TEXT,
            merged_into TEXT,
            deleted INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS votes (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            card_id TEXT,
            participant_id TEXT,
            dots INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS action_items (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            text TEXT,
            owner TEXT,
            done INTEGER DEFAULT 0,
            created_at TEXT
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ─── WebSocket Manager ───────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.connections: dict[str, list[WebSocket]] = {}

    async def connect(self, session_id: str, ws: WebSocket):
        await ws.accept()
        self.connections.setdefault(session_id, []).append(ws)

    def disconnect(self, session_id: str, ws: WebSocket):
        if session_id in self.connections:
            try: self.connections[session_id].remove(ws)
            except: pass

    async def broadcast(self, session_id: str, message: dict):
        dead = []
        for ws in self.connections.get(session_id, []):
            try: await ws.send_text(json.dumps(message))
            except: dead.append(ws)
        for ws in dead:
            try: self.connections[session_id].remove(ws)
            except: pass

manager = ConnectionManager()

# ─── Helpers ─────────────────────────────────────────────────────────────────

def new_id(): return str(uuid.uuid4())[:8]

def get_session(session_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    conn.close()
    if not row: raise HTTPException(404, "Session not found")
    return dict(row)

def get_session_state(session_id: str):
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session: conn.close(); return None
    session = dict(session)

    participants = conn.execute(
        "SELECT COUNT(*) as cnt FROM participants WHERE session_id=?", (session_id,)
    ).fetchone()["cnt"]

    cards = [dict(c) for c in conn.execute(
        "SELECT * FROM cards WHERE session_id=? AND deleted=0 AND merged_into IS NULL ORDER BY text ASC",
        (session_id,)
    ).fetchall()]

    merged_map = {}
    for m in conn.execute(
        "SELECT * FROM cards WHERE session_id=? AND deleted=0 AND merged_into IS NOT NULL",
        (session_id,)
    ).fetchall():
        m = dict(m)
        merged_map.setdefault(m["merged_into"], []).append(m["text"])

    votes = [dict(v) for v in conn.execute(
        "SELECT card_id, participant_id, SUM(dots) as dots FROM votes WHERE session_id=? GROUP BY card_id, participant_id",
        (session_id,)
    ).fetchall()]

    vote_map = {}
    for v in votes:
        cid = v["card_id"]
        if cid not in vote_map: vote_map[cid] = {"total": 0, "voters": {}}
        vote_map[cid]["total"] += v["dots"]
        vote_map[cid]["voters"][v["participant_id"]] = v["dots"]

    for c in cards:
        c["votes"] = vote_map.get(c["id"], {"total": 0, "voters": {}})
        c["merged"] = merged_map.get(c["id"], [])

    action_items = [dict(a) for a in conn.execute(
        "SELECT * FROM action_items WHERE session_id=? ORDER BY created_at ASC", (session_id,)
    ).fetchall()]

    conn.close()
    return {
        "session": session,
        "participant_count": participants,
        "cards": cards,
        "action_items": action_items,
    }

# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/")
async def sessions_overview():
    return FileResponse(BASE_DIR / "templates" / "index.html")

@app.get("/admin/{session_id}")
async def admin_view(session_id: str):
    return FileResponse(BASE_DIR / "templates" / "admin.html")

@app.get("/join/{session_id}")
async def participant_view(session_id: str):
    return FileResponse(BASE_DIR / "templates" / "participant.html")

# ─── Sessions API ─────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def list_sessions():
    conn = get_db()
    rows = conn.execute(
        "SELECT s.*, COUNT(DISTINCT p.id) as participant_count, COUNT(DISTINCT c.id) as card_count "
        "FROM sessions s "
        "LEFT JOIN participants p ON p.session_id = s.id "
        "LEFT JOIN cards c ON c.session_id = s.id AND c.deleted=0 "
        "GROUP BY s.id ORDER BY s.created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/sessions")
async def create_session(data: dict):
    sid = new_id()
    conn = get_db()
    conn.execute(
        "INSERT INTO sessions (id, name, phase, created_at, dot_budget) VALUES (?,?,?,?,?)",
        (sid, data.get("name", "Retrospektive"), "lobby", datetime.now().isoformat(), int(data.get("dot_budget", 5)))
    )
    conn.commit(); conn.close()
    return {"id": sid}

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    conn = get_db()
    for tbl in ["votes","cards","participants","action_items","sessions"]:
        col = "session_id" if tbl != "sessions" else "id"
        conn.execute(f"DELETE FROM {tbl} WHERE {col}=?", (session_id,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.get("/api/sessions/{session_id}")
async def get_session_api(session_id: str):
    state = get_session_state(session_id)
    if not state: raise HTTPException(404, "Not found")
    return state

@app.get("/api/sessions/{session_id}/qr")
async def get_qr(session_id: str, request: None = None, base_url: str = "http://localhost:8000"):
    url = f"{base_url}/join/{session_id}"
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(url); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO(); img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"qr": f"data:image/png;base64,{b64}", "url": url}

@app.post("/api/sessions/{session_id}/phase")
async def set_phase(session_id: str, data: dict):
    conn = get_db()
    conn.execute("UPDATE sessions SET phase=? WHERE id=?", (data["phase"], session_id))
    conn.commit(); conn.close()
    state = get_session_state(session_id)
    await manager.broadcast(session_id, {"type": "state_update", "data": state})
    return {"ok": True}

# ─── Timer ───────────────────────────────────────────────────────────────────

@app.post("/api/sessions/{session_id}/timer")
async def set_timer(session_id: str, data: dict):
    action = data.get("action")
    conn = get_db()
    if action == "set":
        conn.execute("UPDATE sessions SET timer_seconds=?, timer_running=0, timer_started_at=NULL WHERE id=?",
                     (int(data.get("seconds", 300)), session_id))
    elif action == "start":
        conn.execute("UPDATE sessions SET timer_running=1, timer_started_at=? WHERE id=?",
                     (datetime.now().isoformat(), session_id))
    elif action == "pause":
        row = conn.execute("SELECT timer_seconds, timer_started_at FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row and row["timer_started_at"]:
            elapsed = (datetime.now() - datetime.fromisoformat(row["timer_started_at"])).seconds
            remaining = max(0, row["timer_seconds"] - elapsed)
            conn.execute("UPDATE sessions SET timer_seconds=?, timer_running=0, timer_started_at=NULL WHERE id=?",
                         (remaining, session_id))
    elif action == "reset":
        conn.execute("UPDATE sessions SET timer_seconds=0, timer_running=0, timer_started_at=NULL WHERE id=?",
                     (session_id,))
    conn.commit(); conn.close()
    
    # Nur bei set/pause/reset broadcasten, NICHT bei start
    if action != "start":
        state = get_session_state(session_id)
        await manager.broadcast(session_id, {"type": "state_update", "data": state})
    
    return {"ok": True}

# ─── Participants ─────────────────────────────────────────────────────────────

@app.post("/api/sessions/{session_id}/join")
async def join_session(session_id: str):
    get_session(session_id)
    pid = new_id()
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO participants (id, session_id, joined_at) VALUES (?,?,?)",
                 (pid, session_id, datetime.now().isoformat()))
    conn.commit(); conn.close()
    state = get_session_state(session_id)
    await manager.broadcast(session_id, {"type": "state_update", "data": state})
    return {"participant_id": pid}

# ─── Cards ───────────────────────────────────────────────────────────────────

@app.post("/api/sessions/{session_id}/cards")
async def add_card(session_id: str, data: dict):
    cid = new_id()
    conn = get_db()
    conn.execute(
        "INSERT INTO cards (id, session_id, participant_id, category, text, created_at) VALUES (?,?,?,?,?,?)",
        (cid, session_id, data["participant_id"], data["category"], data["text"], datetime.now().isoformat())
    )
    conn.commit(); conn.close()
    state = get_session_state(session_id)
    await manager.broadcast(session_id, {"type": "state_update", "data": state})
    return {"id": cid}

@app.patch("/api/sessions/{session_id}/cards/{card_id}")
async def edit_card(session_id: str, card_id: str, data: dict):
    conn = get_db()
    conn.execute("UPDATE cards SET text=? WHERE id=? AND session_id=?",
                 (data["text"], card_id, session_id))
    conn.commit(); conn.close()
    state = get_session_state(session_id)
    await manager.broadcast(session_id, {"type": "state_update", "data": state})
    return {"ok": True}

@app.delete("/api/sessions/{session_id}/cards/{card_id}")
async def delete_card(session_id: str, card_id: str):
    conn = get_db()
    conn.execute("UPDATE cards SET deleted=1 WHERE id=? AND session_id=?", (card_id, session_id))
    conn.commit(); conn.close()
    state = get_session_state(session_id)
    await manager.broadcast(session_id, {"type": "state_update", "data": state})
    return {"ok": True}

@app.post("/api/sessions/{session_id}/cards/{card_id}/merge")
async def merge_card(session_id: str, card_id: str, data: dict):
    conn = get_db()
    conn.execute("UPDATE cards SET merged_into=? WHERE id=? AND session_id=?",
                 (data["target_id"], card_id, session_id))
    conn.commit(); conn.close()
    state = get_session_state(session_id)
    await manager.broadcast(session_id, {"type": "state_update", "data": state})
    return {"ok": True}

# ─── Votes ───────────────────────────────────────────────────────────────────

@app.post("/api/sessions/{session_id}/vote")
async def vote(session_id: str, data: dict):
    pid = data["participant_id"]
    card_id = data["card_id"]
    action = data.get("action", "add")
    session = get_session(session_id)
    dot_budget = session["dot_budget"]
    conn = get_db()
    used = conn.execute(
        "SELECT COALESCE(SUM(dots),0) as total FROM votes WHERE session_id=? AND participant_id=?",
        (session_id, pid)
    ).fetchone()["total"]
    existing = conn.execute(
        "SELECT id, dots FROM votes WHERE session_id=? AND card_id=? AND participant_id=?",
        (session_id, card_id, pid)
    ).fetchone()
    if action == "add":
        if used >= dot_budget: conn.close(); return {"ok": False, "error": "No dots left", "remaining": 0}
        if existing: conn.execute("UPDATE votes SET dots=dots+1 WHERE id=?", (existing["id"],))
        else: conn.execute("INSERT INTO votes (id, session_id, card_id, participant_id, dots) VALUES (?,?,?,?,1)",
                           (new_id(), session_id, card_id, pid))
    elif action == "remove":
        if existing and existing["dots"] > 1: conn.execute("UPDATE votes SET dots=dots-1 WHERE id=?", (existing["id"],))
        elif existing: conn.execute("DELETE FROM votes WHERE id=?", (existing["id"],))
    conn.commit()
    remaining = dot_budget - conn.execute(
        "SELECT COALESCE(SUM(dots),0) as total FROM votes WHERE session_id=? AND participant_id=?",
        (session_id, pid)
    ).fetchone()["total"]
    conn.close()
    state = get_session_state(session_id)
    await manager.broadcast(session_id, {"type": "state_update", "data": state})
    return {"ok": True, "remaining": remaining}

@app.get("/api/sessions/{session_id}/my_votes/{participant_id}")
async def my_votes(session_id: str, participant_id: str):
    session = get_session(session_id)
    conn = get_db()
    used = conn.execute(
        "SELECT COALESCE(SUM(dots),0) as total FROM votes WHERE session_id=? AND participant_id=?",
        (session_id, participant_id)
    ).fetchone()["total"]
    my_v = conn.execute(
        "SELECT card_id, dots FROM votes WHERE session_id=? AND participant_id=?",
        (session_id, participant_id)
    ).fetchall()
    conn.close()
    return {"remaining": session["dot_budget"] - used, "votes": {v["card_id"]: v["dots"] for v in my_v}}

# ─── Action Items ─────────────────────────────────────────────────────────────

@app.post("/api/sessions/{session_id}/actions")
async def add_action(session_id: str, data: dict):
    aid = new_id()
    conn = get_db()
    conn.execute(
        "INSERT INTO action_items (id, session_id, text, owner, done, created_at) VALUES (?,?,?,?,0,?)",
        (aid, session_id, data["text"], data.get("owner", ""), datetime.now().isoformat())
    )
    conn.commit(); conn.close()
    state = get_session_state(session_id)
    await manager.broadcast(session_id, {"type": "state_update", "data": state})
    return {"id": aid}

@app.patch("/api/sessions/{session_id}/actions/{action_id}")
async def update_action(session_id: str, action_id: str, data: dict):
    conn = get_db()
    if "done" in data:
        conn.execute("UPDATE action_items SET done=? WHERE id=? AND session_id=?",
                     (1 if data["done"] else 0, action_id, session_id))
    if "text" in data:
        conn.execute("UPDATE action_items SET text=? WHERE id=? AND session_id=?",
                     (data["text"], action_id, session_id))
    if "owner" in data:
        conn.execute("UPDATE action_items SET owner=? WHERE id=? AND session_id=?",
                     (data["owner"], action_id, session_id))
    conn.commit(); conn.close()
    state = get_session_state(session_id)
    await manager.broadcast(session_id, {"type": "state_update", "data": state})
    return {"ok": True}

@app.delete("/api/sessions/{session_id}/actions/{action_id}")
async def delete_action(session_id: str, action_id: str):
    conn = get_db()
    conn.execute("DELETE FROM action_items WHERE id=? AND session_id=?", (action_id, session_id))
    conn.commit(); conn.close()
    state = get_session_state(session_id)
    await manager.broadcast(session_id, {"type": "state_update", "data": state})
    return {"ok": True}

# ─── PDF Export ───────────────────────────────────────────────────────────────

@app.get("/api/sessions/{session_id}/export/pdf")
async def export_pdf(session_id: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable

    from reportlab.lib.units import cm

    state = get_session_state(session_id)
    if not state: raise HTTPException(404, "Not found")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=2*cm, leftMargin=2*cm, topMargin=2.5*cm, bottomMargin=2*cm)

    INDIGO = colors.HexColor('#8252fe')
    YELLOW = colors.HexColor('#def471')
    BLACK  = colors.HexColor('#000000')
    LIGHT  = colors.HexColor('#f5f5f5')
    INDIGO_LIGHT = colors.HexColor('#e6dcff')
    GREEN  = colors.HexColor('#43e97b')
    MUTED  = colors.HexColor('#666666')

    title_style   = ParagraphStyle('title',   fontSize=26, textColor=INDIGO, fontName='Helvetica-Bold', spaceAfter=2, leading=30)
    date_style    = ParagraphStyle('date',    fontSize=9,  textColor=MUTED,  fontName='Helvetica', spaceAfter=12)
    section_style = ParagraphStyle('section', fontSize=12, textColor=INDIGO, fontName='Helvetica-Bold', spaceBefore=18, spaceAfter=8)
    body_style    = ParagraphStyle('body',    fontSize=9,  textColor=BLACK,  fontName='Helvetica', spaceAfter=2, leading=13)
    merged_style  = ParagraphStyle('merged',  fontSize=8,  textColor=MUTED,  fontName='Helvetica-Oblique', spaceAfter=2, leading=12, leftIndent=10)

    story = []
    story.append(Paragraph(state['session']['name'], title_style))
    dt = datetime.now().strftime('%d.%m.%Y %H:%M')
    story.append(Paragraph(f"Retrospektive  \u00b7  Exportiert am {dt}", date_style))
    story.append(HRFlowable(width="100%", thickness=2, color=INDIGO, spaceAfter=14))

    votable = [c for c in state['cards'] if c['category'] != 'keep']
    total_votes = sum(c['votes']['total'] for c in votable)
    stats = Table(
        [['Teilnehmende','Karten total','Dots vergeben','Dots/Person'],
         [str(state['participant_count']), str(len(state['cards'])), str(total_votes), str(state['session']['dot_budget'])]],
        colWidths=[4*cm]*4
    )
    stats.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),INDIGO),('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,0),9),
        ('BACKGROUND',(0,1),(-1,1),INDIGO_LIGHT),('FONTNAME',(0,1),(-1,1),'Helvetica-Bold'),('FONTSIZE',(0,1),(-1,1),16),
        ('ALIGN',(0,0),(-1,-1),'CENTER'),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),
        ('GRID',(0,0),(-1,-1),0.5,colors.white),
    ]))
    story.append(stats)

    def merged_cells(c):
        cell = [Paragraph(c['text'], body_style)]
        if c.get('merged'):
            cell.append(Paragraph("zusammengeführt mit:", ParagraphStyle('ml', fontSize=7, textColor=MUTED, fontName='Helvetica-Bold', leading=10)))
            for mt in c['merged']:
                cell.append(Paragraph(f"  – {mt}", merged_style))
        return cell

    sorted_votable = sorted(votable, key=lambda c: c['votes']['total'], reverse=True)
    if sorted_votable:
        story.append(Paragraph("Priorisierung – Stop & Improve", section_style))
        rows = [['#','Karte','Typ','Dots']]
        for i, c in enumerate(sorted_votable):
            rows.append([str(i+1), merged_cells(c), c['category'].upper(), str(c['votes']['total'])])
        t = Table(rows, colWidths=[1*cm, 11*cm, 2.2*cm, 1.8*cm])
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),INDIGO),('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,0),9),
            ('ALIGN',(0,0),(0,-1),'CENTER'),('ALIGN',(2,0),(3,-1),'CENTER'),
            ('FONTNAME',(0,1),(-1,-1),'Helvetica'),('FONTSIZE',(0,1),(-1,-1),9),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,LIGHT]),
            ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#dddddd')),
            ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),('VALIGN',(0,0),(-1,-1),'TOP'),
        ]))
        story.append(t)

    keep_cards = [c for c in state['cards'] if c['category'] == 'keep']
    if keep_cards:
        story.append(Paragraph("Keep – das läuft gut", section_style))
        rows = [['Karte']]
        for c in keep_cards:
            rows.append([merged_cells(c)])
        t = Table(rows, colWidths=[16*cm])
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),GREEN),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,0),9),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,LIGHT]),
            ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#dddddd')),
            ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),('VALIGN',(0,0),(-1,-1),'TOP'),
        ]))
        story.append(t)

    actions = state.get('action_items', [])
    if actions:
        story.append(Paragraph("Action Items", section_style))
        rows = [['☐','Massnahme','Verantwortlich']]
        for a in actions:
            done_sym = '☑' if a['done'] else '☐'
            rows.append([done_sym, Paragraph(a['text'], body_style), a.get('owner','') or '—'])
        t = Table(rows, colWidths=[0.8*cm, 11.2*cm, 4*cm])
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),INDIGO),('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,0),9),
            ('ALIGN',(0,0),(0,-1),'CENTER'),
            ('FONTNAME',(0,1),(-1,-1),'Helvetica'),('FONTSIZE',(0,1),(-1,-1),9),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,LIGHT]),
            ('GRID',(0,0),(-1,-1),0.3,colors.HexColor('#dddddd')),
            ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),('VALIGN',(0,0),(-1,-1),'TOP'),
        ]))
        story.append(t)

    doc.build(story)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf",
                             headers={"Content-Disposition": f"attachment; filename=retro-{session_id}.pdf"})

# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(session_id, websocket)
    try:
        state = get_session_state(session_id)
        await websocket.send_text(json.dumps({"type": "state_update", "data": state}))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(session_id, websocket)

# ─── Start ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
