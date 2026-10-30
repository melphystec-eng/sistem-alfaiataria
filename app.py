import os, sqlite3, secrets
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "sistema.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-mude-em-producao")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB

ESTADOS = ["pendente", "pronto", "entregue", "cancelado"]


# -------- Base de dados --------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db: db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS admin(
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            ativo INTEGER DEFAULT 1,
            is_super INTEGER DEFAULT 0,
            criado_por INTEGER,
            criado_em TEXT
        );
        CREATE TABLE IF NOT EXISTS noticias(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT NOT NULL,
            texto  TEXT NOT NULL,
            imagem TEXT,
            criada_em TEXT NOT NULL,
            visualizacoes INTEGER DEFAULT 0,
            likes    INTEGER DEFAULT 0,
            dislikes INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS fotos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titulo TEXT,
            imagem TEXT NOT NULL,
            criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pedidos(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo TEXT UNIQUE NOT NULL,
            cliente TEXT NOT NULL,
            contacto TEXT,
            descricao TEXT NOT NULL,
            preco REAL NOT NULL,
            estado TEXT NOT NULL DEFAULT 'pendente',
            criado_em TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            admin_id INTEGER,
            admin_user TEXT,
            acao TEXT NOT NULL,
            detalhe TEXT,
            ip TEXT
        );
    """)

    # --- migração para bases antigas ---
    cols = [r[1] for r in db.execute("PRAGMA table_info(admin)").fetchall()]
    for nome, ddl in [
        ("ativo",      "ALTER TABLE admin ADD COLUMN ativo INTEGER DEFAULT 1"),
        ("is_super",   "ALTER TABLE admin ADD COLUMN is_super INTEGER DEFAULT 0"),
        ("criado_por", "ALTER TABLE admin ADD COLUMN criado_por INTEGER"),
        ("criado_em",  "ALTER TABLE admin ADD COLUMN criado_em TEXT"),
    ]:
        if nome not in cols:
            db.execute(ddl)

    if db.execute("SELECT COUNT(*) FROM admin").fetchone()[0] == 0:
        db.execute(
            "INSERT INTO admin(username,password_hash,ativo,is_super,criado_em) "
            "VALUES (?,?,1,1,?)",
            ("admin", generate_password_hash("admin123"),
             datetime.now().isoformat(timespec="seconds")))

    if db.execute("SELECT COUNT(*) FROM admin WHERE is_super=1").fetchone()[0] == 0:
        db.execute("UPDATE admin SET is_super=1 WHERE id=(SELECT MIN(id) FROM admin)")

    db.commit()
    db.close()
    print("✅ Base de dados verificada/criada com sucesso!")


# -------- Helpers --------
def login_required(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get("admin_id"):
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrap

def gerar_codigo():
    return "PED-" + secrets.token_hex(3).upper()

def registar_log(acao, detalhe=""):
    """Grava uma linha na tabela logs (usa a sessão, se existir)."""
    db = get_db()
    db.execute(
        "INSERT INTO logs(data, admin_id, admin_user, acao, detalhe, ip) "
        "VALUES (?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"),
         session.get("admin_id"),
         session.get("admin_user") or "—",
         acao,
         detalhe,
         request.remote_addr if request else None))
    db.commit()


# ======================= ÁREA PÚBLICA =======================
@app.route("/")
def home():
    db = get_db()
    noticias = db.execute("SELECT * FROM noticias ORDER BY criada_em DESC").fetchall()
    return render_template("index.html", noticias=noticias)


@app.route("/noticia/<int:id>")
def ver_noticia(id):
    db = get_db()
    n = db.execute("SELECT * FROM noticias WHERE id=?", (id,)).fetchone()
    if not n: abort(404)
    db.execute("UPDATE noticias SET visualizacoes = visualizacoes + 1 WHERE id=?", (id,))
    db.commit()
    n = db.execute("SELECT * FROM noticias WHERE id=?", (id,)).fetchone()
    return render_template("noticia.html", n=n)


@app.route("/noticia/<int:id>/reagir/<acao>")
def reagir(id, acao):
    if acao not in ("like", "dislike"): abort(400)
    campo = "likes" if acao == "like" else "dislikes"
    db = get_db()
    db.execute(f"UPDATE noticias SET {campo} = {campo} + 1 WHERE id=?", (id,))
    db.commit()
    registar_log("visitante_reagiu", f"noticia_id={id} acao={acao}")
    return redirect(url_for("ver_noticia", id=id))


@app.route("/fotos")
def fotos():
    db = get_db()
    # Agora procura na tabela 'fotos' em vez de 'noticias'
    todas_fotos = db.execute("SELECT * FROM fotos ORDER BY criado_em DESC").fetchall()
    return render_template("fotos.html", fotos=todas_fotos)

@app.route("/consultar", methods=["GET", "POST"])
def consultar():
    pedido = None
    if request.method == "POST":
        codigo = request.form["codigo"].strip().upper()
        db = get_db()
        pedido = db.execute("SELECT * FROM pedidos WHERE codigo=?", (codigo,)).fetchone()
        if not pedido:
            flash("Pedido não encontrado. Verifique o código.")
    return render_template("consultar.html", pedido=pedido)


# ======================= LOGIN =======================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"]
        db = get_db()
        admin = db.execute("SELECT * FROM admin WHERE username=?", (u,)).fetchone()

        if not admin or not check_password_hash(admin["password_hash"], p):
            registar_log("login_falha", f"utilizador={u}")
            flash("Credenciais inválidas.")
            return render_template("login.html")

        if not admin["ativo"]:
            registar_log("login_bloqueado", f"utilizador={u}")
            flash("Conta bloqueada. Contacte o super administrador.")
            return render_template("login.html")

        session["admin_id"]   = admin["id"]
        session["admin_user"] = admin["username"]
        session["is_super"]   = admin["is_super"]
        registar_log("login", f"utilizador={u}")
        flash("Bem-vindo!")
        return redirect(url_for("admin_dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    if session.get("admin_id"):
        registar_log("logout", f"utilizador={session.get('admin_user')}")
    session.clear()
    flash("Sessão terminada.")
    return redirect(url_for("home"))


# ======================= PAINEL ADMIN =======================
@app.route("/admin")
@login_required
def admin_dashboard():
    db = get_db()
    stats = {
        "noticias":     db.execute("SELECT COUNT(*) c FROM noticias").fetchone()["c"],
        "pedidos":      db.execute("SELECT COUNT(*) c FROM pedidos").fetchone()["c"],
        "pendentes":    db.execute("SELECT COUNT(*) c FROM pedidos WHERE estado='pendente'").fetchone()["c"],
        "visualizacoes":db.execute("SELECT COALESCE(SUM(visualizacoes),0) c FROM noticias").fetchone()["c"],
    }
    ultimas = db.execute("SELECT * FROM noticias ORDER BY criada_em DESC LIMIT 5").fetchall()
    return render_template("admin_dashboard.html", stats=stats, ultimas=ultimas)


# -------- Notícias --------
@app.route("/admin/noticias")
@login_required
def admin_noticias():
    db = get_db()
    noticias = db.execute("SELECT * FROM noticias ORDER BY criada_em DESC").fetchall()
    return render_template("admin_noticias.html", noticias=noticias)


@app.route("/admin/noticias/nova", methods=["GET", "POST"])
@login_required
def admin_noticia_nova():
    if request.method == "POST":
        titulo = request.form["titulo"].strip()
        texto  = request.form["texto"].strip()
        imagem = None
        f = request.files.get("imagem")
        if f and f.filename:
            nome = f"{secrets.token_hex(4)}_{secure_filename(f.filename)}"
            f.save(os.path.join(UPLOAD_DIR, nome))
            imagem = nome
        db = get_db()
        db.execute("INSERT INTO noticias(titulo,texto,imagem,criada_em) VALUES (?,?,?,?)",
                   (titulo, texto, imagem, datetime.now().isoformat(timespec="seconds")))
        db.commit()
        registar_log("noticia_criada", f"titulo={titulo}")
        flash("Notícia publicada.")
        return redirect(url_for("admin_noticias"))
    return render_template("admin_noticia_form.html")


@app.route("/admin/noticias/apagar/<int:id>", methods=["POST"])
@login_required
def admin_noticia_apagar(id):
    db = get_db()
    n = db.execute("SELECT * FROM noticias WHERE id=?", (id,)).fetchone()
    if n and n["imagem"]:
        try: os.remove(os.path.join(UPLOAD_DIR, n["imagem"]))
        except OSError: pass
    db.execute("DELETE FROM noticias WHERE id=?", (id,))
    db.commit()
    registar_log("noticia_apagada", f"id={id} titulo={n['titulo'] if n else '?'}")
    flash("Notícia apagada.")
    return redirect(url_for("admin_noticias"))


# -------- Galeria de Fotos (Admin) --------
@app.route("/admin/galeria")
@login_required
def admin_galeria():
    db = get_db()
    fotos = db.execute("SELECT * FROM fotos ORDER BY criado_em DESC").fetchall()
    return render_template("admin_galeria.html", fotos=fotos)


@app.route("/admin/galeria/nova", methods=["GET", "POST"])
@login_required
def admin_galeria_nova():
    if request.method == "POST":
        titulo = request.form.get("titulo", "").strip()
        f = request.files.get("imagem")

        if not f or not f.filename:
            flash("Escolhe uma imagem válida.")
            return redirect(url_for("admin_galeria_nova"))

        nome = f"{secrets.token_hex(4)}_{secure_filename(f.filename)}"
        f.save(os.path.join(UPLOAD_DIR, nome))

        db = get_db()
        db.execute("INSERT INTO fotos(titulo, imagem, criado_em) VALUES (?,?,?)",
                   (titulo, nome, datetime.now().isoformat(timespec="seconds")))
        db.commit()
        registar_log("foto_galeria_criada", f"titulo={titulo}")
        flash("Foto adicionada à galeria.")
        return redirect(url_for("admin_galeria"))

    return render_template("admin_foto_form.html")


@app.route("/admin/galeria/apagar/<int:id>", methods=["POST"])
@login_required
def admin_galeria_apagar(id):
    db = get_db()
    f = db.execute("SELECT * FROM fotos WHERE id=?", (id,)).fetchone()
    if f:
        try:
            os.remove(os.path.join(UPLOAD_DIR, f["imagem"]))
        except OSError:
            pass

        db.execute("DELETE FROM fotos WHERE id=?", (id,))
        db.commit()
        registar_log("foto_galeria_apagada", f"id={id} titulo={f['titulo']}")
        flash("Foto apagada da galeria.")
    return redirect(url_for("admin_galeria"))

# -------- Pedidos --------
@app.route("/admin/pedidos")
@login_required
def admin_pedidos():
    db = get_db()
    pedidos = db.execute("SELECT * FROM pedidos ORDER BY criado_em DESC").fetchall()
    return render_template("admin_pedidos.html", pedidos=pedidos, estados=ESTADOS)


@app.route("/admin/pedidos/novo", methods=["GET", "POST"])
@login_required
def admin_pedido_novo():
    if request.method == "POST":
        cliente   = request.form["cliente"].strip()
        contacto  = request.form.get("contacto", "").strip()
        descricao = request.form["descricao"].strip()
        try:
            preco = float(request.form["preco"])
        except ValueError:
            flash("Preço inválido.")
            return redirect(url_for("admin_pedido_novo"))
        codigo = gerar_codigo()
        db = get_db()
        db.execute("""INSERT INTO pedidos(codigo,cliente,contacto,descricao,preco,estado,criado_em)
                      VALUES (?,?,?,?,?,?,?)""",
                   (codigo, cliente, contacto, descricao, preco, "pendente",
                    datetime.now().isoformat(timespec="seconds")))
        db.commit()
        registar_log("pedido_criado", f"codigo={codigo} cliente={cliente} preco={preco}")
        flash(f"Pedido registado. Código do cliente: {codigo}")
        return redirect(url_for("admin_pedidos"))
    return render_template("admin_pedido_form.html")


@app.route("/admin/pedidos/<int:id>/estado", methods=["POST"])
@login_required
def admin_pedido_estado(id):
    novo = request.form["estado"]
    if novo not in ESTADOS: abort(400)
    db = get_db()
    db.execute("UPDATE pedidos SET estado=? WHERE id=?", (novo, id))
    db.commit()
    registar_log("pedido_estado", f"id={id} novo_estado={novo}")
    flash("Estado atualizado.")
    return redirect(url_for("admin_pedidos"))


# -------- Senha --------
@app.route("/admin/senha", methods=["GET", "POST"])
@login_required
def admin_senha():
    if request.method == "POST":
        atual = request.form["atual"]
        nova  = request.form["nova"]
        conf  = request.form["confirmar"]
        if nova != conf:
            flash("A nova senha e a confirmação não coincidem.")
            return redirect(url_for("admin_senha"))
        if len(nova) < 6:
            flash("A senha deve ter pelo menos 6 caracteres.")
            return redirect(url_for("admin_senha"))
        db = get_db()
        admin = db.execute("SELECT * FROM admin WHERE id=?", (session["admin_id"],)).fetchone()
        if not check_password_hash(admin["password_hash"], atual):
            flash("Senha atual incorreta.")
            return redirect(url_for("admin_senha"))
        db.execute("UPDATE admin SET password_hash=? WHERE id=?",
                   (generate_password_hash(nova), admin["id"]))
        db.commit()
        registar_log("senha_alterada", f"utilizador={admin['username']}")
        flash("Senha alterada com sucesso.")
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_senha.html")


# -------- Gestão de Admins --------
@app.route("/admin/admins")
@login_required
def admin_admins():
    db = get_db()
    admins = db.execute("""
        SELECT a.*, c.username AS criado_por_nome
        FROM admin a LEFT JOIN admin c ON c.id = a.criado_por
        ORDER BY a.id
    """).fetchall()
    return render_template("admin_admins.html", admins=admins)


@app.route("/admin/admins/novo", methods=["GET", "POST"])
@login_required
def admin_admin_novo():
    if request.method == "POST":
        u = request.form["username"].strip()
        p = request.form["password"]
        c = request.form["confirmar"]
        if len(p) < 6:
            flash("A senha deve ter pelo menos 6 caracteres.")
            return redirect(url_for("admin_admin_novo"))
        if p != c:
            flash("As senhas não coincidem.")
            return redirect(url_for("admin_admin_novo"))
        db = get_db()
        if db.execute("SELECT 1 FROM admin WHERE username=?", (u,)).fetchone():
            flash("Nome de utilizador já existe.")
            return redirect(url_for("admin_admin_novo"))
        db.execute(
            "INSERT INTO admin(username,password_hash,ativo,is_super,criado_por,criado_em) "
            "VALUES (?,?,1,0,?,?)",
            (u, generate_password_hash(p), session["admin_id"],
             datetime.now().isoformat(timespec="seconds")))
        db.commit()
        registar_log("admin_criado", f"novo_admin={u}")
        flash(f"Administrador '{u}' criado.")
        return redirect(url_for("admin_admins"))
    return render_template("admin_admin_form.html")


@app.route("/admin/admins/<int:id>/bloquear", methods=["POST"])
@login_required
def admin_bloquear(id):
    db = get_db()
    alvo = db.execute("SELECT * FROM admin WHERE id=?", (id,)).fetchone()
    if not alvo: abort(404)

    if alvo["id"] == session["admin_id"]:
        flash("Não pode bloquear a sua própria conta.")
        return redirect(url_for("admin_admins"))
    if alvo["is_super"]:
        flash("O super administrador não pode ser bloqueado.")
        return redirect(url_for("admin_admins"))

    eu = db.execute("SELECT * FROM admin WHERE id=?", (session["admin_id"],)).fetchone()
    if not eu["is_super"] and alvo["criado_por"] != eu["id"]:
        flash("Só pode bloquear administradores que criou.")
        return redirect(url_for("admin_admins"))

    novo = 0 if alvo["ativo"] else 1
    db.execute("UPDATE admin SET ativo=? WHERE id=?", (novo, id))
    db.commit()

    acao = "admin_desbloqueio" if novo == 1 else "admin_bloqueio"
    registar_log(acao, f"alvo={alvo['username']}")
    flash(f"Admin '{alvo['username']}' {'desbloqueado' if novo==1 else 'bloqueado'}.")
    return redirect(url_for("admin_admins"))

@app.route("/admin/admins/<int:id>/senha", methods=["GET", "POST"])
@login_required
def admin_alterar_senha_outro(id):
    db = get_db()
    alvo = db.execute("SELECT * FROM admin WHERE id=?", (id,)).fetchone()
    if not alvo: abort(404)

    # Verificações de segurança
    if alvo["id"] == session["admin_id"]:
        flash("Para mudar a sua própria senha, use o menu 'Senha'.")
        return redirect(url_for("admin_admins"))

    eu = db.execute("SELECT * FROM admin WHERE id=?", (session["admin_id"],)).fetchone()
    if not eu["is_super"] and alvo["criado_por"] != eu["id"]:
        flash("Não tem permissão para alterar a senha deste administrador.")
        return redirect(url_for("admin_admins"))

    if request.method == "POST":
        nova = request.form["nova"]
        conf = request.form["confirmar"]

        if len(nova) < 6:
            flash("A senha deve ter pelo menos 6 caracteres.")
        elif nova != conf:
            flash("As senhas não coincidem.")
        else:
            db.execute("UPDATE admin SET password_hash=? WHERE id=?",
                       (generate_password_hash(nova), alvo["id"]))
            db.commit()
            registar_log("senha_admin_alterada", f"alvo={alvo['username']}")
            flash(f"Senha do administrador '{alvo['username']}' alterada com sucesso.")
            return redirect(url_for("admin_admins"))

    return render_template("admin_alterar_senha_outro.html", alvo=alvo)

# -------- Registos / Logs --------
@app.route("/admin/logs")
@login_required
def admin_logs():
    db = get_db()
    filtro = request.args.get("acao", "").strip()
    if filtro:
        logs = db.execute(
            "SELECT * FROM logs WHERE acao=? ORDER BY id DESC LIMIT 500",
            (filtro,)).fetchall()
    else:
        logs = db.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT 500").fetchall()
    acoes = [r["acao"] for r in db.execute(
        "SELECT DISTINCT acao FROM logs ORDER BY acao").fetchall()]
    return render_template("admin_logs.html", logs=logs, acoes=acoes, filtro=filtro)


# ======================= ARRANQUE =======================
if __name__ == "__main__":
    with app.app_context():
        init_db()
    port = int(os.environ.get("PORT", 5000))

    app.run(host="0.0.0.0", port=port, debug=False)
