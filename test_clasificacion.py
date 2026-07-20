from supabase import create_client

SUPABASE_URL = "https://stizpdyftzoeuwigxgbi.supabase.co"
SUPABASE_KEY = "sb_secret_CycLP..."  # ← Pon aquí tu secret key completa

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

try:
    categoria = 'blitz'
    columna_elo = f'elo_{categoria}'
    columna_ganadas = f'partidas_ganadas_{categoria}'
    columna_perdidas = f'partidas_perdidas_{categoria}'
    columna_tablas = f'partidas_tablas_{categoria}'
    
    response = supabase.table('usuarios').select(
        f'nick, {columna_elo}, {columna_ganadas}, {columna_perdidas}, {columna_tablas}'
    ).order(columna_elo, desc=True).limit(50).execute()
    
    print(f"✅ Consulta exitosa. Usuarios encontrados: {len(response.data)}")
    for i, usuario in enumerate(response.data, 1):
        print(f"{i}. {usuario['nick']} - ELO: {usuario[columna_elo]}")
        
except Exception as e:
    print(f"❌ Error: {e}")