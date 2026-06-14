import os
import imaplib
import email
from email.header import decode_header
import pandas as pd
import mysql.connector
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
from cuid2 import cuid_wrapper
from datetime import datetime

# Cargar variables de entorno desde la raíz del proyecto
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '../../.env'))

# Generador de IDs compatibles con Prisma
generate_id = cuid_wrapper()

def parse_db_url(url):
    # Parsea mysql://user:pass@host:port/dbname
    url = url.replace("mysql://", "")
    user_pass, host_db = url.split("@")
    user, password = user_pass.split(":")
    host_port, dbname = host_db.split("/")
    host, port = host_port.split(":")
    # Limpiar argumentos extra como ?sslaccept=strict
    if "?" in dbname:
        dbname = dbname.split("?")[0]
    return user, password, host, port, dbname

def connect_db():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL no encontrada en el .env")
    
    user, password, host, port, dbname = parse_db_url(db_url)
    return mysql.connector.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=dbname
    )

def configure_cloudinary():
    cloudinary.config(
        cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
        api_key=os.environ.get("CLOUDINARY_API_KEY"),
        api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
        secure=True
    )

def fetch_config(cursor):
    cursor.execute("SELECT emailHost, emailPort, emailUser, emailPassword, bankSender, emailKeyword, bankFileExtension FROM Configuracion LIMIT 1")
    row = cursor.fetchone()
    if not row:
        raise ValueError("No se encontró configuración en la base de datos.")
    return {
        "emailHost": row[0],
        "emailPort": row[1] or 993,
        "emailUser": row[2],
        "emailPassword": row[3],
        "bankSender": row[4],
        "emailKeyword": row[5],
        "bankFileExtension": row[6] or 'xlsx'
    }

def process_excel(content, cursor):
    df = pd.read_excel(content)
    pagos_procesados = 0
    # Asumimos que el Excel tiene columnas: 'NombreCliente', 'Monto', 'Fecha', 'Referencia'
    for index, row in df.iterrows():
        nombre = str(row.get('NombreCliente', '')).strip()
        try:
            monto = float(row.get('Monto', 0))
        except:
            monto = 0.0
        
        # Buscar al cliente por nombre
        cursor.execute("SELECT id FROM Cliente WHERE nombre = %s LIMIT 1", (nombre,))
        cliente = cursor.fetchone()
        cliente_id = cliente[0] if cliente else None
        
        # Buscar el convenio activo del cliente
        convenio_id = None
        if cliente_id:
            cursor.execute("SELECT id FROM Convenio WHERE clienteId = %s AND estado != 'cancelado' LIMIT 1", (cliente_id,))
            convenio = cursor.fetchone()
            convenio_id = convenio[0] if convenio else None
        
        # Insertar pago
        pago_id = generate_id()
        fecha = datetime.now() # O extraer de row['Fecha'] si existe
        referencia = str(row.get('Referencia', ''))
        
        sql = "INSERT INTO Pago (id, nombreCliente, monto, fecha, referencia, estado, clienteId, convenioId, createdAt) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        val = (pago_id, nombre, monto, fecha, referencia, 'completado', cliente_id, convenio_id, datetime.now())
        cursor.execute(sql, val)
        pagos_procesados += 1
        
        # Actualizar estado del convenio si es necesario
        if convenio_id:
            cursor.execute("UPDATE Convenio SET estado = 'activo' WHERE id = %s", (convenio_id,))
            
    return pagos_procesados

def main():
    print("Iniciando motor de sincronización IMAP (Python)...")
    db = connect_db()
    cursor = db.cursor()
    
    try:
        configure_cloudinary()
        config = fetch_config(cursor)
        
        if not config["emailHost"] or not config["emailUser"] or not config["emailPassword"]:
            print("Credenciales IMAP no configuradas en el panel.")
            return

        # Conectar al buzón
        print(f"Conectando a {config['emailHost']}...")
        mail = imaplib.IMAP4_SSL(config["emailHost"], port=config["emailPort"])
        mail.login(config["emailUser"], config["emailPassword"])
        
        mail.select("INBOX")
        
        # Buscar correos no leídos
        search_criteria = ["UNSEEN"]
        if config["bankSender"]:
            search_criteria.append(f'FROM "{config["bankSender"]}"')
        
        # En imaplib, la búsqueda es un string unido
        status, messages = mail.search(None, *search_criteria)
        if status != "OK" or not messages[0]:
            print("No hay correos nuevos para procesar.")
            mail.logout()
            return
            
        email_ids = messages[0].split()
        ext = config["bankFileExtension"].replace('.', '').lower()
        
        total_pagos = 0
        for e_id in email_ids:
            res, msg_data = mail.fetch(e_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    # Validar Asunto si existe
                    if config["emailKeyword"]:
                        subject, encoding = decode_header(msg["Subject"])[0]
                        if isinstance(subject, bytes):
                            subject = subject.decode(encoding if encoding else "utf-8")
                        if config["emailKeyword"].lower() not in subject.lower():
                            continue # Salta si no coincide el asunto
                    
                    # Procesar adjuntos
                    for part in msg.walk():
                        if part.get_content_maintype() == 'multipart':
                            continue
                        if part.get('Content-Disposition') is None:
                            continue
                            
                        filename = part.get_filename()
                        if filename and filename.lower().endswith(f".{ext}"):
                            print(f"Procesando adjunto: {filename}")
                            content = part.get_payload(decode=True)
                            
                            # Subir a Cloudinary para respaldo (NUEVO REQUISITO)
                            upload_result = cloudinary.uploader.upload(
                                content, 
                                resource_type="raw", 
                                public_id=f"bank_statements/stmt_{generate_id()}",
                                format=ext
                            )
                            url_respaldo = upload_result.get("secure_url")
                            
                            # Registrar respaldo en DB
                            respaldo_id = generate_id()
                            cursor.execute(
                                "INSERT INTO RespaldoExcel (id, nombre, url, fecha) VALUES (%s, %s, %s, %s)",
                                (respaldo_id, filename, url_respaldo, datetime.now())
                            )
                            
                            # Procesar Excel e inyectar a DB
                            # Necesitamos guardar temporalmente en disco para pandas o usar io.BytesIO
                            import io
                            pagos = process_excel(io.BytesIO(content), cursor)
                            total_pagos += pagos
                            
            # Marcar como leído
            mail.store(e_id, '+FLAGS', '\\Seen')
            
        db.commit()
        print(f"Sincronización completada. Se procesaron {total_pagos} pagos.")
        
        mail.logout()
    except Exception as e:
        print(f"Error crítico: {str(e)}")
        db.rollback()
    finally:
        cursor.close()
        db.close()

if __name__ == "__main__":
    main()
