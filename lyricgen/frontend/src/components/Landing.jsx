import { useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "../i18n";
import BrandLockup from "./BrandLockup";
import "./Landing.css";

const API = import.meta.env.VITE_API_URL || "";

const COPY = {
  es: {
    metaTitle: "GenLy AI — Lyric videos con dirección visual generativa",
    metaDescription: "Creá lyric videos con IA desde la canción: transcripción y sincronización, dirección visual por concepto, movimiento, color y efectos, más Short y thumbnail.",
    navEngine: "Motor visual",
    navStyles: "Estilos",
    navWorkflow: "Cómo funciona",
    navFaq: "FAQ",
    navLabel: "Navegación principal",
    languageLabel: "Idioma",
    factsLabel: "Datos del producto",
    login: "Ingresar",
    dashboard: "Ir al workspace",
    navCta: "Crear lyric video",
    menuOpen: "Abrir navegación",
    menuClose: "Cerrar navegación",
    skip: "Saltar al contenido",
    heroEyebrow: "Dirección visual generativa para música",
    heroTitleA: "Tu canción",
    heroTitleB: "dirige el video.",
    heroBody: "GenLy interpreta la letra, construye una dirección visual con IA y sincroniza cada palabra. Elegí concepto, movimiento, color y efectos; después revisá todo antes del render.",
    primaryCta: "Crear mi lyric video",
    secondaryCta: "Explorar el motor",
    heroNote: "MP3 o WAV · Sin templates cerrados · Control antes de generar",
    generatedWith: "Dirección visual GenLy",
    heroMode: "Inspirado en la letra",
    heroConcept: "Acuático",
    heroMovement: "Movimiento sutil",
    heroPalette: "Paleta Neon",
    timelineVerse: "VERSO",
    timelineChorus: "CORO",
    statModes: "modos de dirección",
    statConcepts: "conceptos visuales",
    statOutputs: "entregables por canción",
    statBatch: "pistas por lote",
    engineEyebrow: "El diferencial de GenLy",
    engineTitle: "No elegís un template. Dirigís un lenguaje visual.",
    engineBody: "La dirección nace de la combinación entre la canción y tus decisiones. GenLy conecta letra, concepto, movimiento, paleta y efectos dentro de un mismo sistema visual.",
    modes: [
      { title: "Auto", label: "La IA propone", body: "Elegí los parámetros principales y dejá que GenLy construya una primera dirección visual." },
      { title: "Inspirado en la letra", label: "La canción conduce", body: "La letra define qué aparece en escena; el concepto elegido define cómo se representa." },
      { title: "Mi prompt", label: "Dirección precisa", body: "Describí tu escena y elegí si GenLy debe respetarla literalmente o enriquecerla." },
    ],
    systemNote: "Un sistema paramétrico y generativo: cada eje se puede dirigir de forma independiente.",
    stylesEyebrow: "Style system",
    stylesTitle: "Un estilo no es un filtro.",
    stylesBody: "Probá direcciones visuales construidas con los mismos controles que existen en el producto.",
    axisConcept: "Concepto",
    axisConceptBody: "El mundo visual de la canción: naturaleza, cósmico, vintage, abstracto y más.",
    axisMovement: "Movimiento",
    axisMovementBody: "Cámara fija con escena viva, drift sutil, movimiento cinematográfico, foto o animación.",
    axisPalette: "Paleta",
    axisPaletteBody: "Auto, Oscuro, Neon, Minimal, Cálido o colores propios del artista.",
    axisFx: "Efectos",
    axisFxBody: "Lluvia, nieve, estrellas, bokeh o luz compuestos sobre el fondo.",
    formulaResult: "Dirección visual",
    previewLabel: "Preview de dirección",
    directions: [
      { name: "Neon acuático", concept: "Acuático", movement: "Sutil", palette: "Neon", effect: "Luz" },
      { name: "Natural cinematográfico", concept: "Naturaleza", movement: "Cinemático", palette: "Minimal", effect: "Ninguno" },
      { name: "Atardecer cálido", concept: "Romántico", movement: "Escena viva", palette: "Cálido", effect: "Bokeh" },
    ],
    galleryEyebrow: "Direcciones, no plantillas",
    galleryTitle: "Una canción puede habitar mundos muy distintos.",
    galleryBody: "Explorá combinaciones de concepto, cámara y color. En GenLy podés conservar audio y lyrics y crear una nueva variante visual sin empezar de cero.",
    variantCta: "Crear una variante",
    scenesEyebrow: "Multi-escena",
    scenesTitle: "Una canción. Un universo visual coherente.",
    scenesBody: "GenLy puede dividir la canción en secciones, construir una identidad visual compartida y crear escenas conectadas. El coro puede recuperar su mundo visual y cada escena puede revisarse o regenerarse por separado.",
    scenesPoints: ["Secciones construidas desde la letra y su timing", "Biblia visual compartida entre escenas", "Regeneración puntual sin rehacer todo el video"],
    scenesCredit: "Multi-escena utiliza créditos adicionales.",
    sceneVerse: "Verso",
    sceneChorus: "Coro",
    sceneBridge: "Puente",
    workflowEyebrow: "Del audio al render",
    workflowTitle: "La IA acelera el trabajo. Vos conservás la decisión.",
    workflow: [
      { n: "01", title: "Subí la canción", body: "Cargá uno o hasta cinco archivos MP3 o WAV en un mismo lote." },
      { n: "02", title: "Revisá las lyrics", body: "GenLy transcribe y sincroniza. Corregí texto y timing desde el editor y la timeline." },
      { n: "03", title: "Dirigí el universo visual", body: "Elegí modo, concepto, movimiento, paleta, efectos, tipografía y animación." },
      { n: "04", title: "Generá y entregá", body: "Revisá el resultado y descargá el video, el Short vertical y el thumbnail." },
    ],
    controlEyebrow: "Editor humano en el centro",
    controlTitle: "La IA propone. Vos dirigís.",
    controlBody: "Editá cada línea y su timing, probá tipografías, tamaño, colores, karaoke y transiciones en el preview. Generá cuando el resultado esté listo para vos.",
    controlTags: ["Texto y timing", "Preview sincronizado", "Tipografía y color", "Karaoke y animaciones", "Variantes visuales"],
    editorTitle: "Editor de lyrics",
    editorSynced: "Sincronizado",
    editorLineA: "Tu canción abre un mundo",
    editorLineB: "y cada palabra encuentra su lugar",
    editorLineC: "en movimiento",
    outputsEyebrow: "Entregables reales",
    outputsTitle: "Una canción. Tres piezas listas para publicar.",
    outputsBody: "El flujo estándar produce los formatos principales de un lanzamiento visual desde el mismo proyecto.",
    outputs: [
      { title: "Lyric video", meta: "MP4 · Full HD · 16:9", body: "La pieza principal para YouTube y reproducción horizontal." },
      { title: "Short vertical", meta: "MP4 · 1080 × 1920 · 30 s", body: "Una versión vertical preparada para contenido corto." },
      { title: "Thumbnail", meta: "JPG · 1280 × 720", body: "La portada horizontal generada junto con el video." },
    ],
    proNote: "ProRes y entregas broadcast están disponibles para cuentas habilitadas.",
    capabilitiesEyebrow: "Más allá del primer render",
    capabilitiesTitle: "Construido para iterar, producir y volver a usar.",
    capabilities: [
      { title: "Variantes", body: "Mismo audio y lyrics, una nueva dirección de fondo." },
      { title: "Lotes", body: "Procesá hasta cinco canciones dentro del mismo flujo." },
      { title: "Historial", body: "Buscá, filtrá y retomá cada trabajo desde su estado real." },
      { title: "Publicación", body: "Descargá los archivos o publicá en YouTube con metadata generada con IA." },
    ],
    definitionEyebrow: "Qué es GenLy",
    definitionTitle: "Un lyric video maker con dirección visual generativa.",
    definitionBody: "GenLy es una aplicación web para crear lyric videos a partir de una canción. Combina transcripción y sincronización de letras, generación visual con IA, controles de estilo y un editor de revisión. No funciona como una biblioteca de templates cerrados: el usuario dirige concepto, movimiento, color, efectos, tipografía y animación antes de generar los entregables.",
    definitionFor: "Pensado para artistas, productores, equipos de contenido y sellos que necesitan transformar canciones en piezas visuales sin perder control creativo.",
    faqEyebrow: "Preguntas frecuentes",
    faqTitle: "Lo esencial, sin promesas vagas.",
    faqs: [
      { q: "¿GenLy usa templates de lyric videos?", a: "No trabaja sobre templates de video cerrados. Usa un sistema generativo dirigido mediante concepto, movimiento, paleta, efectos, tipografía y animaciones. También podés usar un fondo propio o una opción disponible en la biblioteca." },
      { q: "¿Puedo corregir la letra y la sincronización?", a: "Sí. Antes del render podés editar el texto, ajustar el timing en una timeline, dividir o unir líneas y revisar el resultado en un preview sincronizado." },
      { q: "¿Qué puedo controlar del estilo visual?", a: "Podés elegir el modo creativo, género, concepto visual, movimiento, paleta, colores personalizados, efectos, tipografía, tamaño, contraste y animación de las lyrics." },
      { q: "¿Qué entrega genera cada canción?", a: "El flujo estándar genera un lyric video Full HD 16:9, un Short vertical de 30 segundos y un thumbnail JPG. Algunas cuentas también tienen entregas ProRes habilitadas." },
      { q: "¿Puedo crear videos con varias escenas?", a: "Sí. El modo multi-escena crea una identidad visual compartida, organiza escenas por secciones de la canción y permite revisar o regenerar escenas individuales. Utiliza créditos adicionales." },
      { q: "¿Puedo trabajar varias canciones juntas?", a: "Sí. Actualmente el wizard admite lotes de hasta cinco archivos MP3 o WAV y permite personalizar canciones individualmente dentro del lote." },
    ],
    contactEyebrow: "Para catálogos y equipos",
    contactTitle: "¿Tenés un volumen de lanzamientos para producir?",
    contactBody: "Contanos cuántas canciones manejás y cómo es tu flujo actual. Te mostramos GenLy con casos alineados a tu operación.",
    contactMagnet: "Demo de producto y evaluación de volumen, sin claims inventados.",
    formName: "Nombre",
    formCompany: "Sello, estudio o empresa",
    formEmail: "Email de trabajo",
    formVolume: "Volumen aproximado",
    volumeOptions: ["1–5 canciones por mes", "6–25 canciones por mes", "Más de 25 canciones por mes"],
    formMessage: "Contanos qué necesitás producir",
    formSubmit: "Solicitar una demo",
    formSending: "Enviando…",
    formSent: "Recibimos tu consulta. Te contactaremos pronto.",
    formError: "No pudimos enviar el formulario. Abrimos tu correo con la consulta preparada.",
    finalEyebrow: "Empezá desde la canción",
    finalTitle: "Tu próximo lyric video no tiene que parecerse a todos los demás.",
    finalBody: "Subí el audio, definí una dirección visual y revisá cada detalle antes de generar.",
    finalCta: "Crear lyric video",
    footerProduct: "Producto",
    footerEngine: "Motor visual",
    footerStyles: "Sistema de estilos",
    footerWorkflow: "Flujo de creación",
    footerCompany: "GenLy AI",
    footerDescription: "Dirección visual generativa para lyric videos.",
    footerRights: "GenLy AI · Producto creado para música",
  },
  en: {
    metaTitle: "GenLy AI — Lyric videos with generative visual direction",
    metaDescription: "Create AI lyric videos from the song: transcription and sync, visual direction by concept, motion, color and effects, plus a Short and thumbnail.",
    navEngine: "Visual engine", navStyles: "Styles", navWorkflow: "How it works", navFaq: "FAQ", navLabel: "Primary navigation", languageLabel: "Language", factsLabel: "Product facts", login: "Sign in", dashboard: "Open workspace", navCta: "Create lyric video", menuOpen: "Open navigation", menuClose: "Close navigation", skip: "Skip to content",
    heroEyebrow: "Generative visual direction for music", heroTitleA: "Your song", heroTitleB: "directs the video.", heroBody: "GenLy interprets the lyrics, builds an AI visual direction and syncs every word. Choose concept, motion, color and effects; then review everything before rendering.", primaryCta: "Create my lyric video", secondaryCta: "Explore the engine", heroNote: "MP3 or WAV · No closed templates · Control before generation", generatedWith: "GenLy visual direction", heroMode: "Inspired by the lyrics", heroConcept: "Aquatic", heroMovement: "Subtle motion", heroPalette: "Neon palette", timelineVerse: "VERSE", timelineChorus: "CHORUS",
    statModes: "direction modes", statConcepts: "visual concepts", statOutputs: "deliverables per song", statBatch: "tracks per batch",
    engineEyebrow: "The GenLy difference", engineTitle: "You don't choose a template. You direct a visual language.", engineBody: "Direction comes from the combination of the song and your decisions. GenLy connects lyrics, concept, motion, palette and effects inside one visual system.",
    modes: [{ title: "Auto", label: "AI proposes", body: "Choose the main parameters and let GenLy build a first visual direction." }, { title: "Inspired by lyrics", label: "The song leads", body: "The lyrics define what appears; the selected concept defines how it is represented." }, { title: "My prompt", label: "Precise direction", body: "Describe your scene and choose whether GenLy should follow it literally or enrich it." }],
    systemNote: "A parametric, generative system: every axis can be directed independently.",
    stylesEyebrow: "Style system", stylesTitle: "A style is not a filter.", stylesBody: "Explore visual directions built with the same controls available in the product.", axisConcept: "Concept", axisConceptBody: "The song's visual world: nature, cosmic, vintage, abstract and more.", axisMovement: "Motion", axisMovementBody: "Live static scene, subtle drift, cinematic camera, still image or animation.", axisPalette: "Palette", axisPaletteBody: "Auto, Dark, Neon, Minimal, Warm or the artist's own colors.", axisFx: "Effects", axisFxBody: "Rain, snow, stars, bokeh or light composited over the background.", formulaResult: "Visual direction", previewLabel: "Direction preview",
    directions: [{ name: "Aquatic neon", concept: "Aquatic", movement: "Subtle", palette: "Neon", effect: "Light" }, { name: "Cinematic nature", concept: "Nature", movement: "Cinematic", palette: "Minimal", effect: "None" }, { name: "Warm sunset", concept: "Romantic", movement: "Live scene", palette: "Warm", effect: "Bokeh" }],
    galleryEyebrow: "Directions, not templates", galleryTitle: "One song can inhabit very different worlds.", galleryBody: "Explore combinations of concept, camera and color. In GenLy you can keep the audio and lyrics and create a new visual variant without starting over.", variantCta: "Create a variant",
    scenesEyebrow: "Multi-scene", scenesTitle: "One song. One coherent visual universe.", scenesBody: "GenLy can divide the song into sections, build a shared visual identity and create connected scenes. The chorus can return to its visual world, and each scene can be reviewed or regenerated separately.", scenesPoints: ["Sections built from lyrics and timing", "A shared visual bible across scenes", "Targeted regeneration without remaking the full video"], scenesCredit: "Multi-scene uses additional credits.", sceneVerse: "Verse", sceneChorus: "Chorus", sceneBridge: "Bridge",
    workflowEyebrow: "From audio to render", workflowTitle: "AI accelerates the work. You keep the decision.", workflow: [{ n: "01", title: "Upload the song", body: "Add one or up to five MP3 or WAV files in the same batch." }, { n: "02", title: "Review the lyrics", body: "GenLy transcribes and syncs. Correct text and timing in the editor and timeline." }, { n: "03", title: "Direct the visual world", body: "Choose mode, concept, motion, palette, effects, typography and animation." }, { n: "04", title: "Generate and deliver", body: "Review the result and download the video, vertical Short and thumbnail." }],
    controlEyebrow: "A human-centered editor", controlTitle: "AI proposes. You direct.", controlBody: "Edit every line and its timing, test typefaces, size, colors, karaoke and transitions in the preview. Generate when the result is ready for you.", controlTags: ["Text and timing", "Synced preview", "Typography and color", "Karaoke and animation", "Visual variants"], editorTitle: "Lyrics editor", editorSynced: "Synced", editorLineA: "Your song opens a world", editorLineB: "and every word finds its place", editorLineC: "in motion",
    outputsEyebrow: "Real deliverables", outputsTitle: "One song. Three pieces ready to publish.", outputsBody: "The standard flow produces the main formats of a visual release from the same project.", outputs: [{ title: "Lyric video", meta: "MP4 · Full HD · 16:9", body: "The main piece for YouTube and horizontal playback." }, { title: "Vertical Short", meta: "MP4 · 1080 × 1920 · 30 s", body: "A vertical version prepared for short-form content." }, { title: "Thumbnail", meta: "JPG · 1280 × 720", body: "The horizontal cover generated with the video." }], proNote: "ProRes and broadcast deliverables are available for enabled accounts.",
    capabilitiesEyebrow: "Beyond the first render", capabilitiesTitle: "Built to iterate, produce and reuse.", capabilities: [{ title: "Variants", body: "Same audio and lyrics, a new background direction." }, { title: "Batches", body: "Process up to five songs in the same workflow." }, { title: "History", body: "Search, filter and resume every job from its real state." }, { title: "Publishing", body: "Download files or publish to YouTube with AI-generated metadata." }],
    definitionEyebrow: "What is GenLy", definitionTitle: "A lyric video maker with generative visual direction.", definitionBody: "GenLy is a web application for creating lyric videos from a song. It combines lyrics transcription and synchronization, AI visual generation, style controls and a review editor. It does not work as a library of closed templates: the user directs concept, motion, color, effects, typography and animation before generating deliverables.", definitionFor: "Built for artists, producers, content teams and labels that need to turn songs into visual pieces without losing creative control.",
    faqEyebrow: "Frequently asked questions", faqTitle: "The essentials, without vague promises.", faqs: [{ q: "Does GenLy use lyric video templates?", a: "It does not rely on closed video templates. It uses a generative system directed through concept, motion, palette, effects, typography and animation. You can also use your own background or an available library option." }, { q: "Can I correct the lyrics and synchronization?", a: "Yes. Before rendering you can edit text, adjust timing on a timeline, split or merge lines and review the result in a synchronized preview." }, { q: "What can I control in the visual style?", a: "You can choose creative mode, genre, visual concept, motion, palette, custom colors, effects, typography, size, contrast and lyrics animation." }, { q: "What does each song generate?", a: "The standard flow generates a Full HD 16:9 lyric video, a 30-second vertical Short and a JPG thumbnail. Some accounts also have ProRes deliverables enabled." }, { q: "Can I create videos with multiple scenes?", a: "Yes. Multi-scene creates a shared visual identity, organizes scenes by song section and lets you review or regenerate individual scenes. It uses additional credits." }, { q: "Can I work on several songs together?", a: "Yes. The wizard currently supports batches of up to five MP3 or WAV files and lets you customize individual songs inside the batch." }],
    contactEyebrow: "For catalogs and teams", contactTitle: "Do you have a release volume to produce?", contactBody: "Tell us how many songs you manage and what your current workflow looks like. We'll show GenLy with cases aligned to your operation.", contactMagnet: "Product demo and volume assessment, grounded in the real product.", formName: "Name", formCompany: "Label, studio or company", formEmail: "Work email", formVolume: "Approximate volume", volumeOptions: ["1–5 songs per month", "6–25 songs per month", "More than 25 songs per month"], formMessage: "Tell us what you need to produce", formSubmit: "Request a demo", formSending: "Sending…", formSent: "We received your request. We'll contact you soon.", formError: "We couldn't send the form. We opened your email with the request prepared.",
    finalEyebrow: "Start from the song", finalTitle: "Your next lyric video doesn't have to look like everyone else's.", finalBody: "Upload the audio, define a visual direction and review every detail before generating.", finalCta: "Create lyric video", footerProduct: "Product", footerEngine: "Visual engine", footerStyles: "Style system", footerWorkflow: "Creation workflow", footerCompany: "GenLy AI", footerDescription: "Generative visual direction for lyric videos.", footerRights: "GenLy AI · A product built for music",
  },
  pt: {
    metaTitle: "GenLy AI — Lyric videos com direção visual generativa",
    metaDescription: "Crie lyric videos com IA a partir da música: transcrição e sincronização, direção visual por conceito, movimento, cor e efeitos, mais Short e thumbnail.",
    navEngine: "Motor visual", navStyles: "Estilos", navWorkflow: "Como funciona", navFaq: "FAQ", navLabel: "Navegação principal", languageLabel: "Idioma", factsLabel: "Dados do produto", login: "Entrar", dashboard: "Ir ao workspace", navCta: "Criar lyric video", menuOpen: "Abrir navegação", menuClose: "Fechar navegação", skip: "Pular para o conteúdo",
    heroEyebrow: "Direção visual generativa para música", heroTitleA: "Sua música", heroTitleB: "dirige o vídeo.", heroBody: "GenLy interpreta a letra, constrói uma direção visual com IA e sincroniza cada palavra. Escolha conceito, movimento, cor e efeitos; depois revise tudo antes do render.", primaryCta: "Criar meu lyric video", secondaryCta: "Explorar o motor", heroNote: "MP3 ou WAV · Sem templates fechados · Controle antes de gerar", generatedWith: "Direção visual GenLy", heroMode: "Inspirado na letra", heroConcept: "Aquático", heroMovement: "Movimento sutil", heroPalette: "Paleta Neon", timelineVerse: "VERSO", timelineChorus: "REFRÃO",
    statModes: "modos de direção", statConcepts: "conceitos visuais", statOutputs: "entregáveis por música", statBatch: "faixas por lote",
    engineEyebrow: "O diferencial GenLy", engineTitle: "Você não escolhe um template. Você dirige uma linguagem visual.", engineBody: "A direção nasce da combinação entre a música e suas decisões. GenLy conecta letra, conceito, movimento, paleta e efeitos em um mesmo sistema visual.", modes: [{ title: "Auto", label: "A IA propõe", body: "Escolha os parâmetros principais e deixe GenLy construir uma primeira direção visual." }, { title: "Inspirado na letra", label: "A música conduz", body: "A letra define o que aparece; o conceito escolhido define como é representado." }, { title: "Meu prompt", label: "Direção precisa", body: "Descreva sua cena e escolha se GenLy deve respeitá-la literalmente ou enriquecê-la." }], systemNote: "Um sistema paramétrico e generativo: cada eixo pode ser dirigido de forma independente.",
    stylesEyebrow: "Style system", stylesTitle: "Um estilo não é um filtro.", stylesBody: "Explore direções visuais construídas com os mesmos controles disponíveis no produto.", axisConcept: "Conceito", axisConceptBody: "O mundo visual da música: natureza, cósmico, vintage, abstrato e mais.", axisMovement: "Movimento", axisMovementBody: "Cena viva fixa, drift sutil, câmera cinematográfica, imagem fixa ou animação.", axisPalette: "Paleta", axisPaletteBody: "Auto, Escuro, Neon, Minimal, Quente ou as cores do artista.", axisFx: "Efeitos", axisFxBody: "Chuva, neve, estrelas, bokeh ou luz compostos sobre o fundo.", formulaResult: "Direção visual", previewLabel: "Preview da direção", directions: [{ name: "Neon aquático", concept: "Aquático", movement: "Sutil", palette: "Neon", effect: "Luz" }, { name: "Natureza cinematográfica", concept: "Natureza", movement: "Cinemático", palette: "Minimal", effect: "Nenhum" }, { name: "Pôr do sol quente", concept: "Romântico", movement: "Cena viva", palette: "Quente", effect: "Bokeh" }],
    galleryEyebrow: "Direções, não templates", galleryTitle: "Uma música pode habitar mundos muito diferentes.", galleryBody: "Explore combinações de conceito, câmera e cor. No GenLy você pode manter áudio e lyrics e criar uma nova variante visual sem começar do zero.", variantCta: "Criar uma variante",
    scenesEyebrow: "Multi-cena", scenesTitle: "Uma música. Um universo visual coerente.", scenesBody: "GenLy pode dividir a música em seções, construir uma identidade visual compartilhada e criar cenas conectadas. O refrão pode retornar ao seu mundo visual e cada cena pode ser revisada ou regenerada separadamente.", scenesPoints: ["Seções construídas a partir da letra e timing", "Bíblia visual compartilhada entre cenas", "Regeneração pontual sem refazer o vídeo inteiro"], scenesCredit: "Multi-cena utiliza créditos adicionais.", sceneVerse: "Verso", sceneChorus: "Refrão", sceneBridge: "Ponte",
    workflowEyebrow: "Do áudio ao render", workflowTitle: "A IA acelera o trabalho. Você mantém a decisão.", workflow: [{ n: "01", title: "Envie a música", body: "Adicione um ou até cinco arquivos MP3 ou WAV no mesmo lote." }, { n: "02", title: "Revise as lyrics", body: "GenLy transcreve e sincroniza. Corrija texto e timing no editor e na timeline." }, { n: "03", title: "Dirija o universo visual", body: "Escolha modo, conceito, movimento, paleta, efeitos, tipografia e animação." }, { n: "04", title: "Gere e entregue", body: "Revise o resultado e baixe o vídeo, Short vertical e thumbnail." }],
    controlEyebrow: "Editor humano no centro", controlTitle: "A IA propõe. Você dirige.", controlBody: "Edite cada linha e seu timing, teste fontes, tamanho, cores, karaokê e transições no preview. Gere quando o resultado estiver pronto para você.", controlTags: ["Texto e timing", "Preview sincronizado", "Tipografia e cor", "Karaokê e animações", "Variantes visuais"], editorTitle: "Editor de lyrics", editorSynced: "Sincronizado", editorLineA: "Sua música abre um mundo", editorLineB: "e cada palavra encontra seu lugar", editorLineC: "em movimento",
    outputsEyebrow: "Entregáveis reais", outputsTitle: "Uma música. Três peças prontas para publicar.", outputsBody: "O fluxo padrão produz os principais formatos de um lançamento visual a partir do mesmo projeto.", outputs: [{ title: "Lyric video", meta: "MP4 · Full HD · 16:9", body: "A peça principal para YouTube e reprodução horizontal." }, { title: "Short vertical", meta: "MP4 · 1080 × 1920 · 30 s", body: "Uma versão vertical preparada para conteúdo curto." }, { title: "Thumbnail", meta: "JPG · 1280 × 720", body: "A capa horizontal gerada junto com o vídeo." }], proNote: "ProRes e entregas broadcast estão disponíveis para contas habilitadas.",
    capabilitiesEyebrow: "Além do primeiro render", capabilitiesTitle: "Construído para iterar, produzir e reutilizar.", capabilities: [{ title: "Variantes", body: "Mesmo áudio e lyrics, uma nova direção de fundo." }, { title: "Lotes", body: "Processe até cinco músicas no mesmo fluxo." }, { title: "Histórico", body: "Busque, filtre e retome cada trabalho a partir do seu estado real." }, { title: "Publicação", body: "Baixe os arquivos ou publique no YouTube com metadata gerada por IA." }],
    definitionEyebrow: "O que é GenLy", definitionTitle: "Um lyric video maker com direção visual generativa.", definitionBody: "GenLy é uma aplicação web para criar lyric videos a partir de uma música. Combina transcrição e sincronização de letras, geração visual com IA, controles de estilo e um editor de revisão. Não funciona como uma biblioteca de templates fechados: o usuário dirige conceito, movimento, cor, efeitos, tipografia e animação antes de gerar os entregáveis.", definitionFor: "Criado para artistas, produtores, equipes de conteúdo e gravadoras que precisam transformar músicas em peças visuais sem perder controle criativo.",
    faqEyebrow: "Perguntas frequentes", faqTitle: "O essencial, sem promessas vagas.", faqs: [{ q: "GenLy usa templates de lyric videos?", a: "Não trabalha com templates de vídeo fechados. Usa um sistema generativo dirigido por conceito, movimento, paleta, efeitos, tipografia e animações. Você também pode usar um fundo próprio ou uma opção disponível na biblioteca." }, { q: "Posso corrigir a letra e a sincronização?", a: "Sim. Antes do render você pode editar o texto, ajustar o timing em uma timeline, dividir ou unir linhas e revisar o resultado em um preview sincronizado." }, { q: "O que posso controlar no estilo visual?", a: "Você pode escolher modo criativo, gênero, conceito visual, movimento, paleta, cores personalizadas, efeitos, tipografia, tamanho, contraste e animação das lyrics." }, { q: "O que cada música gera?", a: "O fluxo padrão gera um lyric video Full HD 16:9, um Short vertical de 30 segundos e um thumbnail JPG. Algumas contas também têm entregas ProRes habilitadas." }, { q: "Posso criar vídeos com várias cenas?", a: "Sim. Multi-cena cria uma identidade visual compartilhada, organiza cenas por seção da música e permite revisar ou regenerar cenas individuais. Utiliza créditos adicionais." }, { q: "Posso trabalhar várias músicas juntas?", a: "Sim. Atualmente o wizard admite lotes de até cinco arquivos MP3 ou WAV e permite personalizar músicas individualmente dentro do lote." }],
    contactEyebrow: "Para catálogos e equipes", contactTitle: "Tem um volume de lançamentos para produzir?", contactBody: "Conte quantas músicas você administra e como é seu fluxo atual. Mostramos GenLy com casos alinhados à sua operação.", contactMagnet: "Demo de produto e avaliação de volume baseadas no produto real.", formName: "Nome", formCompany: "Gravadora, estúdio ou empresa", formEmail: "Email de trabalho", formVolume: "Volume aproximado", volumeOptions: ["1–5 músicas por mês", "6–25 músicas por mês", "Mais de 25 músicas por mês"], formMessage: "Conte o que você precisa produzir", formSubmit: "Solicitar uma demo", formSending: "Enviando…", formSent: "Recebemos sua consulta. Entraremos em contato em breve.", formError: "Não conseguimos enviar o formulário. Abrimos seu email com a consulta preparada.",
    finalEyebrow: "Comece pela música", finalTitle: "Seu próximo lyric video não precisa parecer com todos os outros.", finalBody: "Envie o áudio, defina uma direção visual e revise cada detalhe antes de gerar.", finalCta: "Criar lyric video", footerProduct: "Produto", footerEngine: "Motor visual", footerStyles: "Sistema de estilos", footerWorkflow: "Fluxo de criação", footerCompany: "GenLy AI", footerDescription: "Direção visual generativa para lyric videos.", footerRights: "GenLy AI · Produto criado para música",
  },
};

const DIRECTION_MEDIA = [
  { video: "/samples/ex1.mp4", poster: "/samples/sample-reef.png", accent: "cyan" },
  { video: "/samples/ex2.mp4", poster: "/samples/sample-forest.png", accent: "green" },
  { video: "/samples/ex3.mp4", poster: "/samples/sample-reef.png", accent: "amber" },
];

function ArrowIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6" /></svg>;
}

function CheckIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6" /></svg>;
}

function SparkIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 2c.7 5.7 4.3 9.3 10 10-5.7.7-9.3 4.3-10 10-.7-5.7-4.3-9.3-10-10 5.7-.7 9.3-4.3 10-10Z" /><path d="M19 2c.2 1.7 1.3 2.8 3 3-1.7.2-2.8 1.3-3 3-.2-1.7-1.3-2.8-3-3 1.7-.2 2.8-1.3 3-3Z" /></svg>;
}

function PlayIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path d="m9 7 8 5-8 5V7Z" /></svg>;
}

function SectionHeading({ eyebrow, title, body, align = "left" }) {
  return (
    <div className={`mk-heading mk-heading--${align}`}>
      <p className="mk-eyebrow"><span /><span>{eyebrow}</span></p>
      <h2>{title}</h2>
      {body && <p className="mk-heading__body">{body}</p>}
    </div>
  );
}

function VideoFrame({ src, poster, label, className = "", eager = false }) {
  const frameRef = useRef(null);
  const [shouldLoad, setShouldLoad] = useState(eager);

  useEffect(() => {
    if (eager || shouldLoad) return undefined;
    if (!("IntersectionObserver" in window)) {
      setShouldLoad(true);
      return undefined;
    }
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) {
        setShouldLoad(true);
        observer.disconnect();
      }
    }, { rootMargin: "240px 0px" });
    if (frameRef.current) observer.observe(frameRef.current);
    return () => observer.disconnect();
  }, [eager, shouldLoad]);

  return (
    <div ref={frameRef} className={`mk-video ${className}`} style={{ backgroundImage: `url(${poster})` }}>
      <video key={src} aria-hidden="true" autoPlay={shouldLoad} muted loop playsInline preload={eager ? "metadata" : "none"} poster={poster}>
        {shouldLoad && <source src={src} type="video/mp4" />}
      </video>
      {label && <span className="mk-video__label"><PlayIcon />{label}</span>}
      <span className="mk-video__noise" aria-hidden="true" />
    </div>
  );
}

