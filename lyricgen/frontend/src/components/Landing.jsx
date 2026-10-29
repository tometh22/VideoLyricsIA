import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import BrandLockup from "./BrandLockup";
import ServiceStatusBanner from "./ServiceStatusBanner";
import "./Landing.css";

const API = import.meta.env.VITE_API_URL || "";
const SALES_EMAIL = "tomas@genly.pro";

// Lyric videos hechos con Genly. Las imágenes son fotogramas reales (con la
// letra en pantalla). No se enlaza ni se nombra dónde están publicados.
const FEATURED = { id: "enanitos-verdes-salpicandonos", artist: "Enanitos Verdes", title: "Salpicandonos", duration: "5:12" };
const WORK = [
  [
    { id: "illya-kuryaki-jugo", artist: "Illya Kuryaki and the Valderramas", title: "Jugo" },
    { id: "enanitos-verdes-salpicandonos", artist: "Enanitos Verdes", title: "Salpicandonos" },
    { id: "abuelos-de-la-nada-mil-horas", artist: "Los Abuelos de la Nada", title: "Mil Horas" },
    { id: "luciano-pereyra-princesa", artist: "Luciano Pereyra", title: "Princesa" },
    { id: "coti-te-quise-tanto", artist: "Coti", title: "Te Quise Tanto" },
    { id: "rodrigo-la-chica-del-ascensor", artist: "Rodrigo", title: "La Chica Del Ascensor" },
  ],
  [
    { id: "mercedes-sosa-dejame-que-me-vaya", artist: "Mercedes Sosa", title: "Déjame Que Me Vaya" },
    { id: "viejas-locas-que-vas-a-hacer", artist: "Viejas Locas", title: "¿Qué Vas A Hacer Tan Sola Hoy?" },
    { id: "seru-giran-llorando-en-el-espejo", artist: "Serú Girán", title: "Llorando En El Espejo" },
    { id: "axel-ella", artist: "Axel", title: "Ella" },
    { id: "charly-garcia-tango", artist: "Charly García", title: "Tango" },
    { id: "diego-torres-el-mundo-sigue-igual", artist: "Diego Torres", title: "El Mundo Sigue Igual" },
  ],
];
const LOOK_KEYS = ["cosmico", "cine", "pincel", "pop", "pelicula"];

