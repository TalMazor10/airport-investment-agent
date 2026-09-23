"""
Voice dictation into the chat box, through the browser's built-in speech
recognition.

A microphone button is placed inside the chat input, beside the send button.
Pressing it starts dictation: the words appear in the chat box as they are
recognised, and pauses do not end it. Pressing it again stops. The text stays
in the box, where it can be edited or extended, and is sent only when Enter or
the send button is pressed, as with any typed question.

The Web Speech API is available in Chrome and Edge. Both perform recognition
on their vendors' servers, so dictated audio leaves the machine; typed
questions do not take that path. In other browsers the button is shown
disabled, with an explanation on hover. No speech model or additional API key
is involved.

The component renders nothing of its own. Its script runs in the page and
attaches the button to Streamlit's chat input, re-attaching it whenever
Streamlit redraws the input.
"""

import streamlit as st

_JS = """
export default function () {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const BUTTON_ID = "voice-dictation-button";
  const MIC_ICON = '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor">' +
    '<path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5.3-3c0 3-2.54 5.1-5.3 ' +
    '5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28c3.28-.49 6-3.31 6-6.72h-1.7z"/></svg>';
  const STOP_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor">' +
    '<rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
  const ERRORS = {
    "not-allowed": "Microphone access is blocked. Allow it from the address bar.",
    "service-not-allowed": "Microphone access is blocked. Allow it from the address bar.",
    "no-speech": "No speech detected.",
    "audio-capture": "No microphone found.",
    "network": "Dictation needs an internet connection.",
  };
  let recognition = null;

  const chatBox = () => document.querySelector('[data-testid="stChatInputTextArea"]');

  // Streamlit's chat box is a React component, so its value is set through
  // the native setter and announced with an input event.
  function setText(box, value) {
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
    setter.call(box, value);
    box.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function notify(box, message) {
    const original = box.getAttribute("placeholder") || "";
    box.setAttribute("placeholder", message);
    setTimeout(() => box.setAttribute("placeholder", original), 4000);
  }

  function showState(button, listening) {
    button.innerHTML = listening ? STOP_ICON : MIC_ICON;
    button.title = listening ? "Stop dictation" : "Dictate a question";
    button.style.color = listening ? "#e5484d" : "inherit";
  }

  function toggle() {
    const button = document.getElementById(BUTTON_ID);
    const box = chatBox();
    if (!box || !button) return;
    if (recognition) {
      recognition.stop();
      return;
    }
    const before = box.value.trim() ? box.value.trim() + " " : "";
    let dictated = "";
    recognition = new Recognition();
    recognition.lang = "en-US";
    recognition.continuous = true;
    recognition.interimResults = true;

    recognition.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) dictated += result[0].transcript;
        else interim += result[0].transcript;
      }
      setText(box, (before + dictated + interim).replace(/\\s+/g, " "));
    };
    recognition.onerror = (event) => {
      notify(box, ERRORS[event.error] || "Dictation error: " + event.error);
    };
    recognition.onend = () => {
      recognition = null;
      const current = document.getElementById(BUTTON_ID);
      if (current) showState(current, false);
      box.focus();
      box.setSelectionRange(box.value.length, box.value.length);
    };

    recognition.start();
    showState(button, true);
  }

  function attach() {
    const send = document.querySelector('[data-testid="stChatInputSubmitButton"]');
    if (!send || document.getElementById(BUTTON_ID)) return;
    const button = document.createElement("button");
    button.id = BUTTON_ID;
    button.type = "button";
    Object.assign(button.style, {
      background: "transparent", border: "none", cursor: "pointer", padding: "0.25rem",
      margin: "0 0.25rem", display: "flex", alignItems: "center", justifyContent: "center",
      color: "inherit", borderRadius: "0.5rem", opacity: Recognition ? "0.8" : "0.35",
    });
    if (Recognition) {
      showState(button, false);
      button.onclick = toggle;
    } else {
      button.innerHTML = MIC_ICON;
      button.disabled = true;
      button.title = "Voice input needs Chrome or Edge";
    }
    const slot = send.parentElement;
    slot.parentElement.insertBefore(button, slot);
  }

  attach();
  const observer = new MutationObserver(attach);
  observer.observe(document.body, { childList: true, subtree: true });

  return () => {
    observer.disconnect();
    if (recognition) recognition.abort();
    document.getElementById(BUTTON_ID)?.remove();
  };
}
"""


def dictation_button(key="voice"):
    """Place the dictation button inside the chat input. Renders nothing itself."""
    component = st.components.v2.component("voice_dictation", js=_JS)
    component(key=key)
