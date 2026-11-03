# Licencias de fuentes de los looks de letra

Todas vienen del repositorio oficial de Google Fonts (github.com/google/fonts).

- OFL 1.1: Audiowide, Marcellus, Knewave, Shrikhand, Neonderthaw, Caveat,
  Sacramento, Big Shoulders Display, Michroma.
- Apache 2.0: Permanent Marker.

`Caveat-Bold.ttf`, `BigShouldersDisplay-Black.ttf` y `BigShouldersDisplay-Light.ttf`
son instancias estáticas (wght=700 / 900 / 300) generadas con
`fonttools varLib.instancer` a partir de las fuentes variables oficiales, porque
libass sólo usa la instancia por defecto de una fuente variable. Ninguna de
las dos declara Reserved Font Name, así que la OFL permite la modificación
conservando el nombre.