const COPY = {
  es: {
    metaTitle: "Genly — Hacé el lyric video de tu canción",
    metaDescription: "Subís el audio y Genly transcribe la letra, la sincroniza y te propone una estética. Revisás todo antes de generar y te llevás el video, un vertical y la miniatura.",
    skip: "Saltar al contenido",
    navLabel: "Navegación principal",
    languageLabel: "Idioma",
    navWork: "Videos",
    navLooks: "Estilos",
    navWorkflow: "Cómo funciona",
    navArtists: "Para artistas",
    navLabels: "Para sellos",
    navFaq: "Preguntas",
    login: "Ingresar",
    dashboard: "Ir al workspace",
    navCta: "Crear lyric video",
    menuOpen: "Abrir navegación",
    menuClose: "Cerrar navegación",
    heroEyebrow: "Para artistas independientes y sellos",
    heroTitle: "Hacé el lyric video de tu canción.",
    heroBody: "Subís el audio y Genly transcribe la letra, la sincroniza y te propone una estética. Corregís lo que haga falta y recién ahí generás. Te llevás el video, un vertical para redes y la miniatura. Y si tenés un sello, te los hacemos nosotros.",
    primaryCta: "Crear mi lyric video",
    secondaryCta: "Tengo un sello",
    heroNote: "MP3 o WAV, hasta cinco canciones por vez",
    pathsEyebrow: "Para quién",
    pathsTitle: "Dos formas de trabajar con Genly",
    paths: [
      { tag: "Artistas independientes", title: "Lo hacés vos, desde la app", body: "Creás tu cuenta, subís la canción y armás tu lyric video vos mismo. No hace falta saber de edición.", points: ["Video, vertical y miniatura de cada canción", "Corregís la letra antes de generar", "Hasta cinco canciones por vez"], cta: "Crear mi lyric video" },
      { tag: "Sellos y equipos", title: "Te lo hacemos nosotros", body: "Nuestro equipo produce los videos de tus artistas, revisa la letra palabra por palabra y te los entrega listos para subir.", points: ["Muestra gratis de 30 segundos, una por sello", "Una sola persona para todo el lote", "Precio según volumen"], cta: "Pedir una muestra gratis" },
    ],
    proof: ["Trabajamos con grandes sellos. Hicimos lyric videos de ", "Charly García, Mercedes Sosa, Enanitos Verdes", " y muchos artistas más."],
    featureTag: "Hecho con Genly",
    featureMeta: "Lyric video",
    workEyebrow: "Nuestro trabajo",
    workTitle: "Algunos lyric videos que hicimos.",
    workBody: "Para catálogos de rock nacional, folklore, pop y cuarteto. Cada canción con su estética y la letra revisada línea por línea.",
    pause: "Pausar",
    resume: "Reanudar",
    looksEyebrow: "Estilos de letra",
    looksTitle: "Una misma imagen con cinco estilos de letra.",
    looksBody: "La tipografía, el color y la forma en que entra cada palabra cambian mucho cómo se siente una canción. Tocá uno para comparar.",
    looksNote: "Capturas de nuestro reel de estilos.",
    looksGroup: "Estilos de letra",
    looks: { cosmico: "Cósmico", cine: "Cine", pincel: "Pincel", pop: "Pop 70s", pelicula: "Película" },
    lookAlt: "Estilo {name} sobre la misma escena",
    directionEyebrow: "Dirección visual",
    directionTitle: "El fondo también lo elegís vos.",
    directionBody: "Hay tres formas de arrancar y 15 conceptos visuales para combinar con movimiento, paleta y efectos. No son plantillas cerradas, así que cada combinación da una imagen distinta.",
    modes: [
      { title: "Auto", body: "Elegís lo principal y Genly arma una primera propuesta." },
      { title: "Inspirado en la letra", body: "Lo que dice la canción define qué aparece en pantalla." },
      { title: "Mi prompt", body: "Describís la escena y decidís si Genly la respeta tal cual o la completa." },
    ],
    scenesNote: "Si querés, la canción se puede dividir en escenas que mantienen la misma estética. Eso usa créditos extra.",
    workflowEyebrow: "Cómo funciona",
    workflowTitle: "De la canción al video, en cuatro pasos",
    workflow: [
      { title: "Subí la canción", body: "Uno o hasta cinco archivos MP3 o WAV en el mismo lote." },
      { title: "Revisá la letra", body: "Genly la transcribe y la sincroniza. Corregís texto y tiempos en el editor." },
      { title: "Elegí la estética", body: "Concepto, movimiento, paleta, efectos, tipografía y animación de la letra." },
      { title: "Generá y descargá", body: "Revisás el resultado y te llevás el video, el vertical y la miniatura." },
    ],
    outputsEyebrow: "Qué te llevás",
    outputsTitle: "Tres archivos por cada canción",
    outputs: [
      { title: "Lyric video", meta: "16:9 · MP4 Full HD", body: "La canción entera con la letra sincronizada. El video principal para YouTube." },
      { title: "Vertical", meta: "9:16 · MP4 · 30 s", body: "Un fragmento para Reels, Shorts o TikTok." },
      { title: "Miniatura", meta: "16:9 · JPG 1280×720", body: "La portada del video, con la misma estética." },
    ],
    proNote: "Las cuentas habilitadas también pueden descargar ProRes para entregas broadcast.",
    labelsEyebrow: "Para sellos",
    labelsTitle: "¿Tenés muchas canciones? Te las hacemos nosotros.",
    labelsBody: "Si manejás varios artistas o un catálogo, nuestro equipo produce los videos, revisa la letra palabra por palabra y te los entrega listos para subir.",
    labelsPoints: ["Empezás con una muestra gratis de 30 segundos, una por sello", "Hablás con una sola persona para todo el lote", "Si el error es nuestro, lo corregimos sin costo"],
    formName: "Tu nombre",
    formCompany: "Sello o empresa",
    formEmail: "Email",
    formVolume: "Canciones por mes",
    volumeOptions: ["1 a 5", "6 a 25", "Más de 25"],
    formMessage: "Contanos qué tienen en camino (opcional)",
    formSubmit: "Pedir una muestra gratis",
    formSending: "Enviando…",
    formSent: "Listo, nos llegó. Te escribimos por mail.",
    formError: "No pudimos enviarlo desde acá. Te abrimos el correo con el mensaje armado.",
    faqEyebrow: "Preguntas",
    faqTitle: "Lo que más nos preguntan",
    faqs: [
      { q: "¿Sirve si soy artista independiente?", a: "Sí. Creás tu cuenta, subís la canción y lo hacés vos desde la app. Si preferís que lo hagamos nosotros, escribinos." },
      { q: "¿Son plantillas de lyric video?", a: "No. El fondo sale de lo que elegís: concepto, movimiento, paleta y efectos. Si preferís, también podés subir un fondo propio." },
      { q: "¿Puedo corregir la letra y los tiempos?", a: "Sí. Antes de generar editás el texto, movés los tiempos en la línea de tiempo, dividís o unís líneas y lo ves sincronizado en el preview." },
      { q: "¿Qué puedo cambiar del estilo?", a: "El modo, el género, el concepto visual, el movimiento, la paleta y tus propios colores, los efectos, y la tipografía, el tamaño, el contraste y la animación de la letra." },
      { q: "¿Qué me llevo por cada canción?", a: "Un lyric video Full HD 16:9, un vertical de 30 segundos y una miniatura JPG. Algunas cuentas tienen además entregas ProRes." },
      { q: "¿Puedo hacer un video con varias escenas?", a: "Sí. El modo multiescena divide la canción en partes con una estética en común, y podés rehacer una escena sin tocar las demás. Usa créditos extra." },
      { q: "¿Puedo trabajar varias canciones juntas?", a: "Sí. Podés subir hasta cinco archivos MP3 o WAV en un mismo lote y ajustar cada canción por separado." },
    ],
    finalTitle: "Probalo con tu próxima canción.",
    finalBody: "Subís el audio, elegís el estilo y revisás todo antes de generar.",
    finalLabels: "¿Tenés un sello? Pedí una muestra gratis",
    finalCta: "Crear mi lyric video",
    footerLine: "Lyric videos para artistas y sellos.",
    footerStatus: "Estado del servicio",
    footerNote: "Las imágenes de la sección de videos son fotogramas de lyric videos hechos con Genly.",
  },
  en: {
    metaTitle: "Genly — Make the lyric video for your song",
    metaDescription: "Upload the audio and Genly transcribes the lyrics, syncs them and suggests a look. You review everything before generating and get the video, a vertical edit and the thumbnail.",
    skip: "Skip to content",
    navLabel: "Primary navigation",
    languageLabel: "Language",
    navWork: "Videos",
    navLooks: "Styles",
    navWorkflow: "How it works",
    navArtists: "For artists",
    navLabels: "For labels",
    navFaq: "Questions",
    login: "Sign in",
    dashboard: "Open workspace",
    navCta: "Create lyric video",
    menuOpen: "Open navigation",
    menuClose: "Close navigation",
    heroEyebrow: "For independent artists and labels",
    heroTitle: "Make the lyric video for your song.",
    heroBody: "Upload the audio and Genly transcribes the lyrics, syncs them and suggests a look. You fix whatever needs fixing and only then generate. You get the video, a vertical edit for social and the thumbnail. And if you run a label, we can make them for you.",
    primaryCta: "Create my lyric video",
    secondaryCta: "I run a label",
    heroNote: "MP3 or WAV, up to five songs at a time",
    pathsEyebrow: "Who it’s for",
    pathsTitle: "Two ways to work with Genly",
    paths: [
      { tag: "Independent artists", title: "You make it yourself, in the app", body: "Create your account, upload the song and put your lyric video together yourself. No editing skills needed.", points: ["Video, vertical edit and thumbnail for every song", "Fix the lyrics before generating", "Up to five songs at a time"], cta: "Create my lyric video" },
      { tag: "Labels and teams", title: "We make it for you", body: "Our team makes your artists’ videos, checks the lyrics word by word and delivers them ready to upload.", points: ["Free 30-second sample, one per label", "One person handles the whole batch", "Pricing based on volume"], cta: "Get a free sample" },
    ],
    proof: ["We work with major labels. We’ve made lyric videos for ", "Charly García, Mercedes Sosa, Enanitos Verdes", " and many more artists."],
    featureTag: "Made with Genly",
    featureMeta: "Lyric video",
    workEyebrow: "Our work",
    workTitle: "A few lyric videos we’ve made.",
    workBody: "For catalogs of Argentine rock, folk, pop and cuarteto. Each song with its own look and the lyrics checked line by line.",
    pause: "Pause",
    resume: "Resume",
    looksEyebrow: "Lyric styles",
    looksTitle: "One image with five lyric styles.",
    looksBody: "The typeface, the color and how each word comes in change how a song feels. Tap one to compare.",
    looksNote: "Stills from our styles reel.",
    looksGroup: "Lyric styles",
    looks: { cosmico: "Cosmic", cine: "Cinema", pincel: "Brush", pop: "70s Pop", pelicula: "Film" },
    lookAlt: "{name} style over the same scene",
    directionEyebrow: "Visual direction",
    directionTitle: "You pick the background too.",
    directionBody: "There are three ways to start and 15 visual concepts to combine with motion, palette and effects. They aren’t closed templates, so every combination gives you a different image.",
    modes: [
      { title: "Auto", body: "You choose the basics and Genly puts together a first proposal." },
      { title: "Inspired by the lyrics", body: "What the song says decides what shows up on screen." },
      { title: "My prompt", body: "You describe the scene and decide whether Genly follows it as is or fills it in." },
    ],
    scenesNote: "If you want, the song can be split into scenes that share the same look. That uses extra credits.",
    workflowEyebrow: "How it works",
    workflowTitle: "From song to video in four steps",
    workflow: [
      { title: "Upload the song", body: "One or up to five MP3 or WAV files in the same batch." },
      { title: "Check the lyrics", body: "Genly transcribes and syncs them. You fix text and timing in the editor." },
      { title: "Pick the look", body: "Concept, motion, palette, effects, typeface and lyric animation." },
      { title: "Generate and download", body: "You review the result and get the video, the vertical edit and the thumbnail." },
    ],
    outputsEyebrow: "What you get",
    outputsTitle: "Three files for every song",
    outputs: [
      { title: "Lyric video", meta: "16:9 · Full HD MP4", body: "The full song with synced lyrics. Your main video for YouTube." },
      { title: "Vertical", meta: "9:16 · MP4 · 30 s", body: "An excerpt for Reels, Shorts or TikTok." },
      { title: "Thumbnail", meta: "16:9 · JPG 1280×720", body: "The video’s cover image, in the same style." },
    ],
    proNote: "Enabled accounts can also download ProRes for broadcast delivery.",
    labelsEyebrow: "For labels",
    labelsTitle: "Got a lot of songs? We’ll make them for you.",
    labelsBody: "If you work with several artists or a catalog, our team makes the videos, checks the lyrics word by word and delivers them ready to upload.",
    labelsPoints: ["Start with a free 30-second sample, one per label", "One person handles the whole batch", "If the mistake is ours, we fix it at no cost"],
    formName: "Your name",
    formCompany: "Label or company",
    formEmail: "Email",
    formVolume: "Songs per month",
    volumeOptions: ["1 to 5", "6 to 25", "More than 25"],
    formMessage: "Tell us what you have coming up (optional)",
    formSubmit: "Get a free sample",
    formSending: "Sending…",
    formSent: "Got it. We’ll email you.",
    formError: "We couldn’t send it from here. We opened your email with the message ready.",
    faqEyebrow: "Questions",
    faqTitle: "What people usually ask us",
    faqs: [
      { q: "Does it work for independent artists?", a: "Yes. Create your account, upload the song and make it yourself in the app. If you’d rather we make it, write to us." },
      { q: "Are these lyric video templates?", a: "No. The background comes from what you pick: concept, motion, palette and effects. You can also upload your own background." },
      { q: "Can I fix the lyrics and timing?", a: "Yes. Before generating you edit the text, move timing on the timeline, split or merge lines and watch it synced in the preview." },
      { q: "What can I change about the style?", a: "The mode, genre, visual concept, motion, palette and your own colors, effects, and the lyrics’ typeface, size, contrast and animation." },
      { q: "What do I get for each song?", a: "A Full HD 16:9 lyric video, a 30-second vertical edit and a JPG thumbnail. Some accounts also have ProRes delivery." },
      { q: "Can I make a video with several scenes?", a: "Yes. Multi-scene splits the song into parts that share a look, and you can redo one scene without touching the rest. It uses extra credits." },
      { q: "Can I work on several songs at once?", a: "Yes. You can upload up to five MP3 or WAV files in one batch and adjust each song separately." },
    ],
    finalTitle: "Try it with your next song.",
    finalBody: "Upload the audio, pick a style and review everything before generating.",
    finalLabels: "Run a label? Get a free sample",
    finalCta: "Create my lyric video",
    footerLine: "Lyric videos for artists and labels.",
    footerStatus: "Service status",
    footerNote: "Images in the videos section are stills from lyric videos made with Genly.",
  },
  pt: {
    metaTitle: "Genly — Faça o lyric video da sua música",
    metaDescription: "Você envia o áudio e o Genly transcreve a letra, sincroniza e sugere uma estética. Você revisa tudo antes de gerar e leva o vídeo, um vertical e a thumbnail.",
    skip: "Pular para o conteúdo",
    navLabel: "Navegação principal",
    languageLabel: "Idioma",
    navWork: "Vídeos",
    navLooks: "Estilos",
    navWorkflow: "Como funciona",
    navArtists: "Para artistas",
    navLabels: "Para gravadoras",
    navFaq: "Perguntas",
    login: "Entrar",
    dashboard: "Ir ao workspace",
    navCta: "Criar lyric video",
    menuOpen: "Abrir navegação",
    menuClose: "Fechar navegação",
    heroEyebrow: "Para artistas independentes e gravadoras",
    heroTitle: "Faça o lyric video da sua música.",
    heroBody: "Você envia o áudio e o Genly transcreve a letra, sincroniza e sugere uma estética. Você corrige o que precisar e só então gera. Leva o vídeo, um vertical para redes e a thumbnail. E se você tem uma gravadora, a gente faz para você.",
    primaryCta: "Criar meu lyric video",
    secondaryCta: "Tenho uma gravadora",
    heroNote: "MP3 ou WAV, até cinco músicas por vez",
    pathsEyebrow: "Para quem",
    pathsTitle: "Duas formas de trabalhar com a Genly",
    paths: [
      { tag: "Artistas independentes", title: "Você mesmo faz, pelo app", body: "Você cria sua conta, envia a música e monta seu lyric video sozinho. Não precisa saber editar.", points: ["Vídeo, vertical e thumbnail de cada música", "Você corrige a letra antes de gerar", "Até cinco músicas por vez"], cta: "Criar meu lyric video" },
      { tag: "Gravadoras e equipes", title: "A gente faz para você", body: "Nossa equipe produz os vídeos dos seus artistas, revisa a letra palavra por palavra e entrega tudo pronto para subir.", points: ["Amostra grátis de 30 segundos, uma por gravadora", "Uma só pessoa cuida do lote inteiro", "Preço conforme o volume"], cta: "Pedir uma amostra grátis" },
    ],
    proof: ["Trabalhamos com grandes gravadoras. Fizemos lyric videos de ", "Charly García, Mercedes Sosa, Enanitos Verdes", " e muitos outros artistas."],
    featureTag: "Feito com Genly",
    featureMeta: "Lyric video",
    workEyebrow: "Nosso trabalho",
    workTitle: "Alguns lyric videos que fizemos.",
    workBody: "Para catálogos de rock argentino, folclore, pop e cuarteto. Cada música com sua estética e a letra revisada linha por linha.",
    pause: "Pausar",
    resume: "Retomar",
    looksEyebrow: "Estilos de letra",
    looksTitle: "Uma mesma imagem com cinco estilos de letra.",
    looksBody: "A fonte, a cor e o jeito como cada palavra entra mudam muito como uma música é sentida. Toque em um para comparar.",
    looksNote: "Capturas do nosso reel de estilos.",
    looksGroup: "Estilos de letra",
    looks: { cosmico: "Cósmico", cine: "Cinema", pincel: "Pincel", pop: "Pop 70s", pelicula: "Filme" },
    lookAlt: "Estilo {name} sobre a mesma cena",
    directionEyebrow: "Direção visual",
    directionTitle: "O fundo você também escolhe.",
    directionBody: "São três jeitos de começar e 15 conceitos visuais para combinar com movimento, paleta e efeitos. Não são templates fechados, então cada combinação gera uma imagem diferente.",
    modes: [
      { title: "Auto", body: "Você escolhe o principal e o Genly monta uma primeira proposta." },
      { title: "Inspirado na letra", body: "O que a música diz define o que aparece na tela." },
      { title: "Meu prompt", body: "Você descreve a cena e decide se o Genly segue à risca ou completa." },
    ],
    scenesNote: "Se quiser, a música pode ser dividida em cenas com a mesma estética. Isso usa créditos extras.",
    workflowEyebrow: "Como funciona",
    workflowTitle: "Da música ao vídeo, em quatro passos",
    workflow: [
      { title: "Envie a música", body: "Um ou até cinco arquivos MP3 ou WAV no mesmo lote." },
      { title: "Revise a letra", body: "O Genly transcreve e sincroniza. Você corrige texto e tempos no editor." },
      { title: "Escolha a estética", body: "Conceito, movimento, paleta, efeitos, fonte e animação da letra." },
      { title: "Gere e baixe", body: "Você revisa o resultado e leva o vídeo, o vertical e a thumbnail." },
    ],
    outputsEyebrow: "O que você leva",
    outputsTitle: "Três arquivos por música",
    outputs: [
      { title: "Lyric video", meta: "16:9 · MP4 Full HD", body: "A música inteira com a letra sincronizada. O vídeo principal para o YouTube." },
      { title: "Vertical", meta: "9:16 · MP4 · 30 s", body: "Um trecho para Reels, Shorts ou TikTok." },
      { title: "Thumbnail", meta: "16:9 · JPG 1280×720", body: "A capa do vídeo, com a mesma estética." },
    ],
    proNote: "Contas habilitadas também podem baixar ProRes para entregas broadcast.",
    labelsEyebrow: "Para gravadoras",
    labelsTitle: "Tem muitas músicas? A gente faz para você.",
    labelsBody: "Se você cuida de vários artistas ou de um catálogo, nossa equipe produz os vídeos, revisa a letra palavra por palavra e entrega tudo pronto para subir.",
    labelsPoints: ["Você começa com uma amostra grátis de 30 segundos, uma por gravadora", "Uma só pessoa cuida do lote inteiro", "Se o erro for nosso, corrigimos sem custo"],
    formName: "Seu nome",
    formCompany: "Gravadora ou empresa",
    formEmail: "Email",
    formVolume: "Músicas por mês",
    volumeOptions: ["1 a 5", "6 a 25", "Mais de 25"],
    formMessage: "Conte o que vocês têm pela frente (opcional)",
    formSubmit: "Pedir uma amostra grátis",
    formSending: "Enviando…",
    formSent: "Pronto, recebemos. Vamos te escrever por email.",
    formError: "Não conseguimos enviar daqui. Abrimos seu email com a mensagem pronta.",
    faqEyebrow: "Perguntas",
    faqTitle: "O que mais nos perguntam",
    faqs: [
      { q: "Serve para artista independente?", a: "Sim. Você cria sua conta, envia a música e faz pelo app. Se preferir que a gente faça, fale com a gente." },
      { q: "São templates de lyric video?", a: "Não. O fundo sai do que você escolhe: conceito, movimento, paleta e efeitos. Você também pode enviar um fundo próprio." },
      { q: "Posso corrigir a letra e os tempos?", a: "Sim. Antes de gerar você edita o texto, ajusta os tempos na timeline, divide ou junta linhas e vê tudo sincronizado no preview." },
      { q: "O que posso mudar no estilo?", a: "O modo, o gênero, o conceito visual, o movimento, a paleta e suas próprias cores, os efeitos, e a fonte, o tamanho, o contraste e a animação da letra." },
      { q: "O que eu levo por música?", a: "Um lyric video Full HD 16:9, um vertical de 30 segundos e uma thumbnail JPG. Algumas contas também têm entregas ProRes." },
      { q: "Posso fazer um vídeo com várias cenas?", a: "Sim. O modo multicena divide a música em partes com uma estética em comum, e você pode refazer uma cena sem mexer nas outras. Usa créditos extras." },
      { q: "Posso trabalhar várias músicas juntas?", a: "Sim. Você pode enviar até cinco arquivos MP3 ou WAV no mesmo lote e ajustar cada música separadamente." },
    ],
    finalTitle: "Teste com a sua próxima música.",
    finalBody: "Envie o áudio, escolha o estilo e revise tudo antes de gerar.",
    finalLabels: "Tem uma gravadora? Peça uma amostra grátis",
    finalCta: "Criar meu lyric video",
    footerLine: "Lyric videos para artistas e gravadoras.",
    footerStatus: "Status do serviço",
    footerNote: "As imagens da seção de vídeos são frames de lyric videos feitos com Genly.",
  },
};

function ArrowIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6" /></svg>;
}

function CheckIcon() {
  return <svg aria-hidden="true" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6" /></svg>;
}

function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(() => typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const query = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!query) return undefined;
    const onChange = () => setReduced(query.matches);
    query.addEventListener?.("change", onChange);
    return () => query.removeEventListener?.("change", onChange);
  }, []);
  return reduced;
}

function formatTime(seconds) {
  const whole = Math.floor(seconds);
  const cents = Math.floor((seconds - whole) * 100);
  return `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}.${String(cents).padStart(2, "0")}`;
}

// El título se ilumina palabra por palabra, como una línea de letra sincronizada.
function SyncedTitle({ text, reducedMotion }) {
  const words = text.split(/\s+/);
  const total = (words.length - 1) * 0.34 + 0.8;
  const [elapsed, setElapsed] = useState(reducedMotion ? total : 0);

  useEffect(() => {
    if (reducedMotion) {
      setElapsed(total);
      return undefined;
    }
    let frame;
    const start = performance.now();
    const tick = (now) => {
      const next = Math.min((now - start) / 1000, total);
      setElapsed(next);
      if (next < total) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [text, total, reducedMotion]);

  return (
    <>
      <p className="hm-timecode" aria-hidden="true">
        <span>{formatTime(elapsed)}</span>
        <span className="hm-timecode__bar"><i style={{ transform: `scaleX(${elapsed / total})` }} /></span>
        <span className="hm-timecode__label">sync</span>
      </p>
      <h1 key={text} className={reducedMotion ? "" : "is-syncing"} aria-label={text}>
        {words.map((word, index) => (
          <span key={`${word}-${index}`} aria-hidden="true" className={`hm-word${index >= words.length - 2 ? " is-accent" : ""}`} style={{ "--i": index }}>
            {word}{index < words.length - 1 ? " " : ""}
          </span>
        ))}
      </h1>
    </>
  );
}

function WorkCard({ item, hidden = false }) {
  return (
    <li aria-hidden={hidden || undefined}>
      <figure className="hm-reel__item">
        <img src={`/landing/work/${item.id}-800.jpg`} alt="" width="800" height="450" loading="lazy" decoding="async" />
        <figcaption className="hm-reel__meta"><b>{item.artist}</b><span>{item.title}</span></figcaption>
      </figure>
    </li>
  );
}

export default function Landing({ onStart, onLogin, isLoggedIn = false }) {
  const { lang, setLang } = useI18n();
  const c = COPY[lang] || COPY.es;
  const reducedMotion = usePrefersReducedMotion();
  const [menuOpen, setMenuOpen] = useState(false);
  const [activeMode, setActiveMode] = useState(1);
  const [activeLook, setActiveLook] = useState(LOOK_KEYS[0]);
  const [reelPaused, setReelPaused] = useState(false);
  const [formState, setFormState] = useState("idle");
  const mobileNavRef = useRef(null);
  const mobileMenuRef = useRef(null);
  const mobileTriggerRef = useRef(null);
  const start = isLoggedIn ? onStart : onLogin;
  const startLabel = isLoggedIn ? c.dashboard : c.primaryCta;

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
      const subject = `Genly — Muestra gratis${payload.company ? ` (${payload.company})` : ""}`;
      const body = [`Nombre: ${payload.name}`, `Empresa: ${payload.company}`, `Email: ${payload.email}`, `Canciones por mes: ${payload.volume}`, "", payload.message].join("\n");
      window.location.href = `mailto:${SALES_EMAIL}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    }
  };

  const navLinks = [["#para-quien", c.navArtists], ["#sellos", c.navLabels], ["#videos", c.navWork], ["#estilos", c.navLooks], ["#como-funciona", c.navWorkflow], ["#preguntas", c.navFaq]];
  const languageButtons = ["es", "en", "pt"].map((code) => (
    <button key={code} type="button" onClick={() => setLang(code)} aria-pressed={lang === code}>{code}</button>
  ));

  return (
    <div className="hm-site">
      <a className="hm-skip" href="#main-content">{c.skip}</a>

      <header ref={mobileNavRef} className="hm-nav">
        <div className="hm-shell hm-nav__inner">
          <a href="#top" className="hm-nav__brand" aria-label="Genly"><BrandLockup size="md" priority /></a>
          <nav className="hm-nav__links" aria-label={c.navLabel}>
            {navLinks.map(([href, label]) => <a key={href} href={href}>{label}</a>)}
          </nav>
          <div className="hm-nav__actions">
            <div className="hm-language" role="group" aria-label={c.languageLabel}>{languageButtons}</div>
            {!isLoggedIn && <button type="button" onClick={onLogin} className="hm-login">{c.login}</button>}
            <button type="button" onClick={start} className="hm-button hm-button--light hm-button--compact">{isLoggedIn ? c.dashboard : c.navCta}</button>
          </div>
          <button ref={mobileTriggerRef} type="button" className="hm-menu-trigger" onClick={() => setMenuOpen((open) => !open)} aria-expanded={menuOpen} aria-controls="hm-mobile-menu" aria-label={menuOpen ? c.menuClose : c.menuOpen}>
            <span /><span />
          </button>
        </div>
        {menuOpen && (
          <div ref={mobileMenuRef} id="hm-mobile-menu" className="hm-mobile-menu">
            {navLinks.map(([href, label]) => <a key={href} href={href} onClick={() => setMenuOpen(false)}>{label}</a>)}
            <div className="hm-language hm-language--mobile" role="group" aria-label={c.languageLabel}>{languageButtons}</div>
            {!isLoggedIn && <button type="button" onClick={onLogin} className="hm-button hm-button--ghost">{c.login}</button>}
            <button type="button" onClick={start} className="hm-button hm-button--light">{isLoggedIn ? c.dashboard : c.navCta}<ArrowIcon /></button>
          </div>
        )}
      </header>

      {/* Incidente de plataforma en la home pública: quien entra durante un
          outage lo ve antes de intentar registrarse. */}
      <ServiceStatusBanner variant="landing" />

      <main id="main-content">
        <section id="top" className="hm-hero">
          <div className="hm-shell hm-hero__grid">
            <div className="hm-hero__copy">
              <p className="hm-eyebrow"><span className="hm-live" aria-hidden="true" />{c.heroEyebrow}</p>
              <SyncedTitle text={c.heroTitle} reducedMotion={reducedMotion} />
              <p className="hm-lead">{c.heroBody}</p>
              <div className="hm-hero__actions">
                <button type="button" onClick={start} className="hm-button hm-button--light">{startLabel}<ArrowIcon /></button>
                <a href="#sellos" className="hm-button hm-button--ghost">{c.secondaryCta}</a>
              </div>
              <p className="hm-hero__note"><CheckIcon />{c.heroNote}</p>
              <p className="hm-proof">
                <span className="hm-proof__stack" aria-hidden="true">
                  {WORK[0].slice(0, 3).map((item) => <img key={item.id} src={`/landing/work/${item.id}-800.jpg`} alt="" width="46" height="46" />)}
                </span>
                <span>{c.proof[0]}<b>{c.proof[1]}</b>{c.proof[2]}</span>
              </p>
            </div>
            <figure className="hm-feature">
              <div className="hm-feature__media">
                <img src={`/landing/work/${FEATURED.id}.jpg`} alt={`${FEATURED.artist} — ${FEATURED.title}`} width="1280" height="720" fetchpriority="high" />
              </div>
              <figcaption>
                <span className="hm-feature__tag"><span className="hm-live" aria-hidden="true" />{c.featureTag}</span>
                <span className="hm-feature__title"><strong>{FEATURED.artist}</strong> {FEATURED.title}</span>
                <span className="hm-feature__meta">{c.featureMeta} · {FEATURED.duration}</span>
              </figcaption>
            </figure>
          </div>
        </section>

        <section id="para-quien" className="hm-section hm-shell hm-paths-section" aria-labelledby="hm-paths-title">
          <p className="hm-kicker"><span>01</span>{c.pathsEyebrow}</p>
          <h2 id="hm-paths-title">{c.pathsTitle}</h2>
          <div className="hm-paths">
            {c.paths.map((path, index) => (
              <article key={path.tag} className={`hm-path${index === 1 ? " hm-path--labels" : ""}`}>
                <p className="hm-path__tag">{path.tag}</p>
                <h3>{path.title}</h3>
                <p className="hm-path__body">{path.body}</p>
                <ul className="hm-checks">{path.points.map((point) => <li key={point}><CheckIcon />{point}</li>)}</ul>
                {index === 0
                  ? <button type="button" onClick={start} className="hm-button hm-button--light hm-button--wide">{isLoggedIn ? c.dashboard : path.cta}<ArrowIcon /></button>
                  : <a href="#sellos" className="hm-button hm-button--ghost hm-button--wide">{path.cta}<ArrowIcon /></a>}
              </article>
            ))}
          </div>
        </section>

        <section id="videos" className="hm-reel-section" aria-labelledby="hm-work-title">
          <div className="hm-shell hm-split-head">
            <div>
              <p className="hm-kicker"><span>02</span>{c.workEyebrow}</p>
              <h2 id="hm-work-title">{c.workTitle}</h2>
            </div>
            <div>
              <p className="hm-body">{c.workBody}</p>
              <div className="hm-reel__links">
                                {!reducedMotion && <button type="button" className="hm-chip-button" aria-pressed={reelPaused} onClick={() => setReelPaused((paused) => !paused)}>{reelPaused ? c.resume : c.pause}</button>}
              </div>
            </div>
          </div>
          <div className={`hm-reel${reducedMotion ? "" : " is-moving"}${reelPaused ? " is-paused" : ""}`}>
            {WORK.map((row, rowIndex) => (
              <div key={rowIndex} className={`hm-reel__row${rowIndex === 1 ? " is-reverse" : ""}`}>
                <ul className="hm-reel__track">
                  {row.map((item) => <WorkCard key={item.id} item={item} />)}
                  {!reducedMotion && row.map((item) => <WorkCard key={`${item.id}-copy`} item={item} hidden />)}
                </ul>
              </div>
            ))}
          </div>
        </section>

        <section id="estilos" className="hm-section hm-shell" aria-labelledby="hm-looks-title">
          <div className="hm-split-head">
            <div>
              <p className="hm-kicker"><span>03</span>{c.looksEyebrow}</p>
              <h2 id="hm-looks-title">{c.looksTitle}</h2>
            </div>
            <p className="hm-body">{c.looksBody}</p>
          </div>
          <figure className="hm-looks">
            <div className="hm-looks__frame">
              <img src={`/landing/looks/${activeLook}.jpg`} alt={c.lookAlt.replace("{name}", c.looks[activeLook])} width="1280" height="720" loading="lazy" decoding="async" />
              <span className="hm-looks__name" aria-hidden="true">{c.looks[activeLook]}</span>
            </div>
            <figcaption className="hm-looks__bar">
              <div className="hm-looks__chips" role="group" aria-label={c.looksGroup}>
                {LOOK_KEYS.map((key) => (
                  <button key={key} type="button" aria-pressed={activeLook === key} onClick={() => setActiveLook(key)}>
                    <img src={`/landing/looks/${key}-thumb.jpg`} alt="" width="96" height="54" decoding="async" />
                    <span>{c.looks[key]}</span>
                  </button>
                ))}
              </div>
              <span className="hm-looks__note">{c.looksNote}</span>
            </figcaption>
          </figure>
        </section>

        <section className="hm-section hm-shell" aria-labelledby="hm-direction-title">
          <div className="hm-direction">
            <div>
              <p className="hm-kicker"><span>04</span>{c.directionEyebrow}</p>
              <h2 id="hm-direction-title">{c.directionTitle}</h2>
              <p className="hm-body">{c.directionBody}</p>
              <p className="hm-small">{c.scenesNote}</p>
            </div>
            <div className="hm-modes" role="group" aria-label={c.directionEyebrow}>
              {c.modes.map((mode, index) => (
                <button key={mode.title} type="button" aria-pressed={activeMode === index} onClick={() => setActiveMode(index)}>
                  <span className="hm-modes__n">0{index + 1}</span>
                  <strong>{mode.title}</strong>
                  <span>{mode.body}</span>
                </button>
              ))}
            </div>
          </div>
        </section>

        <section id="como-funciona" className="hm-paper" aria-labelledby="hm-workflow-title">
          <div className="hm-shell">
            <p className="hm-kicker"><span>05</span>{c.workflowEyebrow}</p>
            <h2 id="hm-workflow-title">{c.workflowTitle}</h2>
            <ol className="hm-steps">
              {c.workflow.map((step, index) => <li key={step.title}><span>0{index + 1}</span><h3>{step.title}</h3><p>{step.body}</p></li>)}
            </ol>
            <div className="hm-outputs">
              <div>
                <p className="hm-kicker"><span>06</span>{c.outputsEyebrow}</p>
                <h2>{c.outputsTitle}</h2>
                <p className="hm-small">{c.proNote}</p>
              </div>
              <ul>
                {c.outputs.map((output, index) => (
                  <li key={output.title}>
                    <span className={`hm-ratio hm-ratio--${index + 1}`} aria-hidden="true" />
                    <div><h3>{output.title} <small>{output.meta}</small></h3><p>{output.body}</p></div>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </section>

        <section id="sellos" className="hm-labels" aria-labelledby="hm-labels-title">
          <div className="hm-shell hm-labels__grid">
            <div>
              <p className="hm-kicker"><span>07</span>{c.labelsEyebrow}</p>
              <h2 id="hm-labels-title">{c.labelsTitle}</h2>
              <p className="hm-body">{c.labelsBody}</p>
              <ul className="hm-checks">{c.labelsPoints.map((point) => <li key={point}><CheckIcon />{point}</li>)}</ul>
            </div>
            <form onSubmit={handleSalesSubmit} className="hm-form">
              <div className="hm-form__row">
                <label><span>{c.formName}</span><input name="name" required autoComplete="name" /></label>
                <label><span>{c.formCompany}</span><input name="company" autoComplete="organization" /></label>
              </div>
              <div className="hm-form__row">
                <label><span>{c.formEmail}</span><input name="email" type="email" required autoComplete="email" /></label>
                <label><span>{c.formVolume}</span><select name="volume" defaultValue={c.volumeOptions[0]}>{c.volumeOptions.map((option) => <option key={option}>{option}</option>)}</select></label>
              </div>
              <label><span>{c.formMessage}</span><textarea name="message" rows="3" /></label>
              <button type="submit" className="hm-button hm-button--light hm-button--wide" disabled={formState === "loading" || formState === "sent"}>{formState === "loading" ? c.formSending : c.formSubmit}<ArrowIcon /></button>
              <div className="hm-form__status" aria-live="polite">
                {formState === "sent" && <p className="is-success">{c.formSent}</p>}
                {formState === "error" && <p className="is-error">{c.formError}</p>}
              </div>
            </form>
          </div>
        </section>

        <section id="preguntas" className="hm-section hm-shell hm-faq" aria-labelledby="hm-faq-title">
          <div>
            <p className="hm-kicker"><span>08</span>{c.faqEyebrow}</p>
            <h2 id="hm-faq-title">{c.faqTitle}</h2>
          </div>
          <div className="hm-faq__list">
            {c.faqs.map((faq) => <details key={faq.q}><summary>{faq.q}</summary><p>{faq.a}</p></details>)}
          </div>
        </section>

        <section className="hm-final">
          <div className="hm-shell">
            <h2>{c.finalTitle}</h2>
            <p className="hm-body">{c.finalBody}</p>
            <button type="button" onClick={start} className="hm-button hm-button--light">{isLoggedIn ? c.dashboard : c.finalCta}<ArrowIcon /></button>
            <a href="#sellos" className="hm-text-link">{c.finalLabels} <span aria-hidden="true">→</span></a>
          </div>
        </section>
      </main>

      <footer className="hm-footer">
        <div className="hm-shell hm-footer__top">
          <BrandLockup size="md" />
          <p>{c.footerLine}</p>
          <div className="hm-footer__links">
            <a href="/status">{c.footerStatus}</a>
            <a href={`mailto:${SALES_EMAIL}`}>{SALES_EMAIL}</a>
            {!isLoggedIn && <button type="button" onClick={onLogin}>{c.login}</button>}
          </div>
        </div>
        <div className="hm-shell hm-footer__bottom">
          <span>© {new Date().getFullYear()} Genly</span>
          <span>{c.footerNote}</span>
        </div>
      </footer>
    </div>
  );
}
