from flask import Flask, send_file, request, render_template
from flask_socketio import SocketIO, emit, join_room
import uuid
import socket
import random
import hashlib
import os
import time
import threading
from supabase import create_client, Client

app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = 'elitechess_secreto_2026'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# --- CONEXIÓN A SUPABASE ---
# ⚠️ REEMPLAZA ESTOS VALORES CON LOS TUYOS
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- VARIABLES GLOBALES ---
cola_espera = [] 
salas = {} 
estado_partidas = {} 
control_tablas = {}
control_nicks = {}
MAX_NICKS_POR_IP = 2
usuarios_conectados = {}
partidas_activas = {}
temporizadores_reconexion = {}
sids_activos = {}
desconexiones_por_jugador = {}
temporizadores_bot = {}  # 🤖 Para controlar el tiempo de espera del bot

def emitir_cola_espera():
    print(f"📡 Emitiendo cola - Total en espera: {len(cola_espera)}")
    cola_info = []
    for jugador in cola_espera:
        nick = jugador['data'].get('usuario', 'Anónimo')
        tiempo = jugador['data'].get('tiempo', 5)
        categoria = obtener_categoria(tiempo)
        elo = obtener_elo(nick, categoria)
        
        # 🏁 NUEVO: Obtener el país del jugador desde la base de datos
        pais = 'ES'  # Valor por defecto
        try:
            response = supabase.table('usuarios').select('pais').ilike('nick', nick).execute()
            if response.data and len(response.data) > 0:
                pais = response.data[0].get('pais', 'ES')
                print(f"   🌍 País de {nick}: {pais}")
        except Exception as e:
            print(f"⚠️ Error al obtener país de {nick}: {e}")
        
        info = {
            'id': jugador['id'],
            'nick': nick,
            'tiempo': jugador['data'].get('tiempo', 5),
            'incremento': jugador['data'].get('incremento', 0),
            'elo': elo,
            'color': jugador['data'].get('color', 'random'),
            'pais': pais  # 🏁 AÑADIDO: Enviamos el país al cliente
        }
        print(f"   - {info['nick']} (ELO: {elo}) - {info['tiempo']}+{info['incremento']}s - Color: {info['color']} - País: {info['pais']}")
        cola_info.append(info)
    
    print(f"📤 Enviando evento 'actualizar_cola_espera' con {len(cola_info)} jugadores")
    emit('actualizar_cola_espera', cola_info, broadcast=True)

# --- RUTAS ---
@app.route('/')
def index():
    return send_file('Portada.html')

@app.route('/login.html')
def login_page():
    return send_file('login.html')

@app.route('/juego')
def juego():
    return send_file('configuracion.html')

@app.route('/tablero.html')
def tablero():
    return send_file('tablero.html')

@app.route('/clasificacion')
def clasificacion():
    return send_file('clasificacion.html')

@app.route('/<path:filename>')
def servir_archivo(filename):
    return send_file(filename)

# --- FUNCIONES DE CONTRASEÑA ---
def hash_password(password):
    salt = hashlib.sha256(os.urandom(60)).hexdigest().encode('ascii')
    pwdhash = hashlib.pbkdf2_hmac('sha512', password.encode('utf-8'), salt, 100000)
    pwdhash = pwdhash.hex()
    return (salt.decode('ascii') + pwdhash).encode('ascii').decode('ascii')

def verify_password(stored_password, provided_password):
    salt = stored_password[:64]
    stored_password = stored_password[64:]
    pwdhash = hashlib.pbkdf2_hmac('sha512', provided_password.encode('utf-8'), salt.encode('ascii'), 100000)
    pwdhash = pwdhash.hex()
    return pwdhash == stored_password

# --- SISTEMA ELO ---
def obtener_categoria(tiempo_minutos):
    if tiempo_minutos <= 2:
        return 'bullet'
    elif tiempo_minutos <= 5:
        return 'blitz'
    else:
        return 'rapid'

def calcular_elo(elo_jugador, elo_rival, resultado, k_factor=32):
    puntuacion_esperada = 1 / (1 + 10 ** ((elo_rival - elo_jugador) / 400))
    if resultado == 'victoria':
        puntuacion_real = 1.0
    elif resultado == 'derrota':
        puntuacion_real = 0.0
    else:
        puntuacion_real = 0.5
    nuevo_elo = elo_jugador + k_factor * (puntuacion_real - puntuacion_esperada)
    return round(nuevo_elo)

def actualizar_estadisticas_db(nick, resultado, categoria='blitz'):
    try:
        if categoria not in ['bullet', 'blitz', 'rapid']:
            categoria = 'blitz'
        
        columna = None
        if resultado == 'victoria':
            columna = f'partidas_ganadas_{categoria}'
        elif resultado == 'derrota':
            columna = f'partidas_perdidas_{categoria}'
        else:
            columna = f'partidas_tablas_{categoria}'
        
        # Obtener valor actual
        response = supabase.table('usuarios').select(columna).eq('nick', nick).execute()
        if response.data and len(response.data) > 0:
            valor_actual = response.data[0][columna]
            nuevo_valor = valor_actual + 1
            
            supabase.table('usuarios').update({columna: nuevo_valor}).eq('nick', nick).execute()
            print(f"✅ Estadísticas {categoria} actualizadas para {nick}: {resultado}")
        else:
            print(f"⚠️ Usuario {nick} no encontrado para actualizar estadísticas")
    except Exception as e:
        print(f"❌ Error al actualizar estadísticas {categoria}: {e}")

def obtener_elo(nick, categoria='blitz'):
    try:
        if categoria not in ['bullet', 'blitz', 'rapid']:
            categoria = 'blitz'
        
        nick_limpio = str(nick).strip()
        columna = f'elo_{categoria}'
        
        # Consultamos TODOS los ELOs de una vez para depurar y evitar fallos de columna
        response = supabase.table('usuarios').select('nick, elo_bullet, elo_blitz, elo_rapid').ilike('nick', nick_limpio).execute()
        
        if response.data and len(response.data) > 0:
            usuario = response.data[0]
            elo_valor = usuario.get(columna)
            
            # 🕵️ LOG DE DEPURACIÓN: Nos muestra exactamente qué devolvió la BD
            print(f"🔍 DB Response para '{nick_limpio}': {usuario}")
            
            if elo_valor is None:
                print(f"⚠️ Usuario '{nick_limpio}' encontrado, pero '{columna}' es NULL. Devolviendo 1200")
                return 1200
            
            print(f"✅ ÉXITO: ELO de '{nick_limpio}' en {categoria} es {elo_valor}")
            return elo_valor
        
        print(f"⚠️ Usuario '{nick_limpio}' NO encontrado en DB, devolviendo 1200")
        return 1200
    except Exception as e:
        print(f"❌ Error al obtener ELO de '{nick}': {e}")
        return 1200

def actualizar_elo_db(nick, nuevo_elo, categoria='blitz'):
    try:
        if categoria not in ['bullet', 'blitz', 'rapid']:
            categoria = 'blitz'
        columna = f'elo_{categoria}'
        
        supabase.table('usuarios').update({columna: nuevo_elo}).eq('nick', nick).execute()
        print(f"✅ ELO {categoria} actualizado para {nick}: {nuevo_elo}")
    except Exception as e:
        print(f"❌ Error actualizar ELO {categoria}: {e}")
# --- CONTROL DE TIEMPO DEL SERVIDOR (Autoridad absoluta) ---
# --- CONTROL DE TIEMPO DEL SERVIDOR (Autoridad absoluta) ---
def monitor_tiempos():
    """Hilo en segundo plano que verifica y emite el tiempo real cada 0.5 segundos"""
    while True:
        time.sleep(0.5)
        ahora = time.time()
        
        # Copiamos las claves para evitar errores si se borra una sala mientras iteramos
        for sala_id in list(salas.keys()):
            sala = salas[sala_id]
            
            # Si la partida ya terminó, la ignoramos
            if sala.get('partida_terminada', False):
                continue
            
            tiempo_transcurrido = ahora - sala.get('ultimo_movimiento', ahora)
            t_b = sala.get('tiempo_restante_blanco', 300)
            t_n = sala.get('tiempo_restante_negro', 300)
            turno = sala.get('turno', 'blanco')
            
            # Calcular tiempo real en este instante
            if turno == 'blanco':
                t_b_real = max(0, t_b - tiempo_transcurrido)
                t_n_real = t_n
            else:
                t_b_real = t_b
                t_n_real = max(0, t_n - tiempo_transcurrido)
            
            # 1. Emitir el tiempo real a los clientes para que sus relojes se sincronicen
            socketio.emit('actualizacion_tiempo_servidor', {
                'blancas': round(t_b_real, 1),
                'negras': round(t_n_real, 1)
            }, room=sala_id)
            
            # 2. Verificar si alguien se ha quedado a 0
            ganador = None
            if t_b_real <= 0:
                print(f"⏰ ¡Tiempo agotado! Ganan las NEGRAS en sala {sala_id}")
                ganador = 'negro'
            elif t_n_real <= 0:
                print(f"⏰ ¡Tiempo agotado! Ganan las BLANCAS en sala {sala_id}")
                ganador = 'blanco'
            
            # 3. Si hay un ganador por tiempo, finalizar la partida AQUÍ MISMO
            if ganador:
                sala['partida_terminada'] = True
                if 'desconectado' in sala:
                    del sala['desconectado']
                
                nick_blanco = sala.get('blanco')
                nick_negro = sala.get('negro')
                tiempo_partida = sala.get('tiempo', 5)
                categoria = obtener_categoria(tiempo_partida)
                
                nuevo_elo_blanco = 1200
                nuevo_elo_negro = 1200
                
                try:
                    elo_blanco = obtener_elo(nick_blanco, categoria)
                    elo_negro = obtener_elo(nick_negro, categoria)
                    
                    if ganador == 'blanco':
                        nuevo_elo_blanco = calcular_elo(elo_blanco, elo_negro, 'victoria')
                        nuevo_elo_negro = calcular_elo(elo_negro, elo_blanco, 'derrota')
                        if not sala.get('estadisticas_actualizadas', False):
                            actualizar_estadisticas_db(nick_blanco, 'victoria', categoria)
                            actualizar_estadisticas_db(nick_negro, 'derrota', categoria)
                            sala['estadisticas_actualizadas'] = True
                    else:
                        nuevo_elo_blanco = calcular_elo(elo_blanco, elo_negro, 'derrota')
                        nuevo_elo_negro = calcular_elo(elo_negro, elo_blanco, 'victoria')
                        if not sala.get('estadisticas_actualizadas', False):
                            actualizar_estadisticas_db(nick_blanco, 'derrota', categoria)
                            actualizar_estadisticas_db(nick_negro, 'victoria', categoria)
                            sala['estadisticas_actualizadas'] = True
                    
                    actualizar_elo_db(nick_blanco, nuevo_elo_blanco, categoria)
                    actualizar_elo_db(nick_negro, nuevo_elo_negro, categoria)
                    
                    sala['elo_blanco'] = nuevo_elo_blanco
                    sala['elo_negro'] = nuevo_elo_negro
                    
                except Exception as e:
                    print(f"❌ Error al actualizar ELO por tiempo: {e}")
                
                # Emitir el final de la partida a los clientes
                socketio.emit('partida_finalizada', {
                    'motivo': 'tiempo',
                    'ganador': ganador,
                    'elo_blanco': nuevo_elo_blanco,
                    'elo_negro': nuevo_elo_negro
                }, room=sala_id)
                
                print(f"✅ Partida finalizada en sala {sala_id} por tiempo - Ganador: {ganador}")
# Iniciar el monitor de tiempos al arrancar el servidor (se hace al final del archivo)
# --- EVENTOS SOCKET.IO ---

@socketio.on('connect')
def handle_connect(auth=None):  # 🆕 Añadido 'auth=None' para evitar el error de argumentos
    sid = request.sid
    sids_activos[sid] = True
    print(f"✅ Conectado: {sid} | Total activos: {len(sids_activos)}")
    
    # 🆕 Usamos 'emit' (importado de flask_socketio) en lugar de 'socketio.emit'
    emit('actualizar_contador', len(sids_activos), broadcast=True)

