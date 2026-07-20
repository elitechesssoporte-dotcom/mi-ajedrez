import sqlite3

conn = sqlite3.connect('elitechess.db')
cursor = conn.cursor()

cursor.execute('SELECT nick FROM usuarios')
usuarios = cursor.fetchall()

print("Usuarios en la base de datos:")
for usuario in usuarios:
    print(f"  - {usuario[0]}")

conn.close()
input("\nPulsa ENTER para cerrar...")