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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- CONEXIÓN A SUPABASE ---
# ⚠️ REEMPLAZA ESTOS VALORES CON LOS TUYOS
SUPABASE_URL = "https://stizpdyftzoeuwigxgbi.supabase.co"
SUPABASE_KEY = "sb_secret_CYcLPsygIf_rwaqy9gHcwg_9F6sUzy7"

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
        columna = f'elo_{categoria}'
        
        response = supabase.table('usuarios').select(columna).eq('nick', nick).execute()
        if response.data and len(response.data) > 0:
            return response.data[0][columna]
        return 1200
    except:
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

# --- EVENTOS SOCKET.IO ---

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    sids_activos[sid] = True
    print(f"✅ Conectado: {sid}")

@socketio.on('disconnect')
def handle_disconnect():
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
            'password_hash': password_hash
        }).execute()
        
        user_id = response.data[0]['id']
        print(f"✅ Usuario registrado: {nick} (ID: {user_id})")
        emit('registro_response', {'success': True})
        
    except Exception as e:
        print(f"❌ Error en registro: {e}")
        emit('registro_response', {'success': False, 'message': 'Error al registrar'})

@socketio.on('login')
def login(data):
    print(f"DEBUG: Recibida solicitud de login para: {data.get('nick')}")
    global usuarios_conectados
    nick = data.get('nick')
    password = data.get('password')
    es_invitado = data.get('invitado', False)
    sid = request.sid
    
    try:
        if es_invitado:
            print(f" Login de invitado: {nick}")
            
            response = supabase.table('usuarios').select('id, nick').ilike('nick', nick).execute()
            
            if response.data and len(response.data) > 0:
                user_id = response.data[0]['id']
                nick_real = response.data[0]['nick']
                
                usuarios_conectados[nick_real] = sid
                sids_activos[sid] = True
                
                print(f"✅ Invitado reconectado: {nick_real} (ID: {user_id})")
                emit('login_response', {'success': True, 'nick': nick_real, 'userId': user_id, 'invitado': True})
                return
        else:
            # Creamos usuario nuevo si no existe
            password_hash = hash_password('invitado_temporal')
            
            # Insertamos y capturamos la respuesta
            res_insert = supabase.table('usuarios').insert({
                'nick': nick,
                'password_hash': password_hash,
                'elo_bullet': 1200,
                'elo_blitz': 1200,
                'elo_rapid': 1200
            }).execute()

            # Verificamos si la inserción funcionó
            if res_insert.data and len(res_insert.data) > 0:
                user_id = res_insert.data[0]['id']
            else:
                # Si falló, buscamos el ID
                res_busca = supabase.table('usuarios').select('id').eq('nick', nick).execute()
                if res_busca.data:
                    user_id = res_busca.data[0]['id']
                else:
                    print(f"❌ Error crítico: No se pudo crear ni encontrar el usuario {nick}")
                    emit('login_error', {'message': 'Error al crear usuario'})
                    return
                
                usuarios_conectados[nick] = sid
                sids_activos[sid] = True
                
                print(f"✅ Nuevo invitado registrado: {nick} (ID: {user_id})")
                emit('login_response', {'success': True, 'nick': nick, 'userId': user_id, 'invitado': True})
                return
        
        response = supabase.table('usuarios').select('id, nick, password_hash').ilike('nick', nick).execute()
        
        if not response.data or len(response.data) == 0:
            emit('login_response', {'success': False, 'message': 'Usuario no encontrado'})
            return
        
        user = response.data[0]
        user_id = user['id']
        nick_real = user['nick']
        stored_password = user['password_hash']
        
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
                emit('login_response', {'success': True, 'nick': nick_real, 'userId': user_id, 'reconexion': True})
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
                    emit('login_response', {'success': True, 'nick': nick_real, 'userId': user_id})
                    return
                else:
                    print(f"⚠️ {nick_real} ya está conectado en {old_sid}")
                    emit('login_response', {'success': False, 'message': 'Este usuario ya está conectado en otro dispositivo'})
                    return
            
            usuarios_conectados[nick_real] = sid
            sids_activos[sid] = True
            print(f"✅ Login exitoso: {nick_real} (ID: {user_id}) - Session: {sid}")
            print("DEBUG: Login procesado correctamente")
            emit('login_response', {'success': True, 'nick': nick_real, 'userId': user_id})
        else:
            emit('login_response', {'success': False, 'message': 'Contraseña incorrecta'})
            
    except Exception as e:
        print(f"❌ ERROR DE LOGIN DETALLADO: {str(e)}")
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
                print(f"️ {usuario} intentando jugar contra sí mismo")
                continue
            
            tiempo_rival = rival['data'].get('tiempo', 0)
            tiempo_mio = data.get('tiempo', 0)
            incremento_rival = rival['data'].get('incremento', 0)
            incremento_mio = data.get('incremento', 0)
            
            if tiempo_rival != tiempo_mio or incremento_rival != incremento_mio:
                print(f" {usuario} ({tiempo_mio}+{incremento_mio}s) no coincide con {rival['data'].get('usuario')} ({tiempo_rival}+{incremento_rival}s)")
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
            
            cola_espera.pop(i)
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
                'incremento': data.get('incremento'),
                'estadisticas_actualizadas': False,
                'segundos_blanco': tiempo_inicial_segundos,
                'segundos_negro': tiempo_inicial_segundos
            }
            
            join_room(sala_id, sid=jugador1['id'])
            join_room(sala_id, sid=jugador2['id'])
            
            categoria = obtener_categoria(data.get('tiempo', 5))
            elo1 = obtener_elo(jugador1['nick'], categoria)
            elo2 = obtener_elo(jugador2['nick'], categoria)
            
            emit('partida_encontrada', {
                'sala': sala_id,
                'color': color_rival_final,
                'config': data,
                'rival_nick': usuario,
                'mi_elo': elo1,
                'rival_elo': elo2
            }, room=jugador1['id'])
            
            emit('partida_encontrada', {
                'sala': sala_id,
                'color': color_mio_final,
                'config': data,
                'rival_nick': rival['data'].get('usuario', 'Anónimo'),
                'mi_elo': elo2,
                'rival_elo': elo1
            }, room=jugador2['id'])
            
            print(f"✅ Partida creada: {jugador1['nick']} vs {jugador2['nick']} | Sala: {sala_id}")
            return
        
        cola_espera.append({'id': jugador_id, 'data': data})
        emit('esperando_rival', {'mensaje': f'Esperando rival que elija {data.get("tiempo", 5)} minutos...'})
        print(f"⏳ Jugador {usuario} en cola esperando rival con {data.get('tiempo', 5)} min")
    else:
        cola_espera.append({'id': jugador_id, 'data': data})
        emit('esperando_rival', {'mensaje': 'Esperando a que se conecte un rival...'})
        print(f"⏳ Jugador {usuario} en cola de espera")

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
        
        tiempo_inicial = sala.get('tiempo', 5) * 60
        segundos_blanco = sala.get('segundos_blanco', tiempo_inicial)
        segundos_negro = sala.get('segundos_negro', tiempo_inicial)
        
        emit('estado_partida', {
            'fen': fen_actual,
            'movimientos': movimientos,
            'segundos_blanco': segundos_blanco,
            'segundos_negro': segundos_negro,
            'terminada': sala.get('partida_terminada', False)
        })
        
        print(f"📊 Estado enviado a reconectado - FEN: {fen_actual[:40] if fen_actual else 'start'}...")
        print(f"⏱️ Tiempos - Blancas: {segundos_blanco}s, Negras: {segundos_negro}s")
    else:
        print(f"❌ Sala {sala_id} no encontrada al solicitar estado")
        
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
    segundos_blanco = data.get('segundos_blanco')
    segundos_negro = data.get('segundos_negro')
    
    if sala_id in salas:
        if sala_id not in estado_partidas:
            estado_partidas[sala_id] = {
                'fen': 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
                'movimientos': []
            }
        
        if fen:
            estado_partidas[sala_id]['fen'] = fen
        estado_partidas[sala_id]['movimientos'].append(movimiento)
        
        if segundos_blanco is not None:
            salas[sala_id]['segundos_blanco'] = segundos_blanco
        if segundos_negro is not None:
            salas[sala_id]['segundos_negro'] = segundos_negro
            
        print(f"💾 Tiempos guardados en sala {sala_id}: Blancas={segundos_blanco}s, Negras={segundos_negro}s") 
        print(f"💾 FEN guardado para sala {sala_id}: {fen[:50] if fen else 'N/A'}...")
        
        emit('recibir_movimiento', {
            'movimiento': movimiento
        }, room=sala_id, include_self=False)

