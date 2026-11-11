import os, secrets
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Novas importações para Postgres e Cloudinary
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader

# Carrega variáveis do ficheiro .env
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-mude-em-producao")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB

# Configuração do Cloudinary
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET")
)

ESTADOS = ["pendente", "pronto", "entregue", "cancelado"]


# -------- Base de dados (PostgreSQL) --------
def get_db():
    if "db" not in g:
        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            raise RuntimeError("A variável DATABASE_URL não está definida.")
        # RealDictCursor permite aceder aos campos por nome (ex: n['titulo'])
        g.db = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db: db.close()


def init_db():
    db = get_db()
    cur = db.cursor()

    # Tabelas adaptadas para PostgreSQL (SERIAL, BOOLEAN, TIMESTAMP)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin(
            id SERIAL PRIMARY KEY,
            username VARCHAR(80) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            ativo BOOLEAN DEFAULT TRUE,
            is_super BOOLEAN DEFAULT FALSE,
            criado_por INTEGER,
            criado_em TIMESTAMP
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS noticias(
            id SERIAL PRIMARY KEY,
            titulo VARCHAR(255) NOT NULL,
            texto TEXT NOT NULL,
            imagem VARCHAR(500),
            criada_em TIMESTAMP NOT NULL,
            visualizacoes INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            dislikes INTEGER DEFAULT 0
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pedidos(
            id SERIAL PRIMARY KEY,
            codigo VARCHAR(50) UNIQUE NOT NULL,
            cliente VARCHAR(255) NOT NULL,
            contacto VARCHAR(50),
            descricao TEXT NOT NULL,
            preco REAL NOT NULL,
            estado VARCHAR(20) NOT NULL DEFAULT 'pendente',
            criado_em TIMESTAMP NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS logs(
            id SERIAL PRIMARY KEY,
            data TIMESTAMP NOT NULL,
            admin_id INTEGER,
            admin_user VARCHAR(80),
            acao VARCHAR(100) NOT NULL,
            detalhe TEXT,
            ip VARCHAR(45)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fotos(
            id SERIAL PRIMARY KEY,
            titulo VARCHAR(255),
            imagem VARCHAR(500) NOT NULL,
            criado_em TIMESTAMP NOT NULL
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS mensagens(
            id SERIAL PRIMARY KEY,
            sala VARCHAR(50) NOT NULL,
            remetente_tipo VARCHAR(10) NOT NULL, -- 'cliente' ou 'admin'
            remetente_nome VARCHAR(100) NOT NULL,
            mensagem TEXT NOT NULL,
            criada_em TIMESTAMP NOT NULL
        );
    """)

    # Cria o super admin padrão se a tabela estiver vazia
    cur.execute("SELECT COUNT(*) as c FROM admin")
    if cur.fetchone()['c'] == 0:
        cur.execute(
            "INSERT INTO admin(username, password_hash, ativo, is_super, criado_em) "
            "VALUES (%s, %s, TRUE, TRUE, %s)",
            ("admin", generate_password_hash("admin123"), datetime.now())
        )

    db.commit()
    print("✅ Base de dados PostgreSQL (Neon) verificada/criada com sucesso!")


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
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO logs(data, admin_id, admin_user, acao, detalhe, ip) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (datetime.now(), session.get("admin_id"), session.get("admin_user") or "—",
         acao, detalhe, request.remote_addr if request else None))
    db.commit()


def apagar_imagem_cloudinary(url):
    """Extrai o public_id do URL do Cloudinary e apaga a imagem para não gastar espaço."""
    if not url or "cloudinary.com" not in url:
        return
    try:
        # Exemplo de URL: https://res.cloudinary.com/.../upload/v1234/pasta/imagem.jpg
        partes = url.split("/upload/")
        if len(partes) == 2:
            caminho_com_versao = partes[1]
            # Remove a versão (v1234/) e a extensão (.jpg)
            public_id_com_extensao = caminho_com_versao.split("/", 1)[1]
            public_id = public_id_com_extensao.rsplit(".", 1)[0]
            cloudinary.uploader.destroy(public_id)
    except Exception as e:
        print(f"Erro ao apagar imagem do Cloudinary: {e}")


# ======================= ÁREA PÚBLICA =======================
@app.route("/")
def home():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM noticias ORDER BY criada_em DESC")
    noticias = cur.fetchall()
    return render_template("index.html", noticias=noticias)


@app.route("/noticia/<int:id>")
def ver_noticia(id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM noticias WHERE id=%s", (id,))
    n = cur.fetchone()
    if not n: abort(404)

    cur.execute("UPDATE noticias SET visualizacoes = visualizacoes + 1 WHERE id=%s", (id,))
    db.commit()

    cur.execute("SELECT * FROM noticias WHERE id=%s", (id,))
    n = cur.fetchone()
    return render_template("noticia.html", n=n)


@app.route("/noticia/<int:id>/reagir/<acao>")
def reagir(id, acao):
    if acao not in ("like", "dislike"): abort(400)
    campo = "likes" if acao == "like" else "dislikes"
    db = get_db()
    cur = db.cursor()
    cur.execute(f"UPDATE noticias SET {campo} = {campo} + 1 WHERE id=%s", (id,))
    db.commit()
    registar_log("visitante_reagiu", f"noticia_id={id} acao={acao}")
    return redirect(url_for("ver_noticia", id=id))


@app.route("/fotos")
def fotos():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM fotos ORDER BY criado_em DESC")
    todas_fotos = cur.fetchall()
    return render_template("fotos.html", fotos=todas_fotos)


@app.route("/consultar", methods=["GET", "POST"])
def consultar():
    pedido = None
    if request.method == "POST":
        codigo = request.form["codigo"].strip().upper()
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT * FROM pedidos WHERE codigo=%s", (codigo,))
        pedido = cur.fetchone()
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
        cur = db.cursor()
        cur.execute("SELECT * FROM admin WHERE username=%s", (u,))
        admin = cur.fetchone()

        if not admin or not check_password_hash(admin["password_hash"], p):
            registar_log("login_falha", f"utilizador={u}")
            flash("Credenciais inválidas.")
            return render_template("login.html")

        if not admin["ativo"]:
            registar_log("login_bloqueado", f"utilizador={u}")
            flash("Conta bloqueada. Contacte o super administrador.")
            return render_template("login.html")

        session["admin_id"] = admin["id"]
        session["admin_user"] = admin["username"]
        session["is_super"] = admin["is_super"]
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
    cur = db.cursor()

    cur.execute("SELECT COUNT(*) as c FROM noticias");
    noticias = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) as c FROM pedidos");
    pedidos = cur.fetchone()['c']
    cur.execute("SELECT COUNT(*) as c FROM pedidos WHERE estado='pendente'");
    pendentes = cur.fetchone()['c']
    cur.execute("SELECT COALESCE(SUM(visualizacoes),0) as c FROM noticias");
    visualizacoes = cur.fetchone()['c']

    stats = {"noticias": noticias, "pedidos": pedidos, "pendentes": pendentes, "visualizacoes": visualizacoes}

    cur.execute("SELECT * FROM noticias ORDER BY criada_em DESC LIMIT 5")
    ultimas = cur.fetchall()

    return render_template("admin_dashboard.html", stats=stats, ultimas=ultimas)


# -------- Notícias --------
@app.route("/admin/noticias")
@login_required
def admin_noticias():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM noticias ORDER BY criada_em DESC")
    noticias = cur.fetchall()
    return render_template("admin_noticias.html", noticias=noticias)


@app.route("/admin/noticias/nova", methods=["GET", "POST"])
@login_required
def admin_noticia_nova():
    if request.method == "POST":
        titulo = request.form["titulo"].strip()
        texto = request.form["texto"].strip()
        imagem_url = None

        f = request.files.get("imagem")
        if f and f.filename:
            # Upload para o Cloudinary
            upload_result = cloudinary.uploader.upload(f)
            imagem_url = upload_result.get("secure_url")

        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO noticias(titulo, texto, imagem, criada_em) VALUES (%s, %s, %s, %s)",
                    (titulo, texto, imagem_url, datetime.now()))
        db.commit()
        registar_log("noticia_criada", f"titulo={titulo}")
        flash("Notícia publicada.")
        return redirect(url_for("admin_noticias"))
    return render_template("admin_noticia_form.html")


@app.route("/admin/noticias/apagar/<int:id>", methods=["POST"])
@login_required
def admin_noticia_apagar(id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM noticias WHERE id=%s", (id,))
    n = cur.fetchone()

    if n and n["imagem"]:
        apagar_imagem_cloudinary(n["imagem"])

    cur.execute("DELETE FROM noticias WHERE id=%s", (id,))
    db.commit()
    registar_log("noticia_apagada", f"id={id} titulo={n['titulo'] if n else '?'}")
    flash("Notícia apagada.")
    return redirect(url_for("admin_noticias"))


# -------- Galeria de Fotos (Admin) --------
@app.route("/admin/galeria")
@login_required
def admin_galeria():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM fotos ORDER BY criado_em DESC")
    fotos = cur.fetchall()
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

        upload_result = cloudinary.uploader.upload(f)
        imagem_url = upload_result.get("secure_url")

        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO fotos(titulo, imagem, criado_em) VALUES (%s, %s, %s)",
                    (titulo, imagem_url, datetime.now()))
        db.commit()
        registar_log("foto_galeria_criada", f"titulo={titulo}")
        flash("Foto adicionada à galeria.")
        return redirect(url_for("admin_galeria"))

    return render_template("admin_foto_form.html")


@app.route("/admin/galeria/apagar/<int:id>", methods=["POST"])
@login_required
def admin_galeria_apagar(id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM fotos WHERE id=%s", (id,))
    f = cur.fetchone()

    if f:
        apagar_imagem_cloudinary(f["imagem"])
        cur.execute("DELETE FROM fotos WHERE id=%s", (id,))
        db.commit()
        registar_log("foto_galeria_apagada", f"id={id} titulo={f['titulo']}")
        flash("Foto apagada da galeria.")
    return redirect(url_for("admin_galeria"))


# -------- Pedidos --------
@app.route("/admin/pedidos")
@login_required
def admin_pedidos():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM pedidos ORDER BY criado_em DESC")
    pedidos = cur.fetchall()
    return render_template("admin_pedidos.html", pedidos=pedidos, estados=ESTADOS)


@app.route("/admin/pedidos/novo", methods=["GET", "POST"])
@login_required
def admin_pedido_novo():
    if request.method == "POST":
        cliente = request.form["cliente"].strip()
        contacto = request.form.get("contacto", "").strip()
        descricao = request.form["descricao"].strip()
        try:
            preco = float(request.form["preco"])
        except ValueError:
            flash("Preço inválido.")
            return redirect(url_for("admin_pedido_novo"))
        codigo = gerar_codigo()
        db = get_db()
        cur = db.cursor()
        cur.execute("""INSERT INTO pedidos(codigo, cliente, contacto, descricao, preco, estado, criado_em)
                      VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (codigo, cliente, contacto, descricao, preco, "pendente", datetime.now()))
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
    cur = db.cursor()
    cur.execute("UPDATE pedidos SET estado=%s WHERE id=%s", (novo, id))
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
        nova = request.form["nova"]
        conf = request.form["confirmar"]
        if nova != conf:
            flash("A nova senha e a confirmação não coincidem.")
            return redirect(url_for("admin_senha"))
        if len(nova) < 6:
            flash("A senha deve ter pelo menos 6 caracteres.")
            return redirect(url_for("admin_senha"))
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT * FROM admin WHERE id=%s", (session["admin_id"],))
        admin = cur.fetchone()

        if not check_password_hash(admin["password_hash"], atual):
            flash("Senha atual incorreta.")
            return redirect(url_for("admin_senha"))

        cur.execute("UPDATE admin SET password_hash=%s WHERE id=%s",
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
    cur = db.cursor()
    cur.execute("""
        SELECT a.*, c.username AS criado_por_nome
        FROM admin a LEFT JOIN admin c ON c.id = a.criado_por
        ORDER BY a.id
    """)
    admins = cur.fetchall()
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
        cur = db.cursor()
        cur.execute("SELECT id FROM admin WHERE username=%s", (u,))
        if cur.fetchone():
            flash("Nome de utilizador já existe.")
            return redirect(url_for("admin_admin_novo"))

        cur.execute(
            "INSERT INTO admin(username, password_hash, ativo, is_super, criado_por, criado_em) "
            "VALUES (%s, %s, TRUE, FALSE, %s, %s)",
            (u, generate_password_hash(p), session["admin_id"], datetime.now()))
        db.commit()
        registar_log("admin_criado", f"novo_admin={u}")
        flash(f"Administrador '{u}' criado.")
        return redirect(url_for("admin_admins"))
    return render_template("admin_admin_form.html")


@app.route("/admin/admins/<int:id>/bloquear", methods=["POST"])
@login_required
def admin_bloquear(id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM admin WHERE id=%s", (id,))
    alvo = cur.fetchone()
    if not alvo: abort(404)

    if alvo["id"] == session["admin_id"]:
        flash("Não pode bloquear a sua própria conta.")
        return redirect(url_for("admin_admins"))
    if alvo["is_super"]:
        flash("O super administrador não pode ser bloqueado.")
        return redirect(url_for("admin_admins"))

    cur.execute("SELECT * FROM admin WHERE id=%s", (session["admin_id"],))
    eu = cur.fetchone()

    if not eu["is_super"] and alvo["criado_por"] != eu["id"]:
        flash("Só pode bloquear administradores que criou.")
        return redirect(url_for("admin_admins"))

    novo = not alvo["ativo"]  # Inverte o valor booleano
    cur.execute("UPDATE admin SET ativo=%s WHERE id=%s", (novo, id))
    db.commit()

    acao = "admin_desbloqueio" if novo else "admin_bloqueio"
    registar_log(acao, f"alvo={alvo['username']}")
    flash(f"Admin '{alvo['username']}' {'desbloqueado' if novo else 'bloqueado'}.")
    return redirect(url_for("admin_admins"))


@app.route("/admin/admins/<int:id>/senha", methods=["GET", "POST"])
@login_required
def admin_alterar_senha_outro(id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM admin WHERE id=%s", (id,))
    alvo = cur.fetchone()
    if not alvo: abort(404)

    if alvo["id"] == session["admin_id"]:
        flash("Para mudar a sua própria senha, use o menu 'Senha'.")
        return redirect(url_for("admin_admins"))

    cur.execute("SELECT * FROM admin WHERE id=%s", (session["admin_id"],))
    eu = cur.fetchone()

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
            cur.execute("UPDATE admin SET password_hash=%s WHERE id=%s",
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
    cur = db.cursor()
    filtro = request.args.get("acao", "").strip()

    if filtro:
        cur.execute("SELECT * FROM logs WHERE acao=%s ORDER BY id DESC LIMIT 500", (filtro,))
    else:
        cur.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 500")
    logs = cur.fetchall()

    cur.execute("SELECT DISTINCT acao FROM logs ORDER BY acao")
    acoes = [r["acao"] for r in cur.fetchall()]

    return render_template("admin_logs.html", logs=logs, acoes=acoes, filtro=filtro)


import random
import string

@app.route("/contacto")
def contacto():
    # Se o cliente ainda não tem uma "sala", criamos uma nova (ex: sala_cliente_A3F9)
    if "cliente_sala" not in session:
        codigo = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        session["cliente_sala"] = f"cliente_{codigo}"
        session["cliente_nome"] = "Visitante"
    return render_template("contacto.html")


@app.route("/admin/chat")
@login_required
def admin_chat():
    db = get_db()
    cur = db.cursor()
    # Pega todas as salas de clientes que têm mensagens
    cur.execute("SELECT DISTINCT sala FROM mensagens WHERE sala LIKE 'cliente_%' ORDER BY sala DESC")
    salas_clientes = [r["sala"] for r in cur.fetchall()]
    return render_template("admin_chat.html", salas_clientes=salas_clientes)


@app.route("/api/chat/enviar", methods=["POST"])
def api_chat_enviar():
    data = request.json
    sala = data.get("sala")
    mensagem = data.get("mensagem", "").strip()
    if not mensagem or not sala:
        return {"status": "erro"}, 400

    # Define quem está a enviar
    if session.get("admin_id"):
        tipo = "admin"
        nome = session.get("admin_user")
    else:
        tipo = "cliente"
        nome = session.get("cliente_nome", "Visitante")

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO mensagens(sala, remetente_tipo, remetente_nome, mensagem, criada_em) VALUES (%s, %s, %s, %s, %s)",
        (sala, tipo, nome, mensagem, datetime.now())
    )
    db.commit()
    return {"status": "ok"}


@app.route("/api/chat/receber/<sala>")
def api_chat_receber(sala):
    # Segurança: O cliente só pode ler a sua própria sala
    if sala.startswith("cliente_") and not session.get("admin_id"):
        if session.get("cliente_sala") != sala:
            return {"status": "erro", "msg": "Acesso negado"}, 403

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM mensagens WHERE sala=%s ORDER BY criada_em ASC", (sala,))
    msgs = cur.fetchall()

    resultado = []
    for m in msgs:
        resultado.append({
            "tipo": m["remetente_tipo"],
            "nome": m["remetente_nome"],
            "mensagem": m["mensagem"],
            "hora": m["criada_em"].strftime("%H:%M")
        })
    return {"mensagens": resultado, "meu_tipo": "admin" if session.get("admin_id") else "cliente"}


@app.route("/api/chat/receber/<sala>")
def api_chat_receber(sala):
    if sala.startswith("cliente_") and not session.get("admin_id"):
        if session.get("cliente_sala") != sala:
            return {"status": "erro", "msg": "Acesso negado"}, 403

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM mensagens WHERE sala=%s ORDER BY criada_em ASC", (sala,))
    msgs = cur.fetchall()

    resultado = []
    for m in msgs:
        resultado.append({
            "id": m["id"],  # IMPORTANTE: Adicionámos o ID aqui
            "tipo": m["remetente_tipo"],
            "nome": m["remetente_nome"],
            "mensagem": m["mensagem"],
            "hora": m["criada_em"].strftime("%H:%M")
        })
    return {"mensagens": resultado, "meu_tipo": "admin" if session.get("admin_id") else "cliente"}

@app.route("/api/chat/verificar_novas")
def api_chat_verificar_novas():
    db = get_db()
    cur = db.cursor()
    ultimo_id = request.args.get("ultimo_id", 0, type=int)
    novas = 0

    if session.get("admin_id"):
        # Admin: Conta mensagens de clientes
        cur.execute("""
            SELECT COUNT(*) as c FROM mensagens 
            WHERE sala LIKE 'cliente_%%' AND remetente_tipo = 'cliente' AND id > %s
        """, (ultimo_id,))
        novas = cur.fetchone()['c']
    elif session.get("cliente_sala"):
        # Cliente: Conta mensagens do Admin na sua sala
        cur.execute("""
            SELECT COUNT(*) as c FROM mensagens 
            WHERE sala = %s AND remetente_tipo = 'admin' AND id > %s
        """, (session["cliente_sala"], ultimo_id))
        novas = cur.fetchone()['c']

    return {"novas": novas}


# ======================= ARRANQUE =======================
with app.app_context():
    init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
