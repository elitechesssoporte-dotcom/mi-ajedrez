const CACHE_NAME = 'fighterchess-v2';
const urlsToCache = [
  '/',
  '/splash.html',
  '/login.html',
  '/configuracion.html',
  '/tablero.html',
  '/icon-192.png',
  '/icon-512.png'
];

self.addEventListener('install', (event) => {
  console.log('✅ Service Worker instalado');
});

self.addEventListener('activate', (event) => {
  console.log('✅ Service Worker activado');
});

self.addEventListener('fetch', (event) => {
  // No interceptamos nada para evitar errores
});