@socketio.on('fin_partida')
def fin_partida(data):
    sala_id = data.get('sala')
    motivo = data.get('motivo')
    ganador = data.get('ganador')
    
    if sala_id in salas and salas[sala_id].get('partida_terminada', False):
        print(f"⚠️ Intento de finalizar partida ya terminada en {sala_id}, ignorando...")
        return
    
    if ganador == 'white':
        ganador = 'blanco'
    elif ganador == 'black':
        ganador = 'negro'
    
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

    if sala_id in control_tablas:
        del control_tablas[sala_id]
        print(f" Control de tablas reseteado en sala {sala_id}")
    
    if sala_id in salas:
        salas[sala_id]['partida_terminada'] = True
        if 'desconectado' in salas[sala_id]:
            del salas[sala_id]['desconectado']
        
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
            print(f"🏆 Ganador: {ganador}")
            
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
        
        emit('partida_finalizada', {
            'motivo': motivo,
            'ganador': ganador,
            'elo_blanco': nuevo_elo_blanco,
            'elo_negro': nuevo_elo_negro
        }, room=sala_id)
        print(f"✅ Partida finalizada en sala {sala_id} - Motivo: {motivo}")              
        
        if sala_id in partidas_torneo_activas:
            partida_torneo = partidas_torneo_activas[sala_id]
            torneo_id = partida_torneo['torneo_id']
            
            if torneo_id in torneos:
                torneo = torneos[torneo_id]
                
                if ganador == 'white':
                    torneo['puntos'][partida_torneo['jugador1']] = torneo['puntos'].get(partida_torneo['jugador1'], 0) + 2
                    torneo['puntos'][partida_torneo['jugador2']] = torneo['puntos'].get(partida_torneo['jugador2'], 0) + 0
                elif ganador == 'black':
                    torneo['puntos'][partida_torneo['jugador1']] = torneo['puntos'].get(partida_torneo['jugador1'], 0) + 0
                    torneo['puntos'][partida_torneo['jugador2']] = torneo['puntos'].get(partida_torneo['jugador2'], 0) + 2
                else:
                    torneo['puntos'][partida_torneo['jugador1']] = torneo['puntos'].get(partida_torneo['jugador1'], 0) + 1
                    torneo['puntos'][partida_torneo['jugador2']] = torneo['puntos'].get(partida_torneo['jugador2'], 0) + 1
                
                del partidas_torneo_activas[sala_id]
                
                for jugador in torneo['jugadores']:
                    for sid, nick in usuarios_conectados.items():
                        if nick == jugador:
                            socketio.emit('clasificacion_torneo', obtener_clasificacion_torneo(torneo_id), room=sid)
                            break
                
                print(f"🏆 Puntos actualizados en torneo {torneo['nombre']}")

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
    
    if sala_id in salas and salas[sala_id].get('partida_terminada', False):
        print(f"️ Intento de tablas en partida ya terminada en {sala_id}, ignorando...")
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
    emit('lista_torneos_actualizada', obtener_lista_torneos())

