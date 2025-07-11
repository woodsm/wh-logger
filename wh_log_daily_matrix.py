import os
import psycopg2
import gspread
import datetime
import tempfile
from dotenv import load_dotenv
from sshtunnel import SSHTunnelForwarder
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()
today = datetime.date.today()
yesterday = today - datetime.timedelta(days=1)
date_col = yesterday.strftime('%-m/%-d/%Y')

# Env vars
PG_DBNAME = os.getenv("PG_DBNAME")
PG_USER = os.getenv("PG_USER")
PG_PASSWORD = os.getenv("PG_PASSWORD")
PG_HOST = os.getenv("PG_HOST")
PG_PORT = int(os.getenv("PG_PORT"))
PG_SSLMODE = os.getenv("PG_SSLMODE", "require")
REMOTE_DB_HOST = os.getenv("REMOTE_DB_HOST")
REMOTE_DB_PORT = int(os.getenv("REMOTE_DB_PORT"))
SSH_TUNNEL_HOST = os.getenv("SSH_TUNNEL_HOST")
SSH_TUNNEL_PORT = int(os.getenv("SSH_TUNNEL_PORT"))
SSH_TUNNEL_USER = os.getenv("SSH_TUNNEL_USER")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

# Write SSH private key to temp file
with tempfile.NamedTemporaryFile(delete=False, mode="w") as tmp_key:
    tmp_key.write(os.getenv("SSH_PRIVATE_KEY").replace("\n", "\n").replace("\\n", "\n").encode().decode("unicode_escape"))
    ssh_key_path = tmp_key.name

# Create SSH tunnel
with SSHTunnelForwarder(
    (SSH_TUNNEL_HOST, SSH_TUNNEL_PORT),
    ssh_username=SSH_TUNNEL_USER,
    ssh_pkey=ssh_key_path,
    remote_bind_address=(REMOTE_DB_HOST, REMOTE_DB_PORT),
    local_bind_address=(PG_HOST, PG_PORT)
) as tunnel:
    conn = psycopg2.connect(
        dbname=PG_DBNAME,
        user=PG_USER,
        password=PG_PASSWORD,
        host=PG_HOST,
        port=PG_PORT,
        sslmode=PG_SSLMODE
    )
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT user_id
        FROM wh.user_product
        WHERE updated_at AT TIME ZONE 'UTC' >= %s
          AND updated_at AT TIME ZONE 'UTC' < %s
    """, (yesterday, today))
    active_user_ids = set(str(row[0]) for row in cur.fetchall())

    cur.execute("""
        SELECT DISTINCT u.id::text, LEFT(u.email, 6)
        FROM wh."user" u
        JOIN wh.user_product up ON up.user_id = u.id
    """)
    all_users = {row[0]: row[1] for row in cur.fetchall()}

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("wh-logger-creds.json", scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID)
    tab = sheet.worksheet("usage_log")

    values = tab.get_all_values()
    headers = values[0] if values else []
    user_rows = values[1:] if len(values) > 1 else []
    existing_user_ids = set(row[0] for row in user_rows)

    if not headers:
        headers = ["user_id", "email", date_col]
        tab.update('A1', [headers])
    elif date_col not in headers:
        if headers[0].lower() != "user_id":
            headers[0] = "user_id"
        if headers[1].lower() != "email":
            headers.insert(1, "email")
        headers.append(date_col)
        tab.update('A1', [headers])
    else:
        exit()

    updated_data = []
    for row in user_rows:
        user_id = str(row[0])
        row += [''] * (len(headers) - len(row))
        row[-1] = 'yes' if user_id in active_user_ids else 'no'
        updated_data.append(row)

    new_rows = []
    for user_id, email in all_users.items():
        if user_id not in existing_user_ids:
            row = [''] * len(headers)
            row[0] = user_id
            row[1] = email
            row[-1] = 'yes' if user_id in active_user_ids else 'no'
            new_rows.append(row)

    if updated_data:
        tab.update('A2', updated_data, value_input_option="RAW")
    if new_rows:
        tab.append_rows(new_rows, value_input_option="RAW")

    cur.close()
    conn.close()
