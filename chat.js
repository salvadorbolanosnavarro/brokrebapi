/* ═══════════════════════════════════════════════════════════
   BROKR® — Chat flotante con IA
   ═══════════════════════════════════════════════════════════
   Este componente es omnipresente — flota sobre toda la app.
   
   ╔═══════════════════════════════════════════════════════╗
   ║  PARA CAMBIAR DE PROVEEDOR DE IA:                    ║
   ║  Solo modifica la sección "AI PROVIDER CONFIG" abajo ║
   ║  El resto del código no necesita cambios.            ║
   ╚═══════════════════════════════════════════════════════╝
   ═══════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════════
// AI PROVIDER CONFIG — Cambiar aquí para upgrade a Claude/GPT
// ══════════════════════════════════════════════════════════

const AI_CONFIG = {
  // ── PROVEEDOR ACTUAL: Llama 3.3 vía Groq (a través de tu backend Railway) ──
  endpoint: 'https://brokrebapi-production.up.railway.app/chat',
  model: 'llama-3.3-70b-versatile',
  maxTokens: 1500,
  temperature: 0.7,

  // ── PARA UPGRADE A CLAUDE (Anthropic): ──
  // endpoint: 'https://brokrebapi-production.up.railway.app/chat',  // Actualizar backend para llamar a Anthropic
  // model: 'claude-sonnet-4-20250514',
  // maxTokens: 1500,
  // temperature: 0.7,

  // ── PARA UPGRADE A GPT-4o (OpenAI): ──
  // endpoint: 'https://brokrebapi-production.up.railway.app/chat',  // Actualizar backend para llamar a OpenAI
  // model: 'gpt-4o',
  // maxTokens: 1500,
  // temperature: 0.7,

  // ── SYSTEM PROMPT — La personalidad de BROKR ──
  systemPrompt: `Eres BROKR®, el asistente inmobiliario con IA más avanzado de México. Operas por voz o texto. Eres proactivo, conversacional y ejecutas tareas completas.

═══ MÓDULOS DISPONIBLES ═══
Puedes navegar a cualquier módulo incluyendo al FINAL de tu respuesta:
[ACCION]{"tipo":"navegar","modulo":"NOMBRE"}[/ACCION]

Módulos: chat | isr | ficha-manual | ficha | contratos | avm | props

═══ CÓMO EJECUTAR TAREAS COMPLEJAS ═══

REGLA PRINCIPAL: Cuando el usuario pida una tarea, NO te limites a navegar al módulo. CONDUCE la tarea completa de forma conversacional, recopilando datos y confirmando.

── CONTRATOS ──
Si el usuario pide un contrato, navega al módulo Y empieza a recopilar datos en el mismo mensaje. Pregunta de 2-3 datos a la vez, no uno solo. Ejemplo:

Usuario: "haz un contrato de arrendamiento"
Tú: "Perfecto, voy al módulo de contratos. Dime: ¿nombre completo del arrendador y del arrendatario?"
[ACCION]{"tipo":"navegar","modulo":"contratos"}[/ACCION]

Datos necesarios para arrendamiento: arrendador, arrendatario, dirección del inmueble, renta mensual, depósito, fecha de inicio, duración (meses). Cuando tengas todos, di: "Tengo todo. Ingresa estos datos en el formulario del módulo: [lista]" o pide confirmación antes.

── FICHAS TÉCNICAS ──
Si mencionan un ID EB-XXXXXX: navega a fichas y diles que lo ingresen en el campo.
Si quieren ficha manual: navega a ficha-manual y guíalos para subir fotos y datos.

── CALCULADORA ISR ──
Si dan datos de venta (precio venta, precio compra, año, mejoras), calcula directo aquí en el chat sin necesidad de ir al módulo. Solo navega si quieren ver el desglose visual.

── OPINIÓN DE VALOR ──
Pide: colonia/zona, tipo de inmueble, m², recámaras. Da un rango estimado de mercado basado en tu conocimiento de Morelia.

═══ INSTRUCCIONES DE VOZ ═══
Cuando respondas por voz (respuestas cortas, sin bullet points, sin markdown). Máximo 3 oraciones. Luego el usuario puede pedir más detalle.

Conoces: LISR, Código Civil, contratos, SAT, INPC, mercado de Morelia, Michoacán. Español, preciso y profesional.`,

  // Función para construir el body del request
  // Cuando cambies de proveedor, solo ajusta esta función si el formato cambia
  buildRequestBody(messages) {
    return {
      model: this.model,
      max_tokens: this.maxTokens,
      temperature: this.temperature,
      messages: [
        { role: 'system', content: this.systemPrompt },
        ...messages
      ]
    };
  },

  // Función para extraer la respuesta del proveedor
  // Groq/OpenAI usan el mismo formato; Claude usa uno diferente
  extractReply(data) {
    // Formato Groq / OpenAI:
    return data.choices?.[0]?.message?.content || 'Sin respuesta.';

    // Formato Claude (Anthropic) — descomentar cuando hagas upgrade:
    // return data.content?.[0]?.text || 'Sin respuesta.';
  }
};

// ══════════════════════════════════════════════════════════
// CHAT STATE
// ══════════════════════════════════════════════════════════
let _chatMsgs = [];
let _chatBusy = false;
let _isListening = false;
let _recognition = null;

// Avatar del bot
const BOT_AVATAR = 'B®';

// ══════════════════════════════════════════════════════════
// SEND MESSAGE
// ══════════════════════════════════════════════════════════
async function sendChatMessage() {
  if (_chatBusy) return;
  const inp = g('txtin');
  const txt = inp.value.trim();
  if (!txt) return;

  // Ocultar welcome
  const wlc = g('welcome');
  if (wlc) wlc.remove();

  // Mostrar mensaje del usuario
  addChatMsg('u', txt);
  _chatMsgs.push({ role: 'user', content: txt });

  // Limpiar input
  inp.value = '';
  inp.style.height = 'auto';

  // Mostrar typing
  const tid = addTypingIndicator();
  _chatBusy = true;
  const sndBtn = g('snd');
  if (sndBtn) sndBtn.disabled = true;

  try {
    const r = await fetch(AI_CONFIG.endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(AI_CONFIG.buildRequestBody(_chatMsgs))
    });

    removeTypingIndicator(tid);

    if (!r.ok) {
      const e = await r.json();
      addChatError(e.error?.message || 'Error de conexión');
      _chatMsgs.pop();
      return;
    }

    const data = await r.json();
    let reply = AI_CONFIG.extractReply(data);

    // Ejecutar acciones [ACCION]...[/ACCION]
    const accionRe = /\[ACCION\](.*?)\[\/ACCION\]/gs;
    let match;
    while ((match = accionRe.exec(reply)) !== null) {
      try {
        const ac = JSON.parse(match[1].trim());
        if (ac.tipo === 'navegar' && ac.modulo) {
          if (typeof setPanel === 'function') setPanel(ac.modulo);
        }
      } catch (e) { /* ignora JSON inválido */ }
    }

    // Limpiar texto de acciones
    const clean = reply.replace(/\[ACCION\].*?\[\/ACCION\]/gs, '').trim();

    _chatMsgs.push({ role: 'assistant', content: clean });
    addChatMsg('a', clean);

    // Leer en voz alta si fue input de voz
    if (window._lastInputWasVoice || window._voiceConversationActive) {
      speakText(clean);
      window._lastInputWasVoice = false;
    }

  } catch (e) {
    removeTypingIndicator(tid);
    addChatError('No se pudo conectar con el servidor.');
    _chatMsgs.pop();
  } finally {
    _chatBusy = false;
    if (sndBtn) sndBtn.disabled = false;
    inp.focus();
  }
}

