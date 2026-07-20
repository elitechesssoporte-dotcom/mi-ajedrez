from supabase import create_client

SUPABASE_URL = "https://stizpdyftzoeuwigxgbi.supabase.co"
SUPABASE_KEY = "sb_secret_CYcLPsygIf_rwaqy9gHcwg_9F6sUzy7"  # ← Pega aquí la SECRET KEY completa (la de abajo)

try:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("✅ Conexión exitosa a Supabase")
    
    # Prueba de lectura
    response = supabase.table('usuarios').select('*').limit(1).execute()
    print(f"✅ Query exitosa. Datos: {response.data}")
    
    # Prueba de inserción
    test_data = {
        'nick': 'test_usuario',
        'password_hash': 'test123'
    }
    insert_response = supabase.table('usuarios').insert(test_data).execute()
    print(f"✅ Inserción exitosa. ID: {insert_response.data[0]['id']}")
    
except Exception as e:
    print(f" Error: {e}")