@socketio.on('disconnect')
def handle_disconnect(reason=None):
    global usuarios_conectados, cola_espera, partidas_activas, temporizadores_reconexion
    jugador_id = request.sid
    print(f" Jugador {jugador_id} desconectado")
    
    if jugador_id in sids_activos:
        del sids_activos[jugador_id]
    
    nick_desconectado = None
    for nick, sid in list(usuarios_conectados.items()):
        if sid == jugador_id:
            nick_desconectado = nick
            break
    
    print(f"🔍 Nick desconectado: {nick_desconectado}")
    
    sala_id = None
    if jugador_id in partidas_activas:
        sala_id = partidas_activas[jugador_id]
        print(f"   ✅ Sala encontrada por SID: {sala_id}")
    elif nick_desconectado and nick_desconectado in partidas_activas:
        sala_id = partidas_activas[nick_desconectado]
        print(f"   ✅ Sala encontrada por nick: {sala_id}")
    else:
        print(f"   ❌ NO se encontró sala (no está en partida)")
    
    if sala_id and sala_id in salas and not salas[sala_id].get('partida_terminada', False):
        print(f"⚠️ {nick_desconectado} desconectado durante partida en {sala_id}")
        
        salas[sala_id]['desconectado'] = nick_desconectado
        
        clave_desconexion = f"{nick_desconectado}_{sala_id}"
        if clave_desconexion not in desconexiones_por_jugador:
            desconexiones_por_jugador[clave_desconexion] = 0
        desconexiones_por_jugador[clave_desconexion] += 1
        num_desconexiones = desconexiones_por_jugador[clave_desconexion]
        print(f"   🔢 Desconexión #{num_desconexiones} de {nick_desconectado}")
        
        sala = salas[sala_id]
        nb = sala.get('blanco')
        nn = sala.get('negro')
        
        sid_blanco = usuarios_conectados.get(nb)
        sid_negro = usuarios_conectados.get(nn)
        
        otro_jugador_sid = sid_negro if nick_desconectado == nb else sid_blanco
        
        if num_desconexiones >= 2:
            print(f"   ❌ {nick_desconectado} ha superado el límite de desconexiones. Pierde la partida.")
            
            salas[sala_id]['partida_terminada'] = True
            if 'desconectado' in salas[sala_id]:
                del salas[sala_id]['desconectado']
            ganador = 'negro' if nick_desconectado == nb else 'blanco'
            
            try:
                tiempo_partida = sala.get('tiempo', 5)
                categoria = obtener_categoria(tiempo_partida)
                
                eb = obtener_elo(nb, categoria)
                en = obtener_elo(nn, categoria)
                
                if salas[sala_id].get('estadisticas_actualizadas', False):
                    print(f"⚠️ Estadísticas ya actualizadas para {sala_id}, omitiendo...")
                else:
                    if ganador == 'blanco':
                        neb = calcular_elo(eb, en, 'victoria')
                        nen = calcular_elo(en, eb, 'derrota')
                        actualizar_estadisticas_db(nb, 'victoria', categoria)
                        actualizar_estadisticas_db(nn, 'derrota', categoria)
                    else:
                        neb = calcular_elo(eb, en, 'derrota')
                        nen = calcular_elo(en, eb, 'victoria')
                        actualizar_estadisticas_db(nb, 'derrota', categoria)
                        actualizar_estadisticas_db(nn, 'victoria', categoria)
                    salas[sala_id]['estadisticas_actualizadas'] = True
                
                actualizar_elo_db(nb, neb, categoria)
                actualizar_elo_db(nn, nen, categoria)
                
                datos_final = {
                    'motivo': 'desconexion_repetida',
                    'ganador': ganador,
                    'elo_blanco': neb,
                    'elo_negro': nen
                }
                socketio.emit('partida_finalizada', datos_final, room=sala_id)
                
                if otro_jugador_sid and otro_jugador_sid in sids_activos:
                    socketio.emit('partida_finalizada', datos_final, room=otro_jugador_sid)
                
            except Exception as e:
                print(f"❌ Error al finalizar por desconexión repetida: {e}")
            
            if clave_desconexion in desconexiones_por_jugador:
                del desconexiones_por_jugador[clave_desconexion]
            if nick_desconectado in temporizadores_reconexion:
                del temporizadores_reconexion[nick_desconectado]
            if jugador_id in partidas_activas:
                del partidas_activas[jugador_id]
            if nick_desconectado in partidas_activas:
                del partidas_activas[nick_desconectado]
            if nick_desconectado in usuarios_conectados:
                del usuarios_conectados[nick_desconectado]
            if jugador_id in sids_activos:
                del sids_activos[jugador_id]
            
            print(f"   ✅ Partida finalizada por desconexión repetida")
            return
        
        print(f"    Primera desconexión - Iniciando timer de 45s")
        
        if otro_jugador_sid and otro_jugador_sid in sids_activos:
            print(f"   🔔 Notificando a {otro_jugador_sid}")
            emit('rival_desconectado', {
                'mensaje': 'Tu oponente se ha desconectado. Esperando reconexión (60 segundos)...'
            }, room=otro_jugador_sid)
        
        def timeout_reconexion():
            print(f"⏰ EJECUTANDO timeout para {nick_desconectado} en sala {sala_id}")
            
            if sala_id in salas and not salas[sala_id].get('partida_terminada', False):
                salas[sala_id]['partida_terminada'] = True
                if 'desconectado' in salas[sala_id]:
                    del salas[sala_id]['desconectado']
                
                ganador = 'negro' if nick_desconectado == nb else 'blanco'
                
                try:
                    tiempo_partida = sala.get('tiempo', 5)
                    categoria = obtener_categoria(tiempo_partida)
                    
                    eb = obtener_elo(nb, categoria)
                    en = obtener_elo(nn, categoria)
                    
                    if salas[sala_id].get('estadisticas_actualizadas', False):
                        print(f"⚠️ Estadísticas ya actualizadas para {sala_id}, omitiendo...")
                    else:
                        if ganador == 'blanco':
                            neb = calcular_elo(eb, en, 'victoria')
                            nen = calcular_elo(en, eb, 'derrota')
                            actualizar_estadisticas_db(nb, 'victoria', categoria)
                            actualizar_estadisticas_db(nn, 'derrota', categoria)
                        else:
                            neb = calcular_elo(eb, en, 'derrota')
                            nen = calcular_elo(en, eb, 'victoria')
                            actualizar_estadisticas_db(nb, 'derrota', categoria)
                            actualizar_estadisticas_db(nn, 'victoria', categoria)
                        salas[sala_id]['estadisticas_actualizadas'] = True
                    
                    actualizar_elo_db(nb, neb, categoria)
                    actualizar_elo_db(nn, nen, categoria)
                    
                    datos_final = {
                        'motivo': 'desconexion',
                        'ganador': ganador,
                        'elo_blanco': neb,
                        'elo_negro': nen
                    }
                    socketio.emit('partida_finalizada', datos_final, room=sala_id)
                    
                    if otro_jugador_sid and otro_jugador_sid in sids_activos:
                        socketio.emit('partida_finalizada', datos_final, room=otro_jugador_sid)
                    
                except Exception as e:
                    print(f"❌ Error timeout: {e}")
                
                if clave_desconexion in desconexiones_por_jugador:
                    del desconexiones_por_jugador[clave_desconexion]
                if nick_desconectado in temporizadores_reconexion:
                    del temporizadores_reconexion[nick_desconectado]
                if jugador_id in partidas_activas:
                    del partidas_activas[jugador_id]
                if nick_desconectado in partidas_activas:
                    del partidas_activas[nick_desconectado]
                if nick_desconectado in usuarios_conectados:
                    del usuarios_conectados[nick_desconectado]
                if jugador_id in sids_activos:
                    del sids_activos[jugador_id]
                
                print(f"   ✅ Limpieza completada")
        
        timer = threading.Timer(60, timeout_reconexion)
        timer.daemon = True
        timer.start()
        
        if nick_desconectado:
            temporizadores_reconexion[nick_desconectado] = timer
            print(f"    Temporizador guardado para {nick_desconectado}")
        
        print(f"⏳ Temporizador 45s iniciado para {nick_desconectado}")
        return

    print(f" Liberando sesión normal (no estaba en partida)")
    
    if nick_desconectado and nick_desconectado in usuarios_conectados:
        del usuarios_conectados[nick_desconectado]
        print(f"   🗑️ {nick_desconectado} eliminado de usuarios_conectados")
    
    cola_espera = [j for j in cola_espera if j['id'] != jugador_id]
    
    if jugador_id in partidas_activas:
        del partidas_activas[jugador_id]
    if nick_desconectado and nick_desconectado in partidas_activas:
        del partidas_activas[nick_desconectado]
    
        print(f"   ✅ Sesión liberada correctamente")
    
    # 🆕 Añade esta línea al final de la función handle_disconnect (mismo nivel de indentación que el print de arriba)
        #  NUEVO: Eliminar jugador de todos los torneos activos
        # 🆕 CORREGIDO: Limpieza de torneos al desconectar

        # 🆕 CORREGIDO Y SEGURO: Limpieza de torneos al desconectar
        #  CORREGIDO Y SEGURO: Limpieza de torneos al desconectar
    if nick_desconectado:
        for torneo_id, torneo in torneos.items():
            # 1. Sacarlo de la cola de búsqueda para que no lo emparejen mientras está offline
            if torneo_id in colas_torneo and nick_desconectado in colas_torneo[torneo_id]:
                colas_torneo[torneo_id].remove(nick_desconectado)
                print(f"🗑️ {nick_desconectado} sacado de la cola del torneo {torneo['nombre']} por desconexión")
            
            # 2. IMPORTANTE: NO eliminamos al jugador de torneo['jugadores'] ni borramos sus puntos.
            # Una desconexión no debe expulsarlo del torneo. Conservará sus puntos y podrá
            # reconectar para seguir jugando donde lo dejó.
            print(f" {nick_desconectado} mantiene su lugar en el torneo {torneo['nombre']} (está en partida)")
        
        # Notificar a los demás jugadores del torneo
        for jugador_sid in list(sids_activos):
            try:
                emit('clasificacion_torneo', obtener_clasificacion_torneo(torneo_id), room=jugador_sid)
            except:
                pass

    #  Actualizar contador de usuarios
    emit('actualizar_contador', len(sids_activos), broadcast=True)
    
@socketio.on('registro')
def registro(data):
    global usuarios_conectados
    nick = data.get('nick')
    password = data.get('password')
    
    if nick.lower() in [n.lower() for n in usuarios_conectados.keys()]:
        emit('registro_response', {
            'success': False, 
            'message': 'Este usuario ya está conectado'
        })
        return
    
    try:
        # Verificar si el usuario ya existe
        response = supabase.table('usuarios').select('id, nick').ilike('nick', nick).execute()
        if response.data and len(response.data) > 0:
            print(f"⚠️ Nick '{nick}' ya existe")
            emit('registro_response', {'success': False, 'message': 'El nick ya está en uso'})
            return
        
        password_hash = hash_password(password)
        response = supabase.table('usuarios').insert({
    'nick': nick,
    'password_hash': password_hash,
    'elo_bullet': 1200,
    'elo_blitz': 1200,
    'elo_rapid': 1200,
    'partidas_ganadas_bullet': 0,
    'partidas_perdidas_bullet': 0,
    'partidas_tablas_bullet': 0,
    'partidas_ganadas_blitz': 0,
    'partidas_perdidas_blitz': 0,
    'partidas_tablas_blitz': 0,
    'partidas_ganadas_rapid': 0,
    'partidas_perdidas_rapid': 0,
    'partidas_tablas_rapid': 0,
    'pais': 'ES'
}).execute()
        
        user_id = response.data[0]['id']
        print(f"✅ Usuario registrado: {nick} (ID: {user_id})")
        emit('registro_response', {'success': True})
        
    except Exception as e:
        print(f"❌ Error en registro: {e}")
        emit('registro_response', {'success': False, 'message': 'Error al registrar'})