// ══════════════════════════════════════════════════════════
// UI HELPERS
// ══════════════════════════════════════════════════════════
function addChatMsg(role, content) {
  const area = g('chat');
  if (!area) return;
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  const avatar = role === 'a' ? BOT_AVATAR : '👤';
  d.innerHTML = `<div class="av">${avatar}</div><div class="bbl">${formatChatMsg(content)}</div>`;
  area.appendChild(d);
  area.scrollTop = area.scrollHeight;
}

function addTypingIndicator() {
  const area = g('chat');
  if (!area) return null;
  const id = 'typing-' + Date.now();
  const d = document.createElement('div');
  d.className = 'msg a';
  d.id = id;
  d.innerHTML = `<div class="av">${BOT_AVATAR}</div><div class="bbl"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></div>`;
  area.appendChild(d);
  area.scrollTop = area.scrollHeight;
  return id;
}

function removeTypingIndicator(id) {
  if (!id) return;
  const el = document.getElementById(id);
  if (el) el.remove();
}

function addChatError(msg) {
  const area = g('chat');
  if (!area) return;
  const d = document.createElement('div');
  d.className = 'msg a';
  d.innerHTML = `<div class="av" style="background:var(--danger-bg);color:var(--danger);font-weight:700">!</div><div class="bbl" style="border-color:var(--danger-bd);color:var(--danger)">Error: ${msg}</div>`;
  area.appendChild(d);
  area.scrollTop = area.scrollHeight;
}

