# Portal de entregables UMG

Fuente del build estático compartido por `umg.genly.pro` y
`umgchile.genly.pro`. El portal usa el dominio actual para elegir el destino;
la API entrega únicamente las filas de ese destino.

`index.template.html` contiene las secciones **Todos los entregables**,
**Art Tracks** y **Otros videos**. El botón **Generar y descargar** para un
ProRes pendiente usa staging, donde viven los jobs Art Track. El MP4 y la
portada del corte publicado quedan fijos mientras el master se prepara.

La configuración de Vercel que usa este build está en `vercel.json`. Generar
`dist/index.html` con `DELIVERY_PASSWORD` o con
`PORTAL_SHELL_SOURCE=/Users/tomi/genly-deliveries/dist/index.html` para
conservar el hash de acceso y la ayuda del portal vigente. No guardar la
contraseña ni el HTML generado en Git. El proyecto Vercel existente aún
usa `/Users/tomi/genly-deliveries` como directorio local. Al desplegar, copiar
los tres archivos de esta carpeta allí y generar el build antes de ejecutar Vercel.

El orden de despliegue y el contrato de backend están en
[`docs/UMG_PORTALS.md`](../docs/UMG_PORTALS.md).