def obtener_clasificacion_torneo(torneo_id):
    if torneo_id not in torneos:
        return []
    
    torneo = torneos[torneo_id]
    clasificacion = []
    
    for jugador, puntos in torneo['puntos'].items():
        clasificacion.append({
            'nick': jugador,
            'puntos': puntos
        })
    
    clasificacion.sort(key=lambda x: x['puntos'], reverse=True)
    
    return clasificacion

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
        
        emit('clasificacion_torneo', obtener_clasificacion_torneo(torneo_id))
        emit('jugadores_torneo', torneo['jugadores'])
        
        socketio.emit('lista_torneos_actualizada', obtener_lista_torneos())
    else:
        print(f"❌ Torneo {torneo_id} no encontrado o no activo")
        
@socketio.on('registrar_sesion_torneo')
def registrar_sesion_torneo(data):
    nick = data.get('nick')
    sid = request.sid
    
    if nick:
        usuarios_conectados[nick] = sid
        print(f"✅ {nick} registrado en usuarios_conectados: {sid}")
        print(f"📊 Total usuarios: {len(usuarios_conectados)} - {usuarios_conectados}")
        
        emit('sesion_registrada', {'nick': nick, 'sid': sid})

@socketio.on('buscar_partida_torneo')
def buscar_partida_torneo(data):
    torneo_id = data.get('torneo_id')
    jugador = data.get('jugador')
    
    print(f"🔍 {jugador} buscando partida en torneo {torneo_id}")
    print(f"📋 usuarios_conectados: {usuarios_conectados}")
    
    if torneo_id not in colas_torneo:
        colas_torneo[torneo_id] = []
    
    if jugador in colas_torneo[torneo_id]:
        print(f"ℹ️ {jugador} ya está buscando partida")
        return
    
    colas_torneo[torneo_id].append(jugador)
    
    if len(colas_torneo[torneo_id]) >= 2:
        jugador1 = colas_torneo[torneo_id].pop(0)
        jugador2 = colas_torneo[torneo_id].pop(0)
        
        if jugador1 == jugador2:
            print(f"⚠️ Mismo jugador, reencolando")
            colas_torneo[torneo_id].append(jugador2)
            return
        
        print(f"🎯 Emparejando: {jugador1} vs {jugador2}")
        
        sid_jugador1 = None
        print(f"🔎 Buscando SID para {jugador1}")
        for sid, nick in usuarios_conectados.items():
            print(f"   - {nick} (SID: {sid})")
            if nick == jugador1:
                sid_jugador1 = sid
                break
        
        sid_jugador2 = None
        print(f"🔎 Buscando SID para {jugador2}")
        for sid, nick in usuarios_conectados.items():
            print(f"   - {nick} (SID: {sid})")
            if nick == jugador2:
                sid_jugador2 = sid
                break
        
        sala_id = str(uuid.uuid4())[:8]
        
        if random.random() < 0.5:
            color1, color2 = 'white', 'black'
        else:
            color1, color2 = 'black', 'white'
        
        partidas_torneo_activas[sala_id] = {
            'torneo_id': torneo_id,
            'jugador1': jugador1,
            'jugador2': jugador2,
            'color1': color1,
            'color2': color2
        }
        
        print(f"🎮 Sala creada: {sala_id}")
        print(f"📊 SID encontrados: {sid_jugador1}, {sid_jugador2}")
        
        if sid_jugador1:
            print(f"📤 Enviando a {jugador1} (SID: {sid_jugador1})")
            socketio.emit('partida_torneo_encontrada', {
                'sala': sala_id,
                'color': color1,
                'rival_nick': jugador2,
                'torneo_id': torneo_id
            }, room=sid_jugador1)
        else:
            print(f"❌ No se encontró SID para {jugador1}")
        
        if sid_jugador2:
            print(f"📤 Enviando a {jugador2} (SID: {sid_jugador2})")
            socketio.emit('partida_torneo_encontrada', {
                'sala': sala_id,
                'color': color2,
                'rival_nick': jugador1,
                'torneo_id': torneo_id
            }, room=sid_jugador2)
        else:
            print(f"❌ No se encontró SID para {jugador2}")
        
        if sid_jugador1:
            socketio.emit('puedes_buscar', room=sid_jugador1)
        if sid_jugador2:
            socketio.emit('puedes_buscar', room=sid_jugador2)

# --- INICIAR SERVIDOR ---
if __name__ == '__main__':
    # Render asigna dinámicamente el puerto en la variable de entorno PORT
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🚀 ELITECHESS SERVER INICIANDO EN PUERTO: {port}")
    
    # Usamos host='0.0.0.0' para que sea accesible desde internet
    socketio.run(app, host='0.0.0.0', port=port)
