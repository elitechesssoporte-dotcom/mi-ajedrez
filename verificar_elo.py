import sqlite3

conn = sqlite3.connect('elitechess.db')
cursor = conn.cursor()

cursor.execute('SELECT nick, elo_bullet, elo_blitz, elo_rapid FROM usuarios')
usuarios = cursor.fetchall()

print(" ELOs por categoría:\n")
print(f"{'Jugador':<15} | {'Bullet':<8} | {'Blitz':<8} | {'Rapid':<8}")
print("-" * 50)

for usuario in usuarios:
    print(f"{usuario[0]:<15} | {usuario[1]:<8} | {usuario[2]:<8} | {usuario[3]:<8}")

conn.close()
input("\nPulsa ENTER para cerrar...")