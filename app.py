from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import sqlite3, requests, json, os, hashlib, secrets
from datetime import datetime
from functools import wraps

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "cambia-en-produccion-" + secrets.token_hex(8))

DB = "faq.db"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")


# ── DB ─────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS empresas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                -- Cuenta
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                -- Paso 1: Identidad
                nombre TEXT NOT NULL,
                sector TEXT,
                descripcion TEXT,
                -- Paso 2: Comunicación
                tono TEXT DEFAULT 'profesional',
                idiomas TEXT DEFAULT 'Español',
                nombre_agente TEXT DEFAULT 'Asistente',
                saludo_inicial TEXT,
                -- Paso 3: Operativa
                horario TEXT,
                zonas_servicio TEXT,
                tiempo_respuesta TEXT,
                -- Paso 4: Productos/Servicios
                productos TEXT,
                precios_info TEXT,
                -- Paso 5: Políticas
                politica_devolucion TEXT,
                politica_envio TEXT,
                garantias TEXT,
                -- Paso 6: Contacto
                email_contacto TEXT,
                telefono TEXT,
                web TEXT,
                direccion TEXT,
                -- Meta
                onboarding_completo INTEGER DEFAULT 0,
                creada_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS categorias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                empresa_id INTEGER NOT NULL,
                nombre TEXT NOT NULL,
                icono TEXT DEFAULT '💬',
                orden INTEGER DEFAULT 0,
                FOREIGN KEY (empresa_id) REFERENCES empresas(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS faqs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                empresa_id INTEGER NOT NULL,
                categoria_id INTEGER NOT NULL,
                pregunta TEXT NOT NULL,
                respuesta TEXT NOT NULL,
                activa INTEGER DEFAULT 1,
                creada_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (empresa_id) REFERENCES empresas(id) ON DELETE CASCADE,
                FOREIGN KEY (categoria_id) REFERENCES categorias(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS historial (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                empresa_id INTEGER NOT NULL,
                pregunta TEXT NOT NULL,
                respuesta TEXT NOT NULL,
                fue_ia INTEGER DEFAULT 0,
                creada_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (empresa_id) REFERENCES empresas(id) ON DELETE CASCADE
            );
        """)

init_db()


# ── Auth helpers ───────────────────────────────────────────────────────────────

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("empresa_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def get_empresa():
    with get_db() as db:
        return db.execute("SELECT * FROM empresas WHERE id=?", (session["empresa_id"],)).fetchone()


# ── Ollama ─────────────────────────────────────────────────────────────────────

def construir_contexto(empresa):
    partes = []
    partes.append(f"Eres {empresa['nombre_agente'] or 'el asistente'} de {empresa['nombre']}.")
    if empresa['descripcion']:
        partes.append(f"La empresa: {empresa['descripcion']}")
    if empresa['sector']:
        partes.append(f"Sector: {empresa['sector']}")
    if empresa['tono']:
        partes.append(f"Tono de comunicación: {empresa['tono']}")
    if empresa['idiomas']:
        partes.append(f"Idiomas disponibles: {empresa['idiomas']}")
    if empresa['horario']:
        partes.append(f"Horario de atención: {empresa['horario']}")
    if empresa['zonas_servicio']:
        partes.append(f"Zonas de servicio: {empresa['zonas_servicio']}")
    if empresa['tiempo_respuesta']:
        partes.append(f"Tiempo de respuesta humana: {empresa['tiempo_respuesta']}")
    if empresa['productos']:
        partes.append(f"Productos/servicios: {empresa['productos']}")
    if empresa['precios_info']:
        partes.append(f"Información de precios: {empresa['precios_info']}")
    if empresa['politica_devolucion']:
        partes.append(f"Política de devolución: {empresa['politica_devolucion']}")
    if empresa['politica_envio']:
        partes.append(f"Política de envío: {empresa['politica_envio']}")
    if empresa['garantias']:
        partes.append(f"Garantías: {empresa['garantias']}")
    if empresa['email_contacto']:
        partes.append(f"Email de contacto: {empresa['email_contacto']}")
    if empresa['telefono']:
        partes.append(f"Teléfono: {empresa['telefono']}")
    if empresa['web']:
        partes.append(f"Web: {empresa['web']}")
    if empresa['direccion']:
        partes.append(f"Dirección: {empresa['direccion']}")

    contexto_empresa = "\n".join(partes)

    with get_db() as db:
        faqs = db.execute("""
            SELECT c.nombre as cat, f.pregunta, f.respuesta
            FROM faqs f JOIN categorias c ON f.categoria_id = c.id
            WHERE f.empresa_id=? AND f.activa=1
            ORDER BY c.orden, f.id
        """, (empresa['id'],)).fetchall()

    faq_lines = []
    if faqs:
        faq_lines.append("\nBASE DE CONOCIMIENTO (preguntas frecuentes):")
        cat_actual = None
        for f in faqs:
            if f['cat'] != cat_actual:
                cat_actual = f['cat']
                faq_lines.append(f"\n[{cat_actual}]")
            faq_lines.append(f"P: {f['pregunta']}")
            faq_lines.append(f"R: {f['respuesta']}")

    return contexto_empresa + "\n".join(faq_lines)

def chat_ollama(empresa_id, pregunta):
    with get_db() as db:
        empresa = db.execute("SELECT * FROM empresas WHERE id=?", (empresa_id,)).fetchone()
    contexto = construir_contexto(empresa)
    system = f"""{contexto}

INSTRUCCIONES:
- Responde siempre en el tono indicado y en los idiomas configurados.
- Usa la base de conocimiento como referencia principal.
- Si no tienes información suficiente, indica amablemente que el equipo contactará pronto.
- Sé conciso y útil. No inventes datos como precios o fechas que no estén en el contexto."""

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": pregunta}],
        "stream": False
    }
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=60)
        r.raise_for_status()
        return r.json()["message"]["content"], True
    except requests.exceptions.ConnectionError:
        return "El asistente IA no está disponible en este momento. Por favor, consulta las preguntas frecuentes o contacta con nosotros directamente.", False
    except Exception as e:
        return f"Error al conectar con el asistente: {str(e)}", False


# ── Rutas públicas del chat ────────────────────────────────────────────────────

@app.route("/chat/<int:empresa_id>")
def chat_publico(empresa_id):
    with get_db() as db:
        empresa = db.execute("SELECT * FROM empresas WHERE id=?", (empresa_id,)).fetchone()
        if not empresa:
            return "Empresa no encontrada", 404
        categorias = db.execute("SELECT * FROM categorias WHERE empresa_id=? ORDER BY orden", (empresa_id,)).fetchall()
        faqs = db.execute("""
            SELECT f.*, c.nombre as cat_nombre, c.icono as cat_icono
            FROM faqs f JOIN categorias c ON f.categoria_id=c.id
            WHERE f.empresa_id=? AND f.activa=1 ORDER BY c.orden, f.id
        """, (empresa_id,)).fetchall()
    return render_template("chat.html", empresa=empresa, categorias=categorias, faqs=faqs)

@app.route("/api/chat/<int:empresa_id>", methods=["POST"])
def api_chat(empresa_id):
    data = request.get_json()
    pregunta = data.get("pregunta", "").strip()
    if not pregunta:
        return jsonify({"error": "Pregunta vacía"}), 400
    respuesta, fue_ia = chat_ollama(empresa_id, pregunta)
    with get_db() as db:
        db.execute("INSERT INTO historial (empresa_id, pregunta, respuesta, fue_ia) VALUES (?,?,?,?)",
                   (empresa_id, pregunta, respuesta, 1 if fue_ia else 0))
        db.commit()
    return jsonify({"respuesta": respuesta, "fue_ia": fue_ia})


# ── Auth ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if session.get("empresa_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        with get_db() as db:
            empresa = db.execute("SELECT * FROM empresas WHERE email=? AND password_hash=?",
                                 (email, hash_pw(pw))).fetchone()
        if empresa:
            session["empresa_id"] = empresa["id"]
            session["empresa_nombre"] = empresa["nombre"]
            if not empresa["onboarding_completo"]:
                return redirect(url_for("onboarding", paso=1))
            return redirect(url_for("dashboard"))
        error = "Email o contraseña incorrectos"
    return render_template("login.html", error=error)

@app.route("/registro", methods=["GET", "POST"])
def registro():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        nombre = request.form.get("nombre", "").strip()
        if not email or not pw or not nombre:
            error = "Todos los campos son obligatorios"
        elif len(pw) < 6:
            error = "La contraseña debe tener al menos 6 caracteres"
        else:
            try:
                with get_db() as db:
                    db.execute("INSERT INTO empresas (email, password_hash, nombre) VALUES (?,?,?)",
                               (email, hash_pw(pw), nombre))
                    db.commit()
                    empresa = db.execute("SELECT * FROM empresas WHERE email=?", (email,)).fetchone()
                session["empresa_id"] = empresa["id"]
                session["empresa_nombre"] = empresa["nombre"]
                return redirect(url_for("onboarding", paso=1))
            except sqlite3.IntegrityError:
                error = "Ya existe una cuenta con ese email"
    return render_template("registro.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Onboarding ─────────────────────────────────────────────────────────────────

PASOS_ONBOARDING = [
    {"num": 1, "titulo": "Identidad de la empresa",    "icono": "🏢"},
    {"num": 2, "titulo": "Comunicación y tono",         "icono": "💬"},
    {"num": 3, "titulo": "Operativa y disponibilidad",  "icono": "🕐"},
    {"num": 4, "titulo": "Productos y servicios",       "icono": "📦"},
    {"num": 5, "titulo": "Políticas",                   "icono": "📋"},
    {"num": 6, "titulo": "Datos de contacto",           "icono": "📞"},
]

@app.route("/onboarding/<int:paso>", methods=["GET", "POST"])
@login_required
def onboarding(paso):
    empresa = get_empresa()
    if request.method == "POST":
        campos = {}
        if paso == 1:
            campos = {"sector": request.form.get("sector",""), "descripcion": request.form.get("descripcion","")}
        elif paso == 2:
            campos = {"tono": request.form.get("tono","profesional"), "idiomas": request.form.get("idiomas","Español"),
                      "nombre_agente": request.form.get("nombre_agente","Asistente"), "saludo_inicial": request.form.get("saludo_inicial","")}
        elif paso == 3:
            campos = {"horario": request.form.get("horario",""), "zonas_servicio": request.form.get("zonas_servicio",""),
                      "tiempo_respuesta": request.form.get("tiempo_respuesta","")}
        elif paso == 4:
            campos = {"productos": request.form.get("productos",""), "precios_info": request.form.get("precios_info","")}
        elif paso == 5:
            campos = {"politica_devolucion": request.form.get("politica_devolucion",""),
                      "politica_envio": request.form.get("politica_envio",""), "garantias": request.form.get("garantias","")}
        elif paso == 6:
            campos = {"email_contacto": request.form.get("email_contacto",""), "telefono": request.form.get("telefono",""),
                      "web": request.form.get("web",""), "direccion": request.form.get("direccion","")}

        if campos:
            set_clause = ", ".join(f"{k}=?" for k in campos)
            with get_db() as db:
                db.execute(f"UPDATE empresas SET {set_clause} WHERE id=?", (*campos.values(), session["empresa_id"]))
                db.commit()

        if paso == 6 or request.form.get("saltar") == "1":
            with get_db() as db:
                db.execute("UPDATE empresas SET onboarding_completo=1 WHERE id=?", (session["empresa_id"],))
                # Crear categorías de ejemplo
                count = db.execute("SELECT COUNT(*) as c FROM categorias WHERE empresa_id=?", (session["empresa_id"],)).fetchone()["c"]
                if count == 0:
                    db.execute("INSERT INTO categorias (empresa_id, nombre, icono, orden) VALUES (?,?,?,?)", (session["empresa_id"],"General","💬",1))
                db.commit()
            return redirect(url_for("dashboard"))

        if paso < 6:
            return redirect(url_for("onboarding", paso=paso+1))

    empresa = get_empresa()
    return render_template("onboarding.html", empresa=empresa, paso=paso, pasos=PASOS_ONBOARDING)


# ── Dashboard / Admin ──────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    empresa = get_empresa()
    with get_db() as db:
        stats = {
            "faqs": db.execute("SELECT COUNT(*) as c FROM faqs WHERE empresa_id=? AND activa=1", (empresa["id"],)).fetchone()["c"],
            "cats": db.execute("SELECT COUNT(*) as c FROM categorias WHERE empresa_id=?", (empresa["id"],)).fetchone()["c"],
            "chats": db.execute("SELECT COUNT(*) as c FROM historial WHERE empresa_id=?", (empresa["id"],)).fetchone()["c"],
            "hoy": db.execute("SELECT COUNT(*) as c FROM historial WHERE empresa_id=? AND date(creada_en)=date('now')", (empresa["id"],)).fetchone()["c"],
        }
        categorias = db.execute("SELECT * FROM categorias WHERE empresa_id=? ORDER BY orden", (empresa["id"],)).fetchall()
        faqs = db.execute("""
            SELECT f.*, c.nombre as cat_nombre, c.icono as cat_icono
            FROM faqs f JOIN categorias c ON f.categoria_id=c.id
            WHERE f.empresa_id=? ORDER BY c.orden, f.id
        """, (empresa["id"],)).fetchall()
    return render_template("dashboard.html", empresa=empresa, stats=stats, categorias=categorias, faqs=faqs)

@app.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    empresa = get_empresa()
    guardado = False
    if request.method == "POST":
        campos = {
            "nombre": request.form.get("nombre","").strip(),
            "sector": request.form.get("sector",""), "descripcion": request.form.get("descripcion",""),
            "tono": request.form.get("tono",""), "idiomas": request.form.get("idiomas",""),
            "nombre_agente": request.form.get("nombre_agente",""), "saludo_inicial": request.form.get("saludo_inicial",""),
            "horario": request.form.get("horario",""), "zonas_servicio": request.form.get("zonas_servicio",""),
            "tiempo_respuesta": request.form.get("tiempo_respuesta",""),
            "productos": request.form.get("productos",""), "precios_info": request.form.get("precios_info",""),
            "politica_devolucion": request.form.get("politica_devolucion",""),
            "politica_envio": request.form.get("politica_envio",""), "garantias": request.form.get("garantias",""),
            "email_contacto": request.form.get("email_contacto",""), "telefono": request.form.get("telefono",""),
            "web": request.form.get("web",""), "direccion": request.form.get("direccion",""),
        }
        set_clause = ", ".join(f"{k}=?" for k in campos)
        with get_db() as db:
            db.execute(f"UPDATE empresas SET {set_clause} WHERE id=?", (*campos.values(), session["empresa_id"]))
            db.commit()
        session["empresa_nombre"] = campos["nombre"]
        guardado = True
        empresa = get_empresa()
    return render_template("perfil.html", empresa=empresa, guardado=guardado)

# CRUD Categorías
@app.route("/categorias", methods=["POST"])
@login_required
def crear_categoria():
    nombre = request.form.get("nombre","").strip()
    icono = request.form.get("icono","💬")
    if nombre:
        with get_db() as db:
            max_ord = db.execute("SELECT MAX(orden) as m FROM categorias WHERE empresa_id=?", (session["empresa_id"],)).fetchone()["m"] or 0
            db.execute("INSERT INTO categorias (empresa_id, nombre, icono, orden) VALUES (?,?,?,?)",
                       (session["empresa_id"], nombre, icono, max_ord+1))
            db.commit()
    return redirect(url_for("dashboard"))

@app.route("/categorias/<int:cid>/editar", methods=["POST"])
@login_required
def editar_categoria(cid):
    nombre = request.form.get("nombre","").strip()
    icono = request.form.get("icono","💬")
    if nombre:
        with get_db() as db:
            db.execute("UPDATE categorias SET nombre=?, icono=? WHERE id=? AND empresa_id=?",
                       (nombre, icono, cid, session["empresa_id"]))
            db.commit()
    return redirect(url_for("dashboard"))

@app.route("/categorias/<int:cid>/borrar", methods=["POST"])
@login_required
def borrar_categoria(cid):
    with get_db() as db:
        db.execute("DELETE FROM categorias WHERE id=? AND empresa_id=?", (cid, session["empresa_id"]))
        db.commit()
    return redirect(url_for("dashboard"))

# CRUD FAQs
@app.route("/faqs", methods=["POST"])
@login_required
def crear_faq():
    cat_id = request.form.get("categoria_id")
    pregunta = request.form.get("pregunta","").strip()
    respuesta = request.form.get("respuesta","").strip()
    if cat_id and pregunta and respuesta:
        with get_db() as db:
            db.execute("INSERT INTO faqs (empresa_id, categoria_id, pregunta, respuesta) VALUES (?,?,?,?)",
                       (session["empresa_id"], cat_id, pregunta, respuesta))
            db.commit()
    return redirect(url_for("dashboard"))

@app.route("/faqs/<int:fid>/editar", methods=["POST"])
@login_required
def editar_faq(fid):
    cat_id = request.form.get("categoria_id")
    pregunta = request.form.get("pregunta","").strip()
    respuesta = request.form.get("respuesta","").strip()
    activa = 1 if request.form.get("activa") else 0
    with get_db() as db:
        db.execute("UPDATE faqs SET categoria_id=?, pregunta=?, respuesta=?, activa=? WHERE id=? AND empresa_id=?",
                   (cat_id, pregunta, respuesta, activa, fid, session["empresa_id"]))
        db.commit()
    return redirect(url_for("dashboard"))

@app.route("/faqs/<int:fid>/toggle", methods=["POST"])
@login_required
def toggle_faq(fid):
    with get_db() as db:
        db.execute("UPDATE faqs SET activa=CASE WHEN activa=1 THEN 0 ELSE 1 END WHERE id=? AND empresa_id=?",
                   (fid, session["empresa_id"]))
        db.commit()
    return redirect(url_for("dashboard"))

@app.route("/faqs/<int:fid>/borrar", methods=["POST"])
@login_required
def borrar_faq(fid):
    with get_db() as db:
        db.execute("DELETE FROM faqs WHERE id=? AND empresa_id=?", (fid, session["empresa_id"]))
        db.commit()
    return redirect(url_for("dashboard"))

@app.route("/historial")
@login_required
def historial():
    empresa = get_empresa()
    with get_db() as db:
        registros = db.execute(
            "SELECT * FROM historial WHERE empresa_id=? ORDER BY creada_en DESC LIMIT 200",
            (empresa["id"],)
        ).fetchall()
    return render_template("historial.html", empresa=empresa, registros=registros)

@app.route("/historial/borrar", methods=["POST"])
@login_required
def borrar_historial():
    with get_db() as db:
        db.execute("DELETE FROM historial WHERE empresa_id=?", (session["empresa_id"],))
        db.commit()
    return redirect(url_for("historial"))

if __name__ == "__main__":
    app.run(debug=True, port=5000)
