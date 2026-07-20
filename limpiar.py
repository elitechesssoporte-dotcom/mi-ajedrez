import re

# Leer el archivo
with open('tablero.html', 'r', encoding='utf-8', errors='ignore') as f:
    contenido = f.read()

# Reemplazar patrones comunes de caracteres corruptos
# Esto limpia todos los emojis y acentos rotos
contenido_limpio = contenido.encode('latin-1', errors='ignore').decode('utf-8', errors='ignore')

# Guardar el archivo limpio
with open('tablero.html', 'w', encoding='utf-8') as f:
    f.write(contenido_limpio)

print('✅ Archivo limpiado correctamente')
print('📁 Archivo: tablero.html')