@socketio.on('login')
def login(data):
    global usuarios_conectados
    nick = data.get('nick')
    password = data.get('password')
    es_invitado = data.get('invitado', False)
    sid = request.sid
    
    try:
        if es_invitado:
            print(f" Login de invitado: {nick}")
            
            # 🆕 Añadido 'pais' a la consulta
            response = supabase.table('usuarios').select('id, nick, pais').ilike('nick', nick).execute()
            
            if response.data and len(response.data) > 0:
                user_id = response.data[0]['id']
                nick_real = response.data[0]['nick']
                pais = response.data[0].get('pais', 'OT') # 🆕
                
                usuarios_conectados[nick_real] = sid
                sids_activos[sid] = True
                
                print(f"✅ Invitado reconectado: {nick_real} (ID: {user_id})")
                emit('login_response', {'success': True, 'nick': nick_real, 'userId': user_id, 'invitado': True, 'pais': pais})
                return
            else:
                password_hash = hash_password('invitado_temporal')
                response = supabase.table('usuarios').insert({
                    'nick': nick,
                    'password_hash': password_hash,
                    'elo_bullet': 1200,
                    'elo_blitz': 1200,
                    'elo_rapid': 1200,
                    'pais': 'OT' # 🆕 País por defecto para invitados
                }).execute()
                user_id = response.data[0]['id']
                
                usuarios_conectados[nick] = sid
                sids_activos[sid] = True
                
                print(f"✅ Nuevo invitado registrado: {nick} (ID: {user_id})")
                emit('login_response', {'success': True, 'nick': nick, 'userId': user_id, 'invitado': True, 'pais': 'OT'})
                return
        
        # 🆕 Añadido 'pais' a la consulta
        response = supabase.table('usuarios').select('id, nick, password_hash, pais').ilike('nick', nick).execute()
        
        if not response.data or len(response.data) == 0:
            emit('login_response', {'success': False, 'message': 'Usuario no encontrado'})
            return
        
        user = response.data[0]
        user_id = user['id']
        nick_real = user['nick']
        stored_password = user['password_hash']
        pais = user.get('pais', 'ES') # 🆕 Obtener el país guardado
        
        if verify_password(stored_password, password):
            if nick_real in temporizadores_reconexion:
                print(f"🔄 RECONEXIÓN DETECTADA para {nick_real}")
                timer = temporizadores_reconexion[nick_real]
                if hasattr(timer, 'cancel'):
                    timer.cancel()
                del temporizadores_reconexion[nick_real]
                
                if nick_real in usuarios_conectados:
                    old_sid = usuarios_conectados[nick_real]
                    if old_sid in sids_activos:
                        del sids_activos[old_sid]
                    del usuarios_conectados[nick_real]
                
                if nick_real in partidas_activas:
                    del partidas_activas[nick_real]
                
                usuarios_conectados[nick_real] = sid
                sids_activos[sid] = True
                
                print(f"✅ Reconexión exitosa: {nick_real} -> {sid}")
                emit('login_response', {'success': True, 'nick': nick_real, 'userId': user_id, 'reconexion': True, 'pais': pais})
                return
            
            if nick_real in usuarios_conectados:
                old_sid = usuarios_conectados[nick_real]
                
                if old_sid not in sids_activos:
                    print(f"🔄 SID antiguo inactivo para {nick_real}, permitiendo login")
                    del usuarios_conectados[nick_real]
                    
                    if nick_real in partidas_activas:
                        del partidas_activas[nick_real]
                    
                    usuarios_conectados[nick_real] = sid
                    sids_activos[sid] = True
                    
                    print(f"✅ Login exitoso (reconexión automática): {nick_real} -> {sid}")
                    emit('login_response', {'success': True, 'nick': nick_real, 'userId': user_id, 'pais': pais})
                    return
                else:
                    print(f"⚠️ {nick_real} ya está conectado en {old_sid}")
                    emit('login_response', {'success': False, 'message': 'Este usuario ya está conectado en otro dispositivo'})
                    return
            
            usuarios_conectados[nick_real] = sid
            sids_activos[sid] = True
            print(f"✅ Login exitoso: {nick_real} (ID: {user_id}) - Session: {sid} - País: {pais}")
            emit('login_response', {'success': True, 'nick': nick_real, 'userId': user_id, 'pais': pais})
        else:
            emit('login_response', {'success': False, 'message': 'Contraseña incorrecta'})
            
    except Exception as e:
        print(f"❌ Error en login: {e}")
        emit('login_response', {'success': False, 'message': 'Error al iniciar sesión'})

@socketio.on('reconectar_sesion')
def reconectar_sesion(data):
    global usuarios_conectados
    nick = data.get('nick')
    nuevo_sid = request.sid
    
    if nick:
        if nick in temporizadores_reconexion:
            print(f"🔄 Cancelando temporizador de reconexión para {nick}")
            timer = temporizadores_reconexion[nick]
            if hasattr(timer, 'cancel'):
                timer.cancel()
            del temporizadores_reconexion[nick]
        
        if nick in usuarios_conectados:
            print(f"🔄 Sesión reconectada: {nick} -> {nuevo_sid}")
        else:
            print(f"✅ Sesión registrada por reconexión: {nick} -> {nuevo_sid}")
        
        usuarios_conectados[nick] = nuevo_sid
        sids_activos[nuevo_sid] = True

@socketio.on('verificar_registro')
def verificar_registro(data):
    nick = data.get('nick')
    ip_cliente = request.remote_addr
    
    if ip_cliente not in control_nicks:
        control_nicks[ip_cliente] = []
    
    if nick in control_nicks[ip_cliente]:
        print(f"⚠️ Nick '{nick}' ya existe para IP {ip_cliente}")
        emit('error_registro', {'mensaje': 'Ya tienes este nick registrado'})
        return
    
    if len(control_nicks[ip_cliente]) >= MAX_NICKS_POR_IP:
        print(f"❌ Límite de nicks alcanzado para IP {ip_cliente}")
        emit('error_registro', {
            'mensaje': f'Máximo {MAX_NICKS_POR_IP} nicks permitidos desde un mismo ordenador'
        })
        return
    
    control_nicks[ip_cliente].append(nick)
    print(f"✅ Nick '{nick}' registrado desde IP {ip_cliente} ({len(control_nicks[ip_cliente])}/{MAX_NICKS_POR_IP})")
    
    emit('registro_permitido', {'nick': nick})

@socketio.on('eliminar_cuenta')
def eliminar_cuenta(data):
    nick = data.get('nick')
    password = data.get('password')
    ip_cliente = request.remote_addr
    
    try:
        response = supabase.table('usuarios').select('id, nick, password_hash').ilike('nick', nick).execute()
        
        if not response.data or len(response.data) == 0:
            emit('eliminar_response', {'success': False, 'message': 'Usuario no encontrado'})
            return
        
        user = response.data[0]
        user_id = user['id']
        nick_real = user['nick']
        stored_password = user['password_hash']
        
        if not verify_password(stored_password, password):
            emit('eliminar_response', {'success': False, 'message': 'Contraseña incorrecta'})
            return
        
        supabase.table('usuarios').delete().eq('nick', nick_real).execute()
        
        if ip_cliente in control_nicks and nick_real in control_nicks[ip_cliente]:
            control_nicks[ip_cliente].remove(nick_real)
            print(f"🗑️ Nick '{nick_real}' eliminado desde IP {ip_cliente}")
        
        emit('eliminar_response', {'success': True, 'message': 'Cuenta eliminada correctamente'})
        print(f"✅ Cuenta '{nick_real}' eliminada permanentemente")
        
    except Exception as e:
        print(f"❌ Error al eliminar cuenta: {e}")
        emit('eliminar_response', {'success': False, 'message': 'Error al eliminar cuenta'})
        # ==========================================
# 🤖 SISTEMA DE BOT PARA PARTIDAS NORMALES
# ==========================================
class BotAjedrez:
    def __init__(self, nivel='medio'):
        self.nivel = nivel
        self.board = chess.Board()
        
    def obtener_movimiento(self, fen):
        try:
            self.board.set_fen(fen)
            movimientos = list(self.board.legal_moves)
            if not movimientos:
                return None
            if self.nivel == 'facil':
                return random.choice(movimientos)
            elif self.nivel == 'medio':
                # Prioriza capturas y jaques
                for move in movimientos:
                    if self.board.is_capture(move) or self.board.gives_check(move):
                        return move
                return random.choice(movimientos)
            else:
                return random.choice(movimientos)
        except Exception as e:
            print(f"❌ Error en bot: {e}")
            return None

def activar_bot_contra_jugador(jugador_id, data):
    with app.app_context():
        print(f" Activando bot para {jugador_id}...")
        if data.get('esTorneo') == 'true':
            print("⚠️ No activar bot: es torneo")
            return
        
        sala_id = str(uuid.uuid4())
        nick_jugador = data.get('usuario', 'Anónimo')
        nick_bot = f"🤖 FighterBot"
        
        color_jugador = data.get('color', 'random')
        if color_jugador == 'random':
            color_jugador = 'white' if random.random() < 0.5 else 'black'
        
        color_bot = 'black' if color_jugador == 'white' else 'white'
        nick_blanco = nick_jugador if color_jugador == 'white' else nick_bot
        nick_negro = nick_bot if color_jugador == 'white' else nick_jugador
        
        tiempo_inicial = data.get('tiempo', 5) * 60
        salas[sala_id] = {
            'blanco': nick_blanco, 'negro': nick_negro, 'partida_terminada': False,
            'tiempo': data.get('tiempo', 5), 'incremento': data.get('incremento', 0),
            'estadisticas_actualizadas': False, 'tiempo_restante_blanco': tiempo_inicial,
            'tiempo_restante_negro': tiempo_inicial, 'ultimo_movimiento': time.time(),
            'turno': 'blanco', 'es_bot': True, 'bot_color': color_bot
        }
        
        # ✅ NO USAR join_room - Guardamos solo en partidas_activas
        partidas_activas[jugador_id] = sala_id
        
        categoria = obtener_categoria(data.get('tiempo', 5))
        elo_jugador = obtener_elo(nick_jugador, categoria)
        
        # ✅ Emitir directamente al jugador (sin join_room)
        socketio.emit('partida_encontrada', {
            'sala': sala_id, 'color': color_jugador, 'config': data,
            'rival_nick': nick_bot, 'mi_elo': elo_jugador, 'rival_elo': 1200,
            'segundos_blanco': tiempo_inicial, 'segundos_negro': tiempo_inicial,
            'es_bot': True
        }, room=jugador_id)
        
        print(f"✅ Bot activado: {nick_jugador} vs {nick_bot} | Sala: {sala_id}")
        
        # Iniciar el bot en otro hilo
        threading.Thread(target=jugar_bot, args=(sala_id, color_bot, jugador_id), daemon=True).start()

def jugar_bot(sala_id, color_bot, jugador_id):
    time.sleep(2)
    bot = BotAjedrez(nivel='medio')
    
    while sala_id in salas and not salas[sala_id].get('partida_terminada', False):
        sala = salas[sala_id]
        turno_bot = (color_bot == 'white' and sala.get('turno') == 'blanco') or \
                    (color_bot == 'black' and sala.get('turno') == 'negro')
        
        if turno_bot:
            # 1. Obtener el FEN más reciente del servidor
            fen_actual = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
            if sala_id in estado_partidas:
                fen_actual = estado_partidas[sala_id].get('fen', fen_actual)
            
            print(f"🔍 DEBUG BOT - Sala: {sala_id} | Color Bot: {color_bot.upper()} | Turno Sala: {sala.get('turno')}")
            print(f"🔍 DEBUG BOT - FEN que lee el bot: {fen_actual}")
            
            try:
                bot.board.set_fen(fen_actual)
            except Exception as e:
                print(f"❌ Error al cargar FEN en el bot: {e}. Reiniciando tablero.")
                bot.board.reset()
            
            # 2. Verificación de seguridad: ¿El tablero del bot coincide con su color?
            es_turno_bot_en_tablero = (color_bot == 'white' and bot.board.turn == chess.WHITE) or \
                                      (color_bot == 'black' and bot.board.turn == chess.BLACK)
            
            if not es_turno_bot_en_tablero:
                print(f"⚠️ ADVERTENCIA: El FEN dice que es turno de {'Blancas' if bot.board.turn == chess.WHITE else 'Negras'}, pero el bot es {color_bot.upper()}. Esperando sincronización...")
                time.sleep(1)
                continue

            # 3. Obtener movimiento
            movimiento = bot.obtener_movimiento(fen_actual)
            if movimiento:
                san = bot.board.san(movimiento)
                print(f"🤖 El bot (como {color_bot.upper()}) elige jugar: {san}")
                
                bot.board.push(movimiento)
                fen_nuevo = bot.board.fen()
                
                ahora = time.time()
                tiempo_transcurrido = ahora - sala['ultimo_movimiento']
                incremento = float(sala.get('incremento', 0))
                
                if sala['turno'] == 'blanco':
                    sala['tiempo_restante_blanco'] = max(0, sala['tiempo_restante_blanco'] - tiempo_transcurrido) + incremento
                    sala['turno'] = 'negro'
                else:
                    sala['tiempo_restante_negro'] = max(0, sala['tiempo_restante_negro'] - tiempo_transcurrido) + incremento
                    sala['turno'] = 'blanco'
                sala['ultimo_movimiento'] = time.time()
                
                if sala_id not in estado_partidas:
                    estado_partidas[sala_id] = {'fen': 'start', 'movimientos': []}
                estado_partidas[sala_id]['fen'] = fen_nuevo
                
                promo_map = {
                    chess.QUEEN: 'q', chess.ROOK: 'r',
                    chess.BISHOP: 'b', chess.KNIGHT: 'n'
                }
                promo_str = promo_map.get(movimiento.promotion, 'q') if movimiento.promotion else None
                
                estado_partidas[sala_id]['movimientos'].append({
                    'from': chess.square_name(movimiento.from_square),
                    'to': chess.square_name(movimiento.to_square),
                    'promotion': promo_str
                })
                
                with app.app_context():
                    socketio.emit('recibir_movimiento', {
                        'movimiento': {
                            'from': chess.square_name(movimiento.from_square),
                            'to': chess.square_name(movimiento.to_square),
                            'promotion': promo_str
                        }
                    }, room=sala_id)
                
                print(f"✅ Movimiento del bot enviado: {san}")
                
                if bot.board.is_game_over():
                    salas[sala_id]['partida_terminada'] = True
                    print(f"🏁 Partida contra bot finalizada en {sala_id}")
                    break
        time.sleep(1)