export default function Landing({ onStart, onLogin, isLoggedIn = false }) {
  const { lang, setLang } = useI18n();
  const c = COPY[lang] || COPY.es;
  const [menuOpen, setMenuOpen] = useState(false);
  const [activeMode, setActiveMode] = useState(1);
  const [activeDirection, setActiveDirection] = useState(0);
  const [formState, setFormState] = useState("idle");
  const mobileNavRef = useRef(null);
  const mobileMenuRef = useRef(null);
  const mobileTriggerRef = useRef(null);

  const activeVisual = useMemo(() => ({
    ...DIRECTION_MEDIA[activeDirection],
    ...c.directions[activeDirection],
  }), [activeDirection, c]);

  useEffect(() => {
    document.documentElement.lang = lang;
    document.title = c.metaTitle;
    const updateMeta = (selector, value) => {
      const element = document.head.querySelector(selector);
      if (element) element.setAttribute("content", value);
    };
    updateMeta('meta[name="description"]', c.metaDescription);
    updateMeta('meta[property="og:title"]', c.metaTitle);
    updateMeta('meta[property="og:description"]', c.metaDescription);
    updateMeta('meta[name="twitter:title"]', c.metaTitle);
    updateMeta('meta[name="twitter:description"]', c.metaDescription);
  }, [c, lang]);

  useEffect(() => {
    if (!menuOpen) return undefined;
    requestAnimationFrame(() => mobileMenuRef.current?.querySelector("a, button")?.focus());
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        mobileTriggerRef.current?.focus();
      }
    };
    const onPointerDown = (event) => {
      if (!mobileNavRef.current?.contains(event.target)) setMenuOpen(false);
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [menuOpen]);

  const handleSalesSubmit = async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = {
      name: form.name.value.trim(),
      company: form.company.value.trim(),
      email: form.email.value.trim(),
      volume: form.volume.value,
      message: form.message.value.trim(),
    };
    setFormState("loading");
    try {
      const response = await fetch(`${API}/api/leads`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setFormState("sent");
      form.reset();
    } catch {
      setFormState("error");
      const subject = `GenLy AI — Demo${payload.company ? ` (${payload.company})` : ""}`;
      const body = [`Nombre: ${payload.name}`, `Empresa: ${payload.company}`, `Email: ${payload.email}`, `Volumen: ${payload.volume}`, "", payload.message].join("\n");
      window.location.href = `mailto:tomas@epical.digital?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    }
  };

  const startLabel = isLoggedIn ? c.dashboard : c.primaryCta;

  return (
    <div className="mk-site creative-marketing">
      <a className="mk-skip" href="#main-content">{c.skip}</a>
      <div className="mk-ambient" aria-hidden="true"><i /><i /><i /></div>

      <header ref={mobileNavRef} className="mk-nav">
        <div className="mk-nav__inner">
          <a href="#top" className="mk-nav__brand" aria-label="GenLy AI"><BrandLockup size="md" priority /></a>
          <nav className="mk-nav__links" aria-label={c.navLabel}>
            <a href="#engine">{c.navEngine}</a>
            <a href="#styles">{c.navStyles}</a>
            <a href="#workflow">{c.navWorkflow}</a>
            <a href="#faq">{c.navFaq}</a>
          </nav>
          <div className="mk-nav__actions">
            <div className="mk-language" role="group" aria-label={c.languageLabel}>
              {["es", "en", "pt"].map((code) => (
                <button key={code} type="button" onClick={() => setLang(code)} aria-pressed={lang === code}>{code}</button>
              ))}
            </div>
            {!isLoggedIn && <button type="button" onClick={onLogin} className="mk-login">{c.login}</button>}
            <button type="button" onClick={isLoggedIn ? onStart : onLogin} className="mk-button mk-button--compact">{isLoggedIn ? c.dashboard : c.navCta}<ArrowIcon /></button>
          </div>
          <button ref={mobileTriggerRef} type="button" className="mk-menu-trigger" onClick={() => setMenuOpen((open) => !open)} aria-expanded={menuOpen} aria-controls="mk-mobile-menu" aria-label={menuOpen ? c.menuClose : c.menuOpen}>
            <span /><span /><span />
          </button>
        </div>
        {menuOpen && (
          <div ref={mobileMenuRef} id="mk-mobile-menu" className="mk-mobile-menu">
            <a href="#engine" onClick={() => setMenuOpen(false)}>{c.navEngine}</a>
            <a href="#styles" onClick={() => setMenuOpen(false)}>{c.navStyles}</a>
            <a href="#workflow" onClick={() => setMenuOpen(false)}>{c.navWorkflow}</a>
            <a href="#faq" onClick={() => setMenuOpen(false)}>{c.navFaq}</a>
            <div className="mk-language mk-language--mobile" role="group" aria-label={c.languageLabel}>{["es", "en", "pt"].map((code) => <button key={code} type="button" onClick={() => setLang(code)} aria-pressed={lang === code}>{code}</button>)}</div>
            <button type="button" onClick={isLoggedIn ? onStart : onLogin} className="mk-button">{isLoggedIn ? c.dashboard : c.navCta}<ArrowIcon /></button>
          </div>
        )}
      </header>

      <main id="main-content">
        <section id="top" className="mk-hero mk-container">
          <div className="mk-hero__copy">
            <p className="mk-pill"><SparkIcon />{c.heroEyebrow}</p>
            <h1 aria-label={`${c.heroTitleA} ${c.heroTitleB}`}><span>{c.heroTitleA}</span><strong>{c.heroTitleB}</strong></h1>
            <p className="mk-hero__body">{c.heroBody}</p>
            <div className="mk-hero__actions">
              <button type="button" onClick={isLoggedIn ? onStart : onLogin} className="mk-button">{startLabel}<ArrowIcon /></button>
              <a href="#engine" className="mk-button mk-button--ghost">{c.secondaryCta}</a>
            </div>
            <p className="mk-hero__note"><CheckIcon />{c.heroNote}</p>
          </div>

          <div className="mk-hero-studio">
            <div className="mk-studio__topbar"><span><i />GenLy Studio</span><span className="mk-studio__status">AI direction</span></div>
            <VideoFrame src="/demo.mp4" poster="/samples/sample-reef.png" label={c.generatedWith} eager />
            <div className="mk-studio__controls">
              {[c.heroMode, c.heroConcept, c.heroMovement, c.heroPalette].map((item, index) => <span key={item} className={index === 0 ? "is-active" : ""}>{item}</span>)}
            </div>
            <div className="mk-studio__timeline" aria-hidden="true">
              <div className="mk-studio__time"><span>00:18</span><span>03:24</span></div>
              <div className="mk-studio__track"><i /><b style={{ left: "28%" }} /><span className="verse">{c.timelineVerse}</span><span className="chorus">{c.timelineChorus}</span><span className="verse two">{c.timelineVerse}</span></div>
              <div className="mk-waveform">{Array.from({ length: 54 }, (_, index) => <i key={index} style={{ height: `${18 + ((index * 17) % 68)}%` }} />)}</div>
            </div>
          </div>
        </section>

        <section className="mk-proof mk-container" aria-label={c.factsLabel}>
          {[["3", c.statModes], ["15", c.statConcepts], ["3", c.statOutputs], ["5", c.statBatch]].map(([value, label]) => <div key={label}><strong>{value}</strong><span>{label}</span></div>)}
        </section>

        <section id="engine" className="mk-section mk-container">
          <SectionHeading eyebrow={c.engineEyebrow} title={c.engineTitle} body={c.engineBody} align="center" />
          <div className="mk-modes" role="group" aria-label={c.engineTitle}>
            {c.modes.map((mode, index) => (
              <button key={mode.title} type="button" aria-pressed={activeMode === index} className={activeMode === index ? "is-active" : ""} onClick={() => setActiveMode(index)}>
                <span className="mk-modes__number">0{index + 1}</span><span className="mk-modes__icon"><SparkIcon /></span><small>{mode.label}</small><strong>{mode.title}</strong><p>{mode.body}</p><span className="mk-modes__arrow"><ArrowIcon /></span>
              </button>
            ))}
          </div>
          <div className="mk-system-note"><SparkIcon /><span>{c.systemNote}</span><b>{c.modes[activeMode].title}</b></div>
        </section>

        <section id="styles" className="mk-section mk-section--wide">
          <div className="mk-container">
            <SectionHeading eyebrow={c.stylesEyebrow} title={c.stylesTitle} body={c.stylesBody} />
            <div className="mk-style-lab">
              <div className="mk-style-lab__axes">
                {[[c.axisConcept, c.axisConceptBody, "01"], [c.axisMovement, c.axisMovementBody, "02"], [c.axisPalette, c.axisPaletteBody, "03"], [c.axisFx, c.axisFxBody, "04"]].map(([title, body, number]) => <article key={title}><span>{number}</span><div><h3>{title}</h3><p>{body}</p></div></article>)}
              </div>
              <div className="mk-style-lab__preview">
                <div className="mk-style-lab__label"><span>{c.previewLabel}</span><strong>{activeVisual.name}</strong></div>
                <VideoFrame src={activeVisual.video} poster={activeVisual.poster} />
                <div className="mk-style-lab__meta">
                  <span><small>{c.axisConcept}</small>{activeVisual.concept}</span><span><small>{c.axisMovement}</small>{activeVisual.movement}</span><span><small>{c.axisPalette}</small>{activeVisual.palette}</span><span><small>FX</small>{activeVisual.effect}</span>
                </div>
              </div>
            </div>
            <div className="mk-formula" aria-label={c.formulaResult}>
              {[c.axisConcept, c.axisMovement, c.axisPalette, c.axisFx].map((item, index) => <span key={item}><i>{item}</i>{index < 3 && <b>+</b>}</span>)}<strong>=</strong><em>{c.formulaResult}</em>
            </div>
          </div>
        </section>

        <section className="mk-section mk-container">
          <SectionHeading eyebrow={c.galleryEyebrow} title={c.galleryTitle} body={c.galleryBody} align="center" />
          <div className="mk-gallery">
            {c.directions.map((direction, index) => (
              <button key={direction.name} type="button" className={activeDirection === index ? "is-active" : ""} onClick={() => setActiveDirection(index)} aria-pressed={activeDirection === index}>
                <VideoFrame src={DIRECTION_MEDIA[index].video} poster={DIRECTION_MEDIA[index].poster} />
                <span className="mk-gallery__content"><small>0{index + 1}</small><strong>{direction.name}</strong><span>{direction.concept} · {direction.movement} · {direction.palette}</span></span>
              </button>
            ))}
          </div>
          <div className="mk-centered-action"><button type="button" onClick={isLoggedIn ? onStart : onLogin} className="mk-text-link">{c.variantCta}<ArrowIcon /></button></div>
        </section>

        <section className="mk-section mk-container">
          <div className="mk-scenes">
            <div className="mk-scenes__visual">
              <VideoFrame src="/escenas_demo.mp4" poster="/samples/sample-forest.png" label={c.scenesEyebrow} />
              <div className="mk-scenes__rail"><span className="one">{c.sceneVerse}</span><span className="two">{c.sceneChorus}</span><span className="three">{c.sceneBridge}</span><span className="four">{c.sceneChorus}</span></div>
            </div>
            <div className="mk-scenes__copy">
              <SectionHeading eyebrow={c.scenesEyebrow} title={c.scenesTitle} body={c.scenesBody} />
              <ul>{c.scenesPoints.map((point) => <li key={point}><CheckIcon />{point}</li>)}</ul>
              <p className="mk-scenes__credit">{c.scenesCredit}</p>
            </div>
          </div>
        </section>

        <section id="workflow" className="mk-section mk-section--grid">
          <div className="mk-container">
            <SectionHeading eyebrow={c.workflowEyebrow} title={c.workflowTitle} align="center" />
            <div className="mk-workflow">{c.workflow.map((step) => <article key={step.n}><span>{step.n}</span><h3>{step.title}</h3><p>{step.body}</p></article>)}</div>
          </div>
        </section>

        <section className="mk-section mk-container">
          <div className="mk-editor">
            <div className="mk-editor__copy">
              <SectionHeading eyebrow={c.controlEyebrow} title={c.controlTitle} body={c.controlBody} />
              <div className="mk-editor__tags">{c.controlTags.map((tag) => <span key={tag}><CheckIcon />{tag}</span>)}</div>
            </div>
            <div className="mk-editor-ui" aria-label={c.editorTitle}>
              <div className="mk-editor-ui__bar"><strong>{c.editorTitle}</strong><span><i />{c.editorSynced}</span></div>
              <div className="mk-editor-ui__body">
                <div className="mk-editor-ui__preview"><VideoFrame src="/samples/ex2.mp4" poster="/samples/sample-forest.png" /><span>{c.editorLineB}</span></div>
                <div className="mk-editor-ui__lines">{[c.editorLineA, c.editorLineB, c.editorLineC].map((line, index) => <div key={line} className={index === 1 ? "is-active" : ""}><span>0{index + 1}</span><p>{line}</p><small>00:{12 + index * 4}.0</small></div>)}</div>
                <div className="mk-editor-ui__wave">{Array.from({ length: 70 }, (_, index) => <i key={index} style={{ height: `${14 + ((index * 23) % 72)}%` }} />)}<b /></div>
              </div>
            </div>
          </div>
        </section>

        <section className="mk-section mk-section--outputs">
          <div className="mk-container">
            <SectionHeading eyebrow={c.outputsEyebrow} title={c.outputsTitle} body={c.outputsBody} align="center" />
            <div className="mk-outputs">
              {c.outputs.map((output, index) => <article key={output.title} className={`mk-output mk-output--${index + 1}`}><div className="mk-output__visual"><span>{index === 1 ? "9:16" : "16:9"}</span><i /></div><small>0{index + 1}</small><h3>{output.title}</h3><strong>{output.meta}</strong><p>{output.body}</p></article>)}
            </div>
            <p className="mk-output-note"><SparkIcon />{c.proNote}</p>
          </div>
        </section>

        <section className="mk-section mk-container">
          <SectionHeading eyebrow={c.capabilitiesEyebrow} title={c.capabilitiesTitle} align="center" />
          <div className="mk-capabilities">{c.capabilities.map((item, index) => <article key={item.title}><span>0{index + 1}</span><h3>{item.title}</h3><p>{item.body}</p></article>)}</div>
        </section>

        <section className="mk-section mk-container">
          <div className="mk-definition">
            <div><p className="mk-eyebrow"><span /><span>{c.definitionEyebrow}</span></p><h2>{c.definitionTitle}</h2></div>
            <div><p>{c.definitionBody}</p><p>{c.definitionFor}</p></div>
          </div>
        </section>

        <section id="faq" className="mk-section mk-container">
          <SectionHeading eyebrow={c.faqEyebrow} title={c.faqTitle} align="center" />
          <div className="mk-faq">{c.faqs.map((faq, index) => <details key={faq.q}><summary><span>0{index + 1}</span><strong>{faq.q}</strong><i aria-hidden="true" /></summary><p>{faq.a}</p></details>)}</div>
        </section>

        <section id="contact" className="mk-section mk-container">
          <div className="mk-contact">
            <div className="mk-contact__copy"><p className="mk-eyebrow"><span /><span>{c.contactEyebrow}</span></p><h2>{c.contactTitle}</h2><p>{c.contactBody}</p><strong><SparkIcon />{c.contactMagnet}</strong></div>
            <form onSubmit={handleSalesSubmit} className="mk-contact__form">
              <div className="mk-form-row"><label><span>{c.formName}</span><input name="name" required autoComplete="name" /></label><label><span>{c.formCompany}</span><input name="company" autoComplete="organization" /></label></div>
              <label><span>{c.formEmail}</span><input name="email" type="email" required autoComplete="email" /></label>
              <label><span>{c.formVolume}</span><select name="volume" defaultValue={c.volumeOptions[0]}>{c.volumeOptions.map((option) => <option key={option}>{option}</option>)}</select></label>
              <label><span>{c.formMessage}</span><textarea name="message" rows="3" /></label>
              <button type="submit" className="mk-button" disabled={formState === "loading" || formState === "sent"}>{formState === "loading" ? c.formSending : c.formSubmit}<ArrowIcon /></button>
              <div className="mk-form-status" aria-live="polite">{formState === "sent" && <p className="is-success">{c.formSent}</p>}{formState === "error" && <p className="is-error">{c.formError}</p>}</div>
            </form>
          </div>
        </section>

        <section className="mk-final mk-container"><div><p className="mk-pill"><SparkIcon />{c.finalEyebrow}</p><h2>{c.finalTitle}</h2><p>{c.finalBody}</p><button type="button" onClick={isLoggedIn ? onStart : onLogin} className="mk-button">{c.finalCta}<ArrowIcon /></button></div></section>
      </main>

      <footer className="mk-footer"><div className="mk-container mk-footer__inner"><div><BrandLockup size="md" /><p>{c.footerDescription}</p></div><div><strong>{c.footerProduct}</strong><a href="#engine">{c.footerEngine}</a><a href="#styles">{c.footerStyles}</a><a href="#workflow">{c.footerWorkflow}</a></div><div><strong>{c.footerCompany}</strong><a href="#faq">FAQ</a><a href="#contact">Demo</a><button type="button" onClick={onLogin}>{c.login}</button></div></div><div className="mk-container mk-footer__bottom"><span>{c.footerRights}</span><span>© {new Date().getFullYear()}</span></div></footer>
    </div>
  );
}
