import sqlite3

conn = sqlite3.connect('elitechess.db')
cursor = conn.cursor()

try:
    cursor.execute('ALTER TABLE usuarios ADD COLUMN elo_bullet INTEGER DEFAULT 1200')
    cursor.execute('ALTER TABLE usuarios ADD COLUMN elo_blitz INTEGER DEFAULT 1200')
    cursor.execute('ALTER TABLE usuarios ADD COLUMN elo_rapid INTEGER DEFAULT 1200')
    conn.commit()
    print("✅ Columnas añadidas correctamente.")
except Exception as e:
    print(f"⚠️ Ya existían o hubo un error: {e}")

conn.close()
input("Pulsa ENTER para cerrar...")