@socketio.on('buscar_partida')
def buscar_partida(data):
    jugador_id = request.sid
    usuario = data.get('usuario', 'Anónimo')
    
    usuario_conectado = None
    for n in usuarios_conectados.keys():
        if n.lower() == usuario.lower():
            usuario_conectado = n
            break
    
    if usuario_conectado is None:
        print(f"❌ Usuario {usuario} no está conectado")
        emit('error_busqueda', {'mensaje': 'Debes iniciar sesión primero'})
        return
    
    if usuarios_conectados[usuario_conectado] != jugador_id:
        print(f"🔄 Actualizando sesión de {usuario_conectado}: {usuarios_conectados[usuario_conectado]} -> {jugador_id}")
        usuarios_conectados[usuario_conectado] = jugador_id
    
    print(f"🔍 Jugador {jugador_id} ({usuario_conectado}) busca partida")
    
    if len(cola_espera) > 0:
        for i, rival in enumerate(cola_espera):
            if rival['data'].get('usuario', '').lower() == usuario.lower():
                print(f"⚠️ {usuario} intentando jugar contra sí mismo")
                continue
            
            tiempo_rival = rival['data'].get('tiempo', 0)
            tiempo_mio = data.get('tiempo', 0)
            incremento_rival = rival['data'].get('incremento', 0)
            incremento_mio = data.get('incremento', 0)
            
            if tiempo_rival != tiempo_mio or incremento_rival != incremento_mio:
                print(f"⚠️ {usuario} ({tiempo_mio}+{incremento_mio}s) no coincide con {rival['data'].get('usuario')} ({tiempo_rival}+{incremento_rival}s)")
                continue
            
            rival_color = rival['data']['color']
            mi_color = data['color']
            
            if rival_color == 'random' and mi_color == 'random':
                if random.random() < 0.5:
                    color_rival_final = 'white'
                    color_mio_final = 'black'
                else:
                    color_rival_final = 'black'
                    color_mio_final = 'white'
            elif rival_color == 'random':
                color_rival_final = 'black' if mi_color == 'white' else 'white'
                color_mio_final = mi_color
            elif mi_color == 'random':
                color_mio_final = 'black' if rival_color == 'white' else 'white'
                color_rival_final = rival_color
            else:
                if rival_color == mi_color:
                    continue
                else:
                    color_rival_final = rival_color
                    color_mio_final = mi_color
            
            # --- AQUÍ SE CREA LA SALA Y sala_id ---
            cola_espera.pop(i)
                        # 🤖 Cancelar temporizador del bot si encuentra rival humano
            if jugador_id in temporizadores_bot:
                temporizadores_bot[jugador_id].cancel()
                del temporizadores_bot[jugador_id]
            sala_id = str(uuid.uuid4())
            
            jugador1 = {
                'id': rival['id'], 
                'color': color_rival_final,
                'nick': rival['data'].get('usuario', 'Anónimo')
            }
            jugador2 = {
                'id': jugador_id, 
                'color': color_mio_final,
                'nick': usuario
            }
            
            nick_blanco = jugador1['nick'] if color_rival_final == 'white' else jugador2['nick']
            nick_negro = jugador1['nick'] if color_rival_final == 'black' else jugador2['nick']
            
            tiempo_inicial_segundos = data.get('tiempo', 5) * 60
            salas[sala_id] = {
                'blanco': nick_blanco,
                'negro': nick_negro,
                'partida_terminada': False,
                'tiempo': data.get('tiempo'),
                'incremento': data.get('incremento', 0),
                'estadisticas_actualizadas': False,
                'tiempo_restante_blanco': tiempo_inicial_segundos, # 🆕
                'tiempo_restante_negro': tiempo_inicial_segundos,   # 🆕
                'ultimo_movimiento': time.time(),                   # 🆕 Timestamp del servidor
                'turno': 'blanco'                                   # 🆕 Quién empieza
            
            }
            
            join_room(sala_id, sid=jugador1['id'])
            join_room(sala_id, sid=jugador2['id'])
            
            categoria = obtener_categoria(data.get('tiempo', 5))
            elo1 = obtener_elo(jugador1['nick'], categoria)
            elo2 = obtener_elo(jugador2['nick'], categoria)
            
            # --- EMITIR DENTRO DEL BLOQUE DONDE sala_id YA EXISTE ---
            emit('partida_encontrada', {
                'sala': sala_id,
                'color': color_rival_final,
                'config': data,
                'rival_nick': usuario,
                'mi_elo': elo1,
                'rival_elo': elo2,
                'segundos_blanco': tiempo_inicial_segundos,
                'segundos_negro': tiempo_inicial_segundos
            }, room=jugador1['id'])
            
            emit('partida_encontrada', {
                'sala': sala_id,
                'color': color_mio_final,
                'config': data,
                'rival_nick': rival['data'].get('usuario', 'Anónimo'),
                'mi_elo': elo2,
                'rival_elo': elo1,
                'segundos_blanco': tiempo_inicial_segundos,
                'segundos_negro': tiempo_inicial_segundos
            }, room=jugador2['id'])
            
            print(f"✅ Partida creada: {jugador1['nick']} vs {jugador2['nick']} | Sala: {sala_id}")
            emitir_cola_espera()
            return  # <--- IMPORTANTE: Salir de la función aquí
        
        # Si el bucle termina sin encontrar rival (ej: todos tienen el mismo color)
        cola_espera.append({'id': jugador_id, 'data': data})
                # 🤖 Programar activación del bot si es partida normal (15 segundos)
        if data.get('esTorneo') != 'true':
            timer = threading.Timer(15, activar_bot_contra_jugador, args=(jugador_id, data))
            timer.daemon = True
            timer.start()
            temporizadores_bot[jugador_id] = timer
            print(f"⏰ Temporizador bot iniciado para {usuario} (15s)")
        emit('esperando_rival', {'mensaje': f'Esperando rival que elija {data.get("tiempo", 5)} minutos...'})
        print(f"⏳ Jugador {usuario} en cola esperando rival con {data.get('tiempo', 5)} min")
        emitir_cola_espera()
    else:
        cola_espera.append({'id': jugador_id, 'data': data})
                # 🤖 Programar activación del bot si es partida normal (15 segundos)
        if data.get('esTorneo') != 'true':
            timer = threading.Timer(15, activar_bot_contra_jugador, args=(jugador_id, data))
            timer.daemon = True
            timer.start()
            temporizadores_bot[jugador_id] = timer
            print(f"⏰ Temporizador bot iniciado para {usuario} (15s)")
        emit('esperando_rival', {'mensaje': 'Esperando a que se conecte un rival...'})
        print(f"⏳ Jugador {usuario} en cola de espera")
        emitir_cola_espera()
@socketio.on('pedir_cola_espera')
def pedir_cola_espera():
    print(f"📋 Alguien pidió la cola manualmente")
    emitir_cola_espera()       

@socketio.on('reunirse_a_sala')
def reunirse_a_sala(data):
    sala_id = data.get('sala')
    jugador_id = request.sid
    
    import time
    
    nick = None
    for n, sid in usuarios_conectados.items():
        if sid == jugador_id:
            nick = n
            break
    
    print(f"🔄 Reunirse a sala - Jugador: {jugador_id}, Nick: {nick}, Sala: {sala_id}")

    if sala_id in salas:
        join_room(sala_id, sid=jugador_id)
        partidas_activas[jugador_id] = sala_id
        
        if nick:
            partidas_activas[nick] = sala_id
            print(f"✅ {nick} registrado en partidas_activas (sid y nick)")
            
            if salas[sala_id].get('desconectado') == nick:
                print(f"✅ {nick} se ha reconectado. Emitiendo rival_reconectado")
            # ✅ NO emitir si es partida contra bot
            if not salas[sala_id].get('es_bot', False):
                socketio.emit('rival_reconectado', {}, room=sala_id)
            del salas[sala_id]['desconectado']
            
            if nick in temporizadores_reconexion:
                print(f"🔄 Cancelando temporizador de reconexión para {nick}")
                timer = temporizadores_reconexion[nick]
                if hasattr(timer, 'cancel'):
                    timer.cancel()
                del temporizadores_reconexion[nick]
                print(f"✅ Reconexión exitosa de {nick}")
        else:
            print(f"⚠️ ADVERTENCIA: No se encontró nick para {jugador_id}")
            
@socketio.on('solicitar_estado_partida')
def solicitar_estado_partida(data):
    sala_id = data.get('sala')
    
    if sala_id and sala_id in salas:
        sala = salas[sala_id]
        
        fen_actual = 'start'
        movimientos = []
        if sala_id in estado_partidas:
            fen_actual = estado_partidas[sala_id].get('fen', 'start')
            movimientos = estado_partidas[sala_id].get('movimientos', [])
        
        # 🆕 Calcular tiempos reales al reconectar
        ahora = time.time()
        tiempo_transcurrido = ahora - sala.get('ultimo_movimiento', ahora)
        t_b = sala.get('tiempo_restante_blanco', 300)
        t_n = sala.get('tiempo_restante_negro', 300)
        
        if sala.get('turno') == 'blanco':
            segundos_blanco = max(0, t_b - tiempo_transcurrido)
            segundos_negro = t_n
        else:
            segundos_blanco = t_b
            segundos_negro = max(0, t_n - tiempo_transcurrido)
        
        emit('estado_partida', {
            'fen': fen_actual,
            'movimientos': movimientos,
            'segundos_blanco': round(segundos_blanco, 1),
            'segundos_negro': round(segundos_negro, 1),
            'terminada': sala.get('partida_terminada', False)
        })
        
        print(f"📊 Estado enviado a reconectado - Tiempos reales: B={round(segundos_blanco, 1)}s, N={round(segundos_negro, 1)}s")
        
@socketio.on('actualizar_tiempos')
def actualizar_tiempos(data):
    sala_id = data.get('sala')
    segundos_blanco = data.get('segundos_blanco')
    segundos_negro = data.get('segundos_negro')
    
    if sala_id in salas:
        if segundos_blanco is not None:
            salas[sala_id]['segundos_blanco'] = segundos_blanco
        if segundos_negro is not None:
            salas[sala_id]['segundos_negro'] = segundos_negro

@socketio.on('mover_pieza')
def mover_pieza(data):
    sala_id = data.get('sala')
    movimiento = data.get('movimiento')
    fen = data.get('fen')
    # Ya no necesitamos que el cliente nos envíe los segundos, el servidor los calcula
    
    if sala_id in salas:
        sala = salas[sala_id]
        
        if sala_id not in estado_partidas:
            estado_partidas[sala_id] = {
                'fen': 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
                'movimientos': []
            }
        
        if fen:
            estado_partidas[sala_id]['fen'] = fen
        estado_partidas[sala_id]['movimientos'].append(movimiento)
        
        # --- CÁLCULO DE TIEMPO DEL SERVIDOR ---
        ahora = time.time()
        tiempo_transcurrido = ahora - sala['ultimo_movimiento']
        
        # ✅ FIX: Convertir incremento a número (float) para evitar errores
        incremento = float(sala.get('incremento', 0))
        
        if sala['turno'] == 'blanco':
            # Restar tiempo gastado y sumar incremento
            sala['tiempo_restante_blanco'] = max(0, sala['tiempo_restante_blanco'] - tiempo_transcurrido) + incremento
            sala['turno'] = 'negro'
        else:
            sala['tiempo_restante_negro'] = max(0, sala['tiempo_restante_negro'] - tiempo_transcurrido) + incremento
            sala['turno'] = 'blanco'
            
        # Actualizar el timestamp para el siguiente movimiento
        sala['ultimo_movimiento'] = time.time()
        
        print(f"💾 Movimiento en {sala_id}. Turno ahora: {sala['turno']}. Tiempos reales: B={round(sala['tiempo_restante_blanco'], 1)}s, N={round(sala['tiempo_restante_negro'], 1)}s")
        
        emit('recibir_movimiento', {
            'movimiento': movimiento
        }, room=sala_id, include_self=False)
        
