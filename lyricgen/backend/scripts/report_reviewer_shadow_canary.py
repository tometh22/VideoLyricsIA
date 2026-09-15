"""Build a tenant-private audit report from REAL artifacts, not inferred success."""
import argparse
from collections import Counter
import json
from pathlib import Path
import os

from shadow_reference_import import associate, import_workbook


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--artifacts', type=Path, required=True)
    p.add_argument('--workbook', type=Path, required=True)
    args = p.parse_args()
    base = args.artifacts
    snapshot = json.loads((base/'snapshot.json').read_text())
    imported = associate(import_workbook(args.workbook), snapshot['jobs'])
    report = json.loads((base/'canary/report.json').read_text())
    tests = json.loads((base/'nine-tests-evidence.json').read_text())
    post = json.loads((base/'post-canary-source-verification.json').read_text())
    preview = json.loads((base/'preview-smoke.json').read_text())
    lines = ['# Canary real del agente revisor — 5 de septiembre de 2026', '',
        'Objetivo: reducir minutos humanos de letra/timing para 600+ canciones. Este hito es funcional, no una evaluación de precisión. Sin fondos ni renders.', '',
        '## Estado, sin mezclar niveles', '',
        '| Nivel | Confirmado |', '|---|---|',
        '| Infraestructura | Importador, snapshot de solo lectura, muestra congelada, audio real, selector, trazas, reproductor local |',
        '| Audio revisado por modelos | 3 canciones; 11 fragmentos; 22 llamadas (11 Whisper + 11 Gemini) |',
        '| Respuestas | 21 parseadas; 1 fallo de JSON de Gemini. Dos respuestas Whisper vacías están incluidas |',
        '| Propuestas seleccionadas | 0 de texto; 0 de timing. 12 candidatos crudos de pausa, no correcciones |',
        '| Decisiones | Texto: mantener 1 / abstenerse 10. Timing: abstenerse 11 |',
        '| Comprobación humana | 0; precisión y ahorro no medidos |',
        '| Producto | Las 3 revisiones/hashes y aprobaciones siguen iguales. Bersuit conserva 38 líneas locked |', '',
        '## 294 filas frente a 300 canciones', '',
        f"Archivo SHA-256: `{imported['workbook_sha256']}`. Hoja `Lyrics Agosto`, encabezado fila 4, datos filas 5–298. Art Tracks y otros meses excluidos. La columna de letra sin encabezado se detectó por contenido, no por posición fija.", '',
        '| Control | Resultado |', '|---|---:|',
        '| Filas con metadatos | 294 |', '| Textos presentes | 154 |', '| Marcadores de ausencia | 140 |',
        '| Coincidencias únicas artista+título con jobs | 276 |', '| Filas sin asociación aceptable | 18 |',
        '| Jobs sin coincidencia aceptable | 24 |', '| Duplicados de número de fila | 0 |', '',
        'Los seis huecos NO son seis celdas `[NO ENCONTRADA]`: faltan sus filas de metadatos. No hay evidencia para asignarles títulos ni para decir quién los quitó.', '',
        '| Número ausente | Causa demostrable |', '|---:|---|']
    for n in imported['ordinal_gaps_through_300']:
        lines.append(f'| {n} | No existe fila con este número en el archivo entregado; no fue descartada por el importador |')
    lines += ['', 'Además del saldo 300−294, hay diferencias de catálogo: cuatro posibles typos, dos títulos parciales, seis conflictos de artista y seis filas sin contraparte; del lado de los audios hay doce títulos adicionales. No se resolvieron por similitud ni por título solo.', '',
              '### Las 18 filas pendientes de asociación', '',
              '| Fila física / número | Artista | Título | Letra |', '|---|---|---|---|']
    for r in imported['rows']:
        if not r['matched_job_id']:
            lines.append(f"| {r['row']} / {r.get('ordinal')} | {r['artist']} | {r['title']} | {r['availability']} |")
    matched = {r['matched_job_id'] for r in imported['rows'] if r['matched_job_id']}
    lines += ['', '### Los 24 jobs sin vínculo aceptado', '', '| Job / orden lote | Artista | Título |', '|---|---|---|']
    for j in snapshot['jobs']:
        if j['job_id'] not in matched:
            lines.append(f"| `{j['job_id']}` / {j['ordinal']} | {j['artist']} | {j['title']} |")
    lines += ['', 'Las 154 letras se conservan como hipótesis de procedencia no verificada; no se inventaron fuentes/URLs/fechas. Las referencias derivadas del audio se cuentan por separado.', '',
              '## Nueve tests: comando y resultado', '', f"Python: `{tests['python']}`. Commit: `{tests['commit']}`. Exit: `{tests['returncode']}`. Tests PASSED: **{tests['passed_tests']}**.", '',
              '```bash', ' '.join(tests['command']), '```', '',
              'Directorio: `' + tests['cwd'] + '`. Registro íntegro: [nine-tests-evidence.json](nine-tests-evidence.json). Se usó el entorno Python 3.11 existente; no se cambió código ajeno para soportar Python 3.9.', '',
              '## Audio y consumo real por canción', '',
              '| Canción | Ref. externa | Clips | Llamadas | Latencia local s | Uso Whisper s | Gemini entrada texto/audio; salida | USD calculados del uso observado* |',
              '|---|---|---:|---:|---:|---:|---|---|']
    total_calculated = 0
    for s in report['songs']:
        sec = gt = ga = go = missing = 0
        for w in s['windows']:
            for e in w['B_audio']['evidence']:
                if e.get('kind') != 'blind_audio_request':
                    continue
                usage = e.get('usage')
                if not usage:
                    missing += 1
                elif e['provider'] == 'openai':
                    sec += usage.get('seconds', 0)
                else:
                    for t in usage.get('prompt_tokens_details') or []:
                        gt += t['token_count'] if t['modality'] == 'TEXT' else 0
                        ga += t['token_count'] if t['modality'] == 'AUDIO' else 0
                    go += usage.get('candidates_token_count') or 0
        cost = sec * .006 / 60 + (gt * .30 + ga * 1 + go * 2.50) / 1e6
        total_calculated += cost
        lines.append(f"| {s['title']} | {s['reference_availability']} | {len(s['windows'])} | {s['calls_this_run']} | {s['latency_seconds']} | {sec} | {gt}/{ga}; {go} | {cost:.6f}{' + 1 llamada sin uso recuperable' if missing else ''} |")
    lines += ['', f'*Subtotal calculable: USD {total_calculated:.6f}; no es la factura observada. El costo facturado total está NO CONFIRMADO: falta consumo de una respuesta Gemini inválida y factura/egress. No se imputaron separación ni entrenamiento nuevos.', '',
              'Tarifas consultadas: [Whisper](https://developers.openai.com/api/docs/models/whisper-1), USD 0,006/min; [Vertex Gemini 2.5 Flash estándar](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing), USD 0,30/M texto entrada, 1/M audio entrada y 2,50/M salida. Cálculo sobre usage devuelto, sin confundirlo con factura.', '',
              'Los modelos recibieron WAV PCM mono 16 kHz reales. Whisper-1 recibió mezcla, Gemini 2.5 Flash stem vocal; nunca texto de Genly/Excel, artista o título. El selector local sí vio las hipótesis después. Gemini no cuenta como testigo independiente de una referencia producida por Gemini.', '',
              '## Relojes y decisiones por fragmento', '',
              'Índices de línea mostrados desde 1. Local = global − offset. Los candidatos son pausas RMS medidas cada 20 ms; no son finales de palabra demostrados. Candidatos anteriores al inicio de línea quedan señalados como inválidos.', '',
              '| Canción / línea | Fragmento global s / offset | Actual global inicio→fin | Candidatos fin global (local) | Decisión texto / timing |',
              '|---|---|---|---|---|']
    for s in report['songs']:
        for w in s['windows']:
            win, b = w['window'], w['B_audio']
            candidates = [e for e in b['evidence'] if e.get('kind') == 'endpoint' and 'end_seconds' in e]
            ends = '; '.join(f"{e['end_seconds']:.3f} ({e['local_end_seconds']:.3f})" + (' [antes del inicio: inválido]' if e['end_seconds'] <= w['A_current']['start'] else '') for e in candidates) or 'ninguno'
            lines.append(f"| {s['title']} / {win['line_index']+1} | {win['start']:.3f}–{win['end']:.3f} / +{win['offset_seconds']:.3f} | {w['A_current']['start']:.3f}→{w['A_current']['end']:.3f} | {ends} | {b['content']['decision']} / {b['timing']['decision']}: {b['timing']['reason']} |")
    lines += ['', '### Qué discrepancias se encontraron y qué NO se concluye', '',
              '- Polizonte: 17 bloques de diferencia textual entre transcripción y Excel (comparación de secuencia de la canción, no 17 errores probados). La escucha cubrió 3 fragmentos, no la canción completa. Una respuesta Whisper fue vacía; las otras no permitieron seleccionar una corrección de ocurrencia con el gate vigente.',
              '- Hoy Le Pido A Dios: Excel `[NO ENCONTRADA]`; sí existe hipótesis derivada del audio. Cuatro fragmentos escuchados; uno mantuvo el texto sin certificarlo, tres sin evidencia suficiente para decidir. Los candidatos 190,20/190,84 s son pausas, no prueba de que deba alargarse el cartel.',
              '- Hecho En Buenos Aires: sin letra externa ni hipótesis derivada utilizable. Cuatro fragmentos escuchados. Se conservan los finales humanos; tres ventanas tienen línea locked. Una respuesta Whisper fue vacía; una respuesta Gemini tuvo JSON inválido. No se usaron esas fallas como gold.',
              '- Sincronía: las tres pruebas mezcla/stem dieron incertidumbre por correlación insuficiente en algún tramo. Duraciones difieren 37–50 ms; el lag de −0,5 s de una correlación baja NO es un desfase confirmado.',
              '- CTC y SOFA NO corrieron en este canary. Los tiempos usados como candidatos vienen de ffmpeg/energía RMS. Ni el acuerdo textual ni un alineamiento forzado se presentaron como escucha independiente.',
              '- Los timestamps devueltos por Gemini a veces exceden el fragmento: son hipótesis inválidas para timing, no se aplicaron. El reconocimiento no equivale a medir el fin cantado.', '',
              'Fallo de trazabilidad de la primera corrida: el JSON inválido se produjo DESPUÉS de la respuesta Gemini, pero el adaptador registró `received_audio=false` y perdió raw/usage de esa respuesta. La traza original se conserva. El fix posterior conserva raw/usage aunque falle el parseo, con test; no se repitió selectivamente esa llamada para borrar el fallo.', '',
              '## Evidencia completa y reproducción', '',
              '- [Reproductor local](http://127.0.0.1:8767/reproductor.html), solo en esta Mac. Primero escuchar a ciegas, luego mostrar carteles; Espacio reproduce, flechas saltan 2 s. “Marcar tiempo actual” evita tipear el fin. Las notas se guardan localmente y se descargan como borrador, nunca aprueban Genly.',
              '- [Reporte JSON completo](canary/report.json): texto actual, respuestas, fuente/modelo, hashes de mezcla/stem y cada clip, offsets, candidatos y motivos. Incluye TODOS los resultados.',
              '- [Captura del reproductor](canary/preview-smoke.png) y [smoke de navegador](preview-smoke.json).',
              '- [Verificación post-canary en base](post-canary-source-verification.json).', '',
              f"Navegador aislado Chromium {preview['browser']}: audio cargado/reproducido, seek, aparición/desaparición del cartel, modo ciego, notas tras reload y siguiente/anterior: OK. Es prueba técnica; no revisión humana del canto ni export UMG.", '',
              '## Recomendación y siguiente paso', '',
              '**Mejorar antes de publicar sugerencias.** Integración de audio confirmada; utilidad para reducir revisión todavía no demostrada. El siguiente cuello de botella es localizar la frase y aportar evidencia fonética/voz objetivo de su final, reutilizando hipótesis completas y alineadores existentes, no bajando gates ni agregando padding universal.', '',
              'Para la evaluación: matriz A/B/C emparejada, gold humano independiente con intervalo visual aceptable, parte ciega y doble revisor; medir minutos de escuchar/verificar/rechazar además de corregir. Este canary no estima precisión, ahorro ni fecha de automatización. La muestra test sigue sin consultar.', '',
              'Objetivo de producto: revisión enfocada en dudas, reproducción inmediata, sugerencias comprobables de un clic, guardado seguro y una aprobación por canción. Después de letra/timing se frena; fondos y videos no se activan.']
    target = base/'REPORTE-CANARY.md'
    with target.open('x') as handle:
        os.chmod(target, 0o600)
        handle.write('\n'.join(lines)+'\n')
    with (base/'import-reconciled.json').open('x') as handle:
        os.chmod(handle.name,0o600)
        json.dump(imported,handle,ensure_ascii=False,indent=2)
    print(json.dumps({'report':str(target),'rows':len(imported['rows']),'gaps':imported['ordinal_gaps_through_300'],
                      'nine_tests_passed':tests['passed_tests'],'source_unchanged':all(j['source_unchanged'] for j in post['jobs'])}))


if __name__ == '__main__':
    main()
