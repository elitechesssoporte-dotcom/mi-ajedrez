import sqlite3

conn = sqlite3.connect('elitechess.db')
cursor = conn.cursor()

try:
    # Bullet
    cursor.execute('ALTER TABLE usuarios ADD COLUMN partidas_ganadas_bullet INTEGER DEFAULT 0')
    cursor.execute('ALTER TABLE usuarios ADD COLUMN partidas_perdidas_bullet INTEGER DEFAULT 0')
    cursor.execute('ALTER TABLE usuarios ADD COLUMN partidas_tablas_bullet INTEGER DEFAULT 0')
    
    # Blitz
    cursor.execute('ALTER TABLE usuarios ADD COLUMN partidas_ganadas_blitz INTEGER DEFAULT 0')
    cursor.execute('ALTER TABLE usuarios ADD COLUMN partidas_perdidas_blitz INTEGER DEFAULT 0')
    cursor.execute('ALTER TABLE usuarios ADD COLUMN partidas_tablas_blitz INTEGER DEFAULT 0')
    
    # Rapid
    cursor.execute('ALTER TABLE usuarios ADD COLUMN partidas_ganadas_rapid INTEGER DEFAULT 0')
    cursor.execute('ALTER TABLE usuarios ADD COLUMN partidas_perdidas_rapid INTEGER DEFAULT 0')
    cursor.execute('ALTER TABLE usuarios ADD COLUMN partidas_tablas_rapid INTEGER DEFAULT 0')
    
    conn.commit()
    print("✅ Columnas de estadísticas por categoría añadidas correctamente")
    
except Exception as e:
    print(f"⚠️ Ya existen las columnas o error: {e}")

conn.close()
input("\nPulsa ENTER para cerrar...")