@socketio.on('cancelar_partida')
def cancelar_partida(data):
    sala_id = data.get('sala')
    nick = data.get('nick')
    
    if not sala_id or sala_id not in salas:
        emit('error_cancelar', {'mensaje': 'Partida no encontrada'})
        return
    
    partida = salas[sala_id]
    nb = partida.get('blanco')
    nn = partida.get('negro')
    
    # Contar cuántos movimientos se han hecho en total
    num_movimientos = 0
    if sala_id in estado_partidas and 'movimientos' in estado_partidas[sala_id]:
        num_movimientos = len(estado_partidas[sala_id]['movimientos'])
    
    # Verificar si el jugador que intenta cancelar YA hizo su primera jugada
    if nick == nb and num_movimientos >= 1:
        emit('error_cancelar', {'mensaje': 'Ya has hecho tu primera jugada, no puedes cancelar.'})
        return
    elif nick == nn and num_movimientos >= 2:
        emit('error_cancelar', {'mensaje': 'Ya has hecho tu primera jugada, no puedes cancelar.'})
        return
    
    print(f"🚪 {nick} cancela la partida en sala {sala_id} (Movimientos totales: {num_movimientos})")
    
    sid_blanco = usuarios_conectados.get(nb)
    sid_negro = usuarios_conectados.get(nn)
    
    # Notificar al rival
    rival_sid = sid_negro if nick == nb else sid_blanco
    if rival_sid and rival_sid in sids_activos:
        emit('partida_cancelada', {
            'mensaje': 'Tu rival canceló la partida. Vuelves a la sala de espera.'
        }, room=rival_sid)
    
    # Limpiar la partida de las variables globales (sin tocar ELO)
    if sala_id in salas:
        del salas[sala_id]
    if sala_id in estado_partidas:
        del estado_partidas[sala_id]
    if sala_id in control_tablas:
        del control_tablas[sala_id]
    
    # Limpiar registros de partida activa para ambos jugadores
    if nb in partidas_activas: del partidas_activas[nb]
    if nn in partidas_activas: del partidas_activas[nn]
    if sid_blanco and sid_blanco in partidas_activas: del partidas_activas[sid_blanco]
    if sid_negro and sid_negro in partidas_activas: del partidas_activas[sid_negro]
    
    # Confirmar al jugador que canceló
    emit('partida_cancelada', {
        'mensaje': 'Partida cancelada. No hubo penalización de ELO.'
    })
    
    print(f"✅ Partida {sala_id} cancelada sin penalización y limpiada correctamente")

@socketio.on('fin_partida')
def fin_partida(data):
    sala_id = data.get('sala')
    motivo = data.get('motivo')
    ganador = data.get('ganador')
    
    # 1. Verificar si la partida ya terminó
    if sala_id in salas and salas[sala_id].get('partida_terminada', False):
        print(f"⚠️ Intento de finalizar partida ya terminada en {sala_id}, ignorando...")
        return
    
    # 2. Normalizar el ganador (white/black → blanco/negro)
    if ganador == 'white':
        ganador = 'blanco'
    elif ganador == 'black':
        ganador = 'negro'
    
    # 3. MARCAR LA PARTIDA COMO TERMINADA INMEDIATAMENTE
    #    Esto evita que 'aceptar_tablas' pueda sobrescribir el resultado
    if sala_id in salas:
        salas[sala_id]['partida_terminada'] = True
        if 'desconectado' in salas[sala_id]:
            del salas[sala_id]['desconectado']
    
    # 4. Limpiar temporizadores de reconexión
    for sid, s_id in list(partidas_activas.items()):
        if s_id == sala_id:
            nick_temp = None
            for n, s in usuarios_conectados.items():
                if s == sid:
                    nick_temp = n
                    break
            if nick_temp and nick_temp in temporizadores_reconexion:
                timer = temporizadores_reconexion[nick_temp]
                if hasattr(timer, 'cancel'):
                    timer.cancel()
                del temporizadores_reconexion[nick_temp]

    # 5. Limpiar control de tablas
    if sala_id in control_tablas:
        del control_tablas[sala_id]
        print(f"🗑️ Control de tablas reseteado en sala {sala_id}")
    
    # 6. Procesar ELO y estadísticas
    sala = salas[sala_id]
    nick_blanco = sala.get('blanco')
    nick_negro = sala.get('negro')
    
    tiempo_partida = sala.get('tiempo', 5)
    categoria = obtener_categoria(tiempo_partida)
    print(f"📊 Categoría de la partida: {categoria} ({tiempo_partida} min)")
    
    nuevo_elo_blanco = 1200
    nuevo_elo_negro = 1200
    
    try:
        elo_blanco = obtener_elo(nick_blanco, categoria)
        elo_negro = obtener_elo(nick_negro, categoria)
        
        print(f" Partida: {nick_blanco} (ELO {categoria}: {elo_blanco}) vs {nick_negro} (ELO {categoria}: {elo_negro})")
        print(f"🏆 Ganador: {ganador} | Motivo: {motivo}")
        
        if ganador == 'blanco':
            nuevo_elo_blanco = calcular_elo(elo_blanco, elo_negro, 'victoria')
            nuevo_elo_negro = calcular_elo(elo_negro, elo_blanco, 'derrota')
            
            if not salas[sala_id].get('estadisticas_actualizadas', False):
                actualizar_estadisticas_db(nick_blanco, 'victoria', categoria)
                actualizar_estadisticas_db(nick_negro, 'derrota', categoria)
                salas[sala_id]['estadisticas_actualizadas'] = True
            
        elif ganador == 'negro':
            nuevo_elo_blanco = calcular_elo(elo_blanco, elo_negro, 'derrota')
            nuevo_elo_negro = calcular_elo(elo_negro, elo_blanco, 'victoria')
            
            if not salas[sala_id].get('estadisticas_actualizadas', False):
                actualizar_estadisticas_db(nick_blanco, 'derrota', categoria)
                actualizar_estadisticas_db(nick_negro, 'victoria', categoria)
                salas[sala_id]['estadisticas_actualizadas'] = True
            
        else:
            nuevo_elo_blanco = calcular_elo(elo_blanco, elo_negro, 'tablas')
            nuevo_elo_negro = calcular_elo(elo_negro, elo_blanco, 'tablas')
            
            if not salas[sala_id].get('estadisticas_actualizadas', False):
                actualizar_estadisticas_db(nick_blanco, 'tablas', categoria)
                actualizar_estadisticas_db(nick_negro, 'tablas', categoria)
                salas[sala_id]['estadisticas_actualizadas'] = True
        
        print(f"📊 {nick_blanco}: {elo_blanco} → {nuevo_elo_blanco}")
        print(f"📊 {nick_negro}: {elo_negro} → {nuevo_elo_negro}")
        
        actualizar_elo_db(nick_blanco, nuevo_elo_blanco, categoria)
        actualizar_elo_db(nick_negro, nuevo_elo_negro, categoria)
        
        salas[sala_id]['elo_blanco'] = nuevo_elo_blanco
        salas[sala_id]['elo_negro'] = nuevo_elo_negro
        print(f"💾 ELOs guardados en sala {sala_id}: Blanco={nuevo_elo_blanco}, Negro={nuevo_elo_negro}")
        
    except Exception as e:
        print(f"❌ Error al actualizar ELO: {e}")
    
    # 7. ✅ CORREGIDO: Usar las variables reales 'motivo' y 'ganador'
    emit('partida_finalizada', {
        'motivo': motivo,
        'ganador': ganador,
        'elo_blanco': nuevo_elo_blanco,
        'elo_negro': nuevo_elo_negro
    }, room=sala_id)
    print(f"✅ Partida finalizada en sala {sala_id} - Motivo: {motivo} - Ganador: {ganador}")
    
    # 8. ✅ CORREGIDO: Actualizar puntos del torneo según el resultado REAL
    if sala_id in partidas_torneo_activas:
        partida_torneo = partidas_torneo_activas[sala_id]
        torneo_id = partida_torneo['torneo_id']
        
        if torneo_id in torneos:
            torneo = torneos[torneo_id]
            
            # Determinar quién es jugador1 y jugador2 según el color
            j1 = partida_torneo['jugador1']
            j2 = partida_torneo['jugador2']
            
            if ganador == 'blanco':
                # Blanco gana: +2 al blanco, +0 al negro
                if partida_torneo.get('color1') == 'white':
                    torneo['puntos'][j1] = torneo['puntos'].get(j1, 0) + 2
                    torneo['puntos'][j2] = torneo['puntos'].get(j2, 0) + 0
                    print(f"🏆 {j1} (blanco) gana +2 pts, {j2} (negro) +0 pts")
                else:
                    torneo['puntos'][j1] = torneo['puntos'].get(j1, 0) + 0
                    torneo['puntos'][j2] = torneo['puntos'].get(j2, 0) + 2
                    print(f"🏆 {j2} (blanco) gana +2 pts, {j1} (negro) +0 pts")
                    
            elif ganador == 'negro':
                # Negro gana: +0 al blanco, +2 al negro
                if partida_torneo.get('color1') == 'white':
                    torneo['puntos'][j1] = torneo['puntos'].get(j1, 0) + 0
                    torneo['puntos'][j2] = torneo['puntos'].get(j2, 0) + 2
                    print(f"🏆 {j2} (negro) gana +2 pts, {j1} (blanco) +0 pts")
                else:
                    torneo['puntos'][j1] = torneo['puntos'].get(j1, 0) + 2
                    torneo['puntos'][j2] = torneo['puntos'].get(j2, 0) + 0
                    print(f"🏆 {j1} (negro) gana +2 pts, {j2} (blanco) +0 pts")
                    
            else:
                # Tablas: +1 a cada uno
                torneo['puntos'][j1] = torneo['puntos'].get(j1, 0) + 1
                torneo['puntos'][j2] = torneo['puntos'].get(j2, 0) + 1
                print(f"🤝 Tablas: {j1} +1 pt, {j2} +1 pt")
            
            print(f"📊 Puntos actuales en torneo {torneo['nombre']}: {torneo['puntos']}")
            
            del partidas_torneo_activas[sala_id]
            
            # Liberar jugadores para nuevo emparejamiento
            for jugador in torneo['jugadores']:
                sid = usuarios_conectados.get(jugador)
                if sid and sid in sids_activos:
                    socketio.emit('clasificacion_torneo', 
                                 obtener_clasificacion_torneo(torneo_id), room=sid)
                    socketio.emit('jugadores_torneo', 
                                 torneo['jugadores'], room=sid)
                    socketio.emit('puedes_buscar', room=sid)
                    print(f"🔄 {jugador} liberado para nuevo emparejamiento")
                
@socketio.on('verificar_partida')
def verificar_partida(data):
    sala_id = data.get('sala')
    jugador_id = request.sid
    
    if sala_id and sala_id in salas:
        sala = salas[sala_id]
        
        if sala.get('partida_terminada', False):
            nb = sala.get('blanco')
            nn = sala.get('negro')
            tiempo_partida = sala.get('tiempo', 5)
            categoria = obtener_categoria(tiempo_partida)
            eb = obtener_elo(nb, categoria)
            en = obtener_elo(nn, categoria)
            return {
                'terminada': True,
                'ganador': 'blanco' if eb > en else 'negro',
                'elo_blanco': eb,
                'elo_negro': en
            }
        else:
            nb = sala.get('blanco')
            nn = sala.get('negro')
            
            sid_blanco = usuarios_conectados.get(nb)
            sid_negro = usuarios_conectados.get(nn)
            
            blanco_conectado = sid_blanco in sids_activos if sid_blanco else False
            negro_conectado = sid_negro in sids_activos if sid_negro else False
            
            nick_consultante = None
            for n, sid in usuarios_conectados.items():
                if sid == jugador_id:
                    nick_consultante = n
                    break
            
            rival_conectado = negro_conectado if nick_consultante == nb else blanco_conectado
            
            tiempo_inicial = sala.get('tiempo', 5) * 60
            tiempos = {'blanco': sala.get('segundos_blanco', tiempo_inicial), 
                      'negro': sala.get('segundos_negro', tiempo_inicial)}
            
            print(f"🔍 Verificación - Sala: {sala_id}, Rival conectado: {rival_conectado}")
            
            return {
                'terminada': False,
                'rival_conectado': rival_conectado,
                'tiempos': tiempos
            }
    else:
        return {'terminada': False, 'error': 'Sala no encontrada'}

