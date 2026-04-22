# sin estos comandos, no reconoce el path
# alternativa: ejecutar  python -m paula_tfm.pa_conn_postgres en la terminal
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))


from connect.postgres_client import get_postgres_pool

pool = get_postgres_pool()

with pool.connection() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM tfm_bot.contacts LIMIT 5;")
        rows = cur.fetchall()
        print(rows)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.name AS project, t.title AS task FROM tfm_bot.tasks t JOIN tfm_bot.projects p ON t.project_id = p.id ORDER BY p.id;"
        )
        rows = cur.fetchall()
        print(rows)
