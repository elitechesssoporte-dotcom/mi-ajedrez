// Este es el Service Worker de FighterChess
const CACHE_NAME = 'fighterchess-v1';

// Cuando se instala, no hace nada complicado, solo se activa
self.addEventListener('install', (event) => {
  console.log('✅ Service Worker instalado');
});

// Cuando se activa, limpia cachés viejas si hubiera
self.addEventListener('activate', (event) => {
  console.log('✅ Service Worker activado');
});

// Cuando la app pide algo, simplemente lo deja pasar por internet
self.addEventListener('fetch', (event) => {
  // No interceptamos nada para evitar errores en tu web actual
});