@socketio.on('oferta_tablas')
def oferta_tablas(data):
    sala_id = data.get('sala')
    jugador_id = request.sid
    
    if sala_id not in salas:
        return
    
    if sala_id not in control_tablas:
        control_tablas[sala_id] = {
            'ofertas': 0,
            'ultima_oferta': 0,
            'jugador': None
        }
    
    control = control_tablas[sala_id]
    ahora = time.time()
    
    if control['jugador'] == jugador_id:
        if control['ofertas'] >= 2:
            emit('error_tablas', {'mensaje': 'Ya has agotado tus 2 ofertas de tablas en esta partida'})
            return
        
        tiempo_pasado = ahora - control['ultima_oferta']
        if tiempo_pasado < 60:
            segundos_restantes = int(60 - tiempo_pasado)
            emit('error_tablas', {'mensaje': f'Debes esperar {segundos_restantes} segundos antes de ofrecer tablas de nuevo'})
            return
    
    control['ofertas'] += 1
    control['ultima_oferta'] = ahora
    control['jugador'] = jugador_id
    
    emit('oferta_tablas', {}, room=sala_id, include_self=False)
    print(f"🤝 Oferta de tablas #{control['ofertas']} enviada en sala {sala_id}")

@socketio.on('aceptar_tablas')
def aceptar_tablas(data):
    sala_id = data.get('sala')
    
    # 1. Verificar si la partida ya terminó (evita sobrescribir jaque mate)
    if sala_id in salas and salas[sala_id].get('partida_terminada', False):
        print(f"⚠️ Intento de aceptar tablas en partida ya terminada en {sala_id}, ignorando...")
        return
    
    if sala_id in salas:
        salas[sala_id]['partida_terminada'] = True
        if 'desconectado' in salas[sala_id]:
            del salas[sala_id]['desconectado']
        
        sala = salas[sala_id]
        nick_blanco = sala.get('blanco')
        nick_negro = sala.get('negro')
        
        tiempo_partida = sala.get('tiempo', 5)
        categoria = obtener_categoria(tiempo_partida)
        
        nuevo_elo_blanco = 1200
        nuevo_elo_negro = 1200
        
        try:
            elo_blanco = obtener_elo(nick_blanco, categoria)
            elo_negro = obtener_elo(nick_negro, categoria)
            
            print(f"🤝 Partida tablas: {nick_blanco} (ELO {categoria}: {elo_blanco}) vs {nick_negro} (ELO {categoria}: {elo_negro})")
            
            nuevo_elo_blanco = calcular_elo(elo_blanco, elo_negro, 'tablas')
            nuevo_elo_negro = calcular_elo(elo_negro, elo_blanco, 'tablas')
            
            if not salas[sala_id].get('estadisticas_actualizadas', False):
                actualizar_estadisticas_db(nick_blanco, 'tablas', categoria)
                actualizar_estadisticas_db(nick_negro, 'tablas', categoria)
                salas[sala_id]['estadisticas_actualizadas'] = True
                print(f"✅ Estadísticas actualizadas para tablas en {sala_id}")
            else:
                print(f"⚠️ Estadísticas ya actualizadas para {sala_id}, omitiendo...")
            
            print(f"📊 {nick_blanco}: {elo_blanco} → {nuevo_elo_blanco}")
            print(f"📊 {nick_negro}: {elo_negro} → {nuevo_elo_negro}")
            
            actualizar_elo_db(nick_blanco, nuevo_elo_blanco, categoria)
            actualizar_elo_db(nick_negro, nuevo_elo_negro, categoria)
            
            salas[sala_id]['elo_blanco'] = nuevo_elo_blanco
            salas[sala_id]['elo_negro'] = nuevo_elo_negro
            print(f"💾 ELOs guardados en sala {sala_id}: Blanco={nuevo_elo_blanco}, Negro={nuevo_elo_negro}")
            
        except Exception as e:
            print(f"❌ Error al actualizar ELO en tablas: {e}")
        
        emit('partida_finalizada', {
            'motivo': 'tablas',
            'ganador': 'empate',
            'elo_blanco': nuevo_elo_blanco,
            'elo_negro': nuevo_elo_negro
        }, room=sala_id)
        
        # 🆕 ACTUALIZAR PUNTOS DEL TORNEO EN CASO DE TABLAS
        if sala_id in partidas_torneo_activas:
            partida_torneo = partidas_torneo_activas[sala_id]
            torneo_id = partida_torneo['torneo_id']
            
            if torneo_id in torneos:
                torneo = torneos[torneo_id]
                j1 = partida_torneo['jugador1']
                j2 = partida_torneo['jugador2']
                
                # En tablas, ambos suman 1 punto
                torneo['puntos'][j1] = torneo['puntos'].get(j1, 0) + 1
                torneo['puntos'][j2] = torneo['puntos'].get(j2, 0) + 1
                
                print(f"🤝 Tablas en torneo: {j1} +1 pt, {j2} +1 pt")
                print(f"📊 Puntos actuales en torneo {torneo['nombre']}: {torneo['puntos']}")
                
                del partidas_torneo_activas[sala_id]
                
                # Liberar jugadores para nuevo emparejamiento
                for jugador in torneo['jugadores']:
                    sid = usuarios_conectados.get(jugador)
                    if sid and sid in sids_activos:
                        socketio.emit('clasificacion_torneo', 
                                     obtener_clasificacion_torneo(torneo_id), room=sid)
                        socketio.emit('jugadores_torneo', 
                                     torneo['jugadores'], room=sid)
                        socketio.emit('puedes_buscar', room=sid)
                        print(f"🔄 {jugador} liberado para nuevo emparejamiento")
        
        print(f"✅ Tablas aceptadas en sala {sala_id} - ELOs actualizados")

@socketio.on('rechazar_tablas')
def rechazar_tablas(data):
    sala_id = data.get('sala')
    
    if sala_id in salas:
        emit('tablas_rechazadas', {}, room=sala_id, include_self=False)
        print(f"❌ Tablas rechazadas en sala {sala_id}")

@socketio.on('oferta_revancha')
def oferta_revancha(data):
    sala_id = data.get('sala')
    
    if sala_id in salas:
        emit('oferta_revancha', {}, room=sala_id, include_self=False)

@socketio.on('aceptar_revancha')
def aceptar_revancha(data):
    sala_id = data.get('sala')
    
    if sala_id in control_tablas:
        del control_tablas[sala_id]
        print(f"🔄 Control de tablas reseteado por revancha en sala {sala_id}")
    
    if sala_id in salas:
        sala = salas[sala_id]
        if not sala.get('partida_terminada', False):
            emit('error_revancha', {'mensaje': 'La partida aún no ha terminado'}, room=sala_id)
            print(f"❌ Intento de revancha con partida en curso en sala {sala_id}")
            return
        
        sala['partida_terminada'] = False
        sala['estadisticas_actualizadas'] = False
        
        tiempo_inicial = sala.get('tiempo', 5) * 60
        sala['segundos_blanco'] = tiempo_inicial
        sala['segundos_negro'] = tiempo_inicial
        print(f"⏱️ Tiempos reseteados a {tiempo_inicial}s para revancha")
        
        color_blanco = sala.get('blanco')
        color_negro = sala.get('negro')
        
        sala['blanco'] = color_negro
        sala['negro'] = color_blanco
        
        print(f"🔄 Colores intercambiados en sala {sala_id}")
        
        nb = sala.get('blanco')
        nn = sala.get('negro')
        
        clave_blanco = f"{nb}_{sala_id}"
        clave_negro = f"{nn}_{sala_id}"
        
        if clave_blanco in desconexiones_por_jugador:
            del desconexiones_por_jugador[clave_blanco]
            print(f"🗑️ Contador de desconexiones limpio para {nb}")
        
        if clave_negro in desconexiones_por_jugador:
            del desconexiones_por_jugador[clave_negro]
            print(f"🗑️ Contador de desconexiones limpio para {nn}")
        
        if sala_id in estado_partidas:
            del estado_partidas[sala_id]
            print(f"️ Estado de partida limpio para revancha")
        
        elo_blanco = sala.get('elo_blanco', 1200)
        elo_negro = sala.get('elo_negro', 1200)
        
        emit('revancha_aceptada', {
            'intercambiar_colores': True,
            'elo_blanco': elo_blanco,
            'elo_negro': elo_negro
        }, room=sala_id)
        print(f"✅ Revancha aceptada en sala {sala_id} - ELOs: Blanco={elo_blanco}, Negro={elo_negro}")

@socketio.on('rechazar_revancha')
def rechazar_revancha(data):
    sala_id = data.get('sala')
    
    if sala_id in salas:
        emit('revancha_rechazada', {}, room=sala_id, include_self=False)

@socketio.on('cancelar_busqueda')
def cancelar_busqueda():
    jugador_id = request.sid
    global cola_espera
    
    cola_espera = [j for j in cola_espera if j['id'] != jugador_id]
    
    print(f"❌ Jugador {jugador_id} canceló la búsqueda")
    emit('busqueda_cancelada', {'mensaje': 'Búsqueda cancelada correctamente'})
    emitir_cola_espera()  # 🆕 Avisar que se eliminó de la cola       

@socketio.on('obtener_clasificacion')
def obtener_clasificacion(data):
    categoria = data.get('categoria', 'blitz')
    
    if categoria not in ['bullet', 'blitz', 'rapid']:
        categoria = 'blitz'
    
    try:
        columna_elo = f'elo_{categoria}'
        columna_ganadas = f'partidas_ganadas_{categoria}'
        columna_perdidas = f'partidas_perdidas_{categoria}'
        columna_tablas = f'partidas_tablas_{categoria}'
        
        response = supabase.table('usuarios').select(
            f'nick, {columna_elo}, {columna_ganadas}, {columna_perdidas}, {columna_tablas}'
        ).order(columna_elo, desc=True).limit(50).execute()
        
        jugadores = []
        for i, fila in enumerate(response.data, 1):
            jugadores.append({
                'posicion': i,
                'nick': fila['nick'],
                'elo': fila[columna_elo],
                'ganadas': fila[columna_ganadas],
                'perdidas': fila[columna_perdidas],
                'tablas': fila[columna_tablas]
            })
        
        emit('clasificacion_response', {
            'categoria': categoria,
            'jugadores': jugadores
        })
        
    except Exception as e:
        print(f"❌ Error al obtener clasificación: {e}")
        emit('clasificacion_response', {'categoria': categoria, 'jugadores': []})

# --- SISTEMA DE TORNEOS ---
torneos = {}
colas_torneo = {}
partidas_torneo_activas = {}

@socketio.on('crear_torneo')
def crear_torneo(data):
    nombre = data.get('nombre')
    tiempo = data.get('tiempo')
    duracion = data.get('duracion')
    creador = data.get('creador')
    
    torneo_id = str(uuid.uuid4())[:8]
    
    torneos[torneo_id] = {
        'id': torneo_id,
        'nombre': nombre,
        'tiempo': tiempo,
        'duracion': duracion,
        'creador': creador,
        'jugadores': [creador],
        'puntos': {creador: 0},
        'activo': True,
        'hora_inicio': time.time()
    }
    
    print(f"🏆 Torneo creado: {nombre} (ID: {torneo_id})")
    
    socketio.emit('lista_torneos_actualizada', obtener_lista_torneos())
    emit('torneo_creado', {'torneo_id': torneo_id, 'nombre': nombre})

def obtener_lista_torneos():
    lista = []
    for torneo_id, torneo in torneos.items():
        if torneo['activo']:
            lista.append({
                'id': torneo['id'],
                'nombre': torneo['nombre'],
                'tiempo': torneo['tiempo'],
                'duracion': torneo['duracion'],
                'jugadores': len(torneo['jugadores'])
            })
    return lista

@socketio.on('pedir_torneos')
def pedir_torneos():
    print("📋 Alguien pidió la lista de torneos")
    
    # Combinar torneos normales y suizos
    lista_combinada = []
    
    # Torneos normales
    for tid, t in torneos.items():
        if t.get('activo', False):
            lista_combinada.append({
                'id': tid,
                'nombre': t['nombre'],
                'tiempo': t.get('tiempo', 5),
                'duracion': t.get('duracion', 1800),
                'jugadores': len(t.get('jugadores', [])),
                'tipo': 'normal'
            })
    
    # Torneos suizos
    for tid, t in torneos_suizos.items():
        if t.get('activo', True):  # Los suizos están activos hasta que se inician
            lista_combinada.append({
                'id': tid,
                'nombre': t['nombre'],
                'tiempo': t.get('tiempo', 5),
                'rondas': t.get('rondas', 5),
                'jugadores': len(t.get('jugadores', [])),
                'tipo': 'suizo'
            })
    
    socketio.emit('lista_torneos_actualizada', lista_combinada)