function formatChatMsg(t) {
  return t
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/^#{1,3} (.+)$/gm, '<strong style="font-size:14.5px;display:block;margin-top:5px">$1</strong>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/^(\d+)\. (.+)$/gm, '<li>$2</li>')
    .replace(/(<li>[\s\S]*?<\/li>\n?)+/g, m => `<ul style="padding-left:16px;margin:5px 0">${m}</ul>`)
    .replace(/\n\n/g, '</p><p>')
    .replace(/\n/g, '<br/>')
    .replace(/^(.+)$/, '<p>$1</p>');
}

function newChatConversation() {
  _chatMsgs = [];
  const area = g('chat');
  if (area) {
    area.innerHTML = `<div id="welcome" style="display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:20px;gap:8px;flex:1">
      <div class="wlc-title">Chat con IA</div>
      <div class="wlc-sub">Pregunta sobre ISR, contratos, valuación o pídeme navegar a cualquier módulo</div>
    </div>`;
  }
}

// ══════════════════════════════════════════════════════════
// KEYBOARD & INPUT
// ══════════════════════════════════════════════════════════
function onChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChatMessage();
  }
}

function resizeChatInput(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 110) + 'px';
}

// ══════════════════════════════════════════════════════════
// VOICE INPUT — Reconocimiento de voz
// ══════════════════════════════════════════════════════════
function toggleVoice() {
  if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
    alert('Tu navegador no soporta reconocimiento de voz. Usa Chrome o Edge.');
    return;
  }
  if (_isListening) {
    window._voiceConversationActive = false;
    stopVoice();
    return;
  }
  window._voiceConversationActive = true;
  window._lastInputWasVoice = true;
  startVoice();
}

function startVoice() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  _recognition = new SR();
  _recognition.lang = 'es-MX';
  _recognition.continuous = false;
  _recognition.interimResults = true;

  const btn = g('mic-btn');
  const inp = g('txtin');

  _isListening = true;
  if (btn) btn.classList.add('listening');
  if (inp) { inp.placeholder = '🎙 Escuchando…'; inp.value = ''; }

  _recognition.onresult = (e) => {
    let final = '', interim = '';
    for (let i = 0; i < e.results.length; i++) {
      if (e.results[i].isFinal) final += e.results[i][0].transcript;
      else interim += e.results[i][0].transcript;
    }
    if (inp) {
      inp.value = (final || interim).trim();
      resizeChatInput(inp);
    }
  };

  _recognition.onerror = (e) => {
    if (e.error === 'not-allowed') {
      alert('Permiso de micrófono denegado. Habilítalo en la configuración del navegador.');
    }
    stopVoice();
  };

  _recognition.onend = () => {
    const txt = inp ? inp.value.trim() : '';
    if (_isListening && txt) {
      window._lastInputWasVoice = true;
      stopVoice();
      setTimeout(() => sendChatMessage(), 150);
    } else {
      stopVoice();
    }
  };

  try { _recognition.start(); }
  catch (e) { stopVoice(); }
}

function stopVoice() {
  _isListening = false;
  const btn = g('mic-btn');
  if (btn) { btn.classList.remove('listening'); btn.title = 'Hablar 🎙'; }
  if (_recognition) { try { _recognition.abort(); } catch (e) {} _recognition = null; }
  const inp = g('txtin');
  if (inp && inp.placeholder === '🎙 Escuchando…') inp.placeholder = 'Escribe o habla…';
}

// ══════════════════════════════════════════════════════════
// TEXT-TO-SPEECH — Respuesta por voz
// ══════════════════════════════════════════════════════════
function speakText(text) {
  if (!('speechSynthesis' in window)) return;
  const clean = text
    .replace(/\[ACCION\].*?\[\/ACCION\]/gs, '')
    .replace(/[*_#`]/g, '')
    .replace(/\n+/g, ' ')
    .replace(/https?:\/\/\S+/g, '')
    .trim()
    .slice(0, 500);
  if (!clean) return;

  window.speechSynthesis.cancel();

  const doSpeak = () => {
    const utt = new SpeechSynthesisUtterance(clean);
    utt.lang = 'es-MX';
    utt.rate = 1.05;
    utt.pitch = 1;
    const voices = window.speechSynthesis.getVoices();
    const esVoice = voices.find(v => v.lang === 'es-MX') ||
                    voices.find(v => v.lang === 'es-ES') ||
                    voices.find(v => v.lang.startsWith('es'));
    if (esVoice) utt.voice = esVoice;
    utt.onend = () => {
      if (window._voiceConversationActive) {
        setTimeout(() => startVoice(), 400);
      }
    };
    window.speechSynthesis.speak(utt);
  };

  if (window.speechSynthesis.getVoices().length) doSpeak();
  else {
    window.speechSynthesis.onvoiceschanged = () => {
      doSpeak();
      window.speechSynthesis.onvoiceschanged = null;
    };
  }
}