@socketio.on('unirse_torneo')
def unirse_torneo(data):
    torneo_id = data.get('torneo_id')
    jugador = data.get('jugador')
    
    if torneo_id in torneos and torneos[torneo_id]['activo']:
        torneo = torneos[torneo_id]
        
        if jugador not in torneo['jugadores']:
            torneo['jugadores'].append(jugador)
            torneo['puntos'][jugador] = 0
            print(f"✅ {jugador} se unió al torneo {torneo['nombre']}")
        else:
            print(f"ℹ️ {jugador} ya está en el torneo {torneo['nombre']}")
        
        if torneo_id not in colas_torneo:
            colas_torneo[torneo_id] = []
        
        # ✅ NUEVO: Enviar clasificación y jugadores a TODOS los del torneo
        clasificacion = obtener_clasificacion_torneo(torneo_id)
        
        # Enviar a todos los jugadores conectados del torneo
        for nick in torneo['jugadores']:
            sid = usuarios_conectados.get(nick)
            if sid and sid in sids_activos:
                emit('clasificacion_torneo', clasificacion, room=sid)
                emit('jugadores_torneo', torneo['jugadores'], room=sid)
                print(f"📤 Enviando lista actualizada a {nick}")
        
        # Actualizar lista global de torneos
        socketio.emit('lista_torneos_actualizada', obtener_lista_torneos())
    else:
        print(f"❌ Torneo {torneo_id} no encontrado o no activo")
        
@socketio.on('registrar_sesion_torneo')
def registrar_sesion_torneo(data):
    nick = data.get('nick')
    sid = request.sid
    
    if nick:
        # Si el nick ya estaba registrado con otro SID, limpiar el antiguo
        if nick in usuarios_conectados:
            old_sid = usuarios_conectados[nick]
            if old_sid in sids_activos:
                print(f"🔄 Actualizando SID de {nick}: {old_sid} -> {sid}")
        
        usuarios_conectados[nick] = sid
        sids_activos[sid] = True
        print(f"✅ {nick} registrado para torneos: {sid}")
        print(f"📊 Total usuarios conectados: {len(usuarios_conectados)}")
        
        emit('sesion_registrada', {'nick': nick, 'sid': sid})

@socketio.on('buscar_partida_torneo')
def buscar_partida_torneo(data):
    torneo_id = data.get('torneo_id')
    jugador = data.get('jugador')
    automatico = data.get('automatico', False)
    
    print(f"🔍 {jugador} buscando partida en torneo {torneo_id} (automático: {automatico})")
    
    if torneo_id not in colas_torneo:
        colas_torneo[torneo_id] = []
    
    # Evitar duplicados en la cola
    if jugador in colas_torneo[torneo_id]:
        print(f"⚠️ {jugador} ya está en la cola")
        return
    
    # Verificar que el jugador está conectado
    if jugador not in usuarios_conectados:
        print(f"❌ {jugador} NO está conectado")
        return
    
    # 🆕 NUEVO: Verificar si el jugador ya está en una partida activa
    sid_jugador = usuarios_conectados.get(jugador)
    if sid_jugador and sid_jugador in partidas_activas:
        print(f"️ {jugador} ya está en una partida activa (sala: {partidas_activas[sid_jugador]}). No puede unirse al torneo.")
        # Avisar al cliente
        socketio.emit('error_torneo', {
            'mensaje': 'Ya estás en una partida activa. Termina esa partida antes de buscar en el torneo.'
        }, room=sid_jugador)
        return
    
    colas_torneo[torneo_id].append(jugador)
    print(f"➕ {jugador} añadido a la cola. Total: {len(colas_torneo[torneo_id])}")
    
    # Obtener puntos del jugador que busca
    torneo = torneos.get(torneo_id, {})
    puntos_jugador = torneo.get('puntos', {}).get(jugador, 0)
    
    # Buscar rival con puntos similares (diferencia máxima de 2 puntos)
    rival_encontrado = None
    for posible_rival in colas_torneo[torneo_id]:
        if posible_rival == jugador:
            continue
        
        # 🆕 NUEVO: Verificar que el rival tampoco esté en una partida activa
        sid_rival = usuarios_conectados.get(posible_rival)
        if sid_rival and sid_rival in partidas_activas:
            print(f"⚠️ {posible_rival} está en una partida activa, saltando...")
            continue
        
        puntos_rival = torneo.get('puntos', {}).get(posible_rival, 0)
        
        # Emparejar si tienen puntos similares (±2)
        if abs(puntos_jugador - puntos_rival) <= 2:
            rival_encontrado = posible_rival
            break
    
    if rival_encontrado:
        jugador1 = colas_torneo[torneo_id].pop(colas_torneo[torneo_id].index(jugador))
        jugador2 = colas_torneo[torneo_id].pop(colas_torneo[torneo_id].index(rival_encontrado))
        
        print(f"🎯 Emparejando: {jugador1} ({puntos_jugador}pts) vs {jugador2} ({puntos_rival}pts)")
        
        # CONSULTAR ELOs ACTUALIZADOS ANTES DE ENVIAR
        categoria = obtener_categoria(int(torneo.get('tiempo', 5)))
        elo_jugador1 = obtener_elo(jugador1, categoria)
        elo_jugador2 = obtener_elo(jugador2, categoria)
        print(f"📊 ELOs para emparejamiento: {jugador1}={elo_jugador1}, {jugador2}={elo_jugador2}")
        
        sid_jugador1 = usuarios_conectados.get(jugador1)
        sid_jugador2 = usuarios_conectados.get(jugador2)
        
        sala_id = str(uuid.uuid4())[:8]
        
        if random.random() < 0.5:
            color1, color2 = 'white', 'black'
        else:
            color1, color2 = 'black', 'white'
        
        tiempo_torneo = int(torneo.get('tiempo', 5))
        tiempo_inicial_segundos = tiempo_torneo * 60
        
        salas[sala_id] = {
            'blanco': jugador1 if color1 == 'white' else jugador2,
            'negro': jugador2 if color2 == 'black' else jugador1,
            'partida_terminada': False,
            'tiempo': tiempo_torneo,
            'incremento': 0,
            'estadisticas_actualizadas': False,
            'tiempo_restante_blanco': tiempo_inicial_segundos,
            'tiempo_restante_negro': tiempo_inicial_segundos,
            'ultimo_movimiento': time.time(),
            'turno': 'blanco'
        }
        
        partidas_torneo_activas[sala_id] = {
            'torneo_id': torneo_id,
            'jugador1': jugador1,
            'jugador2': jugador2,
            'color1': color1,
            'color2': color2
        }
        
        print(f"🎮 Sala creada: {sala_id}")
        
        # ENVIAR ELOs AL TABLERO
        if sid_jugador1:
            socketio.emit('partida_torneo_encontrada', {
                'sala': sala_id,
                'color': color1,
                'rival_nick': jugador2,
                'torneo_id': torneo_id,
                'mi_elo': elo_jugador1,
                'rival_elo': elo_jugador2
            }, room=sid_jugador1)
        
        if sid_jugador2:
            socketio.emit('partida_torneo_encontrada', {
                'sala': sala_id,
                'color': color2,
                'rival_nick': jugador1,
                'torneo_id': torneo_id,
                'mi_elo': elo_jugador2,
                'rival_elo': elo_jugador1
            }, room=sid_jugador2)
        
        print(f"✅ Partida de torneo enviada con ELOs: {elo_jugador1} vs {elo_jugador2}")
    else:
        # No hay rival disponible con puntos similares
        print(f"⏳ {jugador} ({puntos_jugador}pts) esperando rival libre...")
        
        # Si es búsqueda automática, avisar al cliente
        if automatico:
            sid_jugador = usuarios_conectados.get(jugador)
            if sid_jugador:
                socketio.emit('sin_rival_disponible', room=sid_jugador)
        
@socketio.on('obtener_tiempo_restante_torneo')
def obtener_tiempo_restante_torneo(data):
    torneo_id = data.get('torneo_id')
    
    if torneo_id in torneos:
        torneo = torneos[torneo_id]
        hora_inicio = torneo.get('hora_inicio', time.time())
        duracion = torneo.get('duracion', 1800)
        
        tiempo_transcurrido = time.time() - hora_inicio
        tiempo_restante = max(0, duracion - tiempo_transcurrido)
        
        print(f"⏱️ Tiempo restante torneo {torneo_id}: {tiempo_restante:.1f}s")
        
        emit('tiempo_restante_torneo', {
            'tiempo_restante': tiempo_restante,
            'torneo_id': torneo_id
        })
    else:
        emit('tiempo_restante_torneo', {
            'tiempo_restante': 0,
            'torneo_id': torneo_id
        })
@socketio.on('unirse_a_rival')
def unirse_a_rival(data):
    global cola_espera 
    rival_id = data.get('rival_id')
    jugador_id = request.sid
    
    rival_idx = -1
    for i, rival in enumerate(cola_espera):
        if rival['id'] == rival_id:
            rival_idx = i
            break
            
    if rival_idx == -1:
        emit('error_unirse', {'mensaje': 'Ese jugador ya no está disponible.'})
        return
        
    rival = cola_espera.pop(rival_idx)
    
    # Sacar al jugador actual de la cola si estaba buscando
    cola_espera = [j for j in cola_espera if j['id'] != jugador_id]
    
    sala_id = str(uuid.uuid4())
    tiempo = rival['data'].get('tiempo', 5)
    incremento = rival['data'].get('incremento', 0)
    
    # ✅ AÑADIR ESTA LÍNEA AQUÍ:
    tiempo_inicial_segundos = tiempo * 60
    
    # Asignar colores: el que se une toma el color opuesto al que eligió el creador
    color_rival = rival['data'].get('color', 'random')
    if color_rival == 'random':
        color_rival_final = 'white' if random.random() < 0.5 else 'black'
        color_mio_final = 'black' if color_rival_final == 'white' else 'white'
    else:
        color_rival_final = color_rival
        color_mio_final = 'black' if color_rival == 'white' else 'white'
        
    nick_rival = rival['data'].get('usuario', 'Anónimo')
    
    nick_mio = 'Anónimo'
    for n, sid in usuarios_conectados.items():
        if sid == jugador_id:
            nick_mio = n
            break
            
    jugador1 = {'id': rival['id'], 'color': color_rival_final, 'nick': nick_rival}
    jugador2 = {'id': jugador_id, 'color': color_mio_final, 'nick': nick_mio}
    
    nick_blanco = jugador1['nick'] if color_rival_final == 'white' else jugador2['nick']
    nick_negro = jugador1['nick'] if color_rival_final == 'black' else jugador2['nick']
    tiempo_inicial_segundos = tiempo * 60
    salas[sala_id] = {
        'blanco': nick_blanco,
        'negro': nick_negro,
        'partida_terminada': False,
        'tiempo': tiempo,
        'incremento': incremento,
        'estadisticas_actualizadas': False,
        'tiempo_restante_blanco': tiempo_inicial_segundos,
        'tiempo_restante_negro': tiempo_inicial_segundos,
        'ultimo_movimiento': time.time(),
        'turno': 'blanco'
    }
    
    join_room(sala_id, sid=jugador1['id'])
    join_room(sala_id, sid=jugador2['id'])
    
    categoria = obtener_categoria(tiempo)
    elo1 = obtener_elo(jugador1['nick'], categoria)
    elo2 = obtener_elo(jugador2['nick'], categoria)
    
    config_rival = {'tiempo': tiempo, 'incremento': incremento, 'color': color_rival_final}
    config_mio = {'tiempo': tiempo, 'incremento': incremento, 'color': color_mio_final}
    
    emit('partida_encontrada', {
        'sala': sala_id, 'color': color_rival_final, 'config': config_rival,
        'rival_nick': nick_mio, 'mi_elo': elo1, 'rival_elo': elo2,
        'segundos_blanco': tiempo_inicial_segundos,
        'segundos_negro': tiempo_inicial_segundos
    }, room=jugador1['id'])
    
    emit('partida_encontrada', {
        'sala': sala_id, 'color': color_mio_final, 'config': config_mio,
        'rival_nick': nick_rival, 'mi_elo': elo2, 'rival_elo': elo1,
        'segundos_blanco': tiempo_inicial_segundos,
        'segundos_negro': tiempo_inicial_segundos
    }, room=jugador2['id'])
    
    print(f"✅ Partida manual creada: {nick_rival} vs {nick_mio} | Sala: {sala_id}")
    emitir_cola_espera()           
# Iniciar el monitor de tiempos en segundo plano
threading.Thread(target=monitor_tiempos, daemon=True).start()
# =====================================================
# TORNEOS SUIZOS
# =====================================================
torneos_suizos = {}  # {torneo_id: {datos}}
emparejamientos_suizos = {}  # {torneo_id: {ronda: [{j1, j2, ganador, color_j1}]}}

@socketio.on('crear_torneo_suizo')
def crear_torneo_suizo(data):
    nombre = data.get('nombre')
    tiempo = data.get('tiempo', 5)
    rondas = data.get('rondas', 5)
    creador = data.get('creador')
    
    torneo_id = str(uuid.uuid4())[:8]
    
    torneos_suizos[torneo_id] = {
        'id': torneo_id,
        'nombre': nombre,
        'tiempo': tiempo,
        'rondas': rondas,
        'ronda_actual': 1,
        'creador': creador,
        'jugadores': [creador],
        'puntos': {creador: 0},
        'historial_colores': {creador: []},  # Lista de colores por ronda
        'historial_rivales': {creador: []},   # Para no repetir rivales
        'activo': True,
        'creado_en': time.time(),
        'finalizado': False
    }
    
    emparejamientos_suizos[torneo_id] = {}
    
    print(f"🏆 Torneo Suizo creado: {nombre} (ID: {torneo_id}) - {rondas} rondas")
    
    # Enviar confirmación al creador
    socketio.emit('torneo_suizo_creado', {
        'torneo_id': torneo_id,
        'nombre': nombre,
        'rondas': rondas,
        'tipo': 'suizo'
    }, room=request.sid)
    
    # Actualizar lista global
    socketio.emit('lista_torneos_actualizada', obtener_lista_torneos_suizos())

def obtener_lista_torneos_suizos():
    lista = []
    for tid, t in torneos_suizos.items():
        if t['activo']:
            lista.append({
                'id': tid,
                'nombre': t['nombre'],
                'tiempo': t['tiempo'],
                'rondas': t['rondas'],
                'jugadores': len(t['jugadores']),
                'tipo': 'suizo'
            })
    return lista

@socketio.on('unirse_torneo_suizo')
def unirse_torneo_suizo(data):
    torneo_id = data.get('torneo_id')
    jugador = data.get('jugador')
    
    if torneo_id not in torneos_suizos:
        print(f"❌ Torneo suizo {torneo_id} no encontrado")
        return
    
    torneo = torneos_suizos[torneo_id]
    
    if not torneo['activo']:
        print(f"⚠️ Torneo suizo {torneo_id} ya no está activo")
        return
    
    if jugador not in torneo['jugadores']:
        torneo['jugadores'].append(jugador)
        torneo['puntos'][jugador] = 0
        torneo['historial_colores'][jugador] = []
        torneo['historial_rivales'][jugador] = []
        print(f"✅ {jugador} se unió al torneo suizo {torneo['nombre']}")
    
    # Enviar datos actualizados a todos
        socketio.emit('torneo_suizo_actualizado', {
        'torneo_id': torneo_id,
        'jugadores': torneo['jugadores'],
        'ronda_actual': torneo['ronda_actual'],
        'rondas_total': torneo['rondas']
    })  # Sin broadcast=True

@socketio.on('iniciar_torneo_suizo')
def iniciar_torneo_suizo(data):
    torneo_id = data.get('torneo_id')
    
    if torneo_id not in torneos_suizos:
        return
    
    torneo = torneos_suizos[torneo_id]
    
    if len(torneo['jugadores']) < 2:
        print(f"⚠️ Se necesitan al menos 2 jugadores")
        return
    
    print(f" Iniciando torneo suizo {torneo['nombre']} - Ronda 1")
    torneo['activo'] = False  # Ya no se pueden unir más
    
    # Generar emparejamientos de la ronda 1
    generar_ronda_suiza(torneo_id)

def generar_ronda_suiza(torneo_id):
    torneo = torneos_suizos[torneo_id]
    ronda = torneo['ronda_actual']
    jugadores = torneo['jugadores']
    
    print(f"🔄 Generando emparejamientos para ronda {ronda}")
    
    # Ordenar jugadores por puntos (descendente)
    jugadores_ordenados = sorted(jugadores, key=lambda x: torneo['puntos'][x], reverse=True)
    
    emparejamientos = []
    usados = set()
    
    print(f" Jugadores disponibles: {jugadores_ordenados}")
    
    # Emparejar jugadores con puntos similares
    for i, j1 in enumerate(jugadores_ordenados):
        if j1 in usados:
            continue
        
        rival_encontrado = None
        # Buscar el siguiente jugador disponible
        for j2 in jugadores_ordenados[i+1:]:
            if j2 in usados:
                continue
            
            # Verificar si ya se enfrentaron
            historial_j1 = torneo['historial_rivales'].get(j1, [])
            if j2 in historial_j1:
                print(f"⚠️ {j1} y {j2} ya se enfrentaron, buscando otro rival")
                continue
            
            rival_encontrado = j2
            break
        
        if rival_encontrado:
            # Asignar colores
            color_j1 = asignar_color_suizo(torneo, j1, rival_encontrado, ronda)
            color_j2 = 'negro' if color_j1 == 'blanco' else 'blanco'
            
            emparejamientos.append({
                'jugador1': j1,
                'jugador2': rival_encontrado,
                'color1': color_j1,
                'color2': color_j2,
                'ganador': None,
                'ronda': ronda
            })
            
            usados.add(j1)
            usados.add(rival_encontrado)
            
            # Registrar en historial
            if j1 not in torneo['historial_rivales']:
                torneo['historial_rivales'][j1] = []
            if rival_encontrado not in torneo['historial_rivales']:
                torneo['historial_rivales'][rival_encontrado] = []
                
            torneo['historial_rivales'][j1].append(rival_encontrado)
            torneo['historial_rivales'][rival_encontrado].append(j1)
            
            if j1 not in torneo['historial_colores']:
                torneo['historial_colores'][j1] = []
            if rival_encontrado not in torneo['historial_colores']:
                torneo['historial_colores'][rival_encontrado] = []
                
            torneo['historial_colores'][j1].append(color_j1)
            torneo['historial_colores'][rival_encontrado].append(color_j2)
            
            print(f"✅ Emparejado: {j1} ({color_j1}) vs {rival_encontrado} ({color_j2})")
        else:
            # BYE - El jugador descansa esta ronda
            torneo['puntos'][j1] += 1
            print(f"🎁 {j1} tiene BYE en ronda {ronda} (+1 punto)")
            usados.add(j1)
    
    emparejamientos_suizos[torneo_id][ronda] = emparejamientos
    
    print(f"✅ Total emparejamientos generados: {len(emparejamientos)}")
    
    # Enviar emparejamientos a todos los jugadores
    for emp in emparejamientos:
        j1 = emp['jugador1']
        j2 = emp['jugador2']
        sid_j1 = usuarios_conectados.get(j1)
        sid_j2 = usuarios_conectados.get(j2)
        
        print(f" Enviando emparejamiento a {j1} y {j2}")
        
        if sid_j1:
            socketio.emit('emparejamiento_suizo', {
                'torneo_id': torneo_id,
                'ronda': ronda,
                'rival': j2,
                'mi_color': emp['color1'],
                'sala': None
            }, room=sid_j1)
        
        if sid_j2:
            socketio.emit('emparejamiento_suizo', {
                'torneo_id': torneo_id,
                'ronda': ronda,
                'rival': j1,
                'mi_color': emp['color2'],
                'sala': None
            }, room=sid_j2)

def asignar_color_suizo(torneo, j1, j2, ronda):
    """Asigna colores respetando máximo 2 consecutivos del mismo color"""
    hist_j1 = torneo['historial_colores'].get(j1, [])
    hist_j2 = torneo['historial_colores'].get(j2, [])
    
    # Contar consecutivos
    def contar_consecutivos(hist):
        if not hist:
            return 0
        count = 1
        for i in range(len(hist)-1, 0, -1):
            if hist[i] == hist[i-1]:
                count += 1
            else:
                break
        return count
    
    cons_j1 = contar_consecutivos(hist_j1)
    cons_j2 = contar_consecutivos(hist_j2)
    
    # Si j1 ya tiene 2 del mismo color, forzar el otro
    if cons_j1 >= 2:
        return 'negro' if hist_j1[-1] == 'blanco' else 'blanco'
    
    # Si j2 ya tiene 2 del mismo color, darle el que necesita
    if cons_j2 >= 2:
        color_necesario_j2 = 'negro' if hist_j2[-1] == 'blanco' else 'blanco'
        return 'negro' if color_necesario_j2 == 'blanco' else 'blanco'
    
    # Si no hay conflicto, alternar normalmente
    if not hist_j1:
        return 'blanco' if ronda % 2 == 1 else 'negro'
    
    return 'negro' if hist_j1[-1] == 'blanco' else 'blanco'

@socketio.on('fin_partida_suizo')
def fin_partida_suizo(data):
    torneo_id = data.get('torneo_id')
    ronda = data.get('ronda')
    ganador = data.get('ganador')  # 'blanco', 'negro', o 'empate'
    
    if torneo_id not in torneos_suizos:
        return
    
    torneo = torneos_suizos[torneo_id]
    emparejamientos = emparejamientos_suizos[torneo_id].get(ronda, [])
    
    # Encontrar el emparejamiento
    for emp in emparejamientos:
        if emp['ronda'] == ronda and emp['ganador'] is None:
            emp['ganador'] = ganador
            
            j1 = emp['jugador1']
            j2 = emp['jugador2']
            
            if ganador == 'blanco':
                torneo['puntos'][j1] += 1
            elif ganador == 'negro':
                torneo['puntos'][j2] += 1
            else:  # empate
                torneo['puntos'][j1] += 0.5
                torneo['puntos'][j2] += 0.5
            
            print(f"✅ Ronda {ronda}: {j1} vs {j2} - Ganador: {ganador}")
            break
    
    # Verificar si todos los emparejamientos de esta ronda terminaron
    todos_terminados = all(emp['ganador'] is not None for emp in emparejamientos)
    
    if todos_terminados:
        print(f"🏁 Ronda {ronda} completada")
        
        if ronda < torneo['rondas']:
            # Siguiente ronda
            torneo['ronda_actual'] = ronda + 1
            generar_ronda_suiza(torneo_id)
        else:
            # Torneo finalizado
            finalizar_torneo_suizo(torneo_id)

def calcular_buchholz_cut1(torneo, jugador):
    """Calcula Buchholz Cut 1: suma puntos de rivales menos el menor"""
    rivales = torneo['historial_rivales'].get(jugador, [])
    puntos_rivales = [torneo['puntos'].get(r, 0) for r in rivales]
    
    if len(puntos_rivales) <= 1:
        return sum(puntos_rivales)
    
    # Eliminar el rival con menos puntos
    puntos_rivales.remove(min(puntos_rivales))
    return sum(puntos_rivales)

def finalizar_torneo_suizo(torneo_id):
    torneo = torneos_suizos[torneo_id]
    torneo['finalizado'] = True
    
    # Calcular clasificación con Buchholz Cut 1
    clasificacion = []
    for jugador in torneo['jugadores']:
        puntos = torneo['puntos'][jugador]
        buchholz = calcular_buchholz_cut1(torneo, jugador)
        clasificacion.append({
            'nick': jugador,
            'puntos': puntos,
            'buchholz': buchholz
        })
    
    # Ordenar: primero por puntos, luego por Buchholz
    clasificacion.sort(key=lambda x: (x['puntos'], x['buchholz']), reverse=True)
    
    print(f"🏆 Torneo Suizo {torneo['nombre']} finalizado!")
    print(f"📊 Clasificación final:")
    for i, c in enumerate(clasificacion, 1):
        print(f"   {i}. {c['nick']}: {c['puntos']} pts (Buchholz: {c['buchholz']})")
    
    # Enviar resultado final
    socketio.emit('torneo_suizo_finalizado', {
        'torneo_id': torneo_id,
        'clasificacion': clasificacion
    }, broadcast=True)
# --- INICIAR SERVIDOR ---
if __name__ == '__main__':
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip_local = s.getsockname()[0]
    except:
        ip_local = "127.0.0.1"
    finally:
        s.close()
    
    print("\n ELITECHESS SERVER")
    print("="*50)
    print(f"📍 Local:   http://localhost:5000")
    print(f"🌐 Red:     http://{ip_local}:5000")
    print(f"📱 Otros PCs: http://{ip_local}:5000")
    print("="*50)